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

from django.db import models
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.utils import translation

from videos.models import VIDEO_TYPE, VIDEO_TYPE_YOUTUBE
from .videos.types import (
    video_type_registrar, UPDATE_VERSION_ACTION,
    DELETE_LANGUAGE_ACTION, VideoTypeError,
    YoutubeVideoType
)
from teams.models import Team
from teams.moderation_const import APPROVED, UNMODERATED
from auth.models import CustomUser as User

from utils.metrics import Meter

logger = logging.getLogger(__name__)

# for now, they kind of match
ACCOUNT_TYPES = VIDEO_TYPE
AMARA_CREDIT = translation.ugettext("Subtitles by the Amara.org community")
AMARA_DESCRIPTION_CREDIT = translation.ugettext(
    "Help us caption and translate this video on Amara.org")


def youtube_sync(video, language):
    """
    Used on debug page for video.

    Simplified version of what's found in
    ``ThirdPartyAccount.mirror_on_third_party``.  It doesn't bother checking if
    we should be syncing this or not.  Only does the new Youtube/Amara
    integration syncing.
    """
    version = language.latest_version()

    always_push_account = ThirdPartyAccount.objects.always_push_account()

    for vurl in video.videourl_set.all():
        vt = video_type_registrar.video_type_for_url(vurl.url)

        try:
            vt.update_subtitles(version, always_push_account)
            Meter('youtube.push.success').inc()
        except:
            Meter('youtube.push.fail').inc()
            logger.error('Always pushing to youtoube has failed.', extra={
                'video': video.video_id,
                'vurl': vurl.pk
            })
        finally:
            Meter('youtube.push.request').inc()


def get_linked_accounts_for_video(video):
    yt_url = video.videourl_set.filter(type=VIDEO_TYPE_YOUTUBE)

    if yt_url.exists():
        accounts = [ThirdPartyAccount.objects.resolve_ownership(u) for u in yt_url]
        return filter(None, accounts)

    return None


def check_authorization(video):
    """
    Make sure that a video can have its subtitles synced to Youtube.  This
    doesn't take into account any language/version information.

    Return a tuple of (is_authorized, ignore_new_syncing_logic).
    """
    team_video = video.get_team_video()

    linked_accounts = get_linked_accounts_for_video(video)

    if not linked_accounts:
        return False, False

    if all([a.is_team_account for a in linked_accounts]):
        if not team_video:
            return False, False

        tpas_for_team = team_video.team.third_party_accounts.all()

        if any(tpa in tpas_for_team for tpa in linked_accounts):
            return True, True

    if all([a.is_individual_account for a in linked_accounts]):
        return True, True

    return False, False


def can_be_synced(version):
    """
    Determine if a subtitle version can be synced to Youtube.

    A version must be public, synced and complete; it must also be either
    "approved" or "unmoderated".

    We can't sync a version if it's the only version in that language and it
    has the "From youtube" note.
    """
    if version:
        if not version.is_public or not version.is_synced():
            # We can't mirror unsynced or non-public versions.
            return False

        if not version.language.is_complete:
            # Don't sync incomplete languages
            return False

        status = version.moderation_status

        if (status != APPROVED) and (status != UNMODERATED):
            return False

        if version.language.is_imported_from_youtube_and_not_worked_on:
            return False

    return True


def translate_string(string, language='en'):
    """
    If a translation for the specified language doesn't exist, return the
    English version.
    """
    cur_language = translation.get_language()
    try:
        translation.activate(language)
        text = translation.ugettext(string)
    finally:
        translation.activate(cur_language)
    return text


def get_amara_credit_text(language='en'):
    return translate_string(AMARA_CREDIT, language)


def add_amara_description_credit(old_description, video_url, language='en',
        prepend=False):
    """
    Prepend the credit to the existing description.
    """
    credit = "%s\n\n%s" % (translate_string(AMARA_DESCRIPTION_CREDIT,
        language), video_url)

    old_description = old_description or u''
    if credit in old_description:
        return old_description

    temp = "%s\n\n%s"

    if prepend:
        return temp % (credit, old_description)
    else:
        return temp % (old_description, credit)


