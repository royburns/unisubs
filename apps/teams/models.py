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
import datetime
import logging
import csv
from itertools import groupby

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.urlresolvers import reverse
from django.core.files import File
from django.db import models
from django.db.models.signals import post_save, post_delete, pre_delete
from django.http import Http404
from django.template.loader import render_to_string
from django.utils.translation import ugettext_lazy as _
from haystack import site
from haystack.query import SQ

import teams.moderation_const as MODERATION
from apps.comments.models import Comment
from auth.models import CustomUser as User
from auth.providers import get_authentication_provider
from messages import tasks as notifier
from teams.moderation_const import WAITING_MODERATION, UNMODERATED, APPROVED
from teams.permissions_const import (
    TEAM_PERMISSIONS, PROJECT_PERMISSIONS, ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER,
    ROLE_CONTRIBUTOR
)
from videos.tasks import (
    upload_subtitles_to_original_service, sync_latest_versions_for_video
)
from teams.tasks import update_one_team_video
from utils import DEFAULT_PROTOCOL
from utils.amazon import S3EnabledImageField, S3EnabledFileField
from utils.panslugify import pan_slugify
from utils.searching import get_terms
from videos.models import Video, SubtitleLanguage, SubtitleVersion
from subtitles.models import (
    SubtitleVersion as NewSubtitleVersion,
)

from functools import partial

logger = logging.getLogger(__name__)

ALL_LANGUAGES = [(val, _(name))for val, name in settings.ALL_LANGUAGES]
VALID_LANGUAGE_CODES = [unicode(x[0]) for x in ALL_LANGUAGES]


# Teams
class TeamManager(models.Manager):
    def get_query_set(self):
        """Return a QS of all non-deleted teams."""
        return super(TeamManager, self).get_query_set().filter(deleted=False)

    def for_user(self, user):
        """Return a QS of all the (non-deleted) teams visible for the given user."""
        if user.is_authenticated():
            return self.get_query_set().filter(
                    models.Q(is_visible=True) |
                    models.Q(members__user=user)
            ).distinct()
        else:
            return self.get_query_set().filter(is_visible=True)

class Team(models.Model):
    APPLICATION = 1
    INVITATION_BY_MANAGER = 2
    INVITATION_BY_ALL = 3
    OPEN = 4
    INVITATION_BY_ADMIN = 5
    MEMBERSHIP_POLICY_CHOICES = (
            (OPEN, _(u'Open')),
            (APPLICATION, _(u'Application')),
            (INVITATION_BY_ALL, _(u'Invitation by any team member')),
            (INVITATION_BY_MANAGER, _(u'Invitation by manager')),
            (INVITATION_BY_ADMIN, _(u'Invitation by admin')),
            )

    VP_MEMBER = 1
    VP_MANAGER = 2
    VP_ADMIN = 3
    VIDEO_POLICY_CHOICES = (
        (VP_MEMBER, _(u'Any team member')),
        (VP_MANAGER, _(u'Managers and admins')),
        (VP_ADMIN, _(u'Admins only'))
    )

    TASK_ASSIGN_CHOICES = (
            (10, 'Any team member'),
            (20, 'Managers and admins'),
            (30, 'Admins only'),
            )
    TASK_ASSIGN_NAMES = dict(TASK_ASSIGN_CHOICES)
    TASK_ASSIGN_IDS = dict([choice[::-1] for choice in TASK_ASSIGN_CHOICES])

    SUBTITLE_CHOICES = (
            (10, 'Anyone'),
            (20, 'Any team member'),
            (30, 'Only managers and admins'),
            (40, 'Only admins'),
            )
    SUBTITLE_NAMES = dict(SUBTITLE_CHOICES)
    SUBTITLE_IDS = dict([choice[::-1] for choice in SUBTITLE_CHOICES])

    name = models.CharField(_(u'name'), max_length=250, unique=True)
    slug = models.SlugField(_(u'slug'), unique=True)
    description = models.TextField(_(u'description'), blank=True, help_text=_('All urls will be converted to links. Line breaks and HTML not supported.'))

    logo = S3EnabledImageField(verbose_name=_(u'logo'), blank=True, upload_to='teams/logo/', thumb_options=dict(autocrop=True, upscale=True))
    is_visible = models.BooleanField(_(u'publicly Visible?'), default=True)
    videos = models.ManyToManyField(Video, through='TeamVideo',  verbose_name=_('videos'))
    users = models.ManyToManyField(User, through='TeamMember', related_name='teams', verbose_name=_('users'))

    # these allow unisubs to do things on user's behalf such as uploding subs to Youtub
    third_party_accounts = models.ManyToManyField("accountlinker.ThirdPartyAccount",  related_name='teams', verbose_name=_('third party accounts'))

    points = models.IntegerField(default=0, editable=False)
    applicants = models.ManyToManyField(User, through='Application', related_name='applicated_teams', verbose_name=_('applicants'))
    created = models.DateTimeField(auto_now_add=True)
    highlight = models.BooleanField(default=False)
    video = models.ForeignKey(Video, null=True, blank=True, related_name='intro_for_teams', verbose_name=_(u'Intro Video'))
    application_text = models.TextField(blank=True)
    page_content = models.TextField(_(u'Page content'), blank=True, help_text=_(u'You can use markdown. This will replace Description.'))
    is_moderated = models.BooleanField(default=False)
    header_html_text = models.TextField(blank=True, default='', help_text=_(u"HTML that appears at the top of the teams page."))
    last_notification_time = models.DateTimeField(editable=False, default=datetime.datetime.now)

    auth_provider_code = models.CharField(_(u'authentication provider code'),
            max_length=24, blank=True, default="")

    # Enabling Features
    projects_enabled = models.BooleanField(default=False)
    workflow_enabled = models.BooleanField(default=False)

    # Policies and Permissions
    membership_policy = models.IntegerField(_(u'membership policy'),
            choices=MEMBERSHIP_POLICY_CHOICES,
            default=OPEN)
    video_policy = models.IntegerField(_(u'video policy'),
            choices=VIDEO_POLICY_CHOICES,
            default=VP_MEMBER)
    task_assign_policy = models.IntegerField(_(u'task assignment policy'),
            choices=TASK_ASSIGN_CHOICES,
            default=TASK_ASSIGN_IDS['Any team member'])
    subtitle_policy = models.IntegerField(_(u'subtitling policy'),
            choices=SUBTITLE_CHOICES,
            default=SUBTITLE_IDS['Anyone'])
    translate_policy = models.IntegerField(_(u'translation policy'),
            choices=SUBTITLE_CHOICES,
            default=SUBTITLE_IDS['Anyone'])
    max_tasks_per_member = models.PositiveIntegerField(_(u'maximum tasks per member'),
            default=None, null=True, blank=True)
    task_expiration = models.PositiveIntegerField(_(u'task expiration (days)'),
            default=None, null=True, blank=True)

    deleted = models.BooleanField(default=False)
    partner = models.ForeignKey('Partner', null=True, blank=True,
            related_name='teams')

    objects = TeamManager()
    all_objects = models.Manager() # For accessing deleted teams, if necessary.

    class Meta:
        ordering = ['name']
        verbose_name = _(u'Team')
        verbose_name_plural = _(u'Teams')


    def save(self, *args, **kwargs):
        creating = self.pk is None
        super(Team, self).save(*args, **kwargs)
        if creating:
            # make sure we create a default project
            self.default_project

    def __unicode__(self):
        return self.name or self.slug

    def render_message(self, msg):
        """Return a string of HTML represention a team header for a notification.

        TODO: Get this out of the model and into a templatetag or something.

        """
        author_page = msg.author.get_absolute_url() if msg.author else ''
        context = {
            'team': self,
            'msg': msg,
            'author': msg.author,
            'author_page': author_page,
            'team_page': self.get_absolute_url(),
            "STATIC_URL": settings.STATIC_URL,
        }
        return render_to_string('teams/_team_message.html', context)

    def is_open(self):
        """Return whether this team's membership is open to the public."""
        return self.membership_policy == self.OPEN

    def is_by_application(self):
        """Return whether this team's membership is by application only."""
        return self.membership_policy == self.APPLICATION

    @classmethod
    def get(cls, slug, user=None, raise404=True):
        """Return the Team with the given slug.

        If a user is given the Team must be visible to that user.  Otherwise the
        Team must be visible to the public.

        If raise404 is given an Http404 exception will be raised if a suitable
        team is not found.  Otherwise None will be returned.

        """
        if user:
            qs = cls.objects.for_user(user)
        else:
            qs = cls.objects.filter(is_visible=True)
        try:
            return qs.get(slug=slug)
        except cls.DoesNotExist:
            try:
                return qs.get(pk=int(slug))
            except (cls.DoesNotExist, ValueError):
                pass

        if raise404:
            raise Http404

    def get_workflow(self):
        """Return the workflow for the given team.

        A workflow will always be returned.  If one isn't specified for the team
        a default (unsaved) one will be populated with default values and
        returned.

        TODO: Refactor this behaviour into something less confusing.

        """
        return Workflow.get_for_target(self.id, 'team')

    @property
    def auth_provider(self):
        """Return the authentication provider class for this Team, or None.

        No DB queries are used, so this is safe to call many times.

        """
        if not self.auth_provider_code:
            return None
        else:
            return get_authentication_provider(self.auth_provider_code)

    # Thumbnails
    def logo_thumbnail(self):
        """Return the URL for a kind-of small version of this team's logo, or None."""
        if self.logo:
            return self.logo.thumb_url(100, 100)

    def medium_logo_thumbnail(self):
        """Return the URL for a medium version of this team's logo, or None."""
        if self.logo:
            return self.logo.thumb_url(280, 100)

    def small_logo_thumbnail(self):
        """Return the URL for a really small version of this team's logo, or None."""
        if self.logo:
            return self.logo.thumb_url(50, 50)


    # URLs
    @models.permalink
    def get_absolute_url(self):
        return ('teams:dashboard', [self.slug])

    def get_site_url(self):
        """Return the full, absolute URL for this team, including http:// and the domain."""
        return '%s://%s%s' % (DEFAULT_PROTOCOL,
                              Site.objects.get_current().domain,
                              self.get_absolute_url())


    # Membership and roles
    def _is_role(self, user, role=None):
        """Return whether the given user has the given role in this team.

        Safe to use with null or unauthenticated users.

        If no role is given, simply return whether the user is a member of this team at all.

        TODO: Change this to use the stuff in teams.permissions.

        """
        if not user or not user.is_authenticated():
            return False
        qs = self.members.filter(user=user)
        if role:
            qs = qs.filter(role=role)
        return qs.exists()

    def is_owner(self, user):
        """
        Return whether the given user is an owner of this team.
        """
        return self._is_role(user, TeamMember.ROLE_OWNER)

    def is_admin(self, user):
        """Return whether the given user is an admin of this team."""
        return self._is_role(user, TeamMember.ROLE_ADMIN)

    def is_manager(self, user):
        """Return whether the given user is a manager of this team."""
        return self._is_role(user, TeamMember.ROLE_MANAGER)

    def is_member(self, user):
        """Return whether the given user is a member of this team."""
        return self._is_role(user)

    def is_contributor(self, user, authenticated=True):
        """Return whether the given user is a contributor of this team, False otherwise."""
        return self._is_role(user, TeamMember.ROLE_CONTRIBUTOR)

    def can_see_video(self, user, team_video=None):
        """I have no idea.

        TODO: Figure out what this thing is, and if it's still necessary.

        """
        if not user.is_authenticated():
            return False
        return self.is_member(user)

    # moderation


    # Moderation
    def moderates_videos(self):
        """Return whether this team moderates videos in some way, False otherwise.

        Moderation means the team restricts who can create subtitles and/or
        translations.

        """
        if self.subtitle_policy != Team.SUBTITLE_IDS['Anyone']:
            return True

        if self.translate_policy != Team.SUBTITLE_IDS['Anyone']:
            return True

        return False

    def video_is_moderated_by_team(self, video):
        """Return whether this team moderates the given video."""
        return video.moderated_by == self


    # Item counts
    @property
    def member_count(self):
        """Return the number of members of this team.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_member_count'):
            setattr(self, '_member_count', self.users.count())
        return self._member_count

    @property
    def videos_count(self):
        """Return the number of videos of this team.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_videos_count'):
            setattr(self, '_videos_count', self.videos.count())
        return self._videos_count

    @property
    def tasks_count(self):
        """Return the number of incomplete, undeleted tasks of this team.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_tasks_count'):
            setattr(self, '_tasks_count', Task.objects.filter(team=self, deleted=False, completed=None).count())
        return self._tasks_count


    # Applications (people applying to join)
    def application_message(self):
        """Return the membership application message for this team, or '' if none exists."""
        try:
            return self.settings.get(key=Setting.KEY_IDS['messages_application']).data
        except Setting.DoesNotExist:
            return ''

    @property
    def applications_count(self):
        """Return the number of open membership applications to this team.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_applications_count'):
            setattr(self, '_applications_count', self.applications.count())
        return self._applications_count


    # Language pairs
    def _lang_pair(self, lp, suffix):
        return SQ(content="{0}_{1}_{2}".format(lp[0], lp[1], suffix))

    def get_videos_for_languages_haystack(self, language=None, num_completed_langs=None,
                                          project=None, user=None, query=None, sort=None):
        from teams.search_indexes import TeamVideoLanguagesIndex

        is_member = (user and user.is_authenticated()
                     and self.members.filter(user=user).exists())

        if is_member:
            qs =  TeamVideoLanguagesIndex.results_for_members(self).filter(team_id=self.id)
        else:
            qs =  TeamVideoLanguagesIndex.results().filter(team_id=self.id)

        if project:
            qs = qs.filter(project_pk=project.pk)

        if query:
            for term in get_terms(query):
                qs = qs.auto_query(qs.query.clean(term).decode('utf-8'))

        if language:
            qs = qs.filter(video_completed_langs=language)

        if num_completed_langs != None:
            qs = qs.filter(num_completed_langs=num_completed_langs)

        qs = qs.order_by({
             'name':  'video_title_exact',
            '-name': '-video_title_exact',
             'subs':  'num_completed_langs',
            '-subs': '-num_completed_langs',
             'time':  'team_video_create_date',
            '-time': '-team_video_create_date',
        }.get(sort or '-time'))

        return qs


    # Projects
    @property
    def default_project(self):
        """Return the default project for this team.

        If it doesn't already exist it will be created.

        TODO: Move the creation into a signal on the team to avoid creating
        multiple default projects here?

        """
        try:
            return Project.objects.get(team=self, slug=Project.DEFAULT_NAME)
        except Project.DoesNotExist:
            p = Project(team=self,name=Project.DEFAULT_NAME)
            p.save()
            return p

    @property
    def has_projects(self):
        """Return whether this team has projects other than the default one."""
        return self.project_set.count() > 1


    # Readable/writeable language codes
    def get_writable_langs(self):
        """Return a list of language code strings that are writable for this team.

        This value may come from memcache.

        """
        return TeamLanguagePreference.objects.get_writable(self)

    def get_readable_langs(self):
        """Return a list of language code strings that are readable for this team.

        This value may come from memcache.

        """
        return TeamLanguagePreference.objects.get_readable(self)


    # Unpublishing
    def unpublishing_enabled(self):
        '''Return whether unpublishing is enabled for this team.

        At the moment unpublishing is only available if the team has reviewing
        and/or approving enabled.

        '''
        w = self.get_workflow()
        return True if w.review_enabled or w.approve_enabled else False


