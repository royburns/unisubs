import logging
from celery.task import task, periodic_task
from celery.schedules import timedelta
from videos.models import VideoUrl, VIDEO_TYPE_YOUTUBE
from videos.types import UPDATE_VERSION_ACTION
from auth.models import CustomUser as User
from models import ThirdPartyAccount
from utils.metrics import Gauge


logger = logging.getLogger(__name__)


def get_youtube_data(user_pk):
    """
    Get data for all videos with completed languages.

    Return a list of 3-tuples like this

        (video, language, version,)

    """
    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        logger.error('User with PK %s not found' % user_pk)
        return

    usernames = [a.username for a in user.third_party_accounts.all()]
    urls = VideoUrl.objects.filter(owner_username__in=usernames)
    videos = [url.video for url in urls]

    data = []

    for video in videos:
        languages = video.completed_subtitle_languages()

        for language in languages:
            version = language.latest_version()
            data.append((video, language, version,))

    return data


@task()
def mirror_existing_youtube_videos(user_pk):
    logger.info('Starting sync of existing youtube videos, pk: %s' % user_pk)
    data = get_youtube_data(user_pk)

    if not data:
        logger.info('No videos/subtitles to upload.')
        return

    for video, language, version in data:
        ThirdPartyAccount.objects.mirror_on_third_party(video, language,
                UPDATE_VERSION_ACTION, version)


@periodic_task(run_every=timedelta(seconds=60))
def gauge_tpas():
    count = ThirdPartyAccount.objects.filter(type=VIDEO_TYPE_YOUTUBE).count()
    Gauge('youtube.accounts_linked').report(count)