class ThirdPartyAccountManager(models.Manager):

    def always_push_account(self):
        """
        Get the ThirdPartyAccount that is able to push to any video on Youtube.
        Raise ``ImproperlyConfigured`` if it can't be found.
        """
        username = getattr(settings, 'YOUTUBE_ALWAYS_PUSH_USERNAME', None)

        try:
            return self.get(username=username)
        except ThirdPartyAccount.DoesNotExist:
            raise ImproperlyConfigured("Can't find youtube account")

    def mirror_on_third_party(self, video, language, action, version=None):
        """
        Does the specified action (video.types.UPDATE_VERSION_ACTION or
                                   video.types.DELETE_LANGUAGE_ACTION) 
        on the original account (e.g. Youtube video).
        For example, to update a given version to Youtube:
             ThirdPartyAccountManager.objects.mirror_on_third_party(
                       video, language, "update_subtitles", version)
        For deleting, we only delete languages, so it should be 
              ThirdPartyAccountManager.objects.mirror_on_third_party(
                        video, language, "delete_subtitles")
        This method is 'safe' to call, meaning that we only do syncing if there 
        are matching third party credentials for this video.
        The update will only be done if the version is synced
        """
        if action not in [UPDATE_VERSION_ACTION, DELETE_LANGUAGE_ACTION]:
            raise NotImplementedError(
                "Mirror to third party does not support the %s action" % action)

        if not version and action == UPDATE_VERSION_ACTION:
            raise ValueError("You need to pass a version when updating subs")

        if not can_be_synced(version):
            return

        is_authorized, ignore_new_syncing_logic = check_authorization(video)

        if not is_authorized:
            return

        try:
            rule = YoutubeSyncRule.objects.all()[0]
            should_sync = rule.should_sync(video)
            always_push_account = self.always_push_account()
        except IndexError:
            should_sync = False

        for vurl in video.videourl_set.all():
            already_updated = False

            try:
                vt = video_type_registrar.video_type_for_url(vurl.url)
            except VideoTypeError, e:
                logger.error('Getting video from youtube failed.', extra={
                    'video': video.video_id,
                    'vurl': vurl.pk,
                    'gdata_exception': str(e)
                })
                return

            if should_sync and not ignore_new_syncing_logic:
                try:
                    vt.update_subtitles(version, always_push_account)
                    already_updated = True
                    Meter('youtube.push.success').inc()
                except Exception, e:
                    Meter('youtube.push.fail').inc()
                    logger.error('Pushing to youtoube has failed.', extra={
                        'video': video.video_id,
                        'vurl': vurl.pk,
                        'gdata_exception': str(e)
                    })
                finally:
                    Meter('youtube.push.request').inc()

            username = vurl.owner_username

            if not username:
                continue

            account = self.resolve_ownership(vurl)

            if not account:
                return

            if hasattr(vt, action):
                if action == UPDATE_VERSION_ACTION and not already_updated:
                    vt.update_subtitles(version, account)
                elif action == DELETE_LANGUAGE_ACTION:
                    vt.delete_subtitles(language, account)

    def resolve_ownership(self, video_url):
        """ Given a VideoUrl, return the ThirdPartyAccount that is
        supposed to be the owner of this video.
        """

        # youtube username is a full name. but sometimes. yeah.
        if video_url.type == 'Y':
            return self._resolve_youtube_ownership(video_url)
        else:
            try:
                return ThirdPartyAccount.objects.get(type=video_url.type,
                                                     username=video_url.owner_username)
            except ThirdPartyAccount.DoesNotExist:
                return None

    def _resolve_youtube_ownership(self, video_url):
        """ Give a youtube video url, returns a TPA that
        is the owner of the video.
        We need this because there could be two
        """
        try:
            return ThirdPartyAccount.objects.get(type=video_url.type,
                                                 full_name=video_url.owner_username)
        except ThirdPartyAccount.DoesNotExist:
            return None
        except ThirdPartyAccount.MultipleObjectsReturned:
            type = YoutubeVideoType(video_url.url)
            uri = type.entry.author[0].uri.text
            # we can easily extract the username from the uri, since it's the last
            # part of the path. this is much easier than making yet another api
            # call to youtube to find out.
            # i.e. https://gdata.youtube.com/feeds/api/users/gdetrez > gdetrez
            username = uri.split("/")[-1]

            # we want to avoid exception handling inside exception handling
            tpa = ThirdPartyAccount.objects.filter(type=video_url.type,
                                                    username=username)[:1]

            return tpa[0] if tpa else None

