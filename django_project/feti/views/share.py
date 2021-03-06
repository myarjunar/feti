import urllib.request
from urllib.parse import unquote
import weasyprint
import os
import json
import smtplib
from more_itertools import unique_everseen
from rest_framework.views import APIView
from haystack.query import SQ, SearchQuerySet
from django.views.generic import TemplateView, UpdateView
from django.http import HttpResponse, Http404
from django.template.loader import get_template
from django.template import RequestContext, Context
from django.utils.crypto import get_random_string
from django.utils._os import safe_join
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.mail import EmailMessage
from django.shortcuts import redirect
from feti.models.campus import Campus
from feti.models.url import URL
from feti.views.api import (
    ApiCourse,
    ApiSavedCampus
)
from feti.api_views.campus import (
    ApiCampus
)
from feti.api_views.common_search import CommonSearch


class SharingMixin(object):
    """Mixin class to query campus and download map"""
    courses_name = []

    def get_course_names(self):
        return self.courses_name

    def get_campus(self, provider, query, user=None):
        api = None
        # Get data
        if provider == 'provider':
            sqs = SearchQuerySet()
            sqs = sqs.filter(
                SQ(campus=query) | SQ(campus_provider=query),
                campus_location_is_null='false',
                courses_is_null='false'
            ).models(Campus)
            campus_data = []

            for result in sqs:
                stored_fields = result.get_stored_fields()
                if stored_fields['campus_location']:
                    campus_location = stored_fields['campus_location']
                    stored_fields['campus_location'] = "{0},{1}".format(
                        campus_location.y, campus_location.x
                    )
                campus_data.append(stored_fields)

            return campus_data
        elif provider == 'course':
            api = ApiCourse()
        elif provider == 'favorites':
            api = ApiSavedCampus()
            return api.filter_model(user=user)
        if api:
            campuses = api.filter_model(query)
            self.courses_name = api.courses_name
            return campuses
        return None

    def download_map(self, filename, markers, provider=None):
        check_existence = True
        osm_static_url = 'http://staticmap.openstreetmap.de/staticmap.php?center=-30.5,24&' \
                         'zoom=5&size=865x512&maptype=mapnik'
        if markers:
            osm_static_url += '&markers=' + markers

        if provider:
            if provider == 'favorites':
                check_existence = False

        path = os.path.join(settings.MEDIA_ROOT, filename)

        # Check if file already exists
        if not check_existence and default_storage.exists(path):
            os.remove(path)

        if not default_storage.exists(path):
            try:
                urllib.request.urlretrieve(osm_static_url, path)
            except urllib.error.URLError:
                raise Http404(
                    'File not found'
                )


class PDFDownload(TemplateView, SharingMixin, CommonSearch):
    template_name = 'feti/pdf_template.html'

    def url_fetcher(self, url):
        if url.startswith('assets:'):
            url = url[len('assets:'):]
            url = "file://" + safe_join(settings.MEDIA_ROOT, url)
        return weasyprint.default_url_fetcher(url)

    def render_to_response(self, context, **response_kwargs):
        """
        Returns a pdf report

        :param context:
        :param response_kwargs: Keyword arguments
        :return: HTTPResponse
        """

        slug = self.kwargs.get('provider', None)
        query, options = self.process_request(self.kwargs)

        course_names = self.get_course_names()

        markers = ''
        campuses = None

        if slug == 'course':
            campuses = self.filter_by_course(query)
            pdf_data = list(
                unique_everseen(
                    [{
                         'campus_provider': x.get_stored_fields()['campus_provider'],
                         'campus_address': x.get_stored_fields()['campus_address'],
                         'campus_website': x.get_stored_fields()['campus_website'],
                         'location': x.get_stored_fields()['campus_location']
                     }
                     for x in campuses])
            )
            for campus in pdf_data:
                location = campus['location']
                markers += '%s,%s,ol-marker-blue|' % (location.y, location.x)
            campuses = pdf_data
        elif slug == 'provider':
            campuses = list(self.filter_indexed_campus(query))
            for result in campuses:
                campus = result.get_stored_fields()
                location = campus['campus_location']
                markers += '%s,%s,ol-marker-blue|' % (location.y, location.x)
        elif slug == 'favorites':
            api_saved_campus = ApiSavedCampus()
            campuses = list(api_saved_campus.filter_model(user=self.request.user))
            for idx, campus in enumerate(campuses):
                location = campus.campus.location.coords
                campus.campus_website = campus.campus.provider.website
                campus.campus_provider = campus.campus.provider
                campus.campus_address = campus.campus.address.address_line
                markers += '%s,%s,ol-marker-blue|' % (location[1], location[0])

        filename = '%s-%s.png' % (query, slug)

        self.download_map(filename=filename, markers=markers, provider=slug)

        template = get_template("feti/pdf_template.html")

        if course_names:
            query = ", ".join(course_names)

        context = {
            "type": "pdf",
            "image": filename,
            "title": "Feti Report",
            "provider": slug,
            "query": query,
            "campuses": campuses
        }

        html = template.render(RequestContext(self.request, context))
        response = HttpResponse(content_type="application/pdf")
        response['Content-Disposition'] = 'attachment; filename="%s.pdf"' % query
        weasyprint.HTML(string=html, url_fetcher=self.url_fetcher).write_pdf(response)

        return response


