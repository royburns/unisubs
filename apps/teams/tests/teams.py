# -*- coding: utf-8 -*-

import os, re, json
from datetime import datetime, timedelta, date

from django.core import mail
from django.core.urlresolvers import reverse
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from django.db.models import ObjectDoesNotExist, Q
from django.test import TestCase

from auth.models import CustomUser as User
from apps.teams import tasks
from apps.teams import moderation_const as MODERATION
from apps.teams.forms import InviteForm
from apps.teams.permissions import add_role
from apps.teams.tests.teamstestsutils import refresh_obj, reset_solr
from apps.teams.models import (
    Team, Invite, TeamVideo, Application, TeamMember,
    TeamLanguagePreference, Project, Partner, TeamNotificationSetting
)
from apps.teams.templatetags import teams_tags
from apps.videos.search_indexes import VideoIndex
from apps.videos import metadata_manager
from apps.videos.models import Video, SubtitleLanguage, SubtitleVersion
from messages.models import Message
from widget.tests import create_two_sub_session, RequestMockup

from utils.test_utils import TestCaseMessagesMixin
from haystack.query import SearchQuerySet

LANGUAGE_RE = re.compile(r"S_([a-zA-Z\-]+)")


def fix_teams_roles(teams=None):
    for t in teams or Team.objects.all():
       for member in t.members.all():
           add_role(t, member.user,  t.members.all()[0], member.role)


class TestNotification(TestCase):

    fixtures = ["test.json"]

    def setUp(self):
        fix_teams_roles()
        self.team = Team(name='test', slug='test')
        self.team.save()

        self.user = User.objects.all()[:1].get()
        self.user.is_active = True
        self.user.notify_by_email = True
        self.user.email = 'test@test.com'
        self.user.save()

        self.tm = TeamMember(team=self.team, user=self.user)
        self.tm.save()

        v1 = Video.objects.all()[:1].get()
        self.tv1 = TeamVideo(team=self.team, video=v1, added_by=self.user)
        self.tv1.save()

        v2 = Video.objects.exclude(pk=v1.pk)[:1].get()
        self.tv2 = TeamVideo(team=self.team, video=v2, added_by=self.user)
        self.tv2.save()

    def test_new_team_video_notification(self):
        #check initial data
        self.assertEqual(self.team.teamvideo_set.count(), 2)
        self.assertEqual(self.team.users.count(), 1)


        #mockup for send_templated_email to test context of email
        import utils

        send_templated_email = utils.send_templated_email

        def send_templated_email_mockup(to, subject, body_template, body_dict, *args, **kwargs):
            send_templated_email_mockup.context = body_dict
            send_templated_email(to, subject, body_template, body_dict, *args, **kwargs)

        utils.send_templated_email = send_templated_email_mockup
        reload(tasks)

        #test notification about two new videos
        TeamVideo.objects.filter(pk__in=[self.tv1.pk, self.tv2.pk]).update(created=datetime.today())
        self.assertEqual(TeamVideo.objects.filter(created__gt=self.team.last_notification_time).count(), 2)
        mail.outbox = []
        self.user.notify_by_email = True
        self.user.save()
        tasks.add_videos_notification.delay()
        self.team = Team.objects.get(pk=self.team.pk)
        self.assertEqual(len(mail.outbox), 2)

        self.assertIn(self.user.email, mail.outbox[0].to[0] )
        self.assertEqual(len(send_templated_email_mockup.context['team_videos']), 2)

        self.user.notify_by_email = False
        self.user.save()
        #test if user turn off notification
        self.user.is_active = False
        self.user.save()
        mail.outbox = []
        tasks.add_videos_notification.delay()
        self.team = Team.objects.get(pk=self.team.pk)
        self.assertEqual(len(mail.outbox), 0)

        self.user.is_active = True
        self.user.notify_by_email = False
        self.user.save()
        mail.outbox = []
        tasks.add_videos_notification.delay()
        self.team = Team.objects.get(pk=self.team.pk)
        self.assertEqual(len(mail.outbox), 0)


        self.tm.save()

        self.user.notify_by_email = True
        self.user.save()
        #test notification if one video is new
        created_date = self.team.last_notification_time + timedelta(seconds=10)
        TeamVideo.objects.filter(pk=self.tv1.pk).update(created=created_date)

        self.assertEqual(TeamVideo.objects.filter(created__gt=self.team.last_notification_time).count(), 1)
        mail.outbox = []
        tasks.add_videos_notification.delay()
        self.team = Team.objects.get(pk=self.team.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(send_templated_email_mockup.context['team_videos']), 1)
        self.assertEqual(send_templated_email_mockup.context['team_videos'][0], self.tv1)

        #test notification if all videos are already old
        created_date = self.team.last_notification_time - timedelta(seconds=10)
        TeamVideo.objects.filter(team=self.team).update(created=created_date)
        self.assertEqual(TeamVideo.objects.filter(created__gt=self.team.last_notification_time).count(), 0)
        mail.outbox = []
        tasks.add_videos_notification.delay()
        self.team = Team.objects.get(pk=self.team.pk)
        self.assertEqual(len(mail.outbox), 0)

    def test_notify_lookup(self):
        p = Partner.objects.create(name='p', slug='p')
        t1 = Team.objects.create(name='t1', slug='t1', partner=p)
        t2 = Team.objects.create(name='t2', slug='t2')
        t3 = Team.objects.create(name='t3', slug='t3')
        t4 = Team.objects.create(name='t4', slug='t4')

        TeamNotificationSetting.objects.create(partner=p)
        TeamNotificationSetting.objects.create(team=t2)
        TeamNotificationSetting.objects.create(team=t4)

        TeamNotificationSetting.objects.notify_team(t1.pk, 'x')
        TeamNotificationSetting.objects.notify_team(t2.pk, 'x')
        TeamNotificationSetting.objects.notify_team(t3.pk, 'x')


class TestTasks(TestCase):

    fixtures = ["staging_users.json", "staging_videos.json", "staging_teams.json"]

    def setUp(self):
        self.tv = TeamVideo.objects.all()[0]
        self.sl = SubtitleLanguage.objects.exclude(language='')[0]
        self.team = Team.objects.all()[0]
        tv = TeamVideo(team=self.team, video=self.sl.video, added_by=self.team.users.all()[:1].get())
        tv.save()


