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

import teams.moderation_const as MODERATION
import widget
from accountlinker.models import ThirdPartyAccount
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.db import transaction
from django.db.models import Q, Count
from django.http import (
    Http404, HttpResponseForbidden, HttpResponseRedirect, HttpResponse,
    HttpResponseBadRequest, HttpResponseServerError
)
from django.shortcuts import get_object_or_404, redirect, render_to_response
from django.template import RequestContext
from django.utils import simplejson as json
from django.utils.encoding import iri_to_uri, force_unicode
from django.utils.translation import ugettext_lazy as _
from django.views.generic.list_detail import object_list
from messages import tasks as notifier
from raven.contrib.django.models import client
from teams.forms import (
    CreateTeamForm, AddTeamVideoForm, EditTeamVideoForm,
    AddTeamVideosFromFeedForm, TaskAssignForm, SettingsForm, TaskCreateForm,
    PermissionsForm, WorkflowForm, InviteForm, TaskDeleteForm,
    GuidelinesMessagesForm, RenameableSettingsForm, ProjectForm, LanguagesForm,
    UnpublishForm, MoveTeamVideoForm, UploadDraftForm, ChooseTeamForm
)
from teams.models import Team, TeamMember, Invite, Application, TeamVideo, Task, Project, Workflow, Setting, TeamLanguagePreference, InviteExpiredException, BillingReport, ApplicationInvalidException
from teams.permissions import (
    can_add_video, can_assign_role, can_assign_tasks, can_create_task_subtitle,
    can_create_task_translate, can_view_tasks_tab, can_invite,
    roles_user_can_assign, can_join_team, can_edit_video, can_delete_tasks,
    can_perform_task, can_rename_team, can_change_team_settings,
    can_perform_task_for, can_delete_team, can_review, can_approve,
    can_delete_video, can_remove_video
)
from teams.signals import api_teamvideo_new, api_subtitles_rejected
from teams.tasks import (
    invalidate_video_caches, invalidate_video_moderation_caches,
    update_video_moderation, update_one_team_video, update_video_public_field,
    invalidate_video_visibility_caches, process_billing_report
)
from videos import metadata_manager
from videos.models import Action, VideoUrl, SubtitleLanguage, Video
from videos.tasks import (
    upload_subtitles_to_original_service, delete_captions_in_original_service,
    delete_captions_in_original_service_by_code
)
from videos.types import UPDATE_VERSION_ACTION
from widget.rpc import add_general_settings
from widget.srt_subs import GenerateSubtitlesHandler
from widget.views import base_widget_params

from apps.auth.models import UserLanguage, CustomUser as User
from apps.videos.tasks import video_changed_tasks
from apps.videos.templatetags.paginator import paginate
from utils import render_to, render_to_json, DEFAULT_PROTOCOL
from utils.chunkediter import chunkediter
from utils.forms import flatten_errorlists
from utils.metrics import time as timefn
from utils.panslugify import pan_slugify
from utils.searching import get_terms
from utils.translation import (
    get_language_choices, languages_with_labels, get_user_languages_from_request
)


logger = logging.getLogger("teams.views")


TASKS_ON_PAGE = getattr(settings, 'TASKS_ON_PAGE', 20)
TEAMS_ON_PAGE = getattr(settings, 'TEAMS_ON_PAGE', 10)
MAX_MEMBER_SEARCH_RESULTS = 40
HIGHTLIGHTED_TEAMS_ON_PAGE = getattr(settings, 'HIGHTLIGHTED_TEAMS_ON_PAGE', 10)
CUTTOFF_DUPLICATES_NUM_VIDEOS_ON_TEAMS = getattr(settings, 'CUTTOFF_DUPLICATES_NUM_VIDEOS_ON_TEAMS', 20)

VIDEOS_ON_PAGE = getattr(settings, 'VIDEOS_ON_PAGE', 16)
MEMBERS_ON_PAGE = getattr(settings, 'MEMBERS_ON_PAGE', 15)
APLICATIONS_ON_PAGE = getattr(settings, 'APLICATIONS_ON_PAGE', 15)
ACTIONS_ON_PAGE = getattr(settings, 'ACTIONS_ON_PAGE', 20)
DEV = getattr(settings, 'DEV', False)
DEV_OR_STAGING = DEV or getattr(settings, 'STAGING', False)

BILLING_CUTOFF = getattr(settings, 'BILLING_CUTOFF', None)


# Management
def index(request, my_teams=False):
    q = request.REQUEST.get('q')

    if my_teams and request.user.is_authenticated():
        ordering = 'name'
        qs = Team.objects.filter(members__user=request.user)
    else:
        ordering = request.GET.get('o', 'members')
        qs = Team.objects.for_user(request.user).annotate(_member_count=Count('users__pk'))

    if q:
        qs = qs.filter(Q(name__icontains=q)|Q(description__icontains=q))

    order_fields = {
        'name': 'name',
        'date': 'created',
        'members': '_member_count'
    }
    order_fields_name = {
        'name': _(u'Name'),
        'date': _(u'Newest'),
        'members': _(u'Most Members')
    }
    order_fields_type = {
        'name': 'asc',
        'date': 'desc',
        'members': 'desc'
    }
    order_type = request.GET.get('ot', order_fields_type.get(ordering, 'desc'))

    if ordering in order_fields and order_type in ['asc', 'desc']:
        qs = qs.order_by(('-' if order_type == 'desc' else '')+order_fields[ordering])

    highlighted_ids = list(Team.objects.for_user(request.user).filter(highlight=True).values_list('id', flat=True))
    random.shuffle(highlighted_ids)
    highlighted_qs = Team.objects.filter(pk__in=highlighted_ids[:HIGHTLIGHTED_TEAMS_ON_PAGE]) \
        .annotate(_member_count=Count('users__pk'))

    extra_context = {
        'my_teams': my_teams,
        'query': q,
        'ordering': ordering,
        'order_type': order_type,
        'order_name': order_fields_name.get(ordering, 'name'),
        'highlighted_qs': highlighted_qs,
    }
    return object_list(request, queryset=qs,
                       paginate_by=TEAMS_ON_PAGE,
                       template_name='teams/teams-list.html',
                       template_object_name='teams',
                       extra_context=extra_context)

@render_to('teams/create.html')
@staff_member_required
def create(request):
    user = request.user

    if not DEV and not (user.is_superuser and user.is_active):
        raise Http404

    if request.method == 'POST':
        form = CreateTeamForm(request.user, request.POST, request.FILES)
        if form.is_valid():
            team = form.save(user)
            messages.success(request, _("""
                Your team has been created. Here are some next steps:
                <ul>
                    <li><a href="%(edit)s">Edit team members' permissions</a></li>
                    <li><a href="%(activate)s">Activate and customize workflows for your team</a></li>
                    <li><a href="%(create)s">Create and customize projects</a></li>
                    <li><a href="%(lang)s">Edit language preferences</a></li>
                    <li><a href="%(custom)s">Customize instructions to caption makers and translators</a></li>
                </ul>
                """ % dict(
                    edit=reverse("teams:settings_permissions", kwargs={"slug": team.slug}),
                    activate=reverse("teams:settings_permissions", kwargs={"slug": team.slug}),
                    create=reverse("teams:settings_projects", kwargs={"slug": team.slug}),
                    lang=reverse("teams:settings_languages", kwargs={"slug": team.slug}),
                    custom=reverse("teams:settings_guidelines", kwargs={"slug": team.slug}),
                )))
            return redirect(reverse("teams:settings_basic", kwargs={"slug":team.slug}))
    else:
        form = CreateTeamForm(request.user)

    return { 'form': form }


# Settings
def _delete_team(request, team):
    if not can_delete_team(team, request.user):
        messages.error(request, _(u'You do not have permission to delete this team.'))
        return None

    team.deleted = True
    team.save()

    return HttpResponseRedirect(reverse('teams:index'))

