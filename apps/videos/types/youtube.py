# Amara, universalsubtitles.org
#
# Copyright (C) 2012 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.
import logging
import random
import re
from datetime import datetime
from urlparse import urlparse
import requests

import gdata.youtube.client
from gdata.youtube.client import YouTubeError
import httplib2
from celery.task import task
from django.conf import settings
from django.utils.http import urlquote
from django.utils.translation import ugettext_lazy as _
from gdata.service import RequestError
from gdata.youtube.service import YouTubeService
from lxml import etree

from auth.models import CustomUser as User
from base import VideoType, VideoTypeError
from utils.subtitles import YoutubeXMLParser
from utils.translation import SUPPORTED_LANGUAGE_CODES
from utils.metrics import Meter, Occurrence

from libs.unilangs.unilangs import LanguageCode
import httplib


logger = logging.getLogger("youtube")

YOUTUBE_API_SECRET  = getattr(settings, "YOUTUBE_API_SECRET", None)
YOUTUBE_ALWAYS_PUSH_USERNAME = getattr(settings,
    'YOUTUBE_ALWAYS_PUSH_USERNAME', None)


_('Private video')
_('Undefined error')


class TooManyRecentCallsException(Exception):
    """
    Raised when the Youtube API responds with yt:quota too_many_recent_calls.
    """

    def __init__(self, *args, **kwargs):
        super(TooManyRecentCallsException, self).__init__(*args, **kwargs)
        logger.info('too_many_calls', extra={
            'exception_args': args,
            'exception_kwargs': kwargs})
        Occurrence('youtube.api_too_many_calls').mark()


from atom.http_core import Uri
import atom

def monkeypatch_class(name, bases, namespace):
    assert len(bases) == 1, "Exactly one base class required"
    base = bases[0]
    for name, value in namespace.iteritems():
        if name != "__metaclass__":
            setattr(base, name, value)
    return base

class HttpClient(atom.http_core.HttpClient):
    __metaclass__ = monkeypatch_class
    debug = None

    def Request(self, http_request):
        return self._http_request(http_request.method, http_request.uri,
                              http_request.headers, http_request._body_parts)

    def _get_connection(self, uri, headers=None):
        """Opens a socket connection to the server to set up an HTTP request.

        Args:
        uri: The full URL for the request as a Uri object.
        headers: A dict of string pairs containing the HTTP headers for the
            request.
        """
        connection = None
        if uri.scheme == 'https':
            if not uri.port:
                connection = httplib.HTTPSConnection(uri.host)
            else:
                connection = httplib.HTTPSConnection(uri.host, int(uri.port))
        else:
            if not uri.port:
                connection = httplib.HTTPConnection(uri.host)
            else:
                connection = httplib.HTTPConnection(uri.host, int(uri.port))
        return connection

    def _http_request(self, method, uri, headers=None, body_parts=None):
        """Makes an HTTP request using httplib.

        Args:
        method: str example: 'GET', 'POST', 'PUT', 'DELETE', etc.
        uri: str or atom.http_core.Uri
        headers: dict of strings mapping to strings which will be sent as HTTP
                headers in the request.
        body_parts: list of strings, objects with a read method, or objects
                    which can be converted to strings using str. Each of these
                    will be sent in order as the body of the HTTP request.
        """

        extra = {
            'youtube_headers': headers,
            'youtube_uri': {
                'host': uri.host,
                'port': uri.port,
                'scheme': uri.scheme,
                'path': uri.path,
                'query': uri.query
            },
            'youtube_method': method,
            'youtube_body_parts': body_parts
        }
        logger.info('youtube api request', extra=extra)

        if isinstance(uri, (str, unicode)):
            uri = Uri.parse_uri(uri)

        connection = self._get_connection(uri, headers=headers)

        if self.debug:
            connection.debuglevel = 1

        if connection.host != uri.host:
            connection.putrequest(method, str(uri))
        else:
            connection.putrequest(method, uri._get_relative_path())

        # Overcome a bug in Python 2.4 and 2.5
        # httplib.HTTPConnection.putrequest adding
        # HTTP request header 'Host: www.google.com:443' instead of
        # 'Host: www.google.com', and thus resulting the error message
        # 'Token invalid - AuthSub token has wrong scope' in the HTTP response.
        if (uri.scheme == 'https' and int(uri.port or 443) == 443 and
            hasattr(connection, '_buffer') and
            isinstance(connection._buffer, list)):

            header_line = 'Host: %s:443' % uri.host
            replacement_header_line = 'Host: %s' % uri.host
            try:
                connection._buffer[connection._buffer.index(header_line)] = (
                    replacement_header_line)
            except ValueError:  # header_line missing from connection._buffer
                pass

        # Send the HTTP headers.
        for header_name, value in headers.iteritems():
            connection.putheader(header_name, value)
        connection.endheaders()

        # If there is data, send it in the request.
        if body_parts and filter(lambda x: x != '', body_parts):
            for part in body_parts:
                _send_data_part(part, connection)

        # Return the HTTP Response from the server.
        return connection.getresponse()


