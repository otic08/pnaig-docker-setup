#!/bin/bash
set -euo pipefail

if [[ "${CKAN__PLUGINS:-}" == *"pnaig_user"* ]]; then
  echo "Applying DB migrations for pnaig_user extension"
  ckan -c "$CKAN_INI" db upgrade -p pnaig_user
else
  echo "Skipping pnaig_user DB migrations (plugin not enabled)"
fi