# This needs to be constructed after the model definition since we need a
# reference to the class itself.
Team._meta.permissions = TEAM_PERMISSIONS


# Project
class ProjectManager(models.Manager):
    def for_team(self, team_identifier):
        """Return all non-default projects for the given team with the given identifier.

        The team_identifier passed may be an actual Team object, or a string
        containing a team slug, or the primary key of a team as an integer.

        """
        if hasattr(team_identifier, "pk"):
            team = team_identifier
        elif isinstance(team_identifier, int):
            team = Team.objects.get(pk=team_identifier)
        elif isinstance(team_identifier, str):
            team = Team.objects.get(slug=team_identifier)
        return Project.objects.filter(team=team).exclude(name=Project.DEFAULT_NAME)

class Project(models.Model):
    # All tvs belong to a project, wheather the team has enabled them or not
    # the default project is just a convenience UI that pretends to be part of
    # the team . If this ever gets changed, you need to change migrations/0044
    DEFAULT_NAME = "_root"

    team = models.ForeignKey(Team)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(blank=True)

    name = models.CharField(max_length=255, null=False)
    description = models.TextField(blank=True, null=True, max_length=2048)
    guidelines = models.TextField(blank=True, null=True, max_length=2048)

    slug = models.SlugField(blank=True)
    order = models.PositiveIntegerField(default=0)

    workflow_enabled = models.BooleanField(default=False)

    objects = ProjectManager()

    def __unicode__(self):
        if self.is_default_project:
            return u"---------"
        return u"%s" % (self.name)

    def save(self, slug=None,*args, **kwargs):
        self.modified = datetime.datetime.now()
        slug = slug if slug is not None else self.slug or self.name
        self.slug = pan_slugify(slug)
        super(Project, self).save(*args, **kwargs)

    @property
    def is_default_project(self):
        """Return whether this project is a default project for a team."""
        return self.name == Project.DEFAULT_NAME


    def get_site_url(self):
        """Return the full, absolute URL for this project, including http:// and the domain."""
        return '%s://%s%s' % (DEFAULT_PROTOCOL, Site.objects.get_current().domain, self.get_absolute_url())

    @models.permalink
    def get_absolute_url(self):
        return ('teams:project_video_list', [self.team.slug, self.slug])


    @property
    def videos_count(self):
        """Return the number of videos in this project.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_videos_count'):
            setattr(self, '_videos_count', TeamVideo.objects.filter(project=self).count())
        return self._videos_count

    @property
    def tasks_count(self):
        """Return the number of incomplete, undeleted tasks in this project.

        Caches the result in-object for performance.

        """
        tasks = Task.objects.filter(team=self.team, deleted=False, completed=None)

        if not hasattr(self, '_tasks_count'):
            setattr(self, '_tasks_count', tasks.filter(team_video__project = self).count())
        return self._tasks_count


    class Meta:
        unique_together = (
                ("team", "name",),
                ("team", "slug",),
        )
        permissions = PROJECT_PERMISSIONS


# TeamVideo
class TeamVideo(models.Model):
    team = models.ForeignKey(Team)
    video = models.OneToOneField(Video)
    description = models.TextField(blank=True,
        help_text=_(u'Use this space to explain why you or your team need to '
                    u'caption or subtitle this video. Adding a note makes '
                    u'volunteers more likely to help out!'))
    thumbnail = S3EnabledImageField(upload_to='teams/video_thumbnails/', null=True, blank=True,
        help_text=_(u'We automatically grab thumbnails for certain sites, e.g. Youtube'),
                                    thumb_sizes=((290,165), (120,90),))
    all_languages = models.BooleanField(_('Need help with all languages'), default=False,
        help_text=_(u'If you check this, other languages will not be displayed.'))
    added_by = models.ForeignKey(User)
    # this is an auto_add like field, but done on the model save so the
    # admin doesn't throw a fit
    created = models.DateTimeField(blank=True)
    partner_id = models.CharField(max_length=100, blank=True, default="")

    project = models.ForeignKey(Project)

    class Meta:
        unique_together = (('team', 'video'),)

    def __unicode__(self):
        return unicode(self.video)

    def link_to_page(self):
        if self.all_languages:
            return self.video.get_absolute_url()
        return reverse('videos:history', [self.video.video_id])

    @models.permalink
    def get_absolute_url(self):
        return ('teams:team_video', [self.pk])

    def get_thumbnail(self):
        if self.thumbnail:
            return self.thumbnail.thumb_url(290, 165)

        video_thumb = self.video.get_thumbnail(fallback=False)
        if video_thumb:
            return video_thumb

        if self.team.logo:
            return self.team.logo_thumbnail()

        return "%simages/video-no-thumbnail-medium.png" % settings.STATIC_URL_BASE

    def _original_language(self):
        if not hasattr(self, 'original_language_code'):
            sub_lang = self.video.subtitle_language()
            setattr(self, 'original_language_code', None if not sub_lang else sub_lang.language)
        return getattr(self, 'original_language_code')

    def save(self, *args, **kwargs):
        if not hasattr(self, "project"):
            self.project = self.team.default_project

        assert self.project.team == self.team, \
                    "%s: Team (%s) is not equal to project's (%s) team (%s)"\
                         % (self, self.team, self.project, self.project.team)

        if not self.pk:
            self.created = datetime.datetime.now()
        super(TeamVideo, self).save(*args, **kwargs)


    def is_checked_out(self, ignore_user=None):
        '''Return whether this video is checked out in a task.

        If a user is given, checkouts by that user will be ignored.  This
        provides a way to ask "can user X check out or work on this task?".

        This is similar to the writelocking done on Videos and
        SubtitleLanguages.

        '''
        tasks = self.task_set.filter(
                # Find all tasks for this video which:
                deleted=False,           # - Aren't deleted
                assignee__isnull=False,  # - Are assigned to someone
                language="",             # - Aren't specific to a language
                completed__isnull=True,  # - Are unfinished
        )
        if ignore_user:
            tasks = tasks.exclude(assignee=ignore_user)

        return tasks.exists()


    # Convenience functions
    def subtitles_started(self):
        """Return whether subtitles have been started for this video."""
        sl = self.video.subtitle_language()
        return True if sl and sl.had_version else False

    def subtitles_finished(self):
        """Return whether at least one set of subtitles has been finished for this video."""
        return (self.subtitles_started() and
                self.video.subtitle_language().is_complete_and_synced())

    def get_workflow(self):
        """Return the appropriate Workflow for this TeamVideo."""
        return Workflow.get_for_team_video(self)

    def move_to(self, new_team, project=None):
        """
        Moves this TeamVideo to a new team.
        This method expects you to have run the correct permissions checks.
        """
        # these imports are here to avoid circular imports, hacky
        from teams.signals import api_teamvideo_new
        from teams.signals import video_moved_from_team_to_team
        from videos import metadata_manager
        # For now, we'll just delete any tasks associated with the moved video.
        self.task_set.update(deleted=True)

        # We move the video by just switching the team, instead of deleting and
        # recreating it.
        self.team = new_team

        # projects are always team dependent:
        if project:
            self.project = project
        else:
            self.project = new_team.default_project

        self.save()

        # We need to make any as-yet-unmoderated versions public.
        # TODO: Dedupe this and the team video delete signal.
        video = self.video

        workflow = new_team.get_workflow()
        if not (workflow.review_enabled or workflow.approve_enabled):
            SubtitleVersion.objects.filter(language__video=video).exclude(
                moderation_status=MODERATION.APPROVED).update(
                    moderation_status=MODERATION.UNMODERATED)

        video.is_public = new_team.is_visible
        video.moderated_by = new_team if new_team.moderates_videos() else None
        video.save()

        # Update all Solr data.
        metadata_manager.update_metadata(video.pk)
        video.update_search_index()
        update_one_team_video(self.pk)

        # Create any necessary tasks.
        autocreate_tasks(self)

        # fire a http notification that a new video has hit this team:
        api_teamvideo_new.send(self)
        video_moved_from_team_to_team.send(sender=self,
                destination_team=new_team, video=self.video)


def _create_translation_tasks(team_video, subtitle_version=None):
    """Create any translation tasks that should be autocreated for this video.

    subtitle_version should be the original SubtitleVersion that these tasks
    will probably be translating from.

    """
    preferred_langs = TeamLanguagePreference.objects.get_preferred(team_video.team)

    for lang in preferred_langs:
        # Don't create tasks for languages that are already complete.
        sl = team_video.video.subtitle_language(lang)
        if sl and sl.is_complete_and_synced():
            continue

        # Don't create tasks for languages that already have one.  This includes
        # review/approve tasks and such.
        # Doesn't matter if it's complete or not.
        task_exists = Task.objects.not_deleted().filter(
            team=team_video.team, team_video=team_video, language=lang
        ).exists()
        if task_exists:
            continue

        # Otherwise, go ahead and create it.
        task = Task(team=team_video.team, team_video=team_video,
                    language=lang, type=Task.TYPE_IDS['Translate'])
        task.subtitle_version = subtitle_version or task.get_subtitle_version()
        # we should only update the team video after all tasks for
        # this video are saved, else we end up with a lot of
        # wasted tasks
        task.save(update_team_video_index=False)

    update_one_team_video.delay(team_video.pk)

def autocreate_tasks(team_video):
    workflow = Workflow.get_for_team_video(team_video)
    existing_subtitles = team_video.video.completed_subtitle_languages(public_only=True)

    # We may need to create a transcribe task, if there are no existing subs.
    if workflow.autocreate_subtitle and not existing_subtitles:
        if not team_video.task_set.not_deleted().exists():
            original_language = team_video.video._original_subtitle_language()
            Task(team=team_video.team,
                 team_video=team_video,
                 subtitle_version=None,
                 language= original_language and original_language.language or '',
                 type=Task.TYPE_IDS['Subtitle']
            ).save()

    # If there are existing subtitles, we may need to create translate tasks.
    #
    # TODO: This sets the "source version" for the translations to an arbitrary
    #       language's version.  In practice this probably won't be a problem
    #       because most teams will transcribe one language and then send to a
    #       new team for translation, but we can probably be smarter about this
    #       if we spend some time.
    if workflow.autocreate_translate and existing_subtitles:
        _create_translation_tasks(team_video)


def team_video_save(sender, instance, created, **kwargs):
    """Update the Solr index for this team video.

    TODO: Rename this to something more specific.

    """
    update_one_team_video.delay(instance.id)

def team_video_delete(sender, instance, **kwargs):
    """Perform necessary actions for when a TeamVideo is deleted.

    TODO: Split this up into separate signals.

    """
    from videos import metadata_manager
    # not using an async task for this since the async task
    # could easily execute way after the instance is gone,
    # and backend.remove requires the instance.
    tv_search_index = site.get_index(TeamVideo)
    tv_search_index.backend.remove(instance)
    try:
        video = instance.video
        # we need to publish all unpublished subs for this video:
        SubtitleVersion.objects.filter(language__video=video).update(
            moderation_status=MODERATION.UNMODERATED)
        video.is_public = True
        video.moderated_by = None
        video.save()

        metadata_manager.update_metadata(video.pk)
        video.update_search_index()
        sync_latest_versions_for_video.delay(video.pk)
    except Video.DoesNotExist:
        pass


def team_video_autocreate_task(sender, instance, created, raw, **kwargs):
    """Create subtitle/translation tasks for a newly added TeamVideo, if necessary."""
    if created and not raw:
        autocreate_tasks(instance)

def team_video_add_video_moderation(sender, instance, created, raw, **kwargs):
    """Set the .moderated_by attribute on a newly created TeamVideo's Video, if necessary."""
    if created and not raw and instance.team.moderates_videos():
        instance.video.moderated_by = instance.team
        instance.video.save()