def _send_data_part(data, connection):
    if isinstance(data, (str, unicode)):
        # I might want to just allow str, not unicode.
        connection.send(data)
        return
    # Check to see if data is a file-like object that has a read method.
    elif hasattr(data, 'read'):
        # Read the file and send it a chunk at a time.
        while 1:
            binarydata = data.read(100000)
            if binarydata == '': break
            connection.send(binarydata)
        return
    else:
        # The data object was not a file.
        # Try to convert to a string and send the data.
        connection.send(str(data))
        return


def get_youtube_service():
    """
    Gets instance of youtube service with the proper developer key
    this is needed, else our quota is serverly damaged.
    """
    yt_service = YouTubeService(developer_key=YOUTUBE_API_SECRET)
    yt_service.ssl = False
    return yt_service


yt_service = get_youtube_service()


@task
def save_subtitles_for_lang(lang, video_pk, youtube_id):
    from videos.models import Video

    yt_lc = lang.get('lang_code')

    try:
        lc  = LanguageCode(yt_lc, "youtube").encode("unisubs")
    except KeyError:
        logger.warn("Youtube import did not find language code", extra={
            "data":{
                "language_code": yt_lc,
                "youtube_id": youtube_id,
            }
        })
        return


    if not lc in SUPPORTED_LANGUAGE_CODES:
        logger.warn("Youtube import did not find language code", extra={
            "data":{
                "language_code": lc,
                "youtube_id": youtube_id,
            }
        })
        return

    try:
        video = Video.objects.get(pk=video_pk)
    except Video.DoesNotExist:
        return

    from videos.models import SubtitleLanguage, SubtitleVersion, Subtitle

    url = u'http://www.youtube.com/api/timedtext?v=%s&lang=%s&name=%s'
    url = url % (youtube_id, yt_lc, urlquote(lang.get('name', u'')))

    xml = YoutubeVideoType._get_response_from_youtube(url)

    if xml is None:
        return

    parser = YoutubeXMLParser(xml)

    if not parser:
        return

    language, create = SubtitleLanguage.objects.get_or_create(
        video=video,
        language=lc,
        defaults={
            'created': datetime.now(),
    })
    language.is_original = False
    language.is_forked = True
    language.save()

    try:
        version_no = language.subtitleversion_set.order_by('-version_no')[:1] \
            .get().version_no + 1
    except SubtitleVersion.DoesNotExist:
        version_no = 0

    version = SubtitleVersion(language=language)
    version.title = video.title
    version.description = video.description
    version.version_no = version_no
    version.datetime_started = datetime.now()
    version.user = User.get_anonymous()
    version.note = u'From youtube'
    version.is_forked = True
    version.save()

    for i, item in enumerate(parser):
        subtitle = Subtitle()
        subtitle.subtitle_text = item['subtitle_text']
        subtitle.start_time = item['start_time']
        subtitle.end_time = item['end_time']
        subtitle.version = version
        subtitle.subtitle_id = int(random.random()*10e12)
        subtitle.subtitle_order = i+1

        try:
            assert subtitle.start_time or subtitle.end_time, item['subtitle_text']
        except AssertionError:
            # Don't bother saving the subtitle if it's not synced
            continue

        subtitle.save()

    version.finished = True
    version.save()

    language.has_version = True
    language.had_version = True
    language.is_complete = True
    language.save()

    from videos.tasks import video_changed_tasks
    video_changed_tasks.delay(video.pk)

    Meter('youtube.lang_imported').inc()


def should_add_credit(subtitle_version=None, video=None):
    # Only add credit to non-team videos
    if not video and not subtitle_version:
        raise Exception("You need to pass in at least one argument")

    if not video:
        video = subtitle_version.language.video

    return not video.get_team_video()