class TeamVideoTest(TestCase):

    fixtures = ["staging_users.json", "staging_videos.json", "staging_teams.json"]

    def setUp(self):
        self.auth = {
            "username": u"admin",
            "password": u"admin"
        }

        self.user = User.objects.get(username=self.auth["username"])
        self.team = Team.objects.get(id=1)


        tm = TeamMember.objects.get(user=self.user, team=self.team)
        tm.role = TeamMember.ROLE_ADMIN
        tm.save()

        reset_solr()

    def _get_team_videos(self):
        return SearchQuerySet().models(TeamVideo).filter(owned_by_team_id=self.team.pk)

    def _search_for_video(self, team_video):
        qs = VideoIndex.public().filter(title=team_video.video_title_exact)

        if not qs:
            return False

        for video in qs:
            if video.video_id == team_video.video_id:
                return True

        return False

    def test_save_updates_is_visible(self):
        videos = self._get_team_videos()
        self.assertTrue(False not in [v.is_public for v in videos])

        self.client.login(**self.auth)

        url = reverse("teams:settings_basic", kwargs={"slug": self.team.slug})

        response = self.client.get(url)
        self.failUnlessEqual(response.status_code, 200)

        data = {
            "name": u"New team",
            "is_visible": u"0",
            "description": u"testing",
        }

        response = self.client.post(url, data, follow=True)
        self.failUnlessEqual(response.status_code, 200)
        self.assertFalse(Team.objects.get(id=1).is_visible)

        videos = self._get_team_videos()

        for video in videos:
            self.assertFalse(video.is_public)
            self.assertFalse(self._search_for_video(video))

        data['is_visible'] = u'1'

        response = self.client.post(url, data, follow=True)
        self.failUnlessEqual(response.status_code, 200)
        self.assertTrue(Team.objects.get(id=1).is_visible)

        videos = self._get_team_videos()

        for video in videos:
            self.assertTrue(video.is_public)
            self.assertTrue(self._search_for_video(video))

    def test_wrong_project_team_fails(self):
        video = Video.objects.filter(teamvideo__isnull=True)[0]
        project = Project.objects.create(slug="one-project", team=self.team)

        team_video = TeamVideo.objects.create(video=video, team=self.team, description="",
                                 added_by=self.user, project=project)

        self.assertTrue(team_video)

        team_video.project = Project.objects.filter(~Q(team=self.team))[0]

        self.assertNotEquals(team_video.project, project)
        self.assertNotEquals(team_video.project.team, self.team)

        try:
            team_video.save()
            self.fail("Assertion for team + project did not work")
        except AssertionError:
            pass