def team_video_rm_video_moderation(sender, instance, **kwargs):
    """Clear the .moderated_by attribute on a newly deleted TeamVideo's Video, if necessary."""
    try:
        # when removing a video, this will be triggered by the fk constraing
        # and will be already removed
        instance.video.moderated_by = None
        instance.video.save()
    except Video.DoesNotExist:
        pass


post_save.connect(team_video_save, TeamVideo, dispatch_uid="teams.teamvideo.team_video_save")
post_save.connect(team_video_autocreate_task, TeamVideo, dispatch_uid='teams.teamvideo.team_video_autocreate_task')
post_save.connect(team_video_add_video_moderation, TeamVideo, dispatch_uid='teams.teamvideo.team_video_add_video_moderation')
post_delete.connect(team_video_delete, TeamVideo, dispatch_uid="teams.teamvideo.team_video_delete")
post_delete.connect(team_video_rm_video_moderation, TeamVideo, dispatch_uid="teams.teamvideo.team_video_rm_video_moderation")


# TeamMember
class TeamMemberManager(models.Manager):
    use_for_related_fields = True

    def create_first_member(self, team, user):
        """Make sure that new teams always have an 'owner' member."""

        tm = TeamMember(team=team, user=user, role=ROLE_OWNER)
        tm.save()
        return tm

class TeamMember(models.Model):
    ROLE_OWNER = ROLE_OWNER
    ROLE_ADMIN = ROLE_ADMIN
    ROLE_MANAGER = ROLE_MANAGER
    ROLE_CONTRIBUTOR = ROLE_CONTRIBUTOR

    ROLES = (
        (ROLE_OWNER, _("Owner")),
        (ROLE_MANAGER, _("Manager")),
        (ROLE_ADMIN, _("Admin")),
        (ROLE_CONTRIBUTOR, _("Contributor")),
    )

    team = models.ForeignKey(Team, related_name='members')
    user = models.ForeignKey(User, related_name='team_members')
    role = models.CharField(max_length=16, default=ROLE_CONTRIBUTOR, choices=ROLES, db_index=True)
    created = models.DateTimeField(default=datetime.datetime.now, null=True,
            blank=True)

    objects = TeamMemberManager()

    def __unicode__(self):
        return u'%s' % self.user


    def project_narrowings(self):
        """Return any project narrowings applied to this member."""
        return self.narrowings.filter(project__isnull=False)

    def language_narrowings(self):
        """Return any language narrowings applied to this member."""
        return self.narrowings.filter(project__isnull=True)


    def project_narrowings_fast(self):
        """Return any project narrowings applied to this member.

        Caches the result in-object for speed.

        """
        return [n for n in  self.narrowings_fast() if n.project]

    def language_narrowings_fast(self):
        """Return any language narrowings applied to this member.

        Caches the result in-object for speed.

        """
        return [n for n in self.narrowings_fast() if n.language]

    def narrowings_fast(self):
        """Return any narrowings (both project and language) applied to this member.

        Caches the result in-object for speed.

        """
        if hasattr(self, '_cached_narrowings'):
            if self._cached_narrowings is not None:
                return self._cached_narrowings

        self._cached_narrowings = self.narrowings.all()
        return self._cached_narrowings


    def has_max_tasks(self):
        """Return whether this member has the maximum number of tasks."""
        max_tasks = self.team.max_tasks_per_member
        if max_tasks:
            if self.user.task_set.incomplete().filter(team=self.team).count() >= max_tasks:
                return True
        return False


    class Meta:
        unique_together = (('team', 'user'),)


def clear_tasks(sender, instance, *args, **kwargs):
    """Unassign all tasks assigned to a user.

    Used when deleting a user from a team.

    """
    tasks = instance.team.task_set.incomplete().filter(assignee=instance.user)
    tasks.update(assignee=None)

pre_delete.connect(clear_tasks, TeamMember, dispatch_uid='teams.members.clear-tasks-on-delete')


