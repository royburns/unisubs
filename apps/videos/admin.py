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

from django.contrib import admin
from videos.models import (
    Video, SubtitleLanguage, SubtitleVersion, VideoFeed, VideoMetadata,
    VideoUrl, SubtitleVersionMetadata, Action, Subtitle
)
from videos.tasks import (
    video_changed_tasks, upload_subtitles_to_original_service
)

from django.core.urlresolvers import reverse
from utils.celery_search_index import update_search_index


class VideoUrlInline(admin.StackedInline):
    model = VideoUrl
    raw_id_fields = ['added_by']
    extra = 0

class VideoAdmin(admin.ModelAdmin):
    actions = None
    list_display = ['__unicode__', 'video_thumbnail', 'languages', 'languages_count', 'is_subtitled']
    search_fields = ['video_id', 'title', 'videourl__url', 'user__username']
    readonly_fields = ['subtitles_fetched_count', 'widget_views_count', 'view_count']
    raw_id_fields = ['user', 'moderated_by']
    inlines = [VideoUrlInline]

    def video_thumbnail(self, obj):
        return '<img width="80" height="60" src="%s"/>' % obj.get_small_thumbnail()

    video_thumbnail.allow_tags = True
    video_thumbnail.short_description = 'Thumbnail'

    def languages(self, obj):
        lang_qs = obj.subtitlelanguage_set.all()
        link_tpl = '<a href="%s">%s</a>'
        links = []
        for item in lang_qs:
            url = reverse('admin:videos_subtitlelanguage_change', args=[item.pk])
            links.append(link_tpl % (url, item.language or '[undefined]'))
        return ', '.join(links)

    languages.allow_tags = True

    def save_model(self, request, obj, form, change):
        obj.save()
        update_search_index.delay(obj.__class__, obj.pk)

class VideoMetadataAdmin(admin.ModelAdmin):
    list_display = ['video', 'key', 'data']
    list_filter = ['key', 'created', 'modified']
    search_fields = ['video__video_id', 'video__title', 'video__user__username',
                     'data']
    raw_id_fields = ['video']

class SubtitleVersionInline(admin.TabularInline):
    model = SubtitleVersion
    fields = ['version_no']
    max_num = 0
    extra = 0


class SubtitleLanguageAdmin(admin.ModelAdmin):
    # We specifically pull video out into a property to force one query per
    # row.  This sounds like a bad idea, but:
    #
    # 1. MySQL was performing a full table scan when using the select_related()
    #    for some reason.
    # 2. It's only 20 extra queries, so it's not the end of the world.
    list_display = ['video_title', 'is_original', 'language', 'is_complete',
                    'had_version', 'subtitle_count', 'new_subtitle_language']
    list_filter = ['is_original', 'is_complete']
    search_fields = ['video__title', 'video__video_id', 'language']
    raw_id_fields = ['video']
    inlines = [SubtitleVersionInline]
    list_per_page = 20
    actions = None

    def video_title(self, obj):
        return unicode(obj.video)
    video_title.short_description = 'video'

    def delete_model(self, request, obj):
        video = obj.video
        super(SubtitleLanguageAdmin, self).delete_model(request, obj)
        video_changed_tasks.delay(video.pk)

    def versions(self, obj):
        version_qs = obj.subtitleversion_set.all()
        link_tpl = '<a href="%s">#%s</a>'
        links = []
        for item in version_qs:
            url = reverse('admin:videos_subtitleversion_change', args=[item.pk])
            links.append(link_tpl % (url, item.version_no))
        return ', '.join(links)

    versions.allow_tags = True

    def save_model(self, request, obj, form, change):
        should_sync_to_youtube = False

        if change:
            old_obj = SubtitleLanguage.objects.get(pk=obj.pk)

            if not old_obj.is_complete and obj.is_complete:
                should_sync_to_youtube = True

        super(SubtitleLanguageAdmin, self).save_model(request, obj, form,
                change)

        if should_sync_to_youtube:
            latest = obj.latest_version()
            upload_subtitles_to_original_service.delay(latest.pk)