@render_to('teams/settings.html')
@login_required
def settings_basic(request, slug):
    team = Team.get(slug, request.user)

    if not can_change_team_settings(team, request.user):
        messages.error(request, _(u'You do not have permission to edit this team.'))
        return HttpResponseRedirect(team.get_absolute_url())

    if request.POST.get('delete'):
        r = _delete_team(request, team)
        if r:
            return r

    if can_rename_team(team, request.user):
        FormClass = RenameableSettingsForm
    else:
        FormClass = SettingsForm

    if request.POST:
        form = FormClass(request.POST, request.FILES, instance=team)

        is_visible = team.is_visible

        if form.is_valid():
            try:
                form.save()
            except:
                logger.exception("Error on changing team settings")
                raise

            if is_visible != form.instance.is_visible:
                update_video_public_field.delay(team.id)
                invalidate_video_visibility_caches.delay(team)

            messages.success(request, _(u'Settings saved.'))
            return HttpResponseRedirect(request.path)
    else:
        form = FormClass(instance=team)

    return { 'team': team, 'form': form, }

@render_to('teams/settings-guidelines.html')
@login_required
def settings_guidelines(request, slug):
    team = Team.get(slug, request.user)

    if not can_change_team_settings(team, request.user):
        messages.error(request, _(u'You do not have permission to edit this team.'))
        return HttpResponseRedirect(team.get_absolute_url())

    initial = dict((s.key_name, s.data) for s in team.settings.messages_guidelines())
    if request.POST:
        form = GuidelinesMessagesForm(request.POST, initial=initial)

        if form.is_valid():
            for key, val in form.cleaned_data.items():
                setting, c = Setting.objects.get_or_create(team=team, key=Setting.KEY_IDS[key])
                setting.data = val
                setting.save()

            messages.success(request, _(u'Guidelines and messages updated.'))
            return HttpResponseRedirect(request.path)
    else:
        form = GuidelinesMessagesForm(initial=initial)

    return { 'team': team, 'form': form, }

@render_to('teams/settings-permissions.html')
@login_required
def settings_permissions(request, slug):
    team = Team.get(slug, request.user)
    workflow = Workflow.get_for_target(team.id, 'team')
    moderated = team.moderates_videos()

    if not can_change_team_settings(team, request.user):
        messages.error(request, _(u'You do not have permission to edit this team.'))
        return HttpResponseRedirect(team.get_absolute_url())

    if request.POST:
        form = PermissionsForm(request.POST, instance=team)
        workflow_form = WorkflowForm(request.POST, instance=workflow)

        if form.is_valid() and workflow_form.is_valid():
            form.save()

            if form.cleaned_data['workflow_enabled']:
                workflow_form.save()

            moderation_changed = moderated != form.instance.moderates_videos()

            if moderation_changed:
                update_video_moderation.delay(team)
                invalidate_video_moderation_caches.delay(team)

            messages.success(request, _(u'Settings saved.'))
            return HttpResponseRedirect(request.path)
    else:
        form = PermissionsForm(instance=team)
        workflow_form = WorkflowForm(instance=workflow)

    return { 'team': team, 'form': form, 'workflow_form': workflow_form, }

@render_to('teams/settings-projects.html')
@login_required
def settings_projects(request, slug):
    team = Team.get(slug, request.user)
    projects = team.project_set.exclude(name=Project.DEFAULT_NAME)

    if not can_change_team_settings(team, request.user):
        messages.error(request, _(u'You do not have permission to edit this team.'))
        return HttpResponseRedirect(team.get_absolute_url())

    return { 'team': team, 'projects': projects, }

def _set_languages(team, codes_preferred, codes_blacklisted):
    tlps = TeamLanguagePreference.objects.for_team(team)

    existing = set(tlp.language_code for tlp in tlps)

    desired_preferred = set(codes_preferred)
    desired_blacklisted = set(codes_blacklisted)
    desired = desired_preferred | desired_blacklisted

    # Figure out which languages need to be deleted/created/changed.
    to_delete = existing - desired

    to_create_preferred = desired_preferred - existing
    to_set_preferred = desired_preferred & existing

    to_create_blacklisted = desired_blacklisted - existing
    to_set_blacklisted = desired_blacklisted & existing

    # Delete unneeded prefs.
    for tlp in tlps.filter(language_code__in=to_delete):
        tlp.delete()

    # Change existing prefs.
    for tlp in tlps.filter(language_code__in=to_set_preferred):
        tlp.preferred, tlp.allow_reads, tlp.allow_writes = True, False, False
        tlp.save()

    for tlp in tlps.filter(language_code__in=to_set_blacklisted):
        tlp.preferred, tlp.allow_reads, tlp.allow_writes = False, False, False
        tlp.save()

    # Create remaining prefs.
    for lang in to_create_preferred:
        tlp = TeamLanguagePreference(team=team, language_code=lang,
                                     allow_reads=False, allow_writes=False,
                                     preferred=True)
        tlp.save()

    for lang in to_create_blacklisted:
        tlp = TeamLanguagePreference(team=team, language_code=lang,
                                     allow_reads=False, allow_writes=False,
                                     preferred=False)
        tlp.save()

@render_to('teams/settings-languages.html')
@login_required
def settings_languages(request, slug):
    team = Team.get(slug, request.user)

    if not can_change_team_settings(team, request.user):
        messages.error(request, _(u'You do not have permission to edit this team.'))
        return HttpResponseRedirect(team.get_absolute_url())

    preferred = [tlp.language_code for tlp in
                 TeamLanguagePreference.objects.for_team(team).filter(preferred=True)]
    blacklisted = [tlp.language_code for tlp in
                   TeamLanguagePreference.objects.for_team(team).filter(preferred=False)]
    initial = {'preferred': preferred, 'blacklisted': blacklisted}

    if request.POST:
        form = LanguagesForm(team, request.POST, initial=initial)

        if form.is_valid():
            _set_languages(team, form.cleaned_data['preferred'], form.cleaned_data['blacklisted'])

            messages.success(request, _(u'Settings saved.'))
            invalidate_video_caches.delay(team.pk)
            return HttpResponseRedirect(request.path)
    else:
        form = LanguagesForm(team, initial=initial)

    return { 'team': team, 'form': form }


def _default_project_for_team(team):
    """Get the default project to filter by for the videos/tasks lists
    """
    if team.slug == 'ted':
        # :( Logic for the TED team is hardcoded here
        try:
            return Project.objects.get(team=team, slug='tedtalks')
        except Project.DoesNotExist:
            logging.warning("_default_project_for_team: "
                    "tedtalks project does not exist")
            return None
    else:
        return None