# MembershipNarrowing
class MembershipNarrowing(models.Model):
    """Represent narrowings that can be made on memberships.

    A single MembershipNarrowing can apply to a project or a language, but not both.

    """
    member = models.ForeignKey(TeamMember, related_name="narrowings")
    project = models.ForeignKey(Project, null=True, blank=True)
    language = models.CharField(max_length=24, blank=True, choices=ALL_LANGUAGES)

    added_by = models.ForeignKey(TeamMember, related_name="narrowing_includer", null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True, blank=None)
    modified = models.DateTimeField(auto_now=True, blank=None)

    def __unicode__(self):
        if self.project:
            return u"Permission restriction for %s to project %s " % (self.member, self.project)
        else:
            return u"Permission restriction for %s to language %s " % (self.member, self.language)


    def save(self, *args, **kwargs):
        # Cannot have duplicate narrowings for a language.
        if self.language:
            duplicate_exists = MembershipNarrowing.objects.filter(
                member=self.member, language=self.language
            ).exclude(id=self.id).exists()

            assert not duplicate_exists, "Duplicate language narrowing detected!"

        # Cannot have duplicate narrowings for a project.
        if self.project:
            duplicate_exists = MembershipNarrowing.objects.filter(
                member=self.member, project=self.project
            ).exclude(id=self.id).exists()

            assert not duplicate_exists, "Duplicate project narrowing detected!"

        return super(MembershipNarrowing, self).save(*args, **kwargs)


class ApplicationInvalidException(Exception):
    pass

class ApplicationManager(models.Manager):

    def can_apply(self, team, user):
        """
        A user can apply either if he is not a member of the team yet, the
        team hasn't said no to the user (either application denied or removed the user'
        and if no applications are pending.
        """
        sour_application_exists =  self.filter(team=team, user=user, status__in=[
            Application.STATUS_MEMBER_REMOVED, Application.STATUS_DENIED,
            Application.STATUS_PENDING]).exists()
        if sour_application_exists:
            return False
        return  not team.is_member(user)

    def open(self, team=None, user=None):
        qs =  self.filter(status=Application.STATUS_PENDING)
        if team:
            qs = qs.filter(team=team)
        if user:
            qs = qs.filter(user=user)
        return qs

# Application
class Application(models.Model):
    team = models.ForeignKey(Team, related_name='applications')
    user = models.ForeignKey(User, related_name='team_applications')
    note = models.TextField(blank=True)
    # None -> not acted upon
    # True -> Approved
    # False -> Rejected
    STATUS_PENDING,STATUS_APPROVED, STATUS_DENIED, STATUS_MEMBER_REMOVED,\
        STATUS_MEMBER_LEFT = xrange(0, 5)
    STATUSES = (
        (STATUS_PENDING, u"Pending"),
        (STATUS_APPROVED, u"Approved"),
        (STATUS_DENIED, u"Denied"),
        (STATUS_MEMBER_REMOVED, u"Member Removed"),
        (STATUS_MEMBER_LEFT, u"Member Left"),
    )
    STATUSES_IDS = dict([choice[::-1] for choice in STATUSES])

    status = models.PositiveIntegerField(default=STATUS_PENDING, choices=STATUSES)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(blank=True, null=True)

    # free text keeping a log of changes to this application
    history = models.TextField(blank=True, null=True)

    objects = ApplicationManager()
    class Meta:
        unique_together = (('team', 'user', 'status'),)


    def approve(self, author, interface):
        """Approve the application.

        This will create an appropriate TeamMember if this application has
        not been already acted upon
        """
        if self.status not in (Application.STATUS_PENDING, Application.STATUS_MEMBER_LEFT):
            raise ApplicationInvalidException("")
        member, created = TeamMember.objects.get_or_create(team=self.team, user=self.user)
        if created:
            notifier.team_member_new.delay(member.pk)
        self.modified = datetime.datetime.now()
        self.status = Application.STATUS_APPROVED
        self.save(author=author, interface=interface)
        return self

    def deny(self, author, interface):
        """
        Marks the application as not approved, then
        Queue a Celery task that will handle properly denying this
        application.
        """
        if self.status != Application.STATUS_PENDING:
            raise ApplicationInvalidException("")
        self.modified = datetime.datetime.now()
        self.status = Application.STATUS_DENIED
        self.save(author=author, interface=interface)
        notifier.team_application_denied.delay(self.pk)
        return self

    def on_member_leave(self, author, interface):
        """
        Marks the appropriate status, but users can still
        reapply to a team if they so desire later.
        """
        self.status = Application.STATUS_MEMBER_LEFT
        self.save(author=author, interface=interface)

    def on_member_removed(self, author, interface):
        """
        Marks the appropriate status so that user's cannot reapply
        to a team after being removed.
        """
        self.status = Application.STATUS_MEMBER_REMOVED
        self.save(author=author, interface=interface)

    def _generate_history_line(self, new_status, author=None, interface=None):
        author = author or "?"
        interface = interface or "web UI"
        new_status = new_status if new_status != None else Application.STATUS_PENDING
        for value,name in Application.STATUSES:
            if value == new_status:
                status = name
        assert status
        return u"%s by %s from %s (%s)\n" % (status, author, interface, datetime.datetime.now())

    def save(self, dispatches_http_callback=True, author=None, interface=None, *args, **kwargs):
        """
        Saves the model, but also appends a line on the history for that
        model, like these:
           - CoolGuy Approved through the web UI.
           - Arthur Left team through the web UI.
        This way,we can keep one application per user per team, never
        delete them (so the messages stay current) and we still can
        track history
        """
        self.history = (self.history or "") + self._generate_history_line(self.status, author, interface)
        super(Application, self).save(*args, **kwargs)
        if dispatches_http_callback:
            from teams.signals import api_application_new
            api_application_new.send(self)

    def __unicode__(self):
        return "Application: %s - %s - %s" % (self.team.slug, self.user.username, self.get_status_display())

# Invites
class InviteExpiredException(Exception):
    pass

class InviteManager(models.Manager):
    def pending_for(self, team, user):
        return self.filter(team=team, user=user, approved=None)

    def acted_on(self, team, user):
        return self.filter(team=team, user=user, approved__notnull=True)

class Invite(models.Model):
    team = models.ForeignKey(Team, related_name='invitations')
    user = models.ForeignKey(User, related_name='team_invitations')
    note = models.TextField(blank=True, max_length=200)
    author = models.ForeignKey(User)
    role = models.CharField(max_length=16, choices=TeamMember.ROLES,
                            default=TeamMember.ROLE_CONTRIBUTOR)
    # None -> not acted upon
    # True -> Approved
    # False -> Rejected
    approved = models.NullBooleanField(default=None)

    objects = InviteManager()

    def accept(self):
        """Accept this invitation.

        Creates an appropriate TeamMember record, sends a notification and
        deletes itself.

        """
        if self.approved is not None:
            raise InviteExpiredException("")
        self.approved = True
        member, created = TeamMember.objects.get_or_create(
            team=self.team, user=self.user, role=self.role)
        if created:
            notifier.team_member_new.delay(member.pk)
        self.save()
        return True

    def deny(self):
        """Deny this invitation.

        Could be useful to send a notification here in the future.

        """
        if self.approved is not None:
            raise InviteExpiredException("")
        self.approved = False
        self.save()


    def message_json_data(self, data, msg):
        data['can-reply'] = False
        return data


# Workflows
class Workflow(models.Model):
    REVIEW_CHOICES = (
        (00, "Don't require review"),
        (10, 'Peer must review'),
        (20, 'Manager must review'),
        (30, 'Admin must review'),
    )
    REVIEW_NAMES = dict(REVIEW_CHOICES)
    REVIEW_IDS = dict([choice[::-1] for choice in REVIEW_CHOICES])

    APPROVE_CHOICES = (
        (00, "Don't require approval"),
        (10, 'Manager must approve'),
        (20, 'Admin must approve'),
    )
    APPROVE_NAMES = dict(APPROVE_CHOICES)
    APPROVE_IDS = dict([choice[::-1] for choice in APPROVE_CHOICES])

    team = models.ForeignKey(Team)

    project = models.ForeignKey(Project, blank=True, null=True)
    team_video = models.ForeignKey(TeamVideo, blank=True, null=True)

    autocreate_subtitle = models.BooleanField(default=False)
    autocreate_translate = models.BooleanField(default=False)

    review_allowed = models.PositiveIntegerField(
            choices=REVIEW_CHOICES, verbose_name='reviewers', default=0)

    approve_allowed = models.PositiveIntegerField(
            choices=APPROVE_CHOICES, verbose_name='approvers', default=0)

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        unique_together = ('team', 'project', 'team_video')


    @classmethod
    def _get_target_team(cls, id, type):
        """Return the team for the given target.

        The target is identified by id (its PK as an integer) and type (a string
        of 'team_video', 'project', or 'team').

        """
        if type == 'team_video':
            return TeamVideo.objects.select_related('team').get(pk=id).team
        elif type == 'project':
            return Project.objects.select_related('team').get(pk=id).team
        else:
            return Team.objects.get(pk=id)

    @classmethod
    def get_for_target(cls, id, type, workflows=None):
        '''Return the most specific Workflow for the given target.

        If target object does not exist, None is returned.

        If workflows is given, it should be a QS or List of all Workflows for
        the TeamVideo's team.  This will let you look it up yourself once and
        use it in many of these calls to avoid hitting the DB each time.

        If workflows is not given it will be looked up with one DB query.

        '''
        if not workflows:
            team = Workflow._get_target_team(id, type)
            workflows = list(Workflow.objects.filter(team=team.id).select_related('project', 'team', 'team_video'))
        else:
            team = workflows[0].team

        default_workflow = Workflow(team=team)

        if not workflows:
            return default_workflow

        if type == 'team_video':
            try:
                return [w for w in workflows
                        if w.team_video and w.team_video.id == id][0]
            except IndexError:
                # If there's no video-specific workflow for this video, there
                # might be a workflow for its project, so we'll start looking
                # for that instead.
                team_video = TeamVideo.objects.get(pk=id)
                id, type = team_video.project_id, 'project'

        if type == 'project':
            try:
                return [w for w in workflows
                        if w.project and w.project.workflow_enabled
                        and w.project.id == id and not w.team_video][0]
            except IndexError:
                # If there's no project-specific workflow for this project,
                # there might be one for its team, so we'll fall through.
                pass

        if not team.workflow_enabled:
            return default_workflow

        return [w for w in workflows
                if (not w.project) and (not w.team_video)][0]


    @classmethod
    def get_for_team_video(cls, team_video, workflows=None):
        '''Return the most specific Workflow for the given team_video.

        If workflows is given, it should be a QuerySet or List of all Workflows
        for the TeamVideo's team.  This will let you look it up yourself once
        and use it in many of these calls to avoid hitting the DB each time.

        If workflows is not given it will be looked up with one DB query.

        NOTE: This function caches the workflow for performance reasons.  If the
        workflow changes within the space of a single request that
        _cached_workflow should be cleared.

        '''
        if not hasattr(team_video, '_cached_workflow'):
            team_video._cached_workflow = Workflow.get_for_target(team_video.id, 'team_video', workflows)
        return team_video._cached_workflow

    @classmethod
    def get_for_project(cls, project, workflows=None):
        '''Return the most specific Workflow for the given project.

        If workflows is given, it should be a QuerySet or List of all Workflows
        for the Project's team.  This will let you look it up yourself once
        and use it in many of these calls to avoid hitting the DB each time.

        If workflows is not given it will be looked up with one DB query.

        '''
        return Workflow.get_for_target(project.id, 'project', workflows)

    @classmethod
    def add_to_team_videos(cls, team_videos):
        '''Add the appropriate Workflow objects to each TeamVideo as .workflow.

        This will only perform one DB query, and it will add the most specific
        workflow possible to each TeamVideo.

        This only exists for performance reasons.

        '''
        if not team_videos:
            return []

        workflows = list(Workflow.objects.filter(team=team_videos[0].team))

        for tv in team_videos:
            tv.workflow = Workflow.get_for_team_video(tv, workflows)


    def get_specific_target(self):
        """Return the most specific target that this workflow applies to."""
        return self.team_video or self.project or self.team


    def __unicode__(self):
        target = self.get_specific_target()
        return u'Workflow %s for %s (%s %d)' % (
                self.pk, target, target.__class__.__name__, target.pk)


    # Convenience functions for checking if a step of the workflow is enabled.
    @property
    def review_enabled(self):
        """Return whether any form of review is enabled for this workflow."""
        return True if self.review_allowed else False

    @property
    def approve_enabled(self):
        """Return whether any form of approval is enabled for this workflow."""
        return True if self.approve_allowed else False

    @property
    def requires_review_or_approval(self):
        """Return whether a given workflow requires review or approval."""
        return self.approve_enabled or self.review_enabled

    @property
    def requires_tasks(self):
        """Return whether a given workflow requires the use of tasks."""
        return (self.requires_review_or_approval or self.autocreate_subtitle
                or self.autocreate_translate)