class TeamsTest(TestCase):

    fixtures = ["staging_users.json", "staging_videos.json", "staging_teams.json"]

    def setUp(self):
        fix_teams_roles()
        self.auth = {
            "username": u"admin",
            "password": u"admin"
        }
        self.user = User.objects.get(username=self.auth["username"])
        reset_solr()

    def _add_team_video(self, team, language, video_url):
        mail.outbox = []
        data = {
            "description": u"",
            "language": language,
            "video_url": video_url,
            "thumbnail": u"",
        }

        if team.has_projects:
            data['project'] = team.project_set.exclude(slug='_root')[0].pk
        else:
            data['project'] = team.default_project.pk

        old_count = TeamVideo.objects.count()
        old_video_count = Video.objects.count()

        url = reverse("teams:add_video", kwargs={"slug": team.slug})
        # the lowest permission level where one can add videos
        member = team.members.get(user=self.user)
        member.role = TeamMember.ROLE_MANAGER
        member.save()
        self.client.post(url, data)
        new_count = TeamVideo.objects.count()
        self.assertEqual(old_count+1, new_count)

        if Video.objects.count() > old_video_count:
            created_video = Video.objects.order_by('-created')[0]
            self.assertEqual(self.user, created_video.user)

    def _set_my_languages(self, *args):
        from auth.models import UserLanguage
        for ul in self.user.userlanguage_set.all():
            ul.delete()
        for lang in args:
            ul = UserLanguage(
                user=self.user,
                language=lang)
            ul.save()
        self.user = User.objects.get(id=self.user.id)

    def _create_new_team_video(self):
        self.client.login(**self.auth)
        response = self.client.get(reverse("teams:create"))

        data = {
            "description": u"",
            "video_url": u"",
            "membership_policy": u"4",
            "video_policy": u"1",
            "logo": u"",
            "slug": u"new-team",
            "name": u"New team",

        }

        response = self.client.post(reverse("teams:create"), data)
        self.assertEqual(response.status_code, 302)

        team = Team.objects.get(slug=data['slug'])

        self._add_team_video(team, u'en', u"http://videos.mozilla.org/firefox/3.5/switch/switch.ogv")

        tv = TeamVideo.objects.order_by('-id')[0]

        result = tasks.update_one_team_video.delay(tv.id)

        if result.failed():
            self.fail(result.traceback)

        return team, tv

    def _make_data(self, video_id, lang):

        return {
            'language': lang,
            'video': video_id,
            'subtitles': open(os.path.join(settings.PROJECT_ROOT, "apps", 'videos', 'fixtures' ,'test.srt'))
            }

    def _tv_search_record_list(self, team):
        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url)
        return response.context['team_video_md_list']

    def _complete_search_record_list(self, team):
        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url)
        return response.context['team_video_md_list']

    def test_team_join_leave(self):
        team = Team.objects.get(pk=1)
        join_url = reverse('teams:join_team', args=[team.slug])
        leave_url = reverse('teams:leave_team', args=[team.slug])

        self.client.login(**self.auth)

        #---------------------------------------
        self.assertTrue(team.is_open())
        TeamMember.objects.filter(team=team, user=self.user).delete()
        self.assertFalse(team.is_member(self.user))
        response = self.client.get(join_url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(team.is_member(self.user))

        #---------------------------------------
        response = self.client.get(leave_url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(team.is_member(self.user))

        #---------------------------------------
        team.membership_policy = Team.INVITATION_BY_MANAGER
        team.save()
        self.assertFalse(team.is_open())
        response = self.client.get(join_url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(team.is_member(self.user))

    def test_add_video(self):
        self.client.login(**self.auth)

        team = Team.objects.get(pk=1)
        TeamMember.objects.get_or_create(user=self.user, team=team)

        self.assertTrue(team.users.count() > 1)

        for tm in team.members.all():
            tm.notify_by_email = True
            tm.save()
            tm.user.is_active = True
            tm.user.notify_by_email = True
            tm.user.save()

        self._add_team_video(team, u'en', u"http://videos.mozilla.org/firefox/3.5/switch/switch.ogv")

    def test_team_video_delete(self):
        #this test can fail only on MySQL
        team = Team.objects.get(pk=1)
        tv = team.teamvideo_set.exclude(video__subtitlelanguage__language='')[:1].get()
        # create a few languages with subs
        from videos.tests import create_langs_and_versions
        video = tv.video
        video.is_public = False
        video.moderated_by = team
        video.save()
        langs = ["en" ,"es", 'fr', 'pt-br']
        versions = create_langs_and_versions(video, langs)
        for v in versions:
            v.moderation_status = MODERATION.WAITING_MODERATION
        tv.delete()
        try:
            TeamVideo.objects.get(pk=tv.pk)
            self.fail()
        except TeamVideo.DoesNotExist:
            pass
        video = refresh_obj(video)
        for lang in langs:
            l = video.subtitle_language(lang)
            self.assertTrue(l.version())
            self.assertTrue(l.has_version)
        self.assertTrue(video.is_public)
        self.assertEqual(video.moderated_by, None)

    def test_complete_contents(self):
        request = RequestMockup(User.objects.all()[0])
        create_two_sub_session(request, completed=True)

        team, new_team_video = self._create_new_team_video()
        en = new_team_video.video.subtitle_language()
        en.is_complete = True
        en.save()
        video = Video.objects.get(id=en.video.id)
        self.assertEqual(True, video.is_complete)

        # We have to update the metadata here to make sure the video is marked
        # as complete for Solr.
        metadata_manager.update_metadata(video.pk)

        reset_solr()

        search_record_list = self._complete_search_record_list(team)
        self.assertEqual(1, len(search_record_list))
        search_record = search_record_list[0]
        self.assertEqual(1, len(search_record.video_completed_langs))
        self.assertEqual('en', search_record.video_completed_langs[0])

    def test_detail_contents_after_edit(self):
        # make sure edits show up in search result from solr
        self.client.login(**self.auth)
        team = Team.objects.get(pk=1)
        tv = team.teamvideo_set.get(pk=1)
        tv.title = ''
        tv.description = ''
        tv.save()
        data = {
            "languages-MAX_NUM_FORMS": u"",
            "languages-INITIAL_FORMS": u"0",
            "title": u"change title",
            "languages-0-language": u"el",
            "languages-0-id": u"",
            "languages-TOTAL_FORMS": u"1",
            "languages-0-completed": u"on",
            "thumbnail": u"",
            "description": u"and descriptionnn",
            "project": team.default_project.pk,

        }
        url = reverse("teams:team_video", kwargs={"team_video_pk": tv.pk})
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse("teams:team_video", kwargs={"team_video_pk": tv.pk}))

        reset_solr()

        tv = team.teamvideo_set.get(pk=1)
        team_video_search_records = self._tv_search_record_list(team)

        team_video = [team_video for team_video in team_video_search_records if team_video.team_video_pk == tv.pk][0]
        self.assertEquals('and descriptionnn', team_video.description)

    def test_detail_contents_after_remove(self):
        # make sure removals show up in search result from solr
        self.client.login(**self.auth)
        team = Team.objects.get(pk=1)
        num_team_videos = len(self._tv_search_record_list(team))

        tv = team.teamvideo_set.get(pk=1)
        url = reverse("teams:remove_video", kwargs={"team_video_pk": tv.pk})
        self.client.post(url)

        self.assertEquals(num_team_videos - 1, len(self._tv_search_record_list(team)))

    def test_detail_contents(self):
        team, new_team_video = self._create_new_team_video()

        reset_solr()

        # The video should be in the list.
        record_list = self._tv_search_record_list(team)
        self.assertEqual(1, len(record_list))
        self.assertEqual(new_team_video.video.video_id, record_list[0].video_id)

    def test_detail_contents_original_subs(self):
        team, new_team_video = self._create_new_team_video()

        # upload some subs to the new video. make sure it still appears.
        data = self._make_data(new_team_video.video.id, 'en')
        response = self.client.post(reverse('videos:upload_subtitles'), data)

        reset_solr()

        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url)

        # The video should be in the list.
        self.assertEqual(1, len(response.context['team_video_md_list']))

        # but we should see no "no work" message
        self.assertTrue('allow_noone_language' not in response.context or \
                            not response.context['allow_noone_language'])

    def test_detail_contents_unrelated_video(self):
        from videos.models import SubtitleLanguage

        team, new_team_video = self._create_new_team_video()
        en = SubtitleLanguage(video=new_team_video.video, language='en')
        en.is_original = True
        en.is_complete = True
        en.save()
        self._set_my_languages('en', 'ru')
        # now add a Russian video with no subtitles.
        self._add_team_video(
            team, u'ru',
            u'http://upload.wikimedia.org/wikipedia/commons/6/61/CollateralMurder.ogv')

        reset_solr()

        team = Team.objects.get(id=team.id)

        self.assertEqual(2, team.teamvideo_set.count())

        # both videos should be in the list
        search_record_list = self._tv_search_record_list(team)
        self.assertEqual(2, len(search_record_list))

        # but the one with en subs should be second, since it was added earlier
        self.assertEqual('en', search_record_list[1].original_language)

    def test_one_tvl(self):
        team, new_team_video = self._create_new_team_video()
        reset_solr()
        self._set_my_languages('ko')
        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url)
        self.assertEqual(1, len(response.context['team_video_md_list']))

    def test_no_dupes_without_buttons(self):
        team, new_team_video = self._create_new_team_video()
        self._set_my_languages('ko', 'en')

        self.client.post(
            reverse('videos:upload_subtitles'),
            self._make_data(new_team_video.video.id, 'en'))

        self.client.post(
            reverse('videos:upload_subtitles'),
            self._make_data(new_team_video.video.id, 'es'))

        reset_solr()

        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url)
        self.assertEqual(1, len(response.context['team_video_md_list']))

    def test_team_create_with_video(self):
        self.client.login(**self.auth)

        response = self.client.get(reverse("teams:create"))
        self.failUnlessEqual(response.status_code, 200)

        data = {
            "description": u"",
            "video_url": u"http://www.youtube.com/watch?v=OFaWxcH6I9E",
            "membership_policy": u"4",
            "video_policy": u"1",
            "logo": u"",
            "slug": u"new-team-with-video",
            "name": u"New team with video"
        }
        response = self.client.post(reverse("teams:create"), data)
        self.failUnlessEqual(response.status_code, 302)
        team = Team.objects.get(slug=data['slug'])
        self.assertTrue(team.video)
        self.assertEqual(team.video.user, self.user)
        self.assertTrue(team.video.title)

    def test_all_views(self):
        self.client.login(**self.auth)

        team = Team(
           slug="new-team",
            membership_policy=4,
            video_policy =1,
           name="New-name")
        team.save()
        tm = TeamMember.objects.create_first_member(team, self.user)
        #------- create ----------

        data = {
            "description": u"",
            "video_url": u"",
            "membership_policy": u"4",
            "video_policy": u"1",
            "logo": u"",
            "slug": u"new-team",
            "name": u"New team"
        }

        #---------- index -------------
        response = self.client.get(reverse("teams:index"))
        self.failUnlessEqual(response.status_code, 200)

        response = self.client.get(reverse("teams:index"), {'q': 'vol'})
        self.failUnlessEqual(response.status_code, 200)

        data = {
            "q": u"vol",
            "o": u"date"
        }
        response = self.client.get(reverse("teams:index"), data)
        self.failUnlessEqual(response.status_code, 200)

        response = self.client.get(reverse("teams:index"), {'o': 'my'})
        self.failUnlessEqual(response.status_code, 200)

        #-------------- applications ----------------
        url = reverse("teams:applications", kwargs={"slug": team.slug})
        response = self.client.get(url)
        self.failUnlessEqual(response.status_code, 200)

        #------------ detail ---------------------
        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url)
        self.failUnlessEqual(response.status_code, 200)

        url = reverse("teams:detail", kwargs={"slug": team.pk})
        response = self.client.get(url)
        self.failUnlessEqual(response.status_code, 200)

        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url)
        self.failUnlessEqual(response.status_code, 200)

        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url, {'q': 'Lions'})
        self.failUnlessEqual(response.status_code, 200)

        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url, {'q': 'empty result'})
        self.failUnlessEqual(response.status_code, 200)

        #------------ detail members -------------

        url = reverse("teams:detail_members", kwargs={"slug": team.slug})
        response = self.client.get(url)
        self.failUnlessEqual(response.status_code, 200)

        url = reverse("teams:detail_members", kwargs={"slug": team.slug})
        response = self.client.get(url, {'q': 'test'})
        self.failUnlessEqual(response.status_code, 200)

        #-------------members activity ---------------
        #Deprecated
        #url = reverse("teams:members_actions", kwargs={"slug": team.slug})
        #response = self.client.get(url)
        #self.failUnlessEqual(response.status_code, 200)

        #------------- add video ----------------------
        self.client.login(**self.auth)

        data = {
            "languages-MAX_NUM_FORMS": u"",
            "description": u"",
            "language": u"en",
            "title": u"",
            "languages-0-language": u"be",
            "languages-0-id": u"",
            "languages-TOTAL_FORMS": u"1",
            "video_url": u"http://www.youtube.com/watch?v=Hhgfz0zPmH4&feature=grec_index",
            "thumbnail": u"",
            "languages-INITIAL_FORMS": u"0",
            "project":team.default_project.pk,
        }
        tv_len = team.teamvideo_set.count()
        url = reverse("teams:add_video", kwargs={"slug": team.slug})
        response = self.client.post(url, data)
        self.assertEqual(tv_len+1, team.teamvideo_set.count())
        self.assertRedirects(response, reverse("teams:dashboard", kwargs={"slug": team.slug}))

        #-------edit team video -----------------
        team = Team.objects.get(pk=1)
        tv = team.teamvideo_set.get(pk=1)
        tv.description = ''
        tv.save()
        data = {
            "languages-MAX_NUM_FORMS": u"",
            "languages-INITIAL_FORMS": u"0",
            "languages-0-language": u"el",
            "languages-0-id": u"",
            "languages-TOTAL_FORMS": u"1",
            "languages-0-completed": u"on",
            "thumbnail": u"",
            "description": u"and description",
            "author": u"Test Author",
            "creation_date": u"2011-01-01",
            "project": team.default_project.pk
        }
        url = reverse("teams:team_video", kwargs={"team_video_pk": tv.pk})
        response = self.client.post(url, data)
        self.assertRedirects(response, reverse("teams:team_video", kwargs={"team_video_pk": tv.pk}))
        tv = team.teamvideo_set.get(pk=1)
        self.assertEqual(tv.description, u"and description")
        meta = tv.video.metadata()
        self.assertEqual(meta.get('author'), 'Test Author')
        self.assertEqual(meta.get('creation_date'), date(2011, 1, 1))

        #-----------delete video -------------
        url = reverse("teams:remove_video", kwargs={"team_video_pk": tv.pk})
        response = self.client.post(url)
        self.failUnlessEqual(response.status_code, 302)
        try:
            team.teamvideo_set.get(pk=1)
            self.fail()
        except ObjectDoesNotExist:
            pass

        #----------inviting to team-----------
        user2 = User.objects.get(username="alerion")
        TeamMember.objects.filter(user=user2, team=team).delete()

        member = TeamMember.objects.get(user=self.user, team=team)
        member.role = TeamMember.ROLE_OWNER
        member.save()

        data = {
            "user_id": user2.id,
            "message": u"test message",
            "role": TeamMember.ROLE_CONTRIBUTOR,
        }
        user_mail_box_count = Message.objects.unread().filter(user=user2).count()
        invite_url = reverse("teams:invite_members", args=(), kwargs={'slug': team.slug})
        response = self.client.post(invite_url, data, follow=True)
        self.failUnlessEqual(response.status_code, 200)

        self.assertEqual(user_mail_box_count + 1,
                         Message.objects.unread().filter(user=user2).count())


        invite = Invite.objects.get(user__username=user2.username, team=team)
        self.assertEqual(invite.role, TeamMember.ROLE_CONTRIBUTOR)
        self.assertEqual(invite.note, u'test message')

        ct = ContentType.objects.get_for_model(Invite)
        Message.objects.filter(object_pk=invite.pk, content_type=ct, user=user2)

        members_count = team.members.count()

        self.client.login(username = user2.username, password ='alerion')
        url = reverse("teams:accept_invite", kwargs={"invite_pk": invite.pk})
        response = self.client.get(url)

        self.assertEqual(members_count+1, team.members.count())

        self.client.login(**self.auth)

        tm,c = TeamMember.objects.get_or_create(user=self.user, team=team)
        tm.role = TeamMember.ROLE_ADMIN
        tm.save()
        url = reverse("teams:remove_member", kwargs={"user_pk": user2.pk, "slug": team.slug})
        response = self.client.post(url)
        self.failUnlessEqual(response.status_code, 302)

        self.assertFalse(team.is_member(user2))

        url = reverse("teams:activity", kwargs={"slug": team.slug})
        response = self.client.post(url)
        self.failUnlessEqual(response.status_code, 200)

        self.client.login()
        TeamMember.objects.filter(user=self.user, team=team).delete()
        self.assertFalse(team.is_member(self.user))
        url = reverse("teams:join_team", kwargs={"slug": team.slug})
        response = self.client.post(url)
        self.failUnlessEqual(response.status_code, 302)
        self.assertTrue(team.is_member(self.user))

    def test_fixes(self):
        url = reverse("teams:detail", kwargs={"slug": 'slug-does-not-exist'})
        response = self.client.get(url)
        self.failUnlessEqual(response.status_code, 404)

    def test_is_visible(self):
        hidden  = Team(name='secret', slug='secret', is_visible=False)
        hidden.save()
        teams = Team.objects.all()
        url = reverse("teams:detail", kwargs={"slug":hidden.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        url = reverse("teams:index")

        response = self.client.get(url)
        teams = response.context['teams_list']
        self.assertTrue(len(teams) < 10)
        teams_pks = [t.pk for t in teams]
        print teams_pks, hidden.pk

        self.assertNotIn(hidden.pk, teams_pks)

    def test_search_with_utf8(self):
        team = Team.objects.get(pk=1)
        video = Video.objects.get(pk=4)

        self.assertTrue(video.get_team_video() is None)

        team_video, _ = TeamVideo.objects.get_or_create(video=video, team=team,
                                                        added_by=self.user)
        url = reverse("teams:detail", kwargs={"slug": team.slug})
        response = self.client.get(url + u"?q=Петух отжигает!!!")
        videos = response.context['team_video_md_list']

        self.assertEquals(len(videos), 1)

        video = videos[0]

        self.assertEquals(video.title, u'\u041f\u0435\u0442\u0443\u0445 \u043e\u0442\u0436\u0438\u0433\u0430\u0435\u0442!!!')


from apps.teams.rpc import TeamsApiClass
from utils.rpc import Error, Msg
from django.contrib.auth.models import AnonymousUser


class TestJqueryRpc(TestCase):

    def setUp(self):
        fix_teams_roles()
        self.team = Team(name='Test', slug='test')
        self.team.save()
        self.user = User.objects.all()[:1].get()
        self.rpc = TeamsApiClass()

    def test_promote_user(self):
        other_user = User.objects.exclude(pk=self.user.pk)[:1].get()
        user_tm = TeamMember(team=self.team, user=self.user)
        user_tm.save()
        other_user_tm = TeamMember(team=self.team, user=other_user)
        other_user_tm.save()

        self.assertEqual(other_user_tm.role, TeamMember.ROLE_CONTRIBUTOR)
        self.assertEqual(user_tm.role, TeamMember.ROLE_CONTRIBUTOR)

        response = self.rpc.promote_user(self.team.pk, other_user_tm.pk, TeamMember.ROLE_MANAGER, AnonymousUser())
        if not isinstance(response, Error):
            self.fail('Anonymouse user is not member of team')

        response = self.rpc.promote_user(self.team.pk, other_user_tm.pk, TeamMember.ROLE_MANAGER, self.user)
        if not isinstance(response, Error):
            self.fail('User should be manager')

        user_tm.role = TeamMember.ROLE_MANAGER
        user_tm.save()

        NEW_ROLE = TeamMember.ROLE_CONTRIBUTOR
        response = self.rpc.promote_user(self.team.pk, other_user_tm.pk, NEW_ROLE, self.user)
        self.assertTrue(isinstance(response, Msg))
        other_user_tm = refresh_obj(other_user_tm)
        self.assertEqual(other_user_tm.role, NEW_ROLE)

        response = self.rpc.promote_user(self.team.pk, user_tm.pk, TeamMember.ROLE_CONTRIBUTOR, self.user)
        if not isinstance(response, Error):
            self.fail('Can\'t promote yourself')

        response = self.rpc.promote_user(self.team.pk, other_user_tm.pk, 'undefined role 123456', self.user)
        if not isinstance(response, Error):
            self.fail('Incorrect role')

        response = self.rpc.promote_user(self.team.pk, 123456, TeamMember.ROLE_MANAGER, self.user)
        if not isinstance(response, Error):
            self.fail('Undefined team member')

        undefined_team_pk = 123456
        self.assertFalse(Team.objects.filter(pk=undefined_team_pk))
        response = self.rpc.promote_user(undefined_team_pk, other_user_tm.pk, TeamMember.ROLE_MANAGER, self.user)
        if not isinstance(response, Error):
            self.fail('Undefined team')

    def test_create_application(self):
        response = self.rpc.create_application(self.team.pk, 'Note', AnonymousUser())
        if not isinstance(response, Error):
            self.fail('User should be authenticated')
        #---------------------------------------

        response = self.rpc.create_application(None, 'Note', self.user)
        if not isinstance(response, Error):
            self.fail('Undefined team')
        #---------------------------------------
        self.team.membership_policy = Team.INVITATION_BY_MANAGER
        self.team.save()

        response = self.rpc.create_application(self.team.pk, 'Note', self.user)
        if not isinstance(response, Error):
            self.fail('Team is not opened')
        #---------------------------------------
        self.team.membership_policy = Team.OPEN
        self.team.save()

        self.assertFalse(self.team.is_member(self.user))

        response = self.rpc.create_application(self.team.pk, 'Note', self.user)

        if isinstance(response, Error):
            self.fail(response)

        self.assertTrue(self.team.is_member(self.user))
        #---------------------------------------
        self.team.members.filter(user=self.user).delete()

        self.team.membership_policy = Team.APPLICATION
        self.team.save()

        self.assertFalse(Application.objects.filter(user=self.user, team=self.team).exists())
        response = self.rpc.create_application(self.team.pk, 'Note', self.user)

        if isinstance(response, Error):
            self.fail(response)

        self.assertFalse(self.team.is_member(self.user))
        self.assertTrue(Application.objects.filter(user=self.user, team=self.team).exists())
        #---------------------------------------


class TeamsDetailQueryTest(TestCase):

    fixtures = ["staging_users.json"]

    def setUp(self):
        fix_teams_roles()
        self.auth = {
            "username": u"admin",
            "password": u"admin"
        }
        self.user = User.objects.get(username=self.auth["username"])

        self.client.login(**self.auth)
        from apps.testhelpers.views import _create_videos, _create_team_videos
        fixture_path = os.path.join(settings.PROJECT_ROOT, "apps", "videos", "fixtures", "teams-list.json")
        data = json.load(open(fixture_path))
        self.videos = _create_videos(data, [self.user])
        self.team, created = Team.objects.get_or_create(name="test-team", slug="test-team")
        self.tvs = _create_team_videos( self.team, self.videos, [self.user])
        reset_solr()

    def _set_my_languages(self, *args):
        from auth.models import UserLanguage
        for ul in self.user.userlanguage_set.all():
            ul.delete()
        for lang in args:
            ul = UserLanguage(
                user=self.user,
                language=lang)
            ul.save()
        self.user = User.objects.get(id=self.user.id)

    def _debug_videos(self):
        from apps.testhelpers.views import debug_video
        return "\n".join([debug_video(v) for v in self.team.videos.all()])

    def _create_rdm_video(self, i):
        video, created = Video.get_or_create_for_url("http://www.example.com/%s.mp4" % i)
        return video

    def test_multi_query(self):
        team, created = Team.objects.get_or_create(slug='arthur')
        team.videos.all().delete()
        from utils import multi_query_set as mq
        created_tvs = [TeamVideo.objects.get_or_create(team=team, added_by=User.objects.all()[0], video=self._create_rdm_video(x) )[0] for x in xrange(10,30)]
        created_pks = [x.pk for x in created_tvs]
        multi = mq.MultiQuerySet(*[TeamVideo.objects.filter(pk=x) for x in created_pks])
        self.assertTrue([x.pk for x in multi] == created_pks)


class TestLanguagePreference(TestCase):
    fixtures = ["staging_users.json", "staging_videos.json", "staging_teams.json"]

    def setUp(self):
        fix_teams_roles()
        self.auth = {
            "username": u"admin",
            "password": u"admin"
        }
        self.team = Team.objects.all()[0]
        self.langs_set = set([x[0] for x in settings.ALL_LANGUAGES])
        from apps.teams.cache import invalidate_lang_preferences
        invalidate_lang_preferences(self.team)


    def test_readable_lang(self):
        # no tlp, should be all languages
        generated =TeamLanguagePreference.objects._generate_readable(self.team )
        cached =TeamLanguagePreference.objects.get_readable(self.team )
        self.assertItemsEqual(self.langs_set, generated )
        self.assertItemsEqual(self.langs_set, cached )
        # create one blocked
        tlp  = TeamLanguagePreference(team=self.team, language_code="en")
        tlp.save()
        # test generation
        generated =TeamLanguagePreference.objects._generate_readable(self.team )
        #test cache
        cached =TeamLanguagePreference.objects.get_readable(self.team )
        self.assertEquals(len(self.langs_set), len(generated)+1)
        self.assertEquals(len(self.langs_set), len(cached)+1)
        self.assertIn("en" , self.langs_set)
        self.assertNotIn("en" , generated)
        self.assertNotIn("en" , cached)

    def test_writable_lang(self):
        # no tlp, should be all languages
        generated =TeamLanguagePreference.objects._generate_writable(self.team )
        cached =TeamLanguagePreference.objects.get_writable(self.team )
        self.assertItemsEqual(self.langs_set, generated )
        self.assertItemsEqual(self.langs_set, cached )
        # create one blocked
        tlp  = TeamLanguagePreference(team=self.team, language_code="en")
        tlp.save()
        # test generation
        generated =TeamLanguagePreference.objects._generate_writable(self.team )
        #test cache
        cached =TeamLanguagePreference.objects.get_writable(self.team )
        self.assertEquals(len(self.langs_set), len(generated)+1)
        self.assertEquals(len(self.langs_set), len(cached)+1)
        self.assertIn("en" , self.langs_set)
        self.assertNotIn("en" , generated)
        self.assertNotIn("en" , cached)

    def test_preferred_lang(self):
        # No preference, so no languages should be preferred.
        generated = TeamLanguagePreference.objects._generate_preferred(self.team)
        cached = TeamLanguagePreference.objects.get_preferred(self.team)
        self.assertItemsEqual([], generated)
        self.assertItemsEqual([], cached)

        # Create one preferred.
        tlp = TeamLanguagePreference(team=self.team, language_code="en", preferred=True)
        tlp.save()

        # Check everything.
        generated = TeamLanguagePreference.objects._generate_preferred(self.team)
        cached = TeamLanguagePreference.objects.get_preferred(self.team)

        self.assertItemsEqual(["en"], generated)
        self.assertItemsEqual(["en"], cached)

        # Make sure this preferred language doesn't show up as a blocker.
        generated = TeamLanguagePreference.objects._generate_readable(self.team)
        cached = TeamLanguagePreference.objects.get_readable(self.team)

        self.assertIn("en", generated)
        self.assertIn("en", cached)

        generated = TeamLanguagePreference.objects._generate_writable(self.team)
        cached = TeamLanguagePreference.objects.get_writable(self.team)

        self.assertIn("en", generated)
        self.assertIn("en", cached)


class TestInvites(TestCase):

    def setUp(self):
        self.user = User.objects.filter(notify_by_message=True)[0]
        self.user.set_password(self.user.username)
        self.user.save()
        self.owner = User.objects.filter(notify_by_message=True)[1]
        self.team = Team.objects.create(name='test-team', slug='test-team', membership_policy=Team.APPLICATION)
        TeamMember.objects.create(user=self.owner, role=TeamMember.ROLE_ADMIN, team=self.team)

    def test_invite_invalid_after_accept(self):
        invite_form = InviteForm(self.team, self.owner, {
            'user_id': self.user.pk,
            'message': 'Subtitle ALL the things!',
            'role':'contributor',
        })
        invite_form.is_valid()
        self.assertFalse(invite_form.errors)
        self.assertEquals(Message.objects.for_user(self.user).count(), 0)
        invite = invite_form.save()
        # user has the invitation message on their inbox now
        self.assertEquals(Message.objects.for_user(self.user).count(), 1)
        invite.accept()
        self.assertTrue(self.team.members.filter(user=self.user).exists())
        self.team.members.filter(user=self.user).delete()
        # now the invite re-accepts:
        self.client.login(
            username=self.user.username,
            password=self.user.username
        )
        url = reverse("teams:accept_invite", args=(invite.pk,))
        response  = self.client.get(url)
        self.assertEqual(response.status_code, 500)
        self.assertIn( 'error_msg', response.context)
        self.assertFalse(self.team.members.filter(user=self.user).exists())

    def test_invite_invalid_after_deny(self):
        invite_form = InviteForm(self.team, self.owner, {
            'user_id': self.user.pk,
            'message': 'Subtitle ALL the things!',
            'role':'contributor',
        })
        invite_form.is_valid()
        self.assertFalse(invite_form.errors)
        self.assertEquals(Message.objects.for_user(self.user).count(), 0)
        invite = invite_form.save()
        # user has the invitation message on their inbox now
        invite.deny()
        self.assertFalse(self.team.members.filter(user=self.user).exists())
        # now the invite re-accepts:
        url = reverse("teams:deny_invite", args=(invite.pk,))
        self.client.login(
            username=self.user.username,
            password=self.user.username
        )
        response  = self.client.get(url)
        self.assertEqual(response.status_code, 500)
        self.assertIn( 'error_msg', response.context)
        self.assertFalse(self.team.members.filter(user=self.user).exists())

    def test_invite_after_removal(self):
        invite_form = InviteForm(self.team, self.owner, {
            'user_id': self.user.pk,
            'message': 'Subtitle ALL the things!',
            'role': TeamMember.ROLE_MANAGER,
        })
        invite_form.is_valid()
        self.assertFalse(invite_form.errors)
        self.assertEquals(Message.objects.for_user(self.user).count(), 0)
        invite = invite_form.save()
        # user has the invitation message on their inbox now
        invite.accept()
        self.assertTrue(self.team.members.filter(user=self.user).exists())
        self.team.members.filter(user=self.user).delete()
        # now the invite re-accepts:
        self.client.login(
            username=self.user.username,
            password=self.user.username
        )
        # acn't accept twice:
        # must import as team.models, not app.teams.models
        # else the module signature won't match
        from ..teams.models import InviteExpiredException
        self.assertRaises(InviteExpiredException, invite.accept)
        self.assertFalse(self.team.members.filter(user=self.user, team=self.team).exists())
        # re-invite
        invite_form = InviteForm(self.team, self.owner, {
            'user_id': self.user.pk,
            'message': 'Subtitle ALL the things!',
            'role': TeamMember.ROLE_CONTRIBUTOR,
        })
        invite_form.is_valid()
        self.assertFalse( invite_form.errors)
        invite = invite_form.save()
        url = reverse("teams:accept_invite", args=(invite.pk,))
        response  = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.team.members.filter(user=self.user, team=self.team).exists())


    def test_invite_after_leaving(self):
        # user is invited
        invite_form = InviteForm(self.team, self.owner, {
            'user_id': self.user.pk,
            'message': 'Subtitle ALL the things!',
            'role': TeamMember.ROLE_MANAGER,
        })
        invite_form.is_valid()
        self.assertFalse(invite_form.errors)
        self.assertEquals(Message.objects.for_user(self.user).count(), 0)
        invite = invite_form.save()
        # user has the invitation message on their inbox now
        # user accepts
        invite.accept()
        self.assertTrue(self.team.members.filter(user=self.user).exists())
        # now the invite re-accepts, should fail
        self.client.login(
            username=self.user.username,
            password=self.user.username
        )

        url = reverse("teams:accept_invite", args=(invite.pk,))
        response  = self.client.get(url)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(self.team.members.filter(user=self.user, team=self.team).exists())

        # user leaves team
        url = reverse("teams:leave_team", args=(self.team.slug,))
        response  = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.team.members.filter(user=self.user, team=self.team).exists())


        # user tries to re-accept old invite - fails
        url = reverse("teams:accept_invite", args=(invite.pk,))
        response  = self.client.get(url)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(self.team.members.filter(user=self.user, team=self.team).exists())
        # user is re-invited, should work


        invite_form = InviteForm(self.team, self.owner, {
            'user_id': self.user.pk,
            'message': 'Subtitle ALL the things!',
            'role': TeamMember.ROLE_MANAGER,
        })
        invite_form.is_valid()
        self.assertFalse(invite_form.errors)
        self.assertEquals(Message.objects.for_user(self.user).count(), 3)
        invite = invite_form.save()
        # user has the invitation message on their inbox now
        # user accepts
        url = reverse("teams:accept_invite", args=(invite.pk,))
        response  = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.team.members.filter(user=self.user, team=self.team).exists())


