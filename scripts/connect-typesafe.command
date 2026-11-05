#!/bin/sh
# Owner-only launcher. Never run in an agent-captured terminal.
set -eu
keybridge_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$keybridge_root"
if [ "$#" -eq 0 ]; then
  printf 'Linked Vercel site directory (Preview setup): '
  IFS= read -r keybridge_site
  set -- --site "$keybridge_site"
fi
keybridge_result=0
python3 -m keybridge connect-typesafe "$@" || keybridge_result=$?
printf '\nPress Return to close this setup.\n'
IFS= read -r keybridge_finished || true
exit "$keybridge_result"
