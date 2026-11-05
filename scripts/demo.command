#!/bin/sh
# Fake-key walkthrough only. No installation, vault or provider access.
set -eu
keybridge_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$keybridge_root"
keybridge_result=0
python3 -m keybridge demo --guided || keybridge_result=$?
if [ -t 0 ]; then
  printf '\nPress Return to close this demonstration.\n'
  IFS= read -r keybridge_finished || true
fi
exit "$keybridge_result"