class TestApplication(TestCase, TestCaseMessagesMixin):
    def setUp(self):
        self.team, c = Team.objects.get_or_create(name='test', slug='test',membership_policy=Team.APPLICATION )
        self.owner = User.objects.create(username='test-owner')
        self.owner.set_password('test')
        self.owner.save()
        TeamMember.objects.create(team=self.team, user=self.owner, role=TeamMember.ROLE_OWNER)

        self.applicant = User.objects.create(username='test-applicant')
        self.applicant.set_password('test')
        self.applicant.save()


        self.rpc = TeamsApiClass()

    def _send_application(self):
        self._login(False)
        # if te team member left, he can auto join
        should_auto_join = False
        try:
            application = Application.objects.get(team=self.team,user=self.applicant)
            should_auto_join = application.status  == Application.STATUS_MEMBER_LEFT
        except Application.DoesNotExist:
            application = None

        response = self.rpc.create_application(self.team.pk, 'Note', self.applicant)
        if isinstance(response, Error):
            self.fail(response)
        if not should_auto_join:
            self.assertFalse(self.team.is_member(self.applicant))
            self.assertTrue(Application.objects.filter(user=self.applicant, team=self.team, status=Application.STATUS_PENDING).exists())
            self.assertTrue(Application.objects.open(user=self.applicant, team=self.team).exists())
        else:
            self.assertTrue(self.team.is_member(self.applicant))
            self.assertTrue(Application.objects.filter(user=self.applicant, team=self.team, status=Application.STATUS_APPROVED).exists())
        return Application.objects.get(pk=application.pk) if application else Application.objects.order_by('-pk')[0]

    def _login(self, as_owner):
        username = self.owner.username if as_owner else self.applicant.username
        self.assertTrue(self.client.login(username=username, password='test'))

    def _approve(self, application):
        self._login(True)
        #num_messages = self._getMessagesCount(level=LEVEL_SUCCESS)
        url = reverse("teams:approve_application", args=(self.team.slug, application.pk))
        response = self.client.post(url, follow=True)
        self.assertEqual(response.redirect_chain[0][1], 302)
        self.assertEqual(response.status_code, 200)

    def _deny(self, application):
        self._login(True)
        #num_messages = self._getMessagesCount(level=LEVEL_SUCCESS)
        url = reverse("teams:deny_application", args=(self.team.slug, application.pk))
        response = self.client.post(url, follow=True)
        self.assertEqual(response.redirect_chain[0][1], 302)
        self.assertEqual(response.status_code, 200)



    def _leave_team(self, user):
        url = reverse("teams:leave_team", args=(self.team.slug,))
        self.client.post(url)

    def _remove_member(self, user):
        self._login(True)
        member_count = self.team.members.count()
        url = reverse("teams:remove_member", args=(self.team.slug,user.pk))
        self.client.post(url)
        self.assertEqual(member_count -1, self.team.members.count())


    def test_user_leaves(self):
        # user applies
        application = self._send_application()
        # owner approves
        self._approve(application)
        # is member!
        self.assertTrue(self.team.is_member(self.applicant))
        # cannot apply again
        self.assertFalse(teams_tags.can_apply(self.team, self.applicant))
        # regular application inbox is empty
        self.assertEquals(Application.objects.open(team=self.team).count(), 0)
        # applicant leaves team
        self.client.logout()
        self._login(False)
        self._leave_team(self.applicant)

        self.assertFalse(Application.objects.filter(team=self.team, status=Application.STATUS_APPROVED).exists())
        self.assertTrue(Application.objects.filter(team=self.team, status=Application.STATUS_MEMBER_LEFT).exists())
        # applicant is not member
        self.assertFalse(self.team.is_member(self.applicant))
        # application can join again
        self.assertTrue(teams_tags.can_apply(self.team, self.applicant))
        application = self._send_application()

        # applicant is a team member again
        self.assertTrue(self.team.is_member(self.applicant))

    def test_user_removed(self):
        # user applies
        application = self._send_application()
        # owner approves
        self._approve(application)
        # is member!
        self.assertTrue(self.team.is_member(self.applicant))
        # cannot apply again
        self.assertFalse(teams_tags.can_apply(self.team, self.applicant))
        # regular application inbox is empty
        self.assertEquals(Application.objects.open(team=self.team).count(), 0)
        # applicant leaves team
        self.client.logout()
        self._login(True)
        self._remove_member(self.applicant)

        self.assertFalse(Application.objects.filter(team=self.team, status=Application.STATUS_APPROVED).exists())
        self.assertTrue(Application.objects.filter(team=self.team, status=Application.STATUS_MEMBER_REMOVED).exists())
        # applicant is not member
        self.assertFalse(self.team.is_member(self.applicant))
        # application can join again
        self.assertFalse(teams_tags.can_apply(self.team, self.applicant))
        self._login(False)
        response = self.rpc.create_application(self.team.pk, 'Note', self.applicant)
        # removed user, cannot send application again
        self.assertTrue( isinstance(response, Error))


    def test_denied_kills_it(self):
        # user applies
        application = self._send_application()
        # owner approves
        self._deny(application)
        # is member!
        self.assertFalse(self.team.is_member(self.applicant))
        # cannot apply again
        self.assertFalse(teams_tags.can_apply(self.team, self.applicant))
        # regular application inbox is empty
        self.assertEquals(Application.objects.open(team=self.team).count(), 0)
        # applicant leaves team
        self.assertTrue(Application.objects.filter(team=self.team, status=Application.STATUS_DENIED).exists())
        # applicant is not member
        self.assertFalse(self.team.is_member(self.applicant))
        # application can join again
        self._login(False)
        response = self.rpc.create_application(self.team.pk, 'Note', self.applicant)
        # removed user, cannot send application again
        self.assertTrue( isinstance(response, Error))


    def test_can_apply(self):
        # user is already a memeber, can't apply
        self.assertFalse(Application.objects.can_apply(self.team, self.owner))
        # if has bad application or is already a member
        self.assertTrue(Application.objects.can_apply(self.team, self.applicant))
        # create applications where team owners have already blocked the user or are still waiting
        for app_status in [Application.STATUS_MEMBER_REMOVED, Application.STATUS_DENIED, Application.STATUS_PENDING]:
            application = Application.objects.create(status=app_status, team=self.team, user=self.applicant)
            self.assertFalse(Application.objects.can_apply(self.team, self.applicant))
            application.delete()


