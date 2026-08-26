#!/bin/sh
set -e

# Install debugpy, local extension requirements, and editable installs.
# This script must run as root because /usr/local/lib/python3.14/site-packages
# is owned by root and is shared with the ckan-dev service.
/srv/app/install_src.sh

# Run the downloads worker under debugpy as the ckan user.
exec su -s /bin/sh ckan -c \
  "exec /usr/local/bin/python -Xfrozen_modules=off -m debugpy --listen 0.0.0.0:5679 --wait-for-client /usr/local/bin/ckan -c /srv/app/ckan.ini jobs worker pnaig-downloads"
