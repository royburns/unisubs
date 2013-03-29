#!/bin/bash

set -e

EXTRAS_DIR='/opt/extras'
VE_DIR='/opt/ve/vagrant/unisubs'
# create virtualenv ; no longer needed
#virtualenv --no-site-packages --distribute $VE_DIR
# Link folders ----------------------------------------------------------------
mkdir -p $EXTRAS_DIR/static-cache
mkdir -p $EXTRAS_DIR/pictures
mkdir -p $EXTRAS_DIR/video
test -e venv               || ln -sf $VE_DIR venv
test -L media/static-cache || ln -s $EXTRAS_DIR/static-cache media/static-cache
test -L user-data/video    || ln -s $EXTRAS_DIR/video user-data/video
test -L user-data/pictures || ln -s $EXTRAS_DIR/pictures user-data/pictures

# Install requirements --------------------------------------------------------
source $VE_DIR/bin/activate
cd deploy
# Hack until we can think of a better solution
#pip install vendor/pycrypto-2.1.0.tar.gz
pip install -r requirements.txt
pip install -r requirements-test.txt
cd ..

# Create a base settings_local.py ---------------------------------------------
cat > settings_local.py <<EOF
# This setting lets non-admin accounts view the Django Debug Toolbar.
# Useful for development when you're debugging queries made for non-admins.
EVERYONE_CAN_DEBUG = True

# Change this to True to tell unisubs to load the compressed version of the
# static media.  You'll still need to actually compress the media yourself.
COMPRESS_MEDIA = False

DEFAULT_PROTOCOL = 'http'

EMAIL_BACKEND = 'django.core.mail.backends.filebased.EmailBackend'

# Sent emails will be stored in files in this folder.
EMAIL_FILE_PATH = '/tmp/unisubsdevemails'

# Basic Celery settings for Vagrant.  Shouldn't need to touch these.
CELERY_IGNORE_RESULT = True
CELERY_ALWAYS_EAGER = True
CELERY_RESULT_BACKEND = "redis"
CELERY_REDIS_HOST = "localhost"
CELERY_REDIS_PORT = 6379
CELERY_REDIS_DB = 0
BROKER_POOL_LIMIT = 10

# Uncomment to enable RabbitMQ-based Celery:
# CELERY_ALWAYS_EAGER = False
# CELERY_RESULT_BACKEND = "amqp"
# BROKER_BACKEND = 'amqplib'
# BROKER_HOST = "localhost"
# BROKER_PORT = 5672
# BROKER_USER = "usrmquser"
# BROKER_PASSWORD = "usrmqpassword"
# BROKER_VHOST = "ushost"

# Change this to True to work in "offline" mode (don't try to load things like
# Google Analytics, etc).
RUN_LOCALLY = False

# Just leave these here for now.
# TODO: Get rid of these.
SPEAKERTEXT_API_TOKEN = ""
SPEAKERTEXT_PASSWORD = ""

# Metrics
RIEMANN_HOST = '10.10.10.44'
ENABLE_METRICS = False

# Youtube
YOUTUBE_ALWAYS_PUSH_USERNAME = None
YOUTUBE_ALWAYS_PUSH_TO = {}
EOF

# Set up the DB ---------------------------------------------------------------
python manage.py syncdb --all --settings=dev_settings --noinput
python manage.py migrate --fake --settings=dev_settings

# Solr-------------------------------------------------------------------------
sudo ./deploy/update_solr_schema_vagrant.sh

# Adjust sys.path -------------------------------------------------------------
cat > venv/lib/python2.6/sitecustomize.py <<EOF
import sys

try:
    sys.path.remove('/opt/ve/vagrant/unisubs/lib/python2.6/site-packages')
except ValueError:
    pass

try:
    sys.path.remove('/usr/lib/python2.6')
except ValueError:
    pass

sys.path = ['/opt/ve/vagrant/unisubs/lib/python2.6/site-packages', '/usr/lib/python2.6'] + sys.path
EOF

# Celery services -------------------------------------------------------------
sudo /etc/init.d/celeryd.vagrant restart
sudo /etc/init.d/celerycam.vagrant restart

# Notice ----------------------------------------------------------------------
echo "========================================================================="
echo "Bootstrapping Complete"
echo ""
echo "For even better performance move the git directory away on your HOST OS:"
echo ""
echo "    mv .git ../unisubs.git && ln -s ../unisubs.git .git"