# Tasks
class TaskManager(models.Manager):
    def not_deleted(self):
        """Return a QS of tasks that are not deleted."""
        return self.get_query_set().filter(deleted=False)


    def incomplete(self):
        """Return a QS of tasks that are not deleted or completed."""
        return self.not_deleted().filter(completed=None)

    def complete(self):
        """Return a QS of tasks that are not deleted, but are completed."""
        return self.not_deleted().filter(completed__isnull=False)


    def _type(self, types, completed=None, approved=None):
        """Return a QS of tasks that are not deleted and are one of the given types.

        types should be a list of strings matching a label in Task.TYPE_CHOICES.

        completed should be one of:

        * True (only show completed tasks)
        * False (only show incomplete tasks)
        * None (don't filter on completion status)

        approved should be either None or a string matching a label in
        Task.APPROVED_CHOICES.

        """
        type_ids = [Task.TYPE_IDS[type] for type in types]
        qs = self.not_deleted().filter(type__in=type_ids)

        if completed == False:
            qs = qs.filter(completed=None)
        elif completed == True:
            qs = qs.filter(completed__isnull=False)

        if approved:
            qs = qs.filter(approved=Task.APPROVED_IDS[approved])

        return qs


    def incomplete_subtitle(self):
        """Return a QS of subtitle tasks that are not deleted or completed."""
        return self._type(['Subtitle'], False)

    def incomplete_translate(self):
        """Return a QS of translate tasks that are not deleted or completed."""
        return self._type(['Translate'], False)

    def incomplete_review(self):
        """Return a QS of review tasks that are not deleted or completed."""
        return self._type(['Review'], False)

    def incomplete_approve(self):
        """Return a QS of approve tasks that are not deleted or completed."""
        return self._type(['Approve'], False)

    def incomplete_subtitle_or_translate(self):
        """Return a QS of subtitle or translate tasks that are not deleted or completed."""
        return self._type(['Subtitle', 'Translate'], False)

    def incomplete_review_or_approve(self):
        """Return a QS of review or approve tasks that are not deleted or completed."""
        return self._type(['Review', 'Approve'], False)


    def complete_subtitle(self):
        """Return a QS of subtitle tasks that are not deleted, but are completed."""
        return self._type(['Subtitle'], True)

    def complete_translate(self):
        """Return a QS of translate tasks that are not deleted, but are completed."""
        return self._type(['Translate'], True)

    def complete_review(self, approved=None):
        """Return a QS of review tasks that are not deleted, but are completed.

        If approved is given the tasks are further filtered on their .approved
        attribute.  It must be a string matching one of the labels in
        Task.APPROVED_CHOICES, like 'Rejected'.

        """
        return self._type(['Review'], True, approved)

    def complete_approve(self, approved=None):
        """Return a QS of approve tasks that are not deleted, but are completed.

        If approved is given the tasks are further filtered on their .approved
        attribute.  It must be a string matching one of the labels in
        Task.APPROVED_CHOICES, like 'Rejected'.

        """
        return self._type(['Approve'], True, approved)

    def complete_subtitle_or_translate(self):
        """Return a QS of subtitle or translate tasks that are not deleted, but are completed."""
        return self._type(['Subtitle', 'Translate'], True)

    def complete_review_or_approve(self, approved=None):
        """Return a QS of review or approve tasks that are not deleted, but are completed.

        If approved is given the tasks are further filtered on their .approved
        attribute.  It must be a string matching one of the labels in
        Task.APPROVED_CHOICES, like 'Rejected'.

        """
        return self._type(['Review', 'Approve'], True, approved)


    def all_subtitle(self):
        """Return a QS of subtitle tasks that are not deleted."""
        return self._type(['Subtitle'])

    def all_translate(self):
        """Return a QS of translate tasks that are not deleted."""
        return self._type(['Translate'])

    def all_review(self):
        """Return a QS of review tasks that are not deleted."""
        return self._type(['Review'])

    def all_approve(self):
        """Return a QS of tasks that are not deleted."""
        return self._type(['Approve'])

    def all_subtitle_or_translate(self):
        """Return a QS of subtitle or translate tasks that are not deleted."""
        return self._type(['Subtitle', 'Translate'])

    def all_review_or_approve(self):
        """Return a QS of review or approve tasks that are not deleted."""
        return self._type(['Review', 'Approve'])