# Videos
@timefn
@render_to('teams/videos-list.html')
def detail(request, slug, project_slug=None, languages=None):
    team = Team.get(slug, request.user)

    user = request.user if request.user.is_authenticated() else None
    try:
        member = team.members.get(user=user)
    except TeamMember.DoesNotExist:
        member = None

    filtered = 0

    if project_slug is None or project_slug == '':
        project_slug = request.GET.get('project')

    if project_slug:
        if project_slug == 'any':
            project = None
        else:
            try:
                project = Project.objects.get(team=team, slug=project_slug)
            except Project.DoesNotExist:
                project = None
    else:
        project = _default_project_for_team(team)

    query = request.GET.get('q')
    sort = request.GET.get('sort')
    language = request.GET.get('lang'
)
    if language or project_slug:
        filtered = filtered + 1

    if language != 'none':
        qs = team.get_videos_for_languages_haystack(
             language, user=request.user, project=project, query=query, sort=sort)
    else:
        qs = team.get_videos_for_languages_haystack(
             num_completed_langs=0, user=request.user, project=project, query=query, sort=sort)

    extra_context = widget.add_onsite_js_files({})

    extra_context['all_videos_count'] = team.get_videos_for_languages_haystack(
        None, user=request.user, project=None, query=None, sort=sort).count()

    extra_context.update({
        'team': team,
        'member': member,
        'project':project,
        'can_add_video': can_add_video(team, request.user, project),
        'can_edit_videos': can_add_video(team, request.user, project),
        'filtered': filtered
    })

    if extra_context['can_add_video'] or extra_context['can_edit_videos']:
        # Cheat and reduce the number of videos on the page if we're dealing with
        # someone who can edit videos in the team, for performance reasons.
        is_editor = True
        per_page = 8
    else:
        is_editor = False
        per_page = VIDEOS_ON_PAGE

    general_settings = {}
    add_general_settings(request, general_settings)
    extra_context['general_settings'] = json.dumps(general_settings)

    if team.video:
        extra_context['widget_params'] = base_widget_params(request, {
            'video_url': team.video.get_video_url(),
            'base_state': {}
        })

    readable_langs = TeamLanguagePreference.objects.get_readable(team)
    language_choices = [(code, name) for code, name in get_language_choices()
                        if code in readable_langs]

    extra_context['project_choices'] = team.project_set.exclude(name='_root')

    extra_context['language_choices'] = language_choices
    extra_context['query'] = query

    sort_names = {
        'name': 'Name, A-Z',
        '-name': 'Name, Z-A',
        'time': 'Time, Oldest',
        '-time': 'Time, Newest',
        'subs': 'Subtitles, Least',
        '-subs': 'Subtitles, Most',
    }
    if sort:
        extra_context['order_name'] = sort_names[sort]
    else:
        extra_context['order_name'] = sort_names['-time']

    extra_context['current_videos_count'] = qs.count()
    extra_context['filtered'] = filtered

    team_video_md_list, pagination_info = paginate(qs, per_page, request.GET.get('page'))
    extra_context.update(pagination_info)
    extra_context['team_video_md_list'] = team_video_md_list
    extra_context['team_workflows'] = list(
        Workflow.objects.filter(team=team.id)
                        .select_related('project', 'team', 'team_video'))

    if not filtered and not query:
        if project:
            is_indexing = project.videos_count != extra_context['current_videos_count']
        else:
            is_indexing = team.videos.all().count() != extra_context['current_videos_count']
        extra_context['is_indexing'] = is_indexing

    if is_editor:
        team_video_ids = [record.team_video_pk for record in team_video_md_list]
        team_videos = list(TeamVideo.objects.filter(id__in=team_video_ids).select_related('video', 'team', 'project'))
        team_videos = dict((tv.pk, tv) for tv in team_videos)
        for record in team_video_md_list:
            if record:
                record._team_video = team_videos.get(record.team_video_pk)
                if record._team_video:
                    record._team_video.original_language_code = record.original_language
                    record._team_video.completed_langs = record.video_completed_langs
    return extra_context

@render_to('teams/add_video.html')
@login_required
def add_video(request, slug):
    team = Team.get(slug, request.user)

    project_id = request.GET.get('project') or request.POST.get('project') or None
    project = Project.objects.get(team=team, pk=project_id) if project_id else team.default_project

    if request.POST and not can_add_video(team, request.user, project):
        messages.error(request, _(u"You can't add that video to this team/project."))
        return HttpResponseRedirect(team.get_absolute_url())

    initial = {
        'video_url': request.GET.get('url', ''),
        'title': request.GET.get('title', '')
    }

    if project:
        initial['project'] = project

    form = AddTeamVideoForm(team, request.user, request.POST or None, request.FILES or None, initial=initial)

    if form.is_valid():
        obj = form.save(False)
        obj.added_by = request.user
        obj.save()

        api_teamvideo_new.send(obj)
        video_changed_tasks.delay(obj.video.pk)
        messages.success(request, form.success_message())
        return redirect(team.get_absolute_url())

    return {
        'form': form,
        'team': team
    }

@login_required
def move_video(request):
    form = MoveTeamVideoForm(request.user, request.POST)

    if form.is_valid():
        team_video = form.cleaned_data['team_video']
        team = form.cleaned_data['team']
        project = form.cleaned_data['project']
        team_video.move_to(team, project)
        messages.success(request, _(u'The video has been moved to the new team.'))
    else:
        for e in flatten_errorlists(form.errors):
            messages.error(request, e)

    return HttpResponseRedirect(request.POST.get('next', '/'))

@render_to('teams/add_videos.html')
@login_required
def add_videos(request, slug):
    team = Team.get(slug, request.user)

    if not can_add_video(team, request.user):
        messages.error(request, _(u"You can't add videos to this team/project."))
        return HttpResponseRedirect(team.get_absolute_url())

    form = AddTeamVideosFromFeedForm(team, request.user, request.POST or None)

    if form.is_valid():
        form.save()
        messages.success(request, form.success_message())
        return redirect(team.get_absolute_url())

    return { 'form': form, 'team': team, }

@login_required
@render_to('teams/team_video.html')
def team_video(request, team_video_pk):
    team_video = get_object_or_404(TeamVideo, pk=team_video_pk)

    if not can_edit_video(team_video, request.user):
        messages.error(request, _(u'You can\'t edit this video.'))
        return HttpResponseRedirect(team_video.team.get_absolute_url())

    meta = team_video.video.metadata()
    form = EditTeamVideoForm(request.POST or None, request.FILES or None,
                             instance=team_video, user=request.user, initial=meta)

    if form.is_valid():
        form.save()
        messages.success(request, _('Video has been updated.'))
        return redirect(team_video)

    context = widget.add_onsite_js_files({})

    context.update({
        'team': team_video.team,
        'team_video': team_video,
        'form': form,
        'user': request.user,
        'widget_params': base_widget_params(request, {'video_url': team_video.video.get_video_url(), 'base_state': {}})
    })
    return context

@render_to_json
@login_required
def remove_video(request, team_video_pk):
    def _error_resp(request, next, error):
        if request.is_ajax():
            return { 'success': False, 'error': error }
        else:
            messages.error(request, error)
            return HttpResponseRedirect(next)

    team_video = get_object_or_404(TeamVideo, pk=team_video_pk)

    if request.method != 'POST':
        return _error_resp(request, reverse('teams:user_teams'),
                           _(u'Request must be a POST request.'))

    next = request.POST.get('next', reverse('teams:user_teams'))
    wants_delete = request.POST.get('del-opt') == 'total-destruction'

    if wants_delete:
        if not can_delete_video(team_video, request.user):
            return _error_resp(request, next,
                               _(u"You can't delete that video."))
    else:
        if not can_remove_video(team_video, request.user):
            return _error_resp(request, next,
                               _(u"You can't remove that video."))

    for task in team_video.task_set.all():
        task.delete()

    video = team_video.video

    if wants_delete:
        # create the action handler before deleting the video, so that
        # it can grab the video's title
        Action.delete_video_handler(video, team_video.team, request.user)
        video.delete()
        msg = _(u'Video has been deleted from Amara.')
    else:
        team_video.delete()
        msg = _(u'Video has been removed from the team.')

    if request.is_ajax():
        return { 'success': True }
    else:
        messages.success(request, msg)
        return HttpResponseRedirect(next)

@timefn
@render_to('teams/activity.html')
def activity(request, slug):
    team = Team.get(slug, request.user)

    user = request.user if request.user.is_authenticated() else None
    try:
        member = team.members.get(user=user)
    except TeamMember.DoesNotExist:
        member = None

    public_only = False if member else True

    # This section is here to work around MySQL's poor decisions.
    #
    # Much like the Tasks page, this query performs extremely poorly when run
    # normally.  So we split it into two parts here so that each will run fast.
    action_ids = Action.objects.for_team(team, public_only=public_only, ids=True)
    action_ids, pagination_info = paginate(action_ids, ACTIONS_ON_PAGE,
                                           request.GET.get('page'))
    action_ids = list(action_ids)

    activity_list = list(Action.objects.filter(id__in=action_ids).select_related(
            'video', 'user', 'language', 'language__video'
    ).order_by())
    activity_list.sort(key=lambda a: action_ids.index(a.pk))

    context = {
        'activity_list': activity_list,
        'team': team,
        'member': member
    }
    context.update(pagination_info)

    return context


