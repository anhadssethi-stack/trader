#!/bin/bash
# Installs a launchd job that runs the daily update at 19:45 Mon-Fri (your Mac's local time).
# If your Mac is not set to India time, change HOUR/MINUTE below to 19:45 IST in your time zone.
# If the Mac is asleep at that time, launchd runs the job when it next wakes.
# Remove with:  launchctl unload ~/Library/LaunchAgents/com.nseresearch.daily.plist
set -euo pipefail
HOUR=19
MINUTE=45
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.nseresearch.daily.plist"
chmod +x "$PROJECT/scripts/run_daily.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/data/logs"

DAYS=""
for wd in 1 2 3 4 5; do
  DAYS+="    <dict><key>Weekday</key><integer>$wd</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
"
done

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nseresearch.daily</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$PROJECT/scripts/run_daily.sh</string></array>
  <key>StartCalendarInterval</key>
  <array>
$DAYS  </array>
  <key>StandardOutPath</key><string>$PROJECT/data/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$PROJECT/data/logs/launchd.err</string>
</dict>
</plist>
PL

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed. The daily job runs Mon-Fri at $HOUR:$MINUTE. Logs: $PROJECT/data/logs/daily.log"
echo "Run it once now to test:  launchctl start com.nseresearch.daily"
