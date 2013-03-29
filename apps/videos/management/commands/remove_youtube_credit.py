# Amara, universalsubtitles.org
#
# Copyright (C) 2013 Participatory Culture Foundation
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

"""
Fix all the Youtube videos!
===========================

This command will attempt to fix all team Youtube videos.  These videos had
their Youtube descriptions changed and a credit sub added by accident by an
earlier sync.

The new descriptions were produced by adding

    `credit`: `amara video url`

    `original description`

Where `credit` is "Help us caption and translate this video on Amara.org".

So, this script looks up all video urls that could possibly be affected by this
and then attempts to fix them one by one.  It reconstructs the supposed
description addition and checks (via the Youtube API) if the Youtube video
description starts with it.  If it does, it replaces it with nothing and
updates the description on Youtube.

Then it resyncs all public versions to Youtube to remove the subtitle credit
which was added as the last subtitle.

You can also fix an individual video by using the --video flag.  It takes a
video_id.

You can process a single team by using the --team flag.  It takes a team slug.
"""

import os
import json
from time import sleep
from optparse import make_option
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import gdata

from apps.videos.models import VideoUrl, Video, VIDEO_TYPE_YOUTUBE
from apps.videos.types import video_type_registrar, UPDATE_VERSION_ACTION
from apps.videos.types.youtube import YouTubeApiBridge


UPLOAD_URI_BASE = 'http://gdata.youtube.com/feeds/api/users/default/uploads/%s'
YOUTUBE_API_SECRET  = getattr(settings, "YOUTUBE_API_SECRET", None)


