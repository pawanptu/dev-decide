#!/bin/sh
# Run a dev-decide script with the first Python 3.8+ found: python3, python, or the Windows launcher (py -3).
# Usage: sh run.sh decide.py --skill    |    sh run.sh hook_route.py
dir=$(dirname "$0")
script=$1
shift
for py in python3 python "py -3"; do
  if $py -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    exec $py "$dir/$script" "$@"
  fi
done
case "$script" in
  hook_route.py|lite.py) exit 0 ;;  # a hook must never break the session
esac
echo "Summary: dev-decide error: Python 3.8 or later was not found (tried python3, python, py -3)."
echo "Reply: tell the user, in one line: dev-decide needs Python 3.8 or later on the PATH."
exit 0