class ThirdPartyAccount(models.Model):
    """
    Links a third party account (e.g. YouTube's') to a certain video URL
    This allows us to push changes in Unisubs back to that video provider.
    The user links a video on unisubs to his youtube account. Once edits to
    any languages are done, we push those back to Youtube.
    For know, this only supports Youtube, but nothing is stopping it from
    working with others.
    """
    type = models.CharField(max_length=10, choices=ACCOUNT_TYPES)

    # this is the third party account user name, eg the youtube user
    username  = models.CharField(max_length=255, db_index=True, 
                                 null=False, blank=False)

    # user's real/full name, like Foo Bar
    full_name = models.CharField(max_length=255, null=True, blank=True, default='')
    oauth_access_token = models.CharField(max_length=255, db_index=True, 
                                          null=False, blank=False)
    oauth_refresh_token = models.CharField(max_length=255, db_index=True,
                                           null=False, blank=False)
    
    objects = ThirdPartyAccountManager()
    
    class Meta:
        unique_together = ("type", "username")

    def __unicode__(self):
        return '%s - %s' % (self.get_type_display(), self.full_name or self.username)

    @property
    def is_team_account(self):
        return self.teams.exists()

    @property
    def is_individual_account(self):
        return self.users.exists()

class YoutubeSyncRule(models.Model):
    """
    An instance of this class determines which Youtube videos should be synced
    back to Youtube via the new integration.

    There should only ever be one instance of this class in the database.

    You should run a query and then call it like this:

        rule = YoutubeSyncRule.objects.all()[0]
        rule.should_sync(video)

    Where ``video`` is a ``videos.models.Video`` instance.

    ``team`` should be a comma-separated list of team slugs that you want to
    sync.  ``user`` should be a comma-separated list of usernames of users
    whose videos should be synced.  ``video`` is a list of video ids of
    videos that should be synced.

    You can also specify a wildcard "*" to any of the above to match any teams,
    any users, or any videos.
    """
    team = models.TextField(default='', blank=True,
            help_text='Comma separated list of slugs')
    user = models.TextField(default='', blank=True,
            help_text='Comma separated list of usernames')
    video = models.TextField(default='', blank=True,
            help_text='Comma separated list of video ids')

    def __unicode__(self):
        return 'Youtube sync rule'

    def team_in_list(self, team):
        if not team:
            return False
        teams = self.team.split(',')
        if '*' in teams:
            return True
        return team in teams

    def user_in_list(self, user):
        if not user:
            return False

        users = self.user.split(',')

        if '*' in users:
            return True
        return user.username in users

    def video_in_list(self, pk):
        pks = self.video.split(',')
        if '*' in pks:
            return True
        if len(pks) == 1 and pks[0] == '':
            return False
        return pk in pks

    def should_sync(self, video):
        tv = video.get_team_video()
        team = None
        if tv:
            team = tv.team.slug

        return self.team_in_list(team) or \
                self.user_in_list(video.user) or \
                self.video_in_list(video.video_id)

    def _clean(self, name):
        if name not in ['team', 'user']:
            return
        field  = getattr(self, name)
        values = set(field.split(','))
        values = [v for v in values if v != '*']
        if len(values) == 1 and values[0] == '':
            return []
        return values

    def clean(self):
        teams = self._clean('team')
        users = self._clean('user')

        if len(teams) != Team.objects.filter(slug__in=teams).count():
            raise ValidationError("One or more teams not found")

        if len(users) != User.objects.filter(username__in=users).count():
            raise ValidationError("One or more users not found")