def add_credit(subs, language_code, video_duration, subtitle_version):
    # If there are no subtitles, don't add any credits.  This shouldn't really
    # happen since only completed subtitle versions can be synced to Youtube.
    # But a little precaution never hurt anyone.
    if len(subs) == 0:
        return subs

    if not should_add_credit(subtitle_version=subtitle_version):
        return subs

    from accountlinker.models import get_amara_credit_text

    last_sub = subs[-1]

    # If the last subtitle doesn't have a marked end, we can't add the credit.
    if last_sub['end'] == -1:
        return subs

    time_left_at_the_end = (video_duration * 1000) - last_sub['end']

    if time_left_at_the_end <= 0:
        return subs

    if time_left_at_the_end >= 3000:
        start = (video_duration - 3) * 1000
    else:
        start = (video_duration * 1000) - time_left_at_the_end

    credit_sub = {
        'text': get_amara_credit_text(language_code),
        'start': start,
        'end': video_duration * 1000,
        'id': '',
        'start_of_paragraph': ''
    }

    subs.append(credit_sub)
    return subs


class YoutubeVideoType(VideoType):

    _url_patterns = [re.compile(x) for x in [
        r'youtube.com/.*?v[/=](?P<video_id>[\w-]+)',
        r'youtu.be/(?P<video_id>[\w-]+)',
    ]]

    HOSTNAMES = ( "youtube.com", "youtu.be", "www.youtube.com",)

    abbreviation = 'Y'
    name = 'Youtube'
    site = 'youtube.com'

    # changing this will cause havock, let's talks about this first
    URL_TEMPLATE = 'http://www.youtube.com/watch?v=%s'

    CAN_IMPORT_SUBTITLES = True

    def __init__(self, url):
        self.url = url
        self.videoid = self._get_video_id(self.url)
        self.entry = self._get_entry(self.video_id)
        author = self.entry.author[0]
        self.username = author.name.text

    @property
    def video_id(self):
        return self.videoid

    def convert_to_video_url(self):
        return 'http://www.youtube.com/watch?v=%s' % self.video_id

    @classmethod
    def video_url(cls, obj):
        """
        This method can be called with wither a VideoType object or
        an actual VideoURL object, therefore the if statement
        """
        if obj.videoid:
            return YoutubeVideoType.url_from_id(obj.videoid)
        else:
            return obj.url

    @classmethod
    def matches_video_url(cls, url):
        hostname = urlparse(url).netloc
        return  hostname in YoutubeVideoType.HOSTNAMES and  cls._get_video_id(url)

    def create_kwars(self):
        return {'videoid': self.video_id}

    def set_values(self, video_obj, fetch_subs_async=True):
        video_obj.title =  self.entry.media.title.text or ''
        description = ''
        if self.entry.media.description:
            description =  self.entry.media.description.text or ''
        video_obj.description = description
        if self.entry.media.duration:
            video_obj.duration = int(self.entry.media.duration.seconds)
        if self.entry.media.thumbnail:
            # max here will return the thumbnail with the biggest height
            thumbnail = max([(int(t.height), t) for t in self.entry.media.thumbnail]) 
            video_obj.thumbnail = thumbnail[1].url
        video_obj.small_thumbnail = 'http://i.ytimg.com/vi/%s/default.jpg' % self.video_id
        video_obj.save()

        Meter('youtube.video_imported').inc()

        try:
            self.get_subtitles(video_obj, async=fetch_subs_async)
        except :
            logger.exception("Error getting subs from youtube:" )

        return video_obj

    def _get_entry(self, video_id):
        Meter('youtube.api_request').inc()
        try:
            return yt_service.GetYouTubeVideoEntry(video_id=str(video_id))
        except RequestError, e:
            err = e[0].get('body', 'Undefined error')
            raise VideoTypeError('Youtube error: %s' % err)

    @classmethod
    def url_from_id(cls, video_id):
        return YoutubeVideoType.URL_TEMPLATE % video_id
        
    @classmethod
    def _get_video_id(cls, video_url):
        for pattern in cls._url_patterns:
            match = pattern.search(video_url)
            video_id = match and match.group('video_id')
            if bool(video_id):
                return video_id
        return False

    @classmethod
    def _get_response_from_youtube(cls, url):
        h = httplib2.Http()
        resp, content = h.request(url, "GET")

        if resp.status < 200 or resp.status >= 400:
            logger.error("Youtube subtitles error", extra={
                    'data': {
                        "url": url,
                        "status_code": resp.status,
                        "response": content
                        }
                    })
            return

        try:
            return etree.fromstring(content)
        except etree.XMLSyntaxError:
            logger.error("Youtube subtitles error. Failed to parse response.", extra={
                    'data': {
                        "url": url,
                        "response": content
                        }
                    })
            return

    def get_subtitled_languages(self):
        url = "http://www.youtube.com/api/timedtext?type=list&v=%s" % self.video_id
        xml = self._get_response_from_youtube(url)

        if  xml is None:
            return []

        output = []
        for lang in xml.xpath('track'):
            item = dict(
                lang_code=lang.get('lang_code'),
                name=lang.get('name', u'')
            )
            output.append(item)

        return output

    def get_subtitles(self, video_obj, async=True):
        langs = self.get_subtitled_languages()

        if async:
            func = save_subtitles_for_lang.delay
        else:
            func = save_subtitles_for_lang.run
        for item in langs:
            func(item, video_obj.pk, self.video_id)

    def _get_bridge(self, third_party_account):
        # Because somehow Django's ORM is case insensitive on CharFields.
        is_always = (third_party_account.full_name.lower() == 
                        YOUTUBE_ALWAYS_PUSH_USERNAME.lower() or
                     third_party_account.username.lower() ==
                        YOUTUBE_ALWAYS_PUSH_USERNAME.lower())

        return YouTubeApiBridge(third_party_account.oauth_access_token,
            third_party_account.oauth_refresh_token, self.videoid, is_always)

    def update_subtitles(self, subtitle_version, third_party_account):
        """
        Updated subtitles on Youtube. This method should not be called
        directly. See accountlinker.models.ThirdPartyAccounts.mirror_on_third_party
        That call will check if the video can be updated(must be synched,
        must be public, etc).
        """
        bridge = self._get_bridge(third_party_account)
        bridge.upload_captions(subtitle_version)

    def delete_subtitles(self, language, third_party_account):
        bridge = self._get_bridge(third_party_account)
        bridge.delete_subtitles(language)


