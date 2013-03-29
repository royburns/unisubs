#!/bin/bash

/opt/ve/vagrant/unisubs/bin/python manage.py build_solr_schema --settings=dev_settings > /etc/solr/conf/vagrant/conf/schema.xml

service tomcat6 restart

/opt/ve/vagrant/unisubs/bin/python manage.py rebuild_index --noinput --settings=dev_settings
