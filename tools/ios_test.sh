#!/bin/sh
# Build the iPad app and run its Swift tests on an iPad simulator, with the Python fake E-Box
# (tools/ios_fakebox.py) running on 127.0.0.1 for the end-to-end tests.
#   tools/ios_test.sh                 # picks the first available iPad simulator
#   SIM="iPad Air 11-inch (M3)" tools/ios_test.sh
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$(mktemp -t fakebox)"
python3 "$ROOT/tools/ios_fakebox.py" > "$LOG" 2>&1 &
FB=$!
trap 'kill $FB 2>/dev/null' EXIT
for i in 1 2 3 4 5 6 7 8 9 10; do grep -q control_port "$LOG" && break; sleep 0.5; done
CONTROL=$(python3 -c "import json,sys; print(json.loads(open(sys.argv[1]).readline())['control_port'])" "$LOG")
if [ -z "$SIM" ]; then
  SIM=$(xcrun simctl list devices available -j | python3 -c "
import json,sys
d=json.load(sys.stdin)['devices']
names=[x['name'] for r,v in sorted(d.items(), reverse=True) if 'iOS' in r for x in v if x['name'].startswith('iPad')]
print(names[0] if names else '')")
fi
[ -n "$SIM" ] || { echo "No iPad simulator installed (Xcode > Settings > Components)."; exit 1; }
echo "Fake E-Box control port $CONTROL; simulator: $SIM"
cd "$ROOT/ios"
TEST_RUNNER_FAKEBOX_CONTROL=$CONTROL TEST_RUNNER_FAKEBOX_REQUIRED=1 \
  xcodebuild -project DMXSceneBuilder.xcodeproj -scheme DMXSceneBuilder \
  -destination "platform=iOS Simulator,name=$SIM" ${DERIVED:+-derivedDataPath "$DERIVED"} \
  CODE_SIGNING_ALLOWED=NO test