class YouTubeApiBridge(gdata.youtube.client.YouTubeClient):

    upload_uri_base = 'http://gdata.youtube.com/feeds/api/users/default/uploads/%s'

    def __init__(self, access_token, refresh_token, youtube_video_id,
            is_always_push_account=False):
        """
        A wrapper around the gdata client, to make life easier.
        In order to edit captions for a video, the oauth credentials
        must be that video's owner on youtube.
        """
        super(YouTubeApiBridge, self).__init__()
        self.access_token = access_token
        self.refresh_token = refresh_token

        self.token = gdata.gauth.OAuth2Token(
            client_id=settings.YOUTUBE_CLIENT_ID,
            client_secret=settings.YOUTUBE_CLIENT_SECRET,
            scope='https://gdata.youtube.com',
            user_agent='universal-subtitles',
            access_token=access_token,
            refresh_token=refresh_token
        )
        self.token.authorize(self)
        self.youtube_video_id  = youtube_video_id
        self.is_always_push_account = is_always_push_account

    def request(self, *args, **kwargs):
        """
        Override the very low-level request method to catch possible
        too_many_recent_calls errors.
        """
        Meter('youtube.api_request').inc()
        try:
            return super(YouTubeApiBridge, self).request(*args, **kwargs)
        except gdata.client.RequestError, e:
            if 'too_many_recent_calls' in str(e):
                raise TooManyRecentCallsException(e.headers, e.reason,
                        e.status, e.body)
            else:
                raise e

    def refresh(self):
        """
        Refresh the access token
        """
        url = 'https://accounts.google.com/o/oauth2/token'

        data = {
            'client_id': settings.YOUTUBE_CLIENT_ID,
            'client_secret': settings.YOUTUBE_CLIENT_SECRET,
            'refresh_token': self.refresh_token,
            'grant_type': 'refresh_token'
        }

        r = requests.post(url, data=data)
        self.access_token = r.json and r.json.get('access_token')

    def _get_captions_info(self):
        """
        Retrieves a dictionary with the current caption data for this youtube video.
        Format is:
        {
            "lang_code": {
                   "url": [url for track]
                    "track": [track entry object, useful for other operations]
             }
        }
        """
        self.captions  = {}
        entry = self.GetVideoEntry(video_id=self.youtube_video_id)
        caption_track = entry.get_link(rel='http://gdata.youtube.com/schemas/2007#video.captionTracks')

        if not caption_track:
            # No tracks were returned.  This video doesn't have any existing
            # captions.
            return self.captions

        captions_feed = self.get_feed(caption_track.href, desired_class=gdata.youtube.data.CaptionFeed)
        captions = captions_feed.entry

        for entry in captions:
            lang = entry.get_elements(tag="content")[0].lang
            url = entry.get_edit_media_link().href
            self.captions[lang] = {
                "url": url,
                "track": entry
            }

        return self.captions

    def get_user_profile(self, username=None):
        if not username:
            raise YouTubeError("You need to pass a username")
        uri = '%s%s' % (gdata.youtube.client.YOUTUBE_USER_FEED_URI, username)
        return self.get_feed(uri, desired_class=gdata.youtube.data.UserProfileEntry)

    def upload_captions(self, subtitle_version):
        """
        Will upload the subtitle version to this youtube video id.
        If the subtitle already exists, will delete it and recreate it.
        This subs should be synced! Else we upload might fail.
        """
        from widget.srt_subs import GenerateSubtitlesHandler

        language = subtitle_version.language.language
        video = subtitle_version.language.video

        try:
            lc = LanguageCode(language.lower(), "unisubs")
            lang = lc.encode("youtube")
        except KeyError:
            logger.error("Couldn't encode LC %s to youtube" % language)
            return

        handler = GenerateSubtitlesHandler.get('srt')
        subs = [x.for_generator() for x in subtitle_version.ordered_subtitles()]

        if not self.is_always_push_account:
            subs = add_credit(subs, language, video.duration, subtitle_version)
            self.add_credit_to_description(subtitle_version.language.video)

        content = unicode(handler(subs, video)).encode('utf-8')
        title = ""

        if hasattr(self, "captions") is False:
            self._get_captions_info()

        # we cant just update, we need to check if it already exists... if so, we delete it
        if lang in self.captions:
            self._delete_track(self.captions[lang]['track'])

        res = self.create_track(self.youtube_video_id, title, lang, content,
                settings.YOUTUBE_CLIENT_ID, settings.YOUTUBE_API_SECRET,
                self.token, {'fmt':'srt'})
        Meter('youtube.subs_pushed').inc()
        return res

    def add_credit_to_description(self, video):
        """
        Get the entry information from Youtube, extract the original
        description, prepend the description with Amara credits and push it
        back to Youtube.

        If our update call doesn't succeed on the first try, we refresh the
        access token and try again.

        If the existing description starts with the credit text, we just
        return.
        """
        from accountlinker.models import add_amara_description_credit, check_authorization
        from apps.videos.templatetags.videos_tags import shortlink_for_video

        if not should_add_credit(video=video):
            return False

        is_authorized, _ = check_authorization(video)

        if not is_authorized:
            return False

        uri = self.upload_uri_base % self.youtube_video_id

        entry = self.GetVideoEntry(uri=uri)
        entry = entry.to_string()
        entry = gdata.youtube.YouTubeVideoEntryFromString(entry)

        old_description = entry.media.description.text or ''

        if old_description:
            old_description = old_description.decode("utf-8")

        video_url = shortlink_for_video(video)

        language_code = video.language

        if not language_code:
            language_code = 'en'

        new_description = add_amara_description_credit(old_description,
                video_url, language_code)

        if new_description == old_description:
            return True

        entry.media.description.text = new_description
        entry = entry.ToString()

        status_code = self._make_update_request(uri, entry)

        if status_code == 401:
            self.refresh()
            status_code = self._make_update_request(uri, entry)

        if status_code == 200:
            Meter('youtube.description_changed').inc()
            return True

        return False

    def _make_update_request(self, uri, entry):
        Meter('youtube.api_request').inc()
        headers = {
            'Content-Type': 'application/atom+xml',
            'Authorization': 'Bearer %s' % self.access_token,
            'GData-Version': '2',
            'X-GData-Key': 'key=%s' % YOUTUBE_API_SECRET
        }
        r = requests.put(uri, data=entry, headers=headers)

        if r.status_code == 403 and 'too_many_recent_calls' in r.content:
            raise TooManyRecentCallsException(r.headers, r.raw)

        return r.status_code

    def _delete_track(self, track):
        res = self.delete_track(self.youtube_video_id, track, settings.YOUTUBE_CLIENT_ID, settings.YOUTUBE_API_SECRET, self.token)
        return res

    def delete_subtitles(self, language):
        """
        Deletes the subtitles for this language on this YouTube video.
        Smart enought to determine if this video already has such subs

        """
        try:
            lc = LanguageCode(language, "unisubs")
            lang = lc.encode("youtube")
        except KeyError:
            logger.error("Couldn't encode LC %s to youtube" % language)
            return

        if hasattr(self, "captions") is False:
            self._get_captions_info()
        if lang in self.captions:
            self._delete_track(self.captions[lang]['track'])
        else:
            logger.error("Couldn't find LC %s in youtube" % lang)
