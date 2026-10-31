#!/bin/bash
# claude-autoswitch installer. Safe to run again. Everything lives in ~/.autoswitch; ./uninstall.sh reverts it.
#   AUTOSWITCH_NO_LAUNCHD=1  skip the launch agent (the scorer is then not started)
#   AUTOSWITCH_PORT=8765     port for the local scorer
#   AUTOSWITCH_LABEL=...     launch agent label (default com.autoswitch.scorer)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BASE="${AUTOSWITCH_DIR:-$HOME/.autoswitch}"
APP="$BASE/app"
LABEL="${AUTOSWITCH_LABEL:-com.autoswitch.scorer}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
VERSION="$(cat "$SRC/VERSION")"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = Darwin ] || die "claude-autoswitch needs macOS (the Claude Code desktop app and launchd)."
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found. Install it with: xcode-select --install"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null || die "python3 3.9 or newer is required (found $("$PY" -V 2>&1))."
"$PY" -c 'import venv, ensurepip' 2>/dev/null || die "python3 has no venv module. Install Python from python.org or Homebrew."

# 1. copy the app (state files in $BASE are kept)
say "Installing to $BASE"
mkdir -p "$BASE" "$BASE/bin" "$BASE/logs"
rm -rf "$APP"; mkdir -p "$APP"
cp -R "$SRC/hook" "$SRC/acpmh" "$SRC/scorer" "$SRC/train" "$SRC/data" "$SRC/bin" "$APP/"
cp -R "$SRC/plugin" "$SRC/.claude-plugin" "$APP/"  # the autoswitch-inline plugin and its marketplace (speed inline)
cp "$SRC/WATCHER.md" "$SRC/VERSION" "$SRC/requirements.txt" "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
chmod +x "$APP/bin/autoswitch" "$APP/hook/watch.sh"

# 2. private virtualenv (only the scorer needs it; the hook itself uses just the standard library)
if [ ! -x "$BASE/venv/bin/python" ] || ! "$BASE/venv/bin/python" -c 'import sklearn' 2>/dev/null \
   || ! cmp -s "$APP/requirements.txt" "$BASE/venv/autoswitch-requirements.txt"; then
  say "Creating virtualenv and installing scikit-learn (about a minute the first time)"
  "$PY" -m venv "$BASE/venv"
  "$BASE/venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt" || die "pip install failed (are you online?)"
  cp "$APP/requirements.txt" "$BASE/venv/autoswitch-requirements.txt"
  rm -f "$BASE/models/.version"  # retrain against the pinned versions
fi

# 3. train the two small models from the bundled public data
if [ ! -f "$BASE/models/synthetic.pkl" ] || [ "$(cat "$BASE/models/.version" 2>/dev/null)" != "$VERSION" ]; then
  say "Training the scorer on the bundled data (a few seconds)"
  "$BASE/venv/bin/python" "$APP/train/train.py" --out "$BASE/models" >/dev/null
  echo "$VERSION" > "$BASE/models/.version"
fi

# 4. screenshot reader (optional): lets the hook see text in a pasted screenshot.
#    typeahead reader (optional, speed predictive): reads the prompt box while you type, never types or clicks
if command -v swiftc >/dev/null 2>&1; then
  say "Building the screenshot reader"
  swiftc -O "$APP/hook/clipimg.swift" -o "$BASE/bin/clipimg" 2>/dev/null || warn "could not build the screenshot reader; screenshots are still never blocked"
  say "Building the typeahead reader (speed predictive)"
  pkill -f "$BASE/bin/typeahead" 2>/dev/null || true  # an old reader exits; the Watcher starts the new one
  swiftc -O "$APP/hook/typeahead.swift" -o "$BASE/bin/typeahead" 2>/dev/null || warn "could not build the typeahead reader; predictive then behaves like instant"
else
  warn "swiftc not found (xcode-select --install): screenshot text will not be read; screenshots are still never blocked"
fi