# Members
@timefn
@render_to('teams/members-list.html')
def detail_members(request, slug, role=None):
    q = request.REQUEST.get('q')
    lang = request.GET.get('lang')
    sort = request.GET.get('sort', 'joined')
    filtered = False

    team = Team.get(slug, request.user)

    user = request.user if request.user.is_authenticated() else None
    try:
        member = team.members.get(user=user)
    except TeamMember.DoesNotExist:
        member = None

    qs = team.members.select_related('user').filter(user__is_active=True)

    if q:
        filtered = True
        for term in filter(None, [term.strip() for term in q.split()]):
            qs = qs.filter(Q(user__first_name__icontains=term)
                         | Q(user__last_name__icontains=term)
                         | Q(user__email__icontains=term)
                         | Q(user__username__icontains=term)
                         | Q(user__biography__icontains=term))

    if lang:
        filtered = True
        qs = qs.filter(user__userlanguage__language=lang)

    if role:
        filtered = True
        if role == 'admin':
            qs = qs.filter(role__in=[TeamMember.ROLE_OWNER, TeamMember.ROLE_ADMIN])
        else:
            qs = qs.filter(role=role)

    if sort == 'joined':
        qs = qs.order_by('created')
    elif sort == '-joined':
        qs = qs.order_by('-created')

    extra_context = widget.add_onsite_js_files({})
    extra_context['filtered'] = filtered

    team_member_list, pagination_info = paginate(qs, MEMBERS_ON_PAGE, request.GET.get('page'))
    extra_context.update(pagination_info)
    extra_context['team_member_list'] = team_member_list

    # if we are a member that can also edit roles, we create a dict of
    # roles that we can assign, this will vary from user to user, since
    # let's say an admin can change roles, but not for anyone above him
    # the owner, for example
    assignable_roles = []
    if roles_user_can_assign(team, request.user):
        for member in team_member_list:
            if can_assign_role(team, request.user, member.role, member.user):
                assignable_roles.append(member)

    users = team.members.values_list('user', flat=True)
    user_langs = set(UserLanguage.objects.filter(user__in=users).values_list('language', flat=True))

    extra_context.update({
        'team': team,
        'member': member,
        'query': q,
        'role': role,
        'assignable_roles': assignable_roles,
        'languages': sorted(languages_with_labels(user_langs).items(), key=lambda pair: pair[1]),
    })

    if team.video:
        extra_context['widget_params'] = base_widget_params(request, {
            'video_url': team.video.get_video_url(),
            'base_state': {}
        })

    return extra_context

@login_required
def remove_member(request, slug, user_pk):
    team = Team.get(slug, request.user)

    member = get_object_or_404(TeamMember, team=team, user__pk=user_pk)

    return_path = reverse('teams:detail_members', args=[], kwargs={'slug': slug})

    if can_assign_role(team, request.user, member.role, member.user):
        user = member.user
        if not user == request.user:
            [application.on_member_removed(author=request.user, interface='web UI') for application in \
             team.applications.filter(user=user, status=Application.STATUS_APPROVED)]
            TeamMember.objects.filter(team=team, user=user).delete()
            messages.success(request, _(u'Member has been removed from the team.'))
            return HttpResponseRedirect(return_path)
        else:
            messages.error(request, _(u'Use the "Leave this team" button to remove yourself from this team.'))
            return HttpResponseRedirect(return_path)
    else:
        messages.error(request, _(u'You don\'t have permission to remove this member from the team.'))
        return HttpResponseRedirect(return_path)

@login_required
def applications(request, slug):
    team = Team.get(slug, request.user)

    if not team.is_member(request.user):
        return  HttpResponseForbidden("Not allowed")

    # default to showing only applications that need to be acted upon
    status = int(request.GET.get('status', Application.STATUS_PENDING))
    qs = team.applications.filter(status=status)

    extra_context = {
        'team': team
    }
    return object_list(request, queryset=qs,
                       paginate_by=APLICATIONS_ON_PAGE,
                       template_name='teams/applications.html',
                       template_object_name='applications',
                       extra_context=extra_context)

@login_required
def approve_application(request, slug, application_pk):
    team = Team.get(slug, request.user)

    if not team.is_member(request.user):
        raise Http404

    if can_invite(team, request.user):
        application = team.applications.get(pk=application_pk)
        try:
            application.approve(request.user, "web UI")
            messages.success(request, _(u'Application approved.'))
        except Application.DoesNotExist:
            messages.error(request, _(u'Application does not exist.'))
        except ApplicationInvalidException:
            messages.error(request, _(u'Application already processed.'))
    else:
        messages.error(request, _(u'You can\'t approve applications.'))

    return redirect('teams:applications', team.pk)

@login_required
def deny_application(request, slug, application_pk):
    team = Team.get(slug, request.user)

    if not team.is_member(request.user):
        raise Http404

    if can_invite(team, request.user):
        application = team.applications.get(pk=application_pk)
        try:
            application.deny(request.user, "web UI")
            messages.success(request, _(u'Application denied.'))
        except Application.DoesNotExist:
            messages.error(request, _(u'Application does not exist.'))
        except ApplicationInvalidException:
            messages.error(request, _(u'Application already processed.'))
    else:
        messages.error(request, _(u'You can\'t deny applications.'))

    return redirect('teams:applications', team.pk)

@render_to('teams/invite_members.html')
@login_required
def invite_members(request, slug):
    team = Team.get(slug, request.user)

    if not can_invite(team, request.user):
        return HttpResponseForbidden(_(u'You cannot invite people to this team.'))
    if request.POST:
        form = InviteForm(team, request.user, request.POST)
        if form.is_valid():
            # the form will fire the notifications for invitees
            # this cannot be done on model signal, since you might be
            # sending invites twice for the same user, and that borks
            # the naive signal for only created invitations
            form.save()
            return HttpResponseRedirect(reverse('teams:detail_members',
                                                args=[], kwargs={'slug': team.slug}))
    else:
        form = InviteForm(team, request.user)

    return {
        'team': team,
        'form': form,
    }

@login_required
def accept_invite(request, invite_pk, accept=True):
    invite = get_object_or_404(Invite, pk=invite_pk, user=request.user)
    try:
        if accept:
            invite.accept()
            return redirect(reverse("teams:detail", kwargs={"slug": invite.team.slug}))
        else:
            invite.deny()
            return redirect(request.META.get('HTTP_REFERER', '/'))
    except InviteExpiredException:
        return HttpResponseServerError(render_to_response("generic-error.html", {
            "error_msg": _("This invite is no longer valid"),
        }, RequestContext(request)))

@login_required
def join_team(request, slug):
    team = get_object_or_404(Team, slug=slug)
    user = request.user

    if not can_join_team(team, user):
        messages.error(request, _(u'You cannot join this team.'))
    else:
        member = TeamMember(team=team, user=user, role=TeamMember.ROLE_CONTRIBUTOR)
        member.save()
        messages.success(request, _(u'You are now a member of this team.'))
        notifier.team_member_new.delay(member.pk)
    return redirect(team)

def _check_can_leave(team, user):
    """Return an error message if the member cannot leave the team, otherwise None."""

    try:
        member = TeamMember.objects.get(team=team, user=user)
    except TeamMember.DoesNotExist:
        return u'You are not a member of this team.'

    if not team.members.exclude(pk=member.pk).exists():
        return u'You are the last member of this team.'

    is_last_owner = (
        member.role == TeamMember.ROLE_OWNER
        and not team.members.filter(role=TeamMember.ROLE_OWNER).exclude(pk=member.pk).exists()
    )
    if is_last_owner:
        return u'You are the last owner of this team.'

    is_last_admin = (
        member.role == TeamMember.ROLE_ADMIN
        and not team.members.filter(role=TeamMember.ROLE_ADMIN).exclude(pk=member.pk).exists()
        and not team.members.filter(role=TeamMember.ROLE_OWNER).exists()
    )
    if is_last_admin:
        return u'You are the last admin of this team.'

    return None

@login_required
def leave_team(request, slug):
    team = get_object_or_404(Team, slug=slug)
    user = request.user

    error = _check_can_leave(team, user)
    if error:
        messages.error(request, _(error))
    else:
        member = TeamMember.objects.get(team=team, user=user)
        tm_user_pk = member.user.pk
        team_pk = member.team.pk
        member.delete()
        [application.on_member_leave(request.user, "web UI") for application in \
         member.team.applications.filter(status=Application.STATUS_APPROVED)]
            
        notifier.team_member_leave(team_pk, tm_user_pk)

        messages.success(request, _(u'You have left this team.'))
    return redirect(request.META.get('HTTP_REFERER') or team)