class Task(models.Model):
    TYPE_CHOICES = (
        (10, 'Subtitle'),
        (20, 'Translate'),
        (30, 'Review'),
        (40, 'Approve'),
    )
    TYPE_NAMES = dict(TYPE_CHOICES)
    TYPE_IDS = dict([choice[::-1] for choice in TYPE_CHOICES])

    APPROVED_CHOICES = (
        (10, 'In Progress'),
        (20, 'Approved'),
        (30, 'Rejected'),
    )
    APPROVED_NAMES = dict(APPROVED_CHOICES)
    APPROVED_IDS = dict([choice[::-1] for choice in APPROVED_CHOICES])
    APPROVED_FINISHED_IDS = (20, 30)

    type = models.PositiveIntegerField(choices=TYPE_CHOICES)

    team = models.ForeignKey(Team)
    team_video = models.ForeignKey(TeamVideo)
    language = models.CharField(max_length=16, choices=ALL_LANGUAGES, blank=True,
                                db_index=True)
    assignee = models.ForeignKey(User, blank=True, null=True)
    subtitle_version = models.ForeignKey(SubtitleVersion, blank=True, null=True)
    new_subtitle_version = models.ForeignKey(NewSubtitleVersion,
                                             blank=True, null=True)

    # The original source version being reviewed or approved.
    #
    # For example, if person A creates two versions while working on a subtitle
    # task:
    #
    #  v1  v2
    # --o---o
    #   s   s
    #
    # and then the reviewer and approver make some edits
    #
    #  v1  v2  v3  v4  v5
    # --o---o---o---o---o
    #   s   s   r   r   a
    #       *
    #
    # the review_base_version will be v2.  Once approved, if an edit is made it
    # needs to be approved as well, and the same thing happens:
    #
    #  v1  v2  v3  v4  v5  v6  v7
    # --o---o---o---o---o---o---o
    #   s   s   r   r   a   e   a
    #                       *
    #
    # This is used when rejecting versions, and may be used elsewhere in the
    # future as well.
    review_base_version = models.ForeignKey(SubtitleVersion, blank=True,
                                            null=True, related_name='tasks_based_on')
    new_review_base_version = models.ForeignKey(NewSubtitleVersion, blank=True,
                                                null=True,
                                                related_name='tasks_based_on_new')

    deleted = models.BooleanField(default=False)

    # TODO: Remove this field.
    public = models.BooleanField(default=False)

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, editable=False)
    completed = models.DateTimeField(blank=True, null=True)
    expiration_date = models.DateTimeField(blank=True, null=True)

    # Arbitrary priority for tasks. Some teams might calculate this
    # on complex criteria and expect us to be able to sort tasks on it.
    # Higher numbers mean higher priority
    priority = models.PositiveIntegerField(blank=True, default=0, db_index=True)
    # Review and Approval -specific fields
    approved = models.PositiveIntegerField(choices=APPROVED_CHOICES,
                                           null=True, blank=True)
    body = models.TextField(blank=True, default="")

    objects = TaskManager()

    def __unicode__(self):
        return u'Task %s (%s) for %s' % (self.id or "unsaved",
                                         self.get_type_display(),
                                         self.team_video)


    @property
    def workflow(self):
        '''Return the most specific workflow for this task's TeamVideo.'''
        return Workflow.get_for_team_video(self.team_video)


    def _add_comment(self):
        """Add a comment on the SubtitleLanguage for this task with the body as content."""
        if self.body.strip():
            lang_ct = ContentType.objects.get_for_model(SubtitleLanguage)
            comment = Comment(
                content=self.body,
                object_pk=self.subtitle_version.language.pk,
                content_type=lang_ct,
                submit_date=self.completed,
                user=self.assignee,
            )
            comment.save()
            notifier.send_video_comment_notification.delay(comment.pk,
                                    version_pk=self.subtitle_version.pk)

    def future(self):
        """Return whether this task expires in the future."""
        return self.expiration_date > datetime.datetime.now()

    def get_widget_url(self):
        """Return a URL for whatever dialog is used to perform this task."""
        mode = Task.TYPE_NAMES[self.type].lower()
        if self.subtitle_version:
            base_url = self.subtitle_version.language.get_widget_url(mode, self.pk)
        else:
            video = self.team_video.video
            if self.language and video.subtitle_language(self.language):
                lang = video.subtitle_language(self.language)
                base_url = reverse("videos:translation_history", kwargs={
                    "video_id": video.video_id,
                    "lang": lang.language,
                    "lang_id": lang.pk,
                })
            else:
                # subtitle tasks might not have a language
                base_url = video.get_absolute_url()
        return base_url + "?t=%s" % self.pk


    def _set_version_moderation_status(self):
        """Set this task's subtitle_version's moderation_status to the appropriate value.

        This assumes that this task is an Approve/Review task, and that the
        approved field is set to Approved or Rejected.

        """
        assert self.get_type_display() in ('Approve', 'Review'), \
               "Tried to set version moderation status from a non-review/approval task."

        assert self.get_approved_display() in ('Approved', 'Rejected'), \
               "Tried to set version moderation status from an un-ruled-upon task."

        if self.approved == Task.APPROVED_IDS['Approved']:
            moderation_status = MODERATION.APPROVED
        else:
            moderation_status = MODERATION.REJECTED

        SubtitleVersion.objects.filter(pk=self.subtitle_version.pk).update(
                moderation_status=moderation_status)

    def _send_back(self, sends_notification=True):
        """Handle "rejection" of this task.

        This will:

        * Create a new task with the appropriate type (translate or subtitle).
        * Try to reassign it to the previous assignee, leaving it unassigned
          if that's not possible.
        * Send a notification unless sends_notification is given as False.

        NOTE: This function does not modify the *current* task in any way.

        """
        # when sending back, instead of always sending back
        # to the first step (translate/subtitle) go to the 
        # step before this one:
        # Translate/Subtitle -> Review -> Approve
        # also, you can just send back approve and review tasks.
        if self.type == Task.TYPE_IDS['Approve'] and self.workflow.review_enabled:
            type = Task.TYPE_IDS['Review']
        else:
            if self.subtitle_version.language.is_original:
                type = Task.TYPE_IDS['Subtitle']
            else:
                type = Task.TYPE_IDS['Translate']

        # let's guess which assignee should we use
        # by finding the last user that did this task type
        previous_task = Task.objects.complete().filter(
            team_video=self.team_video, language=self.language, team=self.team, type=type
        ).order_by('-completed')[:1]

        if previous_task:
            assignee = previous_task[0].assignee
        else:
            assignee = None

        # The target assignee may have left the team in the mean time.
        if not self.team.members.filter(user=assignee).exists():
            assignee = None

        # TODO: Shouldn't this be WAITING_MODERATION?
        self.subtitle_version.moderation_status = WAITING_MODERATION
        self.subtitle_version.save()

        task = Task(team=self.team, team_video=self.team_video,
                    language=self.language, type=type, assignee=assignee)

        if type == Task.TYPE_IDS['Review']:
            task.subtitle_version = self.subtitle_version

        task.set_expiration()

        task.save()

        if sends_notification:
            # notify original submiter (assignee of self)
            notifier.reviewed_and_sent_back.delay(self.pk)


    def complete(self):
        '''Mark as complete and return the next task in the process if applicable.'''
        self.completed = datetime.datetime.now()
        self.save()

        return { 'Subtitle': self._complete_subtitle,
                 'Translate': self._complete_translate,
                 'Review': self._complete_review,
                 'Approve': self._complete_approve,
        }[Task.TYPE_NAMES[self.type]]()

    def _can_publish_directly(self, subtitle_version):
        from teams.permissions import can_publish_edits_immediately
        return (can_publish_edits_immediately(self.team_video,
                                                    self.assignee,
                                                    self.language) and
                subtitle_version and
                subtitle_version.prev_version() and
                subtitle_version.language.is_complete_and_synced())

    def _find_previous_assignee(self, type):
        """Find the previous assignee for a new review/approve task for this video.

        NOTE: This is different than finding out the person to send a task back
              to!  This is for saying "who reviewed this task last time?".

        For now, we'll assign the review/approval task to whomever did it last
        time (if it was indeed done), but only if they're still eligible to
        perform it now.

        """
        from teams.permissions import can_review, can_approve

        if type == 'Approve':
            # if there's a previous version, it's a post-publish edit.
            # and according to #1039 we don't wanna auto-assign
            # the assignee
            if self.subtitle_version and self.subtitle_version.prev_version() and \
                    self.subtitle_version.language.is_complete_and_synced():
                return None

            type = Task.TYPE_IDS['Approve']
            can_do = can_approve
        elif type == 'Review':
            type = Task.TYPE_IDS['Review']
            can_do = partial(can_review, allow_own=True)
        else:
            return None

        last_task = self.team_video.task_set.complete().filter(
            language=self.language, type=type
        ).order_by('-completed')[:1]

        if last_task:
            candidate = last_task[0].assignee
            if candidate and can_do(self.team_video, candidate, self.language):
                return candidate

    def _complete_subtitle(self):
        """Handle the messy details of completing a subtitle task."""
        subtitle_version = self.team_video.video.latest_version(
                                language_code=self.language, public_only=False)

        # TL;DR take a look at #1206 to know why i did this
        if self.workflow.requires_review_or_approval and not self._can_publish_directly(subtitle_version):
            if self.workflow.review_enabled:
                task = Task(team=self.team, team_video=self.team_video,
                            subtitle_version=subtitle_version,
                            review_base_version=subtitle_version,
                            language=self.language, type=Task.TYPE_IDS['Review'],
                            assignee=self._find_previous_assignee('Review'))
                task.set_expiration()
                task.save()
            elif self.workflow.approve_enabled:
                task = Task(team=self.team, team_video=self.team_video,
                            subtitle_version=subtitle_version,
                            review_base_version=subtitle_version,
                            language=self.language, type=Task.TYPE_IDS['Approve'],
                            assignee=self._find_previous_assignee('Approve'))
                task.set_expiration()
                task.save()
        else:
            # Subtitle task is done, and there is no approval or review
            # required, so we mark the version as approved.
            subtitle_version.moderation_status = MODERATION.APPROVED
            subtitle_version.save()

            # We need to make sure this is updated correctly here.
            from apps.videos import metadata_manager
            metadata_manager.update_metadata(self.team_video.video.pk)

            if self.workflow.autocreate_translate:
                # TODO: Switch to autocreate_task?
                _create_translation_tasks(self.team_video, subtitle_version)

            upload_subtitles_to_original_service.delay(subtitle_version.pk)

    def _complete_translate(self):
        """Handle the messy details of completing a translate task."""
        subtitle_version = self.team_video.video.latest_version(
                                language_code=self.language, public_only=False)

        # TL;DR take a look at #1206 to know why i did this
        if self.workflow.requires_review_or_approval and not self._can_publish_directly(subtitle_version):
            if self.workflow.review_enabled:
                task = Task(team=self.team, team_video=self.team_video,
                            subtitle_version=subtitle_version,
                            review_base_version=subtitle_version,
                            language=self.language, type=Task.TYPE_IDS['Review'],
                            assignee=self._find_previous_assignee('Review'))
                task.set_expiration()
                task.save()
            elif self.workflow.approve_enabled:
                # The review step may be disabled.  If so, we check the approve step.
                task = Task(team=self.team, team_video=self.team_video,
                            subtitle_version=subtitle_version,
                            review_base_version=subtitle_version,
                            language=self.language, type=Task.TYPE_IDS['Approve'],
                            assignee=self._find_previous_assignee('Approve'))
                task.set_expiration()
                task.save()
        else:
            # Translation task is done, and there is no approval or review
            # required, so we mark the version as approved.
            subtitle_version.moderation_status = MODERATION.APPROVED
            subtitle_version.save()

            # We need to make sure this is updated correctly here.
            from apps.videos import metadata_manager
            metadata_manager.update_metadata(self.team_video.video.pk)
            upload_subtitles_to_original_service.delay(subtitle_version.pk)

            task = None

        return task

    def _complete_review(self):
        """Handle the messy details of completing a review task."""
        approval = self.approved == Task.APPROVED_IDS['Approved']

        self._add_comment()

        task = None
        if self.workflow.approve_enabled:
            # Approval is enabled, so if the reviewer thought these subtitles
            # were good we create the next task.
            if approval:
                task = Task(team=self.team, team_video=self.team_video,
                            subtitle_version=self.subtitle_version,
                            review_base_version=self.subtitle_version,
                            language=self.language, type=Task.TYPE_IDS['Approve'],
                            assignee=self._find_previous_assignee('Approve'))
                task.set_expiration()
                task.save()
                # approval review
                notifier.reviewed_and_pending_approval.delay(self.pk)
            else:
                # The reviewer rejected this version, so it should be explicitly
                # made non-public.
                self._set_version_moderation_status()

                # Send the subtitles back for improvement.
                self._send_back()
        else:
            # Approval isn't enabled, so the ruling of this Review task
            # determines whether the subtitles go public.
            self._set_version_moderation_status()

            if approval:
                # If the subtitles are okay, go ahead and autocreate translation
                # tasks if necessary.
                if self.workflow.autocreate_translate:
                    _create_translation_tasks(self.team_video, self.subtitle_version)

                # non approval review
                notifier.reviewed_and_published.delay(self.pk)
                upload_subtitles_to_original_service.delay(self.subtitle_version.pk)
            else:
                # Send the subtitles back for improvement.
                self._send_back()

        if self.assignee:
            # TODO: See if we can eliminate the need for this if check.
            self.subtitle_version.set_reviewed_by(self.assignee)

        return task

    def _complete_approve(self):
        """Handle the messy details of completing an approve task."""
        approval = self.approved == Task.APPROVED_IDS['Approved']

        self._add_comment()

        # If we manage to get here, the ruling on this Approve task determines
        # whether the subtitles should go public.
        self._set_version_moderation_status()

        # If the subtitles are okay, go ahead and autocreate translation tasks.
        if approval:
            # But only if we haven't already.
            if self.workflow.autocreate_translate:
                _create_translation_tasks(self.team_video, self.subtitle_version)
            upload_subtitles_to_original_service.delay(self.subtitle_version.pk)
        else:
            # Send the subtitles back for improvement.
            self._send_back()

        if self.assignee:
            # TODO: See if we can eliminate the need for this if check.
            self.subtitle_version.set_approved_by(self.assignee)

        notifier.approved_notification.delay(self.pk, approval)


    def get_perform_url(self):
        '''Return the URL that will open whichever dialog is necessary to perform this task.'''
        mode = Task.TYPE_NAMES[self.type].lower()
        if self.get_subtitle_version():
            base_url = self.get_subtitle_version().language.get_widget_url(mode, self.pk)
        else:
            video = self.team_video.video
            if self.language and video.subtitle_language(self.language) :
                lang = video.subtitle_language(self.language)
                base_url = reverse("videos:translation_history", kwargs={
                    "video_id": video.video_id,
                    "lang": lang.language,
                    "lang_id": lang.pk,
                })
            else:
                # subtitle tasks might not have a language
                base_url = video.get_absolute_url()
        return base_url+  "?t=%s" % self.pk

    def get_reviewer(self):
        if self.type == 40:
            previous = Task.objects.complete().filter(
                team_video=self.team_video,
                language=self.language,
                team=self.team,
                type=Task.TYPE_IDS['Review']).order_by('-completed')[:1]

            if previous:
                reviewer = previous[0].assignee
            else:
                reviewer = None

            return reviewer

    def set_expiration(self):
        """Set the expiration_date of this task.  Does not save().

        Requires that self.team and self.assignee be set correctly.

        """
        if not self.assignee or not self.team.task_expiration:
            self.expiration_date = None
        else:
            limit = datetime.timedelta(days=self.team.task_expiration)
            self.expiration_date = datetime.datetime.now() + limit

    def get_subtitle_version(self):
        """ Gets the subtitle version related to this task.
        If the task has a subtitle_version attached, return it and
        if not, try to find it throught the subtitle language of the video.

        Note: we need this since we don't attach incomplete subtitle_version
        to the task (and if we do we need to set the status to unmoderated and
        that causes the version to get published).
        """

        # autocreate sets the subtitle_version to another
        # language's subtitle_version and that was breaking
        # not only the interface but the new upload method.
        if self.subtitle_version and \
                self.subtitle_version.language.language == self.language:
            return self.subtitle_version

        if not hasattr(self, "_subtitle_version"):
            video = Video.objects.get(teamvideo=self.team_video_id)
            language = video.subtitle_language(self.language)
            self._subtitle_version = language.version(public_only=False) if language else None

        return self._subtitle_version

    def is_blocked(self):
        if self.get_type_display() != 'Translate':
            return False

        subtitle_version = self.get_subtitle_version()

        if not subtitle_version:
            return False

        standard_language = subtitle_version.language.standard_language

        if not standard_language:
            return False

        complete = standard_language.is_complete_and_synced() and standard_language.version(public_only=False).is_public()
        return not complete

    def save(self, update_team_video_index=True, *args, **kwargs):
        if self.type in (self.TYPE_IDS['Review'], self.TYPE_IDS['Approve']) and not self.deleted:
            assert self.subtitle_version, \
                   "Review and Approve tasks must have a subtitle_version!"

        if self.language:
            assert self.language in VALID_LANGUAGE_CODES, \
                "Subtitle Language should be a valid code."
        result = super(Task, self).save(*args, **kwargs)
        if update_team_video_index:
            update_one_team_video.delay(self.team_video.pk)
        return result


