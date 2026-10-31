"""Switch while you type (speed `predictive`, macOS). The model of a message is fixed the moment it is sent, and a
hook that holds the message can't change it (tested: a switch landing while the hook waited still sent on the old
model). So the switch has to happen before Enter. The typeahead reader (typeahead.swift) watches the Code tab's
prompt box and, each time the draft settles, runs:

  typeahead.py draft    stdin {"session_id": "<app chat id>", "text": "...", "title": "..."}: decide like the hook,
                        and queue a switch-only request (resend false) for the Watcher if the chat should change model
  typeahead.py spawn    start the reader detached if speed is predictive (or inline) and it is not running (watch.py)

The decision is the hook's own (hint.py with AUTOSWITCH_DRY=1), so drafts and sent prompts follow the same rules:
budget mode, context blend, pins, dwell guard, handoffs. A draft never starts a handoff or a rollover, never counts
toward the lasting-switch rule and is never re-sent: at submit the hook runs as usual and only queues a switch if
the draft's switch did not happen. Nothing here can stop or delay a message.
"""
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402
import sbqueue as sb  # noqa: E402

SESSIONS = os.path.expanduser(os.environ.get("AUTOSWITCH_SESSIONS") or paths.sessions_dir())
PROJECTS = os.path.expanduser(os.environ.get("AUTOSWITCH_PROJECTS") or "~/.claude/projects")
MIN_CHARS = int(os.environ.get("AUTOSWITCH_TYPEAHEAD_MIN") or "3")


def log(rec):
    try:
        os.makedirs(sb.BASE, exist_ok=True)
        with open(sb.path("typeahead.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), **rec}) + "\n")
    except OSError:
        pass


def app_record(app_id):
    """The app's record for a chat id (cliSessionId, model, title), or None for a chat with no record yet."""
    for p in glob.glob(os.path.join(SESSIONS, "*", "*", app_id + ".json")):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("sessionId") == app_id:
            return d
    return None


def transcript(cli_id):
    found = glob.glob(os.path.join(PROJECTS, "*", cli_id + ".jsonl"))
    return found[0] if found else None


def decide(app_id, rec, text):
    """The hook's decision for this draft, as hint.py prints it in dry mode, or None."""
    env = {**os.environ, "AUTOSWITCH_DRY": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
           "CLAUDE_CODE_HOST_SESSION_ID": app_id}
    hook = {"prompt": text, "session_id": rec["cliSessionId"], "transcript_path": transcript(rec["cliSessionId"]),
            "cwd": rec.get("cwd")}
    try:
        out = subprocess.run([sys.executable, os.path.join(HERE, "hint.py")], input=json.dumps(hook),
                             capture_output=True, text=True, encoding="utf-8", env=env, timeout=5).stdout.strip()
        return json.loads(out.splitlines()[-1]) if out else None
    except (subprocess.SubprocessError, ValueError, OSError, IndexError):
        return None


def draft(app_id, text, title=None):
    """Returns what was done, for the log and the tests."""
    text = (text or "").strip()
    dry = os.environ.get("AUTOSWITCH_TYPEAHEAD_DRY") == "1"  # log what it would do, queue nothing
    if not dry and (sb.speed() not in ("predictive", "inline") or sb.mode() != "enforce"):
        return "off"
    if not app_id or len(text) < MIN_CHARS or text.startswith("/"):
        return "skip"
    if not sb.watcher_alive():
        return "no watcher"
    rec = app_record(app_id)
    if not rec or not rec.get("cliSessionId"):
        return "no record"  # a brand-new chat: the hook handles its first message
    if sb.speed() == "inline" and sb.inline_marked(rec["cliSessionId"]):
        return "off"  # the plugin picks this chat's model at Enter: nothing to do while typing
    if title and rec.get("title") and title.strip() != rec["title"].strip():
        return "title mismatch"  # the open chat and the record disagree: never switch the wrong chat
    d = decide(app_id, rec, text)
    if not d:
        return "no decision"
    tgt = d.get("target")
    if dry:
        return "would %s (%s)" % ("queue " + tgt if tgt and not d.get("screenshot") and not d.get("handoff_to") else "stay",
                                  d.get("reason"))
    if d.get("screenshot") or d.get("handoff_to") or not tgt:
        sb.drop_typeahead(app_id)  # the draft no longer wants an earlier draft's switch
        return "stay"
    if sb.open_switch(app_id) == tgt:
        return "already queued"
    sb.drop_typeahead(app_id)
    sb.enqueue(app_id, tgt, text[:2000], d.get("label"), d.get("confidence"), cli_session_id=rec["cliSessionId"],
               resend=False, typeahead=True)
    return "queued " + tgt


def spawn():
    """Start the reader (the typeahead binary next to clipimg) detached, once. It exits by itself when the
    Watcher's heartbeat goes stale, so it lives as long as a Watcher does. macOS only."""
    if sys.platform != "darwin":
        return "not macOS"
    if sb.speed() not in ("predictive", "inline"):  # inline: still needed for chats opened before the plugin
        return "off"
    pid_path = sb.path("typeahead.pid")
    try:
        with open(pid_path, encoding="utf-8") as f:
            os.kill(int(f.read().strip()), 0)
        return "running"
    except (OSError, ValueError):
        pass
    lock = sb.path("typeahead.lock")  # watch.py may call spawn again while a first build is still compiling
    try:
        if time.time() - os.path.getmtime(lock) < 180:
            return "busy"
    except OSError:
        pass
    os.makedirs(sb.BASE, exist_ok=True)
    with open(lock, "w"):
        pass
    try:
        return _start()
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _start():
    pid_path = sb.path("typeahead.pid")
    binary = os.environ.get("AUTOSWITCH_TYPEAHEAD_BIN") or sb.path("bin", "typeahead")
    src = os.path.join(HERE, "typeahead.swift")
    if not os.path.exists(binary) or os.path.getmtime(binary) < os.path.getmtime(src):
        os.makedirs(os.path.dirname(binary), exist_ok=True)
        try:
            r = subprocess.run(["swiftc", "-O", src, "-o", binary], capture_output=True, text=True)
        except OSError as e:
            log({"spawn": "build failed", "err": repr(e)})
            return "build failed"
        if r.returncode != 0:
            log({"spawn": "build failed", "err": r.stderr[-500:]})
            return "build failed"
    with open(sb.path("typeahead.err"), "a") as err:
        p = subprocess.Popen([binary, sys.executable, os.path.abspath(__file__)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
    with open(pid_path, "w", encoding="utf-8") as f:
        f.write(str(p.pid))
    return "started %d" % p.pid


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "draft":
            sys.stdin.reconfigure(encoding="utf-8")
            req = json.load(sys.stdin)
            t0 = time.time()
            what = draft(req.get("session_id"), req.get("text"), req.get("title"))
            log({"session": req.get("session_id"), "chars": len(req.get("text") or ""), "did": what,
                 "ms": int((time.time() - t0) * 1000)})
        elif cmd == "spawn":
            print(spawn())
    except Exception as e:  # never let a draft break anything
        log({"error": repr(e)})
    sys.exit(0)