@permission_required('teams.change_team')
def highlight(request, slug, highlight=True):
    item = get_object_or_404(Team, slug=slug)
    item.highlight = highlight
    item.save()
    return redirect(request.META.get('HTTP_REFERER', '/'))

def _member_search_result(member, team, task_id, team_video_id, task_type, task_lang):
    result = [member.user.id, u'%s (%s)' % (member.user, member.user.username)]

    if task_id:
        task = Task.objects.not_deleted().get(team=team, pk=task_id)
        if member.has_max_tasks():
            result += [False]
        else:
            result += [can_perform_task(member.user, task)]
    elif team_video_id:
        team_video = TeamVideo.objects.get(pk=team_video_id)
        if member.has_max_tasks():
            result += [False]
        else:
            result += [can_perform_task_for(member.user, task_type, team_video, task_lang)]
    else:
        result += [None]

    return result

@render_to_json
def search_members(request, slug):
    team = Team.get(slug, request.user)
    q = request.GET.get('term', '').replace('(', '').replace(')', '')
    terms = get_terms(q)

    task_id = request.GET.get('task')
    task_type = request.GET.get('task_type')
    task_lang = request.GET.get('task_lang')
    team_video_id = request.GET.get('team_video')

    members = team.members.filter(user__is_active=True)
    for term in terms:
        members = members.filter(
            Q(user__username__icontains=term) |
            Q(user__first_name__icontains=term) |
            Q(user__last_name__icontains=term)
        )
    members = members.select_related('user')[:MAX_MEMBER_SEARCH_RESULTS]

    results = [_member_search_result(m, team, task_id, team_video_id, task_type, task_lang)
               for m in members]

    return { 'results': results }

def role_saved(request, slug):
    messages.success(request, _(u'Member saved.'))
    return_path = reverse('teams:detail_members', args=[], kwargs={'slug': slug})
    return HttpResponseRedirect(return_path)


# Tasks
def _get_or_create_workflow(team_slug, project_id, team_video_id):
    try:
        workflow = Workflow.objects.get(team__slug=team_slug, project=project_id,
                                        team_video=team_video_id)
    except Workflow.DoesNotExist:
        # We special case this because Django won't let us create new models
        # with the IDs, we need to actually pass in the Model objects for
        # the ForeignKey fields.
        #
        # Most of the time we won't need to do these three extra queries.

        team = Team.objects.get(slug=team_slug)
        project = Project.objects.get(pk=project_id) if project_id else None
        team_video = TeamVideo.objects.get(pk=team_video_id) if team_video_id else None

        workflow = Workflow(team=team, project=project, team_video=team_video)

    return workflow

def _task_languages(team, user):
    languages = filter(None, Task.objects.filter(team=team, deleted=False)
                                         .values_list('language', flat=True)
                                         .distinct())

    language_labels = dict(get_language_choices(with_empty=True))

    # TODO: Handle the team language setting here once team settings are
    # implemented.
    languages = list(set(languages))
    lang_data = []
    for l in languages:
        if language_labels.get(l):
            lang_data.append({'code': l, 'name': language_labels[l]} )
        else:
            logger.error("Failed to find language code for task", extra={
                "data": {
                    "language_code": l,
                    "supported": language_labels
                }
            })
    return lang_data

def _tasks_list(request, team, project, filters, user):
    '''List tasks for the given team, optionally filtered.

    `filters` should be an object/dict with zero or more of the following keys:

    * type: a string describing the type of task. 'Subtitle', 'Translate', etc.
    * completed: true or false
    * assignee: user ID as an integer
    * team_video: team video ID as an integer

    '''
    tasks = Task.objects.filter(team=team.id, deleted=False)

    if project:
        tasks = tasks.filter(team_video__project = project)

    if filters.get('team_video'):
        tasks = tasks.filter(team_video=filters['team_video'])

    if filters.get('completed'):
        tasks = tasks.filter(completed__isnull=False)
    else:
        tasks = tasks.filter(completed=None)

    if filters.get('language'):
        if filters['language'] != 'all':
            tasks = tasks.filter(language=filters['language'])
    elif request.user.is_authenticated() and request.user.get_languages():
        languages = [ul.language for ul in request.user.get_languages()] + ['']
        tasks = tasks.filter(language__in=languages)

    if filters.get('q'):
        terms = get_terms(filters['q'])
        for term in terms:
            tasks = tasks.filter(team_video__video__title__icontains=term)

    if filters.get('type'):
        tasks = tasks.filter(type=Task.TYPE_IDS[filters['type']])

    if filters.get('assignee'):
        assignee = filters.get('assignee')

        if assignee == 'me':
            tasks = tasks.filter(assignee=user)
        elif assignee == 'none':
            tasks = tasks.filter(assignee=None)
        elif assignee and assignee.isdigit():
            tasks = tasks.filter(assignee=int(assignee))
        elif assignee and assignee != 'anyone':
            tasks = tasks.filter(assignee=User.objects.get(username=assignee))
    else:
        tasks = tasks.filter(assignee=None)

    return tasks

def _order_tasks(request, tasks):
    sort = request.GET.get('sort', '-created')
    # Most teams won't use priorities. For those who do, that should be
    # the default sorting.
    order_clause = ["-priority"]
    if sort == 'created':
        order_clause.append('created')
    elif sort == '-created':
        order_clause.append('-created')
    elif sort == 'expires':
        tasks = tasks.exclude(expiration_date=None)
        order_clause.append('expiration_date')
    elif sort == '-expires':
        tasks = tasks.exclude(expiration_date=None)
        order_clause.append('-expiration_date')
    tasks = tasks.order_by(*order_clause)
    return tasks

def _get_task_filters(request):
    return { 'language': request.GET.get('lang'),
             'type': request.GET.get('type'),
             'team_video': request.GET.get('team_video'),
             'assignee': request.GET.get('assignee'),
             'q': request.GET.get('q'), }

def _cache_video_url(tasks):
    team_video_pks = [t.team_video_id for t in tasks]
    video_pks = [t.team_video.video_id for t in tasks]

    video_urls = dict([(vu.video_id, vu.effective_url) for vu in
                       VideoUrl.objects.filter(video__in=video_pks, primary=True)])

    for t in tasks:
        t.cached_video_url = video_urls.get(t.team_video.video_id)

@timefn
@render_to('teams/dashboard.html')
def dashboard(request, slug):

    team = Team.get(slug, request.user)
    user = request.user if request.user.is_authenticated() else None
    try:
        member = team.members.get(user=user)
    except TeamMember.DoesNotExist:
        member = None

    if user:
        user_languages = set([ul for ul in user.get_languages()])
        user_filter = {'assignee':str(user.id),'language':'all'}
        user_tasks = _tasks_list(request, team, None, user_filter, user).order_by('expiration_date')[0:14]
        user_tasks = user_tasks.select_related('team_video')
        _cache_video_url(user_tasks)
    else:
        user_languages = None
        user_tasks = None

    filters = {'assignee': 'none'}

    widget_settings = {}
    from apps.widget.rpc import add_general_settings
    add_general_settings(request, widget_settings)
    
    videos = []

    if member and team.workflow_enabled:

        # TED's dashboard should only show TEDTalks tasks
        # http://i.imgur.com/fjjqx.gif
        if team.slug == 'ted':
            project = Project.objects.get(team=team, slug='tedtalks')
        else:
            project = None

        if not user_languages:
            user_languages = get_user_languages_from_request(request)
            filters['language'] = user_languages[0]

        tasks = _order_tasks(request,
                             _tasks_list(request, team,
                                         project, filters,
                                         user))
        tasks = tasks.select_related('team_video', 'team_video__team',
                                     'team_video__project', 'team_video__video')
        
        for task in chunkediter(tasks, 100):
            if not can_perform_task(user, task):
                continue

            task_vid = task.team_video

            if not task_vid in videos:
                task_vid.tasks = []
                videos.append(task_vid)

            vid_index = videos.index(task_vid)
            videos[vid_index].tasks.append(task)

            if len(videos) >= VIDEOS_ON_PAGE:
                break
        
        for video in videos:
            _cache_video_url(video.tasks)
    else:
        team_videos = team.videos.select_related("teamvideo").order_by("-teamvideo__created")
        # TED's dashboard should only show TEDTalks videos
        # http://i.imgur.com/fjjqx.gif
        if team.slug == 'ted':
            project = Project.objects.get(team=team, slug='tedtalks')
            team_videos = team_videos.filter(teamvideo__project=project)

        team_videos = team_videos[0:VIDEOS_ON_PAGE]

        if not user_languages:
            for tv in team_videos:
                videos.append(tv.teamvideo) 
        else:
            lang_list = [l.language for l in user_languages]

            for video in team_videos.all():
                subtitled_languages = (video.subtitlelanguage_set
                                                 .filter(language__in=lang_list)
                                                 .filter(is_complete=True)
                                                 .values_list("language", flat=True))
                if len(subtitled_languages) != len(user_languages):
                    tv = video.teamvideo
                    tv.languages = [l for l in user_languages if l.language not in subtitled_languages]
                    videos.append(tv)

    context = {
        'team': team,
        'member': member,
        'user_tasks': user_tasks,
        'videos': videos,
        'can_add_video': can_add_video(team, request.user),
        'widget_settings': widget_settings
    }
    
    return context