class Command(BaseCommand):

    option_list = BaseCommand.option_list + (
        make_option('--video', '-d', dest='video_id', type="str",
            default=None),
        make_option('--team', '-t', dest='team', type="str",
            default=None),
        make_option('--query', '-q', dest='query', type="str",
            default=None),
        make_option('--youtube', '-u', dest='youtube_username', type="str",
            default=None),
    )

    CACHE_PATH = os.path.join(getattr(settings, 'PROJECT_ROOT'), 'yt-cache')

    def log(self, msg):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self.stdout.write(now + ' ' + str(msg) + '\n')
        self.stdout.flush()

    def _resync_subs_for_video(self, video):
        from apps.accountlinker.models import ThirdPartyAccount
        languages = video.subtitlelanguage_set.all()
        self.log(video)

        for language in languages:
            latest_version = language.latest_version(public_only=True)

            if latest_version:
                self.log(' ' + str(language))
            else:
                self.log('  no version for:' + str(language))
                continue

            ThirdPartyAccount.objects.mirror_on_third_party(video,
                    language, UPDATE_VERSION_ACTION,
                    version=latest_version)
        return video.video_id

    def _fix_video(self, vurl):
        from apps.accountlinker.models import ThirdPartyAccount
        from apps.videos.templatetags.videos_tags import shortlink_for_video

        video = vurl.video
        language_code = video.language

        if not language_code:
            language_code = 'en'

        vt = video_type_registrar.video_type_for_url(vurl.url)

        username = vurl.owner_username
        account = ThirdPartyAccount.objects.get(type=vurl.type,
                username=username)

        bridge = YouTubeApiBridge(account.oauth_access_token,
                                account.oauth_refresh_token,
                                vt.videoid)

        video_url = shortlink_for_video(video)
        self.log(video_url)

        uri = UPLOAD_URI_BASE % bridge.youtube_video_id
        entry = bridge.GetVideoEntry(uri=uri)
        entry = entry.to_string()
        entry = gdata.youtube.YouTubeVideoEntryFromString(entry)

        current_description = entry.media.description.text

        # For some reason the above video.get_absolute_url() call didn't
        # include the /en/ prefix.
        unisubs_video_url = video_url
        amara_video_url = video_url

        unisubs_supposed_credit = self._get_supposed_credit(unisubs_video_url,
                language_code)
        amara_supposed_credit = self._get_supposed_credit(amara_video_url,
                language_code)

        credits = (amara_supposed_credit, unisubs_supposed_credit,)

        if not current_description.startswith(credits):
            self.log("%s doesn't have desc credit" % vurl.url)
            return video.video_id

        if current_description.startswith(amara_supposed_credit):
            self.log("fixing supposed amara credit")
            new_description = current_description.replace(
                    amara_supposed_credit, '')

        if current_description.startswith(unisubs_supposed_credit):
            self.log("fixing supposed unisubs credit")
            new_description = current_description.replace(
                    unisubs_supposed_credit, '')

        entry.media.description.text = new_description
        entry = entry.ToString()

        status_code = bridge._make_update_request(uri, entry)

        if status_code == 401:
            bridge.refresh()
            status_code = bridge._make_update_request(uri, entry)

        if status_code == 200:
            self.log('%s success' % vurl.url)
            return video.video_id

        self.log('FAIL %s' % vurl.url)

    def _get_supposed_credit(self, vurl, language='en'):
        # Sometimes I hate Python :(
        from apps.accountlinker.models import (
            translate_string, AMARA_DESCRIPTION_CREDIT
        )
        credit = translate_string(AMARA_DESCRIPTION_CREDIT, language)
        return str("%s: %s\n\n" % (credit, vurl)).strip()

    def _load_cache_file(self):
        if os.path.exists(self.CACHE_PATH):
            return json.loads(open(self.CACHE_PATH).read())
        return {'desc': [], 'sub': []}

    def _save_cache_file(self, data):
        with open(self.CACHE_PATH, 'w') as f:
            f.write(json.dumps(data))

    def _sleep(self):
        self.log('Pausing')
        sleep(3)

    def _get_videos_from_query(self, query):
        uri = "http://gdata.youtube.com/feeds/api/videos?q=%s&v=2"
        uri = uri % query

        from videos.feed_parser import FeedParser

        urls = []

        feed_parser = FeedParser(uri)
        _iter = feed_parser.items()

        for vt, info, entry in _iter:
            urls.append(vt.convert_to_video_url())

        next_url = [x for x in feed_parser.feed.feed.get('links', []) if x['rel'] == 'next']

        while next_url:
            url = next_url[0].href
            feed_parser = FeedParser(url)
            _iter = feed_parser.items()

            for vt, info, entry in _iter:
                urls.append(vt.convert_to_video_url())

            self.log("%s videos loaded" % len(urls))

            next_url = [x for x in feed_parser.feed.feed.get('links', []) if x['rel'] == 'next']
            self._sleep()

        return VideoUrl.objects.select_related('video').filter(url__in=urls)

    def handle(self, video_id, team, query, youtube_username, *args, **kwargs):
        if video_id:
            try:
                video = Video.objects.get(video_id=video_id)
            except Video.DoesNotExist:
                raise CommandError("Video doesn't exist")

            yt_urls = video.videourl_set.filter(type=VIDEO_TYPE_YOUTUBE)

            if not yt_urls.exists():
                raise CommandError("Not a Youtube video.")

            for vurl in yt_urls:
                try:
                    self._fix_video(vurl)
                except Exception, e:
                    self.log(e)

            self._resync_subs_for_video(video)

            return

        self.cache = self._load_cache_file()

        try:

            if query:
                urls = self._get_videos_from_query(query)
                videos = [u.video for u in urls]
            elif youtube_username:
                urls = VideoUrl.objects.select_related('video').filter(
                        owner_username=youtube_username)
                videos = [vurl.video for vurl in urls]
            else:

                all_team_videos = Video.objects.filter(teamvideo__isnull=False)

                if team:
                    self.log('Only processing videos for %s' % team)
                    all_team_videos = all_team_videos.filter(
                            teamvideo__team__slug=team)

                videos = all_team_videos.exclude(video_id__in=self.cache['desc'])

                urls = VideoUrl.objects.filter(type=VIDEO_TYPE_YOUTUBE,
                        video__in=videos)

            self.log('%s video descriptions to process' % len(urls))

            for vurl in urls:
                self.log('Starting to process video %s' % vurl.video.video_id)
                try:
                    video_id = self._fix_video(vurl)
                    if video_id:
                        self.cache['desc'].append(video_id)
                    self.log('Done processing video')
                except Exception, e:
                    self.log(e)
                self.log('Done processing video')
                self._sleep()

            # Now, sync all completed languages to Youtube to remove the last
            # sub credit.

            videos = all_team_videos.exclude(video_id__in=self.cache['sub'])
            self.log('%s videos to resync' % len(videos))

            for video in videos:
                self.log('Start resync for %s' % video.video_id)
                try:
                    video_id = self._resync_subs_for_video(video)
                    if video_id:
                        self.cache['sub'].append(video_id)
                except Exception, e:
                    self.log(e)
                self.log('End resync')
                self._sleep()

        except Exception, e:
            self.log(e)
        finally:
            self._save_cache_file(self.cache)