class SubtitleVersionAdmin(admin.ModelAdmin):
    # We specifically pull language out into a property to force one query per
    # row.  This sounds like a bad idea, but:
    #
    # 1. MySQL was performing a full table scan when using the select_related()
    #    for some reason.
    # 2. It's only 20 extra queries, so it's not the end of the world.
    list_display = ['video', 'language_title', 'version_no', 'note',
                    'timeline_changes', 'text_changes', 'datetime_started',
                    'moderation_status', 'origin']
    list_filter = []
    raw_id_fields = ['language', 'user', 'forked_from']
    search_fields = ['language__video__title', 'language__video__video_id',
                     'language__language']
    list_per_page = 20

    def has_delete_permission(self, request, obj=None):
        return False

    def language_title(self, obj):
        return unicode(obj.language)
    language_title.short_description = 'language'
    language_title.admin_order_field = 'language__language'

    def video(self, obj):
        if obj.language.video:
            return obj.language.video.title
        else:
            return None

    def timeline_changes(self, obj):
        if obj.time_change:
            return '%s %%' % int(obj.time_change * 100)
        return "0 %"

    def text_changes(self, obj):
        if obj.text_change:
            return '%s %%' % int(obj.text_change * 100)
        return "0 %"

    def origin(self, obj):
        return obj.get_workflow_origin()

class SubtitleVersionMetadataAdmin(admin.ModelAdmin):
    list_display = ['video', 'subtitle_version', 'key']
    list_filter = ['key', 'created', 'modified']
    raw_id_fields = ['subtitle_version']
    search_fields = ['subtitle_version__language__video__video_id',
                     'subtitle_version__language__video__title']

    def video(self, obj):
        return obj.subtitle_version.language.video.title

class SubtitleAdmin(admin.ModelAdmin):
    list_display = ['version', 'subtitle_id', 'subtitle_order', 'subtitle_text', 'start_time', 'end_time']

class VideoFeedAdmin(admin.ModelAdmin):
    list_display = ['url', 'last_link', 'created', 'user']
    raw_id_fields = ['user']

#admin.site.register(Subtitle, SubtitleAdmin)
admin.site.register(SubtitleVersion, SubtitleVersionAdmin)
admin.site.register(Subtitle)
admin.site.register(SubtitleVersionMetadata, SubtitleVersionMetadataAdmin)
admin.site.register(Video, VideoAdmin)
admin.site.register(VideoMetadata, VideoMetadataAdmin)
admin.site.register(SubtitleLanguage, SubtitleLanguageAdmin)
admin.site.register(VideoFeed, VideoFeedAdmin)

#Fix Celery tasks display
from djcelery.models import TaskState
from djcelery.admin import TaskMonitor
from django import forms

class TaskStateForm(forms.ModelForm):
    traceback_display = forms.CharField(required=False, label=u'Traceback')

    class Meta:
        model = TaskState


class FixedTaskMonitor(TaskMonitor):
    form = TaskStateForm
    fieldsets = (
        (None, {
            "fields": ("state", "task_id", "name", "args", "kwargs",
                       "eta", "runtime", "worker", "tstamp"),
            "classes": ("extrapretty", ),
        }),
        ("Details", {
            "classes": ("collapse", "extrapretty"),
            "fields": ("result", "traceback_display", "expires"),
        })
    )

    readonly_fields = ("state", "task_id", "name", "args", "kwargs",
                       "eta", "runtime", "worker", "result", "traceback_display",
                       "expires", "tstamp")

    def traceback_display(self, obj):
        return '<pre>%s</pre>' % obj.traceback

    traceback_display.allow_tags = True
    traceback_display.short_description = 'Traceback'


class ActionAdmin(admin.ModelAdmin):
    list_display = ('video', 'language', 'user', 'team', 'action_type',
        'created')

    class Meta:
        model = Action

admin.site.unregister(TaskState)
admin.site.register(TaskState, FixedTaskMonitor)
admin.site.register(Action, ActionAdmin)