def task_moderate_version(sender, instance, created, **kwargs):
    """If we create a review or approval task for this subtitle_version, mark it.

    It *must* be awaiting moderation if we've just created one of these tasks
    (and it's not a pre-completed task).

    """
    if created and instance.subtitle_version:
        if instance.type in (Task.TYPE_IDS['Review'], Task.TYPE_IDS['Approve']):
            if not instance.completed:
                instance.subtitle_version.moderation_status = WAITING_MODERATION
                instance.subtitle_version.save()

post_save.connect(task_moderate_version, Task,
                  dispatch_uid="teams.task.task_moderate_version")


# Settings
class SettingManager(models.Manager):
    use_for_related_fields = True

    def guidelines(self):
        """Return a QS of settings related to team guidelines."""
        keys = [key for key, name in Setting.KEY_CHOICES
                if name.startswith('guidelines_')]
        return self.get_query_set().filter(key__in=keys)

    def messages(self):
        """Return a QS of settings related to team messages."""
        keys = [key for key, name in Setting.KEY_CHOICES
                if name.startswith('messages_')]
        return self.get_query_set().filter(key__in=keys)

    def messages_guidelines(self):
        """Return a QS of settings related to team messages or guidelines."""
        keys = [key for key, name in Setting.KEY_CHOICES
                if name.startswith('messages_') or name.startswith('guidelines_')]
        return self.get_query_set().filter(key__in=keys)

class Setting(models.Model):
    KEY_CHOICES = (
        (100, 'messages_invite'),
        (101, 'messages_manager'),
        (102, 'messages_admin'),
        (103, 'messages_application'),
        (200, 'guidelines_subtitle'),
        (201, 'guidelines_translate'),
        (202, 'guidelines_review'),
        # 300s means if this team will block those notifications
        (300, 'block_invitation_sent_message'),
        (301, 'block_application_sent_message'),
        (302, 'block_application_denided_message'),
        (303, 'block_team_member_new_message'),
        (304, 'block_team_member_leave_message'),
        (305, 'block_task_assigned_message'),
        (306, 'block_reviewed_and_published_message'),
        (307, 'block_reviewed_and_pending_approval_message'),
        (308, 'block_reviewed_and_sent_back_message'),
        (309, 'block_approved_message'),
        (310, 'block_new_video_message'),
    )
    KEY_NAMES = dict(KEY_CHOICES)
    KEY_IDS = dict([choice[::-1] for choice in KEY_CHOICES])

    key = models.PositiveIntegerField(choices=KEY_CHOICES)
    data = models.TextField(blank=True)
    team = models.ForeignKey(Team, related_name='settings')

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, editable=False)

    objects = SettingManager()

    class Meta:
        unique_together = (('key', 'team'),)

    def __unicode__(self):
        return u'%s - %s' % (self.team, self.key_name)

    @property
    def key_name(self):
        """Return the key name for this setting.

        TODO: Remove this and replace with get_key_display()?

        """
        return Setting.KEY_NAMES[self.key]


# TeamLanguagePreferences
class TeamLanguagePreferenceManager(models.Manager):
    def _generate_writable(self, team):
        """Return the set of language codes that are writeable for this team."""
        langs_set = set([x[0] for x in settings.ALL_LANGUAGES])

        unwritable = self.for_team(team).filter(allow_writes=False, preferred=False).values("language_code")
        unwritable = set([x['language_code'] for x in unwritable])

        return langs_set - unwritable

    def _generate_readable(self, team):
        """Return the set of language codes that are readable for this team."""
        langs = set([x[0] for x in settings.ALL_LANGUAGES])

        unreadable = self.for_team(team).filter(allow_reads=False, preferred=False).values("language_code")
        unreadable = set([x['language_code'] for x in unreadable])

        return langs - unreadable

    def _generate_preferred(self, team):
        """Return the set of language codes that are preferred for this team."""
        preferred = self.for_team(team).filter(preferred=True).values("language_code")
        return set([x['language_code'] for x in preferred])


    def for_team(self, team):
        """Return a QS of all language preferences for the given team."""
        return self.get_query_set().filter(team=team)

    def on_changed(cls, sender,  instance, *args, **kwargs):
        """Perform any necessary actions when a language preference changes.

        TODO: Refactor this out of the manager...

        """
        from teams.cache import invalidate_lang_preferences
        invalidate_lang_preferences(instance.team)


    def get_readable(self, team):
        """Return the set of language codes that are readable for this team.

        This value may come from memcache if possible.

        """
        from teams.cache import get_readable_langs
        return get_readable_langs(team)

    def get_writable(self, team):
        """Return the set of language codes that are writeable for this team.

        This value may come from memcache if possible.

        """
        from teams.cache import get_writable_langs
        return get_writable_langs(team)

    def get_preferred(self, team):
        """Return the set of language codes that are preferred for this team.

        This value may come from memcache if possible.

        """
        from teams.cache import get_preferred_langs
        return get_preferred_langs(team)

class TeamLanguagePreference(models.Model):
    """Represent language preferences for a given team.

    First, TLPs may mark a language as "preferred".  If that's the case then the
    other attributes of this model are irrelevant and can be ignored.
    "Preferred" languages will have translation tasks automatically created for
    them when subtitles are added.

    If preferred is False, the TLP describes a *restriction* on the language
    instead.  Writing in that language may be prevented, or both reading and
    writing may be prevented.

    (Note: "writing" means not only writing new subtitles but also creating
    tasks, etc)

    This is how the restriction settings should interact.  TLP means that we
    have created a TeamLanguagePreference for that team and language.

    | Action                                 | NO  | allow_read=True,  | allow_read=False, |
    |                                        | TLP | allow_write=False | allow_write=False |
    ========================================================================================
    | assignable as tasks                    | X   |                   |                   |
    | assignable as narrowing                | X   |                   |                   |
    | listed on the widget for viewing       | X   | X                 |                   |
    | listed on the widget for improving     | X   |                   |                   |
    | returned from the api read operations  | X   | X                 |                   |
    | upload / write operations from the api | X   |                   |                   |
    | show up on the start dialog            | X   |                   |                   |
    +----------------------------------------+-----+-------------------+-------------------+

    Remember, this table only applies if preferred=False.  If the language is
    preferred the "restriction" attributes are effectively garbage.  Maybe we
    should make the column nullable to make this more clear?

    allow_read=True, allow_write=True, preferred=False is invalid.  Just remove
    the row all together.

    """
    team = models.ForeignKey(Team, related_name="lang_preferences")
    language_code = models.CharField(max_length=16)

    allow_reads = models.BooleanField()
    allow_writes = models.BooleanField()
    preferred = models.BooleanField(default=False)

    objects = TeamLanguagePreferenceManager()

    class Meta:
        unique_together = ('team', 'language_code')


    def clean(self, *args, **kwargs):
        if self.allow_reads and self.allow_writes:
            raise ValidationError("No sense in having all allowed, just remove the preference for this language.")

        if self.preferred and (self.allow_reads or self.allow_writes):
            raise ValidationError("Cannot restrict a preferred language.")

        super(TeamLanguagePreference, self).clean(*args, **kwargs)

    def __unicode__(self):
        return u"%s preference for team %s" % (self.language_code, self.team)


post_save.connect(TeamLanguagePreference.objects.on_changed, TeamLanguagePreference)


# TeamNotificationSettings
class TeamNotificationSettingManager(models.Manager):
    def notify_team(self, team_pk, event_name, **kwargs):
        """Notify the given team of a given event.

        Finds the matching notification settings for this team, instantiates
        the notifier class, and sends the appropriate notification.

        If the notification settings has an email target, sends an email.

        If the http settings are filled, then sends the request.

        This can be ran as a Celery task, as it requires no objects to be passed.

        """
        try:
            team = Team.objects.get(pk=team_pk)
        except Team.DoesNotExist:
            logger.error("A pk for a non-existent team was passed in.",
                    extra={"team_pk": team_pk, "event_name": event_name})
            return

        try:
            if team.partner:
                notification_settings = self.get(partner=team.partner)
            else:
                notification_settings = self.get(team=team)
        except TeamNotificationSetting.DoesNotExist:
            return

        notification_settings.notify(event_name, **kwargs)