# 5. port, mode and speed. An existing mode is kept; a fresh install starts in shadow (log only, change nothing).
PORT="${AUTOSWITCH_PORT:-$(cat "$BASE/port" 2>/dev/null || echo 8765)}"
port_free() { ! "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(0 if s.connect_ex(('127.0.0.1',$1))==0 else 1)"; }
if ! port_free "$PORT" && [ "$(curl -s -m 1 "http://127.0.0.1:$PORT/health" || true)" != ok ]; then
  while ! port_free "$PORT"; do PORT=$((PORT + 1)); done
  warn "default port was busy, using $PORT"
fi
echo "$PORT" > "$BASE/port"
# a fresh install (no mode file yet) also gets speed=instant; an existing speed file, or an upgrade, is never touched
if [ ! -f "$BASE/mode" ]; then
  [ -f "$BASE/speed" ] || echo instant > "$BASE/speed"
  echo shadow > "$BASE/mode"
fi

# 6. launch agent: starts the scorer at login and keeps it alive
if [ -z "${AUTOSWITCH_NO_LAUNCHD:-}" ]; then
  say "Starting the scorer at login (launch agent $LABEL)"
  mkdir -p "$(dirname "$PLIST")"
  x() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"; }
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$(x "$LABEL")</string>
  <key>ProgramArguments</key><array>
    <string>$(x "$BASE/venv/bin/python")</string>
    <string>$(x "$APP/scorer/scorer.py")</string>
    <string>--port</string><string>$(x "$PORT")</string>
    <string>--models</string><string>$(x "$BASE/models")</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(x "$BASE/logs/scorer.log")</string>
  <key>StandardErrorPath</key><string>$(x "$BASE/logs/scorer.log")</string>
</dict></plist>
PL
  DOMAIN="gui/$(id -u)"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in $(seq 1 20); do launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break; sleep 0.25; done  # bootout is asynchronous
  loaded=
  for _ in $(seq 1 10); do launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && { loaded=1; break; }; sleep 0.5; done
  [ -n "$loaded" ] || launchctl load -w "$PLIST" 2>/dev/null || warn "could not load the launch agent"
  for _ in $(seq 1 40); do [ "$(curl -s -m 1 "http://127.0.0.1:$PORT/health" || true)" = ok ] && break; sleep 0.5; done
fi

# 7. the hook, added to Claude Code's settings (backed up first; the old router hint is replaced, not stacked)
say "Adding the hook to ~/.claude/settings.json"
"$APP/bin/autoswitch" _settings-install "$PY"

# 8. the autoswitch command
for d in "$HOME/.local/bin" "$HOME/bin"; do
  case ":$PATH:" in *":$d:"*) [ -d "$d" ] && [ -w "$d" ] && { ln -sf "$APP/bin/autoswitch" "$d/autoswitch"; LINK="$d/autoswitch"; break; };; esac
done
if [ -z "${LINK:-}" ]; then
  mkdir -p "$HOME/.local/bin" && ln -sf "$APP/bin/autoswitch" "$HOME/.local/bin/autoswitch"; LINK="$HOME/.local/bin/autoswitch"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
    RC="$HOME/.zshrc"; [ "${SHELL##*/}" = "bash" ] && RC="$HOME/.bash_profile"
    if ! grep -qs '\.local/bin' "$RC" 2>/dev/null; then
      printf '\n# added by claude-autoswitch\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
    fi
    warn "added ~/.local/bin to your PATH in $RC: open a new Terminal window (or run: source $RC) to use the \`autoswitch\` command";; esac
fi

# 9. smoke test: the hook must answer through the scorer
if [ -z "${AUTOSWITCH_NO_LAUNCHD:-}" ]; then
  OUT="$(echo '{"prompt":"what does git rebase do","session_id":"install-check"}' | AUTOSWITCH_MODE=hint "$PY" "$APP/hook/hint.py" 2>/dev/null || true)"
  case "$OUT" in *"Router suggests"*) say "Self-test passed: $OUT";; *) warn "self-test got no answer from the scorer; see $BASE/logs/scorer.log";; esac
fi

cat <<DONE

$(printf '\033[1m')Installed claude-autoswitch $VERSION$(printf '\033[0m') in shadow mode: it logs what it would switch and changes nothing.

  autoswitch report     what it would have done on your real prompts (after some use)
  autoswitch on         switch for real   (also: off | shadow | hint | status)
  autoswitch speed      inline (recommended: right model on the first message, no stop, no wait),
                        predictive, instant (default on fresh installs) or seamless, see the README

To switch on, you need one Watcher chat (details below).

DONE
"$APP/bin/autoswitch" watcher
echo
echo "Restart Claude Code (or open a new chat) so it picks up the hook. Remove everything with ./uninstall.sh"