class EmailShare(UpdateView, SharingMixin):
    template_name = 'feti/pdf_template.html'

    def post(self, request, *args, **kwargs):
        data = request.body

        try:
            retrieved_data = json.loads(data.decode("utf-8"))
        except ValueError:
            raise Http404(
                'Error json value'
            )

        email_address = retrieved_data['email']
        provider = retrieved_data['provider']
        query = retrieved_data['query']
        link = retrieved_data['link']
        email_host = 'noreply@kartoza.com'
        campuses = self.get_campus(provider=provider, query=query)

        htmly = get_template(self.template_name)
        filename = '%s-%s.png' % (query, provider)
        markers = ''

        if provider != 'provider':
            email_data = list(
                unique_everseen(
                    [{
                         'campus_provider': x['campus_provider'],
                         'campus_address': x['campus_address'],
                         'campus_website': x['campus_website'],
                         'location': x['campus_location']
                     }
                     for x in campuses])
            )

            for idx, campus in enumerate(email_data):
                location = campus['location'].split(',')
                markers += '%s,%s,bluelight%s|' % (location[0], location[1], str(idx))
            campuses = email_data

        else:
            for idx, campus in enumerate(campuses):
                location = campus['campus_location'].split(',')
                markers += '%s,%s,bluelight%s|' % (location[0], location[1], str(idx))

        subject, from_email, to = 'Feti Report', \
                                  email_host, \
                                  [email_address]

        self.download_map(filename=filename, markers=markers)

        d = Context({
            "type": "email",
            "title": "Feti Report",
            "query": query,
            "provider": provider,
            "campuses": campuses,
            "link": link
        })

        content = htmly.render(d)
        msg = EmailMessage(subject, content, from_email, to)

        msg.content_subtype = 'html'
        try:
            file = default_storage.open(os.path.join(settings.MEDIA_ROOT, filename))
            image = file.read()
            msg.attach(
                filename=filename,
                content=image,
                mimetype='image/png')
            file.close()
            msg.send(fail_silently=False)
        except smtplib.SMTPException:
            raise Http404(
                'Sending email failed'
            )

        return HttpResponse('success')


class ApiRandomString(UpdateView):
    def generate_random_string(self, url):
        """ Generate random string from url and store it to db"""
        try:
            url = URL.objects.get(url=url)
        except URL.DoesNotExist:
            url = URL(url=url)
            url.save()
        return url.random_string

    def post(self, request, *args, **kwargs):
        data = request.body

        try:
            retrieved_data = json.loads(data.decode("utf-8"))
        except ValueError:
            raise Http404(
                'Error json value'
            )
        url = unquote(retrieved_data['url'])
        response = self.generate_random_string(url)

        return HttpResponse(response)


class ApiGetURL(APIView):
    def get(self, request, random):
        if URL.objects.filter(random_string=random).exists():
            return redirect(URL.objects.filter(random_string=random).first().url)
        return redirect('/')