@timefn
@render_to('teams/tasks.html')
def team_tasks(request, slug, project_slug=None):
    team = Team.get(slug, request.user)

    if not can_view_tasks_tab(team, request.user):
        messages.error(request, _("You cannot view this team's tasks."))
        return HttpResponseRedirect(team.get_absolute_url())

    if not project_slug:
        project_slug = request.GET.get('project')

    user = request.user if request.user.is_authenticated() else None
    member = team.members.get(user=user) if user else None
    languages = _task_languages(team, request.user)
    languages = sorted(languages, key=lambda l: l['name'])
    filters = _get_task_filters(request)
    filtered = 0

    if project_slug != '' and project_slug != None:
        if project_slug == 'any':
            project = None
        else:
            try:
                project = Project.objects.get(team=team, slug=project_slug)
            except Project.DoesNotExist:
                project = None
    else:
        # User didn't specify a project to filter on.  We use the default
        # project only if:
        #   - There was no team_video specified
        #   - The user isn't looking at their own tasks
        if (filters.get('team_video') is None and
                filters.get('assignee') != 'me'):
            project = _default_project_for_team(team)
        else:
            project = None

    tasks = _order_tasks(request,
                         _tasks_list(request, team, project, filters, user))
    tasks, pagination_info = paginate(tasks, TASKS_ON_PAGE, request.GET.get('page'))

    # We pull out the task IDs here for performance.  It's ugly, I know.
    #
    # MySQL doesn't use the ideal indexes when you try to filter and
    # select_related all the various stuff, but if you split the process into
    # two queries they'll both be fast.
    #
    # Thanks, MySQL.
    task_ids = list(tasks.values_list('id', flat=True))
    tasks = list(Task.objects.filter(id__in=task_ids).select_related(
            'team_video__video',
            'team_video__team',
            'team_video__project',
            'assignee',
            'team',
            'subtitle_version__language__standard_language',
            'subtitle_version__user'))
    tasks.sort(key=lambda t: task_ids.index(t.pk))

    if filters.get('team_video'):
        filters['team_video'] = TeamVideo.objects.get(pk=filters['team_video'])

    if filters.get('assignee'):
        if filters['assignee'] == 'me':
            filters['assignee'] = team.members.get(user=request.user)
        elif filters['assignee'] == 'none':
            filters['assignee'] == None
        elif filters['assignee'].isdigit():
            filters['assignee'] = team.members.get(user=filters['assignee'])
        elif filters['assignee'] != 'anyone':
            filters['assignee'] = team.members.get(user=User.objects.get(username=filters['assignee']))

        filtered = filtered + 1

    if filters.get('language'):
        filtered = filtered + 1

    if filters.get('type'):
        filtered = filtered + 1

    if project_slug is not None:
        filtered = filtered + 1

    widget_settings = {}
    from apps.widget.rpc import add_general_settings
    add_general_settings(request, widget_settings)

    team_video_pks = [t.team_video_id for t in tasks]
    video_pks = Video.objects.filter(teamvideo__in=team_video_pks).values_list('id', flat=True)

    video_urls = dict([(vu.video_id, vu.effective_url) for vu in
                       VideoUrl.objects.filter(video__in=video_pks, primary=True)])

    for t in tasks:
        t.cached_video_url = video_urls.get(t.team_video.video_id)

    context = {
        'team': team,
        'project': project, # TODO: Review
        'user_can_delete_tasks': can_delete_tasks(team, request.user),
        'user_can_assign_tasks': can_assign_tasks(team, request.user),
        'assign_form': TaskAssignForm(team, member),
        'languages': languages,
        'tasks': tasks,
        'filters': filters,
        'widget_settings': widget_settings,
        'filtered': filtered,
        'member': member,
        'upload_draft_form': UploadDraftForm(user=request.user),
        'project_choices': team.project_set.exclude(name='_root'),
    }

    context.update(pagination_info)

    return context

@render_to('teams/create_task.html')
def create_task(request, slug, team_video_pk):
    team = get_object_or_404(Team, slug=slug)
    team_video = get_object_or_404(TeamVideo, pk=team_video_pk, team=team)
    can_assign = can_assign_tasks(team, request.user, team_video.project)

    if request.POST:
        form = TaskCreateForm(request.user, team, team_video, request.POST)

        if form.is_valid():
            task = form.save(commit=False)

            task.team = team
            task.team_video = team_video

            task.set_expiration()

            if task.type == Task.TYPE_IDS['Subtitle']:
                task.language = ''

            # TODO: Remove this?
            if task.type in [Task.TYPE_IDS['Review'], Task.TYPE_IDS['Approve']]:
                task.approved = Task.APPROVED_IDS['In Progress']
                task.subtitle_version = task.team_video.video.latest_version(language_code=task.language)

            task.save()
            notifier.team_task_assigned.delay(task.pk)
            return HttpResponseRedirect(reverse('teams:team_tasks', args=[],
                                                kwargs={'slug': team.slug}))
    else:
        form = TaskCreateForm(request.user, team, team_video)

    subtitlable = json.dumps(can_create_task_subtitle(team_video, request.user))
    translatable_languages = json.dumps(can_create_task_translate(team_video, request.user))

    language_choices = json.dumps(get_language_choices(True))

    return { 'form': form, 'team': team, 'team_video': team_video,
             'translatable_languages': translatable_languages,
             'language_choices': language_choices,
             'subtitlable': subtitlable,
             'can_assign': can_assign, }

@login_required
def perform_task(request, slug=None, task_pk=None):
    task_pk = task_pk or request.POST.get('task_id')

    task = get_object_or_404(Task, pk=task_pk)

    if slug:
        team = get_object_or_404(Team,slug=slug)
        if task.team != team:
            return HttpResponseForbidden(_(u'You are not allowed to perform this task.'))

    if not can_perform_task(request.user, task):
        return HttpResponseForbidden(_(u'You are not allowed to perform this task.'))

    task.assignee = request.user
    task.save()

    # ... perform task ...
    return HttpResponseRedirect(task.get_perform_url())

def _delete_subtitle_version(version):
    sl = version.language
    n = version.version_no

    # Delete this specific version...
    version.delete()

    # We also want to delete all draft subs leading up to this version.
    for v in sl.subtitleversion_set.filter(version_no__lt=n).order_by('-version_no'):
        if v.is_public:
            break
        v.delete()

    # And if we've deleted everything in the language, we can delete the language as well.
    if not sl.subtitleversion_set.exists():
        sl.delete()

