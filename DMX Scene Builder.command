#!/bin/bash
# DMX Scene Builder launcher. Double-click this in Finder to start the app.
cd "$(dirname "$0")" || exit 1

FRAMEWORK_PY="/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
if [ -x "$FRAMEWORK_PY" ]; then
  PY="$FRAMEWORK_PY"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif [ -x "/usr/bin/python3" ]; then
  PY="/usr/bin/python3"
else
  PY=""
fi

echo "========================================================"
echo "  DMX SCENE BUILDER, Jeff Holmes Presents"
echo "========================================================"
echo ""

if [ -z "$PY" ]; then
  echo "Couldn't find Python 3 on this Mac, so DMX Scene Builder can't start."
  echo "Ask for help getting Python 3 installed, then try again."
  echo ""
  echo "Press any key to close this window..."
  read -n 1 -s -r
  exit 1
fi

echo "Starting the server with: $PY"
echo "Leave this window open while you work. Close it to stop DMX Scene Builder."
echo ""

"$PY" -m scenebuilder "$@"
status=$?
if [ $status -ne 0 ]; then
  echo ""
  echo "DMX Scene Builder stopped with a problem (see above)."
  echo "Press any key to close this window..."
  read -n 1 -s -r
fi
exit $status
