#!/bin/bash
# Fully reverts install.sh: stops the scorer, removes the launch agent and the hook (restoring any hook it replaced),
# and deletes ~/.autoswitch. Pass --keep-data to keep your shadow log and settings in ~/.autoswitch.
set -euo pipefail

BASE="${AUTOSWITCH_DIR:-$HOME/.autoswitch}"
LABEL="${AUTOSWITCH_LABEL:-com.autoswitch.scorer}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CLI="$BASE/app/bin/autoswitch"
SRC="$(cd "$(dirname "$0")" && pwd)"
[ -x "$CLI" ] || CLI="$SRC/bin/autoswitch"

echo "==> Removing the hook from ~/.claude/settings.json"
if ! python3 "$CLI" _settings-uninstall; then
  echo "error: could not restore ~/.claude/settings.json; nothing was removed. Fix the problem above and run ./uninstall.sh again." >&2
  exit 1
fi

python3 "$CLI" _plugin-uninstall || true  # the autoswitch-inline plugin, if `autoswitch speed inline` installed it
pkill -f "$BASE/bin/typeahead" 2>/dev/null || true  # the typeahead reader (speed predictive)

echo "==> Stopping the scorer"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"

for d in "$HOME/.local/bin" "$HOME/bin"; do
  if [ -L "$d/autoswitch" ]; then case "$(readlink "$d/autoswitch")" in "$BASE"/*) rm -f "$d/autoswitch";; esac; fi
done

if [ "${1:-}" = "--keep-data" ]; then
  rm -rf "${BASE:?}/app" "${BASE:?}/venv" "${BASE:?}/models" "${BASE:?}/bin"
  echo "==> Kept your data in $BASE"
else
  rm -rf "${BASE:?}"
fi
echo "Done. Close the Watcher chat if it is still open. Backups of settings.json (settings.json.autoswitch-backup-*) were left in ~/.claude; delete them if you like."