def delete_task(request, slug):
    '''Mark a task as deleted.

    The task will not be physically deleted from the database, but will be
    flagged and won't appear in further task listings.

    '''
    team = get_object_or_404(Team, slug=slug)
    next = request.POST.get('next', reverse('teams:team_tasks', args=[], kwargs={'slug': slug}))

    form = TaskDeleteForm(team, request.user, data=request.POST)
    if form.is_valid():
        task = form.cleaned_data['task']
        video = task.team_video.video
        task.deleted = True

        if task.subtitle_version:
            if form.cleaned_data['discard_subs']:
                _delete_subtitle_version(task.subtitle_version)
                task.subtitle_version = None

            if task.get_type_display() in ['Review', 'Approve']:
                # TODO: Handle subtitle/translate tasks here too?
                if not form.cleaned_data['discard_subs'] and task.subtitle_version:
                    task.subtitle_version.moderation_status = MODERATION.APPROVED
                    task.subtitle_version.save()
                    metadata_manager.update_metadata(video.pk)

        task.save()

        messages.success(request, _('Task deleted.'))
    else:
        messages.error(request, _('You cannot delete this task.'))

    return HttpResponseRedirect(next)

def assign_task(request, slug):
    '''Assign a task to the given user, or unassign it if null/None.'''
    team = get_object_or_404(Team, slug=slug)
    next = request.POST.get('next', reverse('teams:team_tasks', args=[], kwargs={'slug': slug}))

    form = TaskAssignForm(team, request.user, data=request.POST)
    if form.is_valid():
        task = form.cleaned_data['task']
        assignee = form.cleaned_data['assignee']

        if task.assignee == request.user:
            was_mine = True
        else:
            was_mine = False

        task.assignee = assignee
        task.set_expiration()
        task.save()
        notifier.team_task_assigned.delay(task.pk)

        if task.assignee is None and was_mine:
            messages.success(request, _('Task declined.'))
        else:
            messages.success(request, _('Task assigned.'))
    else:
        messages.error(request, _('You cannot assign this task.'))

    return HttpResponseRedirect(next)

@render_to_json
@login_required
def assign_task_ajax(request, slug):
    '''Assign a task to the given user, or unassign it if null/None.'''
    team = get_object_or_404(Team, slug=slug)

    form = TaskAssignForm(team, request.user, data=request.POST)
    if form.is_valid():
        task = form.cleaned_data['task']
        assignee = form.cleaned_data['assignee']

        if not assignee:
            return HttpResponseForbidden(u'Invalid assignment attempt - assignee is empty (%s).' % assignee)

        if task.assignee == assignee:
            return { 'success': True }

        task.assignee = assignee
        task.set_expiration()

        task.save()
        notifier.team_task_assigned.delay(task.pk)

        return { 'success': True }
    else:
        return HttpResponseForbidden(u'Invalid assignment attempt.')

@login_required
@transaction.commit_manually
def upload_draft(request, slug):

    if request.POST:
        form = UploadDraftForm(request.user, request.POST, request.FILES)

        try:
            is_valid = form.is_valid()
        except Exception, e:
            client.create_from_exception()
            messages.error(u"Sorry, there was a problem while uploading your draft. Care to try again?")
            transaction.rollback()
            is_valid = False

        if is_valid:
            try:
                form.save()
            except Exception, e:
                messages.error(request, unicode(e))
                transaction.rollback()
                client.create_from_exception()
            else:
                messages.success(request, _(u"Draft uploaded successfully."))
                transaction.commit()
        else:
            for key, value in form.errors.items():
                messages.error(request, _('/n'.join([force_unicode(i) for i in value])))

            transaction.rollback()

        if transaction.is_dirty():
            transaction.rollback()

        return HttpResponseRedirect(reverse('teams:team_tasks', args=[], kwargs={'slug': slug}))
    else:
        return HttpResponseBadRequest()

# copied a lot of those from widget/views.py:download_subtitles
# we need to make them share some code. for sure.
def download_draft(request, slug, task_pk, type="srt"):
    task = Task.objects.get(pk=task_pk)
    team = get_object_or_404(Team,slug=slug)

    if task.team != team:
        return HttpResponseForbidden(_(u'You are not allowed to download this transcript.'))

    if type not in GenerateSubtitlesHandler:
        raise Http404

    subtitle_version = task.get_subtitle_version()

    subtitle = GenerateSubtitlesHandler[type].create(subtitle_version)
    response = HttpResponse(unicode(subtitle), mimetype="text/plain")
    original_filename = '%s.%s' % (subtitle_version.video.lang_filename(task.language), subtitle.file_type)

    if not 'HTTP_USER_AGENT' in request.META or u'WebKit' in request.META['HTTP_USER_AGENT']:
        # Safari 3.0 and Chrome 2.0 accepts UTF-8 encoded string directly.
        filename_header = 'filename=%s' % original_filename.encode('utf-8')
    elif u'MSIE' in request.META['HTTP_USER_AGENT']:
        try:
            original_filename.encode('ascii')
        except UnicodeEncodeError:
            original_filename = 'subtitles.' + subtitle.file_type

        filename_header = 'filename=%s' % original_filename
    else:
        # For others like Firefox, we follow RFC2231 (encoding extension in HTTP headers).
        filename_header = 'filename*=UTF-8\'\'%s' % iri_to_uri(original_filename.encode('utf-8'))

    response['Content-Disposition'] = 'attachment; ' + filename_header

    return response


# Projects
def project_list(request, slug):
    team = get_object_or_404(Team, slug=slug)
    projects = Project.objects.for_team(team)
    return render_to_response("teams/project_list.html", {
        "team":team,
        "projects": projects
    }, RequestContext(request))

@render_to('teams/settings-projects-add.html')
@login_required
def add_project(request, slug):
    team = Team.get(slug, request.user)

    if request.POST:
        form = ProjectForm(request.POST)
        workflow_form = WorkflowForm(request.POST)

        if form.is_valid() and workflow_form.is_valid():

            if team.project_set.filter(slug=pan_slugify(form.cleaned_data['name'])).exists():
                messages.error(request, _(u"There's already a project with this name"))
            else:
                project = form.save(commit=False)
                project.team = team
                project.save()

                if project.workflow_enabled:
                    workflow = workflow_form.save(commit=False)
                    workflow.team = team
                    workflow.project = project
                    workflow.save()

                messages.success(request, _(u'Project added.'))
                return HttpResponseRedirect(
                        reverse('teams:settings_projects', args=[], kwargs={'slug': slug}))
    else:
        form = ProjectForm()
        workflow_form = WorkflowForm()

    return { 'team': team, 'form': form, 'workflow_form': workflow_form, }

@render_to('teams/settings-projects-edit.html')
@login_required
def edit_project(request, slug, project_slug):
    team = Team.get(slug, request.user)
    project = Project.objects.get(slug=project_slug, team=team)
    project_list_url = reverse('teams:settings_projects', args=[], kwargs={'slug': slug})

    if project.is_default_project:
        messages.error(request, _(u'You cannot edit that project.'))
        return HttpResponseRedirect(project_list_url)

    try:
        workflow = Workflow.objects.get(team=team, project=project)
    except Workflow.DoesNotExist:
        workflow = None

    if request.POST:
        if request.POST.get('delete', None) == 'Delete':
            project.delete()
            messages.success(request, _(u'Project deleted.'))
            return HttpResponseRedirect(project_list_url)
        else:
            form = ProjectForm(request.POST, instance=project)
            workflow_form = WorkflowForm(request.POST, instance=workflow)

            # if the project doesn't have workflow enabled, the workflow form
            # is going to fail to validate (workflow is None)
            # there's probably a better way of doing this...
            if form.is_valid() and workflow_form.is_valid if project.workflow_enabled else form.is_valid():
                form.save()

                if project.workflow_enabled:
                    workflow = workflow_form.save(commit=False)
                    workflow.team = team
                    workflow.project = project
                    workflow.save()

                messages.success(request, _(u'Project saved.'))
                return HttpResponseRedirect(project_list_url)

    else:
        form = ProjectForm(instance=project)
        workflow_form = WorkflowForm(instance=workflow)

    return { 'team': team, 'project': project, 'form': form, 'workflow_form': workflow_form, }