class TeamNotificationSetting(models.Model):
    """Info on how a team should be notified of changes to its videos.

    For now, a team can be notified by having a http request sent with the
    payload as the notification information.  This cannot be hardcoded since
    teams might have different urls for each environment.

    Some teams have strict requirements on mapping video ids to their internal
    values, and also their own language codes. Therefore we need to configure
    a class that can do the correct mapping.

    TODO: allow email notifications

    """
    EVENT_VIDEO_NEW = "video-new"
    EVENT_VIDEO_EDITED = "video-edited"
    EVENT_LANGUAGE_NEW = "language-new"
    EVENT_LANGUAGE_EDITED = "language-edit"
    EVENT_SUBTITLE_NEW = "subs-new"
    EVENT_SUBTITLE_APPROVED = "subs-approved"
    EVENT_SUBTITLE_REJECTED = "subs-rejected"
    EVENT_APPLICATION_NEW = 'application-new'

    team = models.OneToOneField(Team, related_name="notification_settings",
            null=True, blank=True)
    partner = models.OneToOneField('Partner',
        related_name="notification_settings",  null=True, blank=True)

    # the url to post the callback notifing partners of new video activity
    request_url = models.URLField(blank=True, null=True)
    basic_auth_username = models.CharField(max_length=255, blank=True, null=True)
    basic_auth_password = models.CharField(max_length=255, blank=True, null=True)

    # not being used, here to avoid extra migrations in the future
    email = models.EmailField(blank=True, null=True)

    # integers mapping to classes, see unisubs-integration/notificationsclasses.py
    notification_class = models.IntegerField(default=1,)

    objects = TeamNotificationSettingManager()

    def get_notification_class(self):
        try:
            from notificationclasses import NOTIFICATION_CLASS_MAP

            return NOTIFICATION_CLASS_MAP[self.notification_class]
        except ImportError:
            logger.exception("Apparently unisubs-integration is not installed")

    def notify(self, event_name,  **kwargs):
        """Resolve the notification class for this setting and fires notfications."""
        notification = self.get_notification_class()(self.team, self.partner,
                event_name,  **kwargs)
        if self.request_url:
            success, content = notification.send_http_request(
                self.request_url,
                self.basic_auth_username,
                self.basic_auth_password
            )
            return success, content
        # FIXME: spec and test this, for now just return
        return

    def __unicode__(self):
        if self.partner:
            return u'NotificationSettings for partner %s' % self.partner
        return u'NotificationSettings for team %s' % self.team


class BillingReport(models.Model):
    team = models.ForeignKey(Team)
    start_date = models.DateField()
    end_date = models.DateField()
    csv_file = S3EnabledFileField(blank=True, null=True,
            upload_to='teams/billing/')
    processed = models.DateTimeField(blank=True, null=True)

    def __unicode__(self):
        return "%s (%s - %s)" % (self.team.slug,
                self.start_date.strftime('%Y-%m-%d'),
                self.end_date.strftime('%Y-%m-%d'))

    def start_datetime(self):
        midnight = datetime.time(0, 0, 0)
        return datetime.datetime.combine(self.start_date, midnight)

    def end_datetime(self):
        almost_midnight = datetime.time(23, 59, 59)
        return datetime.datetime.combine(self.end_date, almost_midnight)

    def _should_bill(self, language, version, start, end):
        if not version:
            return False

        if version.moderation_status not in [APPROVED, UNMODERATED]:
            return False

        # 97% is done according to our contracts
        if version.moderation_status == UNMODERATED:
            if not language.is_complete and language.percent_done < 97:
                return False

        if (version.datetime_started <= start or
                version.datetime_started >= end):
            return False

        return True

    def _get_lang_data(self, languages, start_date):
        workflow = self.team.get_workflow()

        imported, crowd_created = self._separate_languages(languages)

        if workflow.approve_enabled:
            imported_data = [(language, language.first_approved_version)
                                    for language in imported]
            crowd_created_data = [(language, language.first_approved_version)
                                    for language in crowd_created]
        else:
            imported_data = [(language, language.latest_version()) for
                                                language in imported]
            crowd_created_data = [(language, language.latest_version()) for
                                                language in crowd_created]

        old_version_counter = 1

        created_result = []

        for lang, ver in crowd_created_data:
            if ver and ver.datetime_started < start_date:
                old_version_counter += 1
                continue

            created_result.append((lang, ver))

        return created_result, imported_data, old_version_counter

    def _separate_languages(self, languages):
        """
        Return two lists;  a list of imported languages and a list of crowd
        created languages.

        Imported language is a language either
        * Whose version 0 contains a note of "From youtube"
        * that was completed before team.created
        * that is not English

        Crowd created language is a language
        * that is not imported
        """
        imported = []
        crowd_created = []

        for lang in languages:
            try:
                v = lang.subtitleversion_set.filter(version_no=0)[0]
            except IndexError:
                # Throw away languages that don't have a zero version.
                continue

            if lang.language == 'en':
                crowd_created.append(lang)
                continue

            if v.note == 'From youtube':
                imported.append(lang)
                continue

            if v.datetime_started < self.team.created:
                imported.append(lang)
                continue

            crowd_created.append(lang)

        return imported, crowd_created

    def _get_row_data(self, host, header=None):
        if not header:
            header = []

        rows = [header]

        start_date = self.start_datetime()
        end_date = self.end_datetime()

        tvs = TeamVideo.objects.filter(team=self.team).order_by('video__title')

        for tv in tvs:
            languages = tv.video.subtitlelanguage_set.all()

            created_data, imported_data, old_version_counter = \
                    self._get_lang_data(languages, start_date)

            created_rows = self._loop(created_data, 'created', start_date,
                    end_date, tv, host, old_version_counter)

            imported_rows = self._loop(imported_data, 'imported', start_date,
                    end_date, tv, host)

            rows = rows + created_rows + imported_rows

        return rows

    def _loop(self, iterable, source, start, end, tv, host, counter=None):
        rows = []

        for language, v in iterable:

            if not self._should_bill(language, v, start, end):
                continue

            row = self._prepare_row(tv, language, v, source, counter, host)

            if not row:
                continue

            rows.append(row)

            if counter is not None:
                counter += 1

        return rows

    def _prepare_row(self, tv, language, version, source, counter, host):
        subs = version.ordered_subtitles()

        if len(subs) == 0:
            return None

        start = subs[0].start_time
        end = subs[-1].end_time

        # The -1 value for the end_time isn't allowed anymore but some
        # legacy data will still have it.
        if end == -1:
            end = subs[-1].start_time

        if not end:
            end = subs[-1].start_time

        return [
            tv.video.title_display_unabridged().encode('utf-8'),
            host + tv.video.get_absolute_url(),
            language.language,
            source,
            round((float(end) - float(start)) / (60 * 1000), 2),
            version.datetime_started.strftime("%Y-%m-%d %H:%M:%S"),
            counter or ''
        ]

    def process(self):
        domain = Site.objects.get_current().domain
        protocol = getattr(settings, 'DEFAULT_PROTOCOL')
        host = '%s://%s' % (protocol, domain)

        header = ['Video title', 'Video URL', 'Video language', 'Source',
                'Billable minutes', 'Version created', 'Language number']

        rows = self._get_row_data(host, header)

        fn = '/tmp/bill-%s-%s-%s-%s.csv' % (self.team.slug, self.start_str,
                self.end_str, self.pk)

        with open(fn, 'w') as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        self.csv_file = File(open(fn, 'r'))
        self.processed = datetime.datetime.utcnow()
        self.save()

    @property
    def start_str(self):
        return self.start_date.strftime("%Y%m%d")

    @property
    def end_str(self):
        return self.end_date.strftime("%Y%m%d")


class BillingRecordManager(models.Manager):

    def data_for_team(self, team, start, end):
        return self.filter(team=team, created__gte=start, created__lte=end)

    def csv_report_for_team(self, team, start, end, add_header=True):
        all_records = self.data_for_team(team, start, end)

        header = [
            'Video ID',
            'Language',
            'Minutes',
            'Original',
            'Team',
            'Created',
            'Source',
            'User'
        ]

        if add_header:
            rows = [header]
        else:
            rows = []

        for video, records in groupby(all_records, lambda r: r.video):
            for r in records:
                rows.append([
                    video.video_id,
                    r.subtitle_language.language,
                    r.minutes,
                    r.is_original,
                    r.team.slug,
                    r.created.strftime('%Y-%m-%d %H:%S:%M'),
                    r.source,
                    r.user.username
                ])

        return rows


class BillingRecord(models.Model):
    video = models.ForeignKey(Video)
    subtitle_version = models.ForeignKey(SubtitleVersion)
    subtitle_language = models.ForeignKey(SubtitleLanguage)
    minutes = models.FloatField(blank=True, null=True)
    is_original = models.BooleanField()
    team = models.ForeignKey(Team)
    created = models.DateTimeField()
    source = models.CharField(max_length=255)
    user = models.ForeignKey(User)

    objects = BillingRecordManager()

    class Meta:
        unique_together = ('video', 'subtitle_language')

    def __unicode__(self):
        return "%s - %s" % (self.video.video_id,
                self.subtitle_language.language)

    def save(self, *args, **kwargs):
        if not self.minutes and self.minutes != 0.0:
            self.minutes = self.get_minutes()

        assert self.minutes is not None

        return super(BillingRecord, self).save(*args, **kwargs)

    def get_minutes(self):
        """
        Return the number of minutes the subtitles specified in `version`
        cover as a float.
        """
        subs = self.subtitle_version.ordered_subtitles()

        if len(subs) == 0:
            return 0.0

        start = subs[0].start_time
        end = subs[-1].end_time

        # The -1 value for the end_time isn't allowed anymore but some
        # legacy data will still have it.
        if end == -1:
            end = subs[-1].start_time

        if not end:
            end = subs[-1].start_time

        return round((float(end) - float(start)) / (60 * 1000), 2)


class Partner(models.Model):
    name = models.CharField(_(u'name'), max_length=250, unique=True)
    slug = models.SlugField(_(u'slug'), unique=True)
    can_request_paid_captions = models.BooleanField(default=False)
    
    # The `admins` field specifies users who can do just about anything within
    # the partner realm.
    admins = models.ManyToManyField('auth.CustomUser',
            related_name='managed_partners', blank=True, null=True)

    def __unicode__(self):
        return self.name

    def is_admin(self, user):
        return user in self.admins.all()