class PartnerTest(TestCase):

    def test_is_admin(self):
        partner = Partner.objects.create(name='Le Partner', slug='partner')
        user = User.objects.get(username='adam')

        self.assertFalse(partner.is_admin(user))
        partner.admins.add(user)
        self.assertTrue(partner.is_admin(user))


class BillingTest(TestCase):

    fixtures = [
        "staging_users.json",
        "staging_videos.json",
        "staging_teams.json"
    ]

    def test_approved(self):
        from apps.teams.models import Workflow, BillingReport
        from apps.teams.moderation_const import APPROVED

        self.assertEquals(0, Workflow.objects.count())

        team = Team.objects.all()[0]
        team.workflow_enabled = True
        team.save()

        Workflow.objects.create(team=team, approve_allowed=20)

        self.assertEquals(1, Workflow.objects.count())
        self.assertTrue(team.get_workflow().approve_enabled)

        language = SubtitleLanguage.objects.all()[0]

        for i in range(1, 10):
            SubtitleVersion.objects.create(language=language,
                    datetime_started=datetime(2012, 1, i, 0, 0, 0),
                    version_no=i)

        v1 = SubtitleVersion.objects.get(language=language, version_no=3)
        v2 = SubtitleVersion.objects.get(language=language, version_no=6)

        v1.moderation_status = APPROVED
        v1.save()
        v2.moderation_status = APPROVED
        v2.save()

        b = BillingReport.objects.create(team=team,
                start_date=date(2012, 1, 1), end_date=date(2012, 1, 2))

        langs = language.video.subtitlelanguage_set.all()
        c = langs[0]
        d = team.created - timedelta(days=5)
        SubtitleVersion.objects.create(language=c, version_no=0,
                note='From youtube',
                datetime_started=d)

        self.assertTrue(len(langs) > 0)
        created, imported, _ = b._get_lang_data(langs, datetime(2012, 1, 1, 13, 30, 0))

        self.assertTrue(len(created) > 0)

        v = created[0][1]
        self.assertEquals(v.version_no, 3)

        team.workflow_enabled = False
        team.save()

        created, imported, _ = b._get_lang_data(langs, datetime(2012, 1, 1, 13, 30, 0))
        self.assertEquals(1, len(created))
        v = created[0][1]
        self.assertEquals(v.version_no, 9)

    def test_get_imported(self):
        from apps.teams.models import BillingReport
        team = Team.objects.all()[0]
        video = Video.objects.all()[0]

        team_created = team.created

        b = BillingReport.objects.create(team=team,
                start_date=date(2012, 1, 1), end_date=date(2012, 1, 2))

        SubtitleLanguage.objects.all().delete()

        sl_en = SubtitleLanguage.objects.create(video=video, language='en')
        sl_cs = SubtitleLanguage.objects.create(video=video, language='cs')
        sl_fr = SubtitleLanguage.objects.create(video=video, language='fr')
        sl_es = SubtitleLanguage.objects.create(video=video, language='es')
        SubtitleLanguage.objects.create(video=video, language='ru')

        before_team_created = team_created - timedelta(days=10)
        after_team_created = team_created + timedelta(days=10)

        # Imported
        SubtitleVersion.objects.create(language=sl_fr,
                datetime_started=before_team_created, note='From youtube',
                version_no=0)

        # Created
        SubtitleVersion.objects.create(language=sl_fr,
                datetime_started=after_team_created,
                version_no=1)

        SubtitleVersion.objects.create(language=sl_en,
                datetime_started=before_team_created, note='From youtube',
                version_no=0)

        # Imported
        SubtitleVersion.objects.create(language=sl_es,
                datetime_started=before_team_created,
                version_no=0)

        # Imported
        SubtitleVersion.objects.create(language=sl_cs,
                datetime_started=after_team_created, note='From youtube',
                version_no=0)

        # Done with setup, let's test things

        languages = SubtitleLanguage.objects.all()
        imported, crowd_created = b._separate_languages(languages)

        self.assertEquals(len(imported), 3)
        imported_pks = [i.pk for i in imported]
        self.assertTrue(sl_fr.pk in imported_pks)
        self.assertTrue(sl_es.pk in imported_pks)
        self.assertTrue(sl_cs.pk in imported_pks)

    def test_record_insertion(self):
        from apps.teams.models import BillingRecord
        from apps.videos.tasks import video_changed_tasks
        BillingRecord.objects.all().delete()

        user = User.objects.all()[0]

        video = Video.objects.filter(teamvideo__isnull=False)[0]
        video.user = user
        video.save()

        sl = SubtitleLanguage.objects.create(video=video, language='en',
                is_complete=True)
        now = datetime(2013, 4, 2, 0, 0, 0)

        sl.subtitleversion_set.all().delete()

        sv = SubtitleVersion.objects.create(language=sl, user=user,
                datetime_started=now, version_no=0)

        video_changed_tasks(video.pk, sv.pk)

        self.assertEquals(1, BillingRecord.objects.all().count())

        br = BillingRecord.objects.all()[0]

        self.assertEquals(br.video.pk, video.pk)
        self.assertEquals(br.team.pk, video.get_team_video().team.pk)
        self.assertEquals(br.created, now)
        self.assertEquals(br.is_original, sl.is_original)
        self.assertEquals(br.user.pk, user.pk)
        self.assertEquals(br.subtitle_language.pk, sl.pk)

        team = video.get_team_video().team
        start = datetime(2013, 1, 1, 0, 0)
        end = datetime(2013, 5, 1, 0, 0, 0)

        csv_data = BillingRecord.objects.csv_report_for_team(team, start, end)

        self.assertEquals(2, len(csv_data))
        self.assertEquals(8, len(csv_data[1]))

        # 2
        sl.is_original = False
        sl.save()
        sv = SubtitleVersion.objects.create(language=sl, user=user,
                datetime_started=now, version_no=1)

        video_changed_tasks(video.pk, sv.pk)

        # A new one shouldn't be created for the same language
        self.assertEquals(1, BillingRecord.objects.all().count())

    def test_two_languages(self):
        from apps.teams.models import BillingRecord
        from apps.videos.tasks import video_changed_tasks
        BillingRecord.objects.all().delete()

        user = User.objects.all()[0]

        video = Video.objects.filter(teamvideo__isnull=False)[0]
        video.user = user
        video.save()

        sl_en = SubtitleLanguage.objects.create(video=video, language='en',
                is_complete=True)
        sl_cs = SubtitleLanguage.objects.create(video=video, language='cs',
                is_complete=True)
        now = datetime(2013, 4, 2, 0, 0, 0)

        sv_en = SubtitleVersion.objects.create(language=sl_en, user=user,
                datetime_started=now, version_no=0)

        sv_cs = SubtitleVersion.objects.create(language=sl_cs, user=user,
                datetime_started=now, version_no=0)

        video_changed_tasks(video.pk, sv_en.pk)
        video_changed_tasks(video.pk, sv_cs.pk)

        self.assertEquals(2, BillingRecord.objects.all().count())

    def test_incomplete_language(self):
        from apps.teams.models import BillingRecord
        from apps.videos.tasks import video_changed_tasks
        BillingRecord.objects.all().delete()

        user = User.objects.all()[0]

        video = Video.objects.filter(teamvideo__isnull=False)[0]
        video.user = user
        video.save()

        sl_en = SubtitleLanguage.objects.create(video=video, language='en',
                is_complete=False)
        now = datetime(2013, 4, 2, 0, 0, 0)

        sv_en = SubtitleVersion.objects.create(language=sl_en, user=user,
                datetime_started=now, version_no=0)

        video_changed_tasks(video.pk, sv_en.pk)

        self.assertEquals(0, BillingRecord.objects.all().count())

    def test_original_language(self):
        from apps.teams.models import BillingRecord
        from apps.videos.tasks import video_changed_tasks

        BillingRecord.objects.all().delete()

        user = User.objects.all()[0]

        video = Video.objects.filter(teamvideo__isnull=False)[0]
        video.user = user
        video.save()

        sl_en = SubtitleLanguage.objects.create(video=video, language='en',
                is_original=False, is_complete=True)
        now = datetime(2013, 4, 2, 0, 0, 0)

        sv_en = SubtitleVersion.objects.create(language=sl_en, user=user,
                datetime_started=now, version_no=0)

        video_changed_tasks(video.pk, sv_en.pk)

        self.assertEquals(1, BillingRecord.objects.all().count())

        br = BillingRecord.objects.all()[0]
        self.assertFalse(br.is_original)