@render_to('teams/_third-party-accounts.html')
@login_required
def third_party_accounts(request, slug):
    from accountlinker.views import _generate_youtube_oauth_request_link
    team = get_object_or_404(Team, slug=slug)
    if not can_change_team_settings(team, request.user):
        messages.error(request, _(u'You do not have permission to edit this team.'))
        return HttpResponseRedirect(team.get_absolute_url())

    new_youtube_url = _generate_youtube_oauth_request_link(
            json.dumps({'team': team.pk}))
    linked_accounts = team.third_party_accounts.all()
    return {
        "team":team,
        "new_youtube_url": new_youtube_url,
        "linked_accounts": linked_accounts,
    }

@login_required
def sync_third_party_account(request, slug, account_id):
    team = get_object_or_404(Team, slug=slug)
    if not can_change_team_settings(team, request.user):
        messages.error(request, _(u'You do not have permission to edit this team.'))
        return HttpResponseRedirect(team.get_absolute_url())

    team.third_party_accounts.get(pk=account_id)
    for video in team.videos.all():
        version = video.latest_version()
        if version is not None:
            ThirdPartyAccount.objects.mirror_on_third_party(
                    version.video, version.language, UPDATE_VERSION_ACTION,
                    version)
    messages.success(request, _(u'Successfully synced subtitles.'))
    return HttpResponseRedirect(reverse('teams:third-party-accounts',
        kwargs={'slug': team.slug}))


# Unpublishing
def _create_task_after_unpublishing(subtitle_version):
    team_video = subtitle_version.language.video.get_team_video()
    lang = subtitle_version.language.language

    # If there's already an open task for this language we don't need another.
    open_task_exists = team_video.task_set.incomplete().filter(language=lang).exists()

    if open_task_exists:
        return None

    workflow = Workflow.get_for_team_video(team_video)
    if workflow.approve_allowed:
        type = Task.TYPE_IDS['Approve']
        can_do = can_approve
    else:
        type = Task.TYPE_IDS['Review']
        can_do = can_review

    # Try to guess the appropriate assignee by looking at the last task.
    last_task = (team_video.task_set.complete().filter(language=lang, type=type)
                                               .order_by('-completed')
                                               [:1])
    assignee = None
    if last_task:
        candidate = last_task[0].assignee
        if candidate and can_do(team_video, candidate, lang):
            assignee = candidate

    task = Task(team=team_video.team, team_video=team_video,
                assignee=assignee, language=lang, type=type,
                subtitle_version=subtitle_version)
    task.set_expiration()
    task.save()

    return task

def _propagate_unpublish_to_external_services(language_pk, language_code, video):
    """Push the 'unpublishing' of subs to third-party providers for the given language.

    The unpublishing must be fully complete before this function is called.

    """
    try:
        language = SubtitleLanguage.objects.get(pk=language_pk)
    except SubtitleLanguage.DoesNotExist:
        delete_captions_in_original_service_by_code.delay(language_code, video.pk)
        return

    # Find the latest public version to determine what kind of third-party call
    # we need to make.
    latest_version = language.latest_version(public_only=True)

    if latest_version:
        # There's a latest version that's still public, so third-party services
        # should use that one.
        upload_subtitles_to_original_service.delay(latest_version.pk)
    else:
        # There's no latest version that's still public, but we know the
        # language still exists.
        #
        # This means that all of the subs in the language have been unpublished
        # and are awaiting moderation.
        #
        # In this case we should delete the subs from the external service
        # entirely, since we know that all the subs we have are bad.
        delete_captions_in_original_service.delay(language_pk)

def _propagate_unpublish_to_tasks(team_video, language_pk, language_code):
    """Push the 'unpublishing' of a language to any tasks applying to it.

    The unpublishing must be fully complete before this function is called.

    """
    try:
        language = SubtitleLanguage.objects.get(pk=language_pk)
        if language and language.latest_version(public_only=False):
            # Don't kill any tasks if there are still versions remaining.
            return
    except SubtitleLanguage.DoesNotExist:
        pass

    tasks_to_delete = team_video.task_set.not_deleted()

    # If there is still no original language left, we can just delete all the
    # tasks for this video because someone deleted everything.
    #
    # If there *is* an original language left, we just delete tasks for the
    # languages that were unpublished.
    if team_video.video.subtitle_language():
        tasks_to_delete = tasks_to_delete.filter(language=language_code)

    tasks_to_delete.update(deleted=True)

def unpublish(request, slug):
    team = get_object_or_404(Team, slug=slug)

    form = UnpublishForm(request.user, team, request.POST)
    if not form.is_valid():
        messages.error(request, _(u'Invalid unpublishing request.\nErrors:\n') + '\n'.join(flatten_errorlists(form.errors)))
        return HttpResponseRedirect(request.POST.get('next', team.get_absolute_url()))

    version = form.cleaned_data['subtitle_version']
    team_video = version.language.video.get_team_video()
    video = version.language.video
    scope = form.cleaned_data['scope']
    should_delete = form.cleaned_data['should_delete']
    language = version.language

    results = []
    if scope == 'version':
        results.append([version.language.pk, version.language.language,
                        version.unpublish(delete=should_delete)])
    elif scope == 'dependents':
        translations = list(SubtitleLanguage.objects.filter(video=language.video,
                                                            standard_language=language,
                                                            is_forked=False))
        for l in [language] + translations:
            results.append([l.pk, l.language,
                            l.unpublish(delete=should_delete)])
    else:
        assert False, 'Invalid scope.'

    for language_pk, language_code, version_for_task in results:
        _propagate_unpublish_to_external_services(language_pk, language_code, video)
        _propagate_unpublish_to_tasks(team_video, language_pk, language_code)

        if version_for_task:
            _create_task_after_unpublishing(version_for_task)

    metadata_manager.update_metadata(team_video.video.pk)
    update_one_team_video(team_video.pk)

    messages.success(request, _(u'Successfully unpublished subtitles.'))
    api_subtitles_rejected.send(version)
    return HttpResponseRedirect(request.POST.get('next', team.get_absolute_url()))

@login_required
def auto_captions_status(request, slug):
    """
    Prints a simple table of partner status for captions, this should
    should be used internally (as a cvs file with tab delimiters)
    """
    buffer = []
    team = get_object_or_404(Team, slug=slug)
    if not team.is_member(request.user):
        return  HttpResponseForbidden("Not allowed")
    buffer.append( "Video\tproject\tURL\tstatus\tjob_id\ttask_id\tcreated on\tcompleted on")
    for tv in team.teamvideo_set.all().select_related("job", "project", "video"):
        jobs = tv.job_set.all()
        extra = ""
        if jobs.exists():
            j = jobs[0]
            extra = "%s\t%s\t%s\t%s\t%s" % (j.status, j.job_id, j.task_id, j.created_on, j.completed_on)
        url = "%s://%s%s" % (DEFAULT_PROTOCOL, Site.objects.get_current().domain, tv.video.get_absolute_url())
        buffer.append( "Video:%s\t %s\t%s\t %s" % (tv.video.title,tv.project.name, url, extra))
    response =  HttpResponse( "\n".join(buffer), content_type="text/csv")
    response['Content-Disposition'] = 'filename=team-status.csv'
    return response


# Billing
@staff_member_required
def billing(request):
    user = request.user

    if not DEV and not (user.is_superuser and user.is_active):
        raise Http404

    if request.method == 'POST':
        form = ChooseTeamForm(request.POST)
        if form.is_valid():
            team = form.cleaned_data.get('team')
            start_date = form.cleaned_data.get('start_date')
            end_date = form.cleaned_data.get('end_date')

            report = BillingReport.objects.create(team=team,
                    start_date=start_date, end_date=end_date)

            process_billing_report.delay(report.pk)

    else:
        form = ChooseTeamForm()

    reports = BillingReport.objects.all().order_by('-pk')

    return render_to_response('teams/billing/choose.html', {
        'form': form,
        'reports': reports,
        'cutoff': BILLING_CUTOFF
    }, RequestContext(request))
