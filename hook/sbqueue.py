"""File queue shared by the hook (producer) and the Watcher (consumer).

Layout under $AUTOSWITCH_DIR (default ~/.autoswitch):
  enabled            flag file; the hook does nothing unless it exists
  pending/<id>.json  request written by the hook
  working/<id>.json  claimed by the Watcher (moved here by watch.py)
  done/<id>.json     finished or failed requests
  pass/<id>          loop guard: one-shot token the hook consumes
"""
import json
import os
import re
import time

BASE = os.path.expanduser(os.environ.get("AUTOSWITCH_DIR", "~/.autoswitch"))
MARK = "[autoswitch:"  # prefix the Watcher puts on a re-sent prompt: [autoswitch:<id>]


def path(*parts):
    return os.path.join(BASE, *parts)


def raw_mode():
    """The configured mode word: $AUTOSWITCH_MODE wins, then the `mode` file; the legacy
    `enabled` flag file means enforce. Defaults to `hint`."""
    m = os.environ.get("AUTOSWITCH_MODE")
    if not m:
        try:
            with open(path("mode"), encoding="utf-8") as f:
                m = f.read().strip()
        except OSError:
            m = "enforce" if os.path.exists(path("enabled")) else "hint"
    return m


def mode():
    """hint (internal name: "off") | shadow | enforce. Anything unknown is the hint-only mode."""
    m = raw_mode()
    return m if m in ("shadow", "enforce") else "off"


def silent():
    """True when the user ran `autoswitch off`: the hook does nothing at all."""
    return raw_mode() == "off"


def speed():
    """seamless (default): hold the prompt, switch, re-send it, so it answers on the new model.
    instant: never hold; the switch applies from the next message. $AUTOSWITCH_SPEED, then the `speed` file."""
    v = (os.environ.get("AUTOSWITCH_SPEED") or "").strip()
    if not v:
        try:
            with open(path("speed"), encoding="utf-8") as f:
                v = f.read().strip()
        except (OSError, UnicodeError, ValueError):
            v = ""  # unreadable or corrupt file: seamless
    return "instant" if v.lower() == "instant" else "seamless"


def enabled():
    return mode() == "enforce"


def watcher_alive():
    """True if a Watcher session is running: watch.py touches `watcher.alive` every few seconds.
    Without one, a blocked prompt would never be re-sent, so the hook must not block."""
    try:
        return time.time() - os.path.getmtime(path("watcher.alive")) < float(os.environ.get("AUTOSWITCH_WATCHER_TTL", "30"))
    except OSError:
        return False


def enqueue(app_session_id, target_model, prompt, label, confidence, cli_session_id=None, resend=True):
    import uuid
    rid = uuid.uuid4().hex[:12]
    for d in ("pending", "working", "done", "pass"):
        os.makedirs(path(d), exist_ok=True)
    req = {
        "id": rid,
        "session_id": app_session_id,
        "cli_session_id": cli_session_id,
        "model": target_model,
        "prompt": prompt,
        "label": label,
        "confidence": confidence,
        "ts": time.time(),
        "resend": resend,  # False: switch the model only (the prompt went through with its screenshot)
    }
    # the token lets exactly one re-sent prompt with this id pass the hook. It must exist before the request is
    # visible in pending/: settle reads "no token" as "the re-send arrived" and would close it ok at once
    tok = path("pass", rid)
    if resend:
        with open(tok, "w"):
            pass
    tmp = path("pending", rid + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f)
    try:
        os.replace(tmp, path("pending", rid + ".json"))  # atomic (also over an existing file on Windows): watcher only sees *.json
    except OSError:
        for f in (tok, tmp):
            if os.path.exists(f):
                os.remove(f)
        raise
    return rid


_WRAP = re.compile(r"<cross-session-message\b[^>]*>(.*?)</cross-session-message>", re.S)
_MARKER = re.compile(r"^\s*" + re.escape(MARK) + r"[A-Za-z0-9]*\]\s?")


def unwrap(prompt):
    """The user text inside a send_message wrapper (tags, "From ..." labels), else prompt unchanged."""
    m = _WRAP.search(prompt)
    return m.group(1).strip() if m else prompt


def strip_marker(prompt):
    return _MARKER.sub("", prompt, count=1)


_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S)


def strip_reminders(prompt):
    """Drop app-injected <system-reminder> blocks (an unclosed one runs to the end)."""
    return _REMINDER.sub("", prompt).strip()


def bare(prompt):
    """What the user actually asked: wrapper, reminders and loop-guard marker removed.
    Score and queue this. Empty string means nothing of the user's is left."""
    return strip_reminders(strip_marker(strip_reminders(unwrap(prompt))))


def consume_pass(prompt):
    """The request id (and the token is spent) if prompt carries a valid [autoswitch:<id>] marker, wrapped or not."""
    p = unwrap(prompt).lstrip()
    if not p.startswith(MARK):
        return None
    rid = p[len(MARK):].split("]", 1)[0]
    tok = path("pass", rid)
    if rid.isalnum() and os.path.exists(tok):
        os.remove(tok)
        return rid
    return None


def take_lost(cli_session_id, rid=None):
    """The earlier prompt a failed switch never re-sent for this chat (read once, then removed), or None.
    rid: only take the note left by that request; another request's note in the same chat is kept."""
    if not cli_session_id or not str(cli_session_id).replace("-", "").isalnum():
        return None
    p = path("lost", cli_session_id + ".json")
    try:
        with open(p, encoding="utf-8") as f:
            note = json.load(f)
        if rid is not None and note.get("id") not in (None, rid):
            return None
        prompt = note["prompt"]
        os.remove(p)
        return prompt
    except (OSError, ValueError, KeyError, AttributeError):
        return None
