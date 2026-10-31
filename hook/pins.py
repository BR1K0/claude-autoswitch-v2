"""Pin wins: leave a chat alone if its model was picked by hand.

Three signals:
1. PostModelSwitch hook (modelswitch.py): source "command" (/model) or "picker" (model menu) = by hand;
   "sdk" = a tool such as set_session_model. Recorded per CLI session in $AUTOSWITCH_DIR/pins/.
   (Not seen to fire for the desktop picker or /model in CLI 2.1.309 tests, so it is not relied on alone.)
2. Transcript: every model change, the Watcher's set_session_model included, leaves a timestamped
   "<command-name>/model" entry in the chat's transcript. The Watcher records when it finished a set
   (lastset, `ts`). A /model entry more than GRACE seconds newer than that record was made by hand.
3. Model difference: the current model differs from the Watcher's last set (no transcript needed).
4. observe(): the model seen on the previous prompt differs from this one and from the Watcher's last set. Pins the chat
   until `unpin`.
Not detectable: the model a chat started on; a hand change within GRACE seconds after a Watcher set;
and a hand pick that equals the Watcher's last set when the transcript is unavailable.
"""
import datetime
import json
import os
import time

import sbqueue as sb

HAND = ("command", "picker")
GRACE = float(os.environ.get("AUTOSWITCH_PIN_GRACE", "60"))  # seconds: covers the Watcher's own /model entry
TAIL_BYTES = 4 * 1024 * 1024  # only the end of a transcript matters
MODEL_CMD = "<command-name>/model</command-name>"


def _p(kind, key):
    return sb.path(kind, key + ".json")


def _write(kind, key, data):
    os.makedirs(sb.path(kind), exist_ok=True)
    with open(_p(kind, key), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _read(kind, key):
    try:
        with open(_p(kind, key), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def record_switch(hook):
    """From a PostModelSwitch hook payload."""
    _write("pins", hook["session_id"], {"model": hook.get("to_model"), "source": hook.get("source"),
                                         "hand": hook.get("source") in HAND})


def record_watcher_set(app_session_id, model, rid=None):
    rec = {"model": model, "ts": time.time()}
    if rid:
        rec["id"] = rid  # which request set it, so a late refusal of an older request can't clear a newer record
    _write("lastset", app_session_id, rec)


def clear_watcher_set(app_session_id, model=None, rid=None, since=None):
    """Undo record_watcher_set: the Watcher reported it did not set the model after all.
    Only the record that request wrote is removed: with `rid`, the stored id must match (a record without an id
    must match `model` and be no older than `since`, the request's ts). A newer switch's record is kept."""
    if rid is not None or model is not None:
        last = _read("lastset", app_session_id)
        if not isinstance(last, dict):
            return
        if last.get("id") is not None and rid is not None:
            if last["id"] != rid:
                return
        elif model is not None and (last.get("model") != model or (since is not None and last.get("ts", 0) < since)):
            return
    try:
        os.remove(_p("lastset", app_session_id))
    except OSError:
        pass


def last_model_command(transcript_path):
    """Epoch time of the newest "/model" command entry in the transcript's tail, or None."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    newest = None
    for line in tail.splitlines():
        if MODEL_CMD not in line:
            continue
        try:
            ts = json.loads(line).get("timestamp")
        except ValueError:
            continue
        if not ts:
            continue  # the untimed caveat twin of the same command
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        newest = t if newest is None else max(newest, t)
    return newest


def is_manual(app_session_id):
    return _read("pinned", app_session_id) is not None


def pin(app_session_id, model, why):
    _write("pinned", app_session_id, {"model": model, "why": why, "ts": time.time()})


def observe(app_session_id, current_model):
    """Called on every prompt with the chat's current model. A model change nobody here made pins the chat:
    it differs from the model the hook saw last time AND from the model the Watcher last asked for.
    The Watcher's own switch passes because prep.py records its target in lastset before the set."""
    if not current_model:
        return
    seen = _read("seen", app_session_id)
    last = _read("lastset", app_session_id) or {}
    if seen and seen.get("model") != current_model and current_model != last.get("model"):
        pin(app_session_id, current_model, f"hand change: {seen.get('model')} -> {current_model}")
    _write("seen", app_session_id, {"model": current_model, "ts": time.time()})


def unpin(app_session_id=None):
    """Clear the pin of one chat, or of all chats when app_session_id is None. Returns how many were cleared."""
    if app_session_id:
        keys = [app_session_id]
    else:
        d = sb.path("pinned")
        keys = [f[:-5] for f in os.listdir(d) if f.endswith(".json")] if os.path.isdir(d) else []
    n = 0
    for k in keys:
        try:
            os.remove(_p("pinned", k))
            n += 1
        except OSError:
            pass
    return n


def is_pinned(cli_session_id, app_session_id, current_model, transcript_path=None):
    if is_manual(app_session_id):
        return True  # a hand change seen by observe(); stays until unpin
    rec = _read("pins", cli_session_id)
    if rec and rec.get("hand") and rec.get("model") == current_model:
        return True  # the PostModelSwitch hook saw a hand pick (when it fires at all)
    # a non-hand hook record (the Watcher's own sdk switch) says nothing about later /model commands
    last = _read("lastset", app_session_id)
    if last and last.get("model") != current_model:
        return True
    hand_at = last_model_command(transcript_path)
    if hand_at is None:
        return False
    if last is None:
        return True  # a /model command with no Watcher record at all: someone else picked it
    return hand_at > _set_time(app_session_id, last) + GRACE


def _set_time(app_session_id, last):
    """When the Watcher last set this chat. A lastset record from before `ts` existed uses the file's mtime."""
    if "ts" in last:
        return last["ts"]
    return os.path.getmtime(_p("lastset", app_session_id))
