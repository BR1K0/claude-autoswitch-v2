"""One call that does everything the Watcher needs before it touches the chat.

  prep.py <working/req.json>

Resolves the chat (waits for a brand-new chat's record), builds the re-send text, drops stale
requests, and prints one compact JSON line:
  {"status": "ok", "path", "session_id", "model", "set_model", "send", "message"}
`set_model` is false when the chat is already on `model`: only send_message is needed.
`send` is false (and there is no `message`) when the prompt carried a screenshot and already went through:
the Watcher only switches the model, for the next turn.
A request that cannot be acted on is closed here with `done` and prints {"status": "stale"} or
{"status": "unresolved"}, so watch.sh can tell that apart from a crash (non-zero exit / no output).
Output is pure ASCII (non-ASCII escaped) so one event line carries any prompt byte-for-byte.
"""
import json
import sys
import time

import finish

STALE_SECONDS = 600


def prep(req_path):
    req = finish.load(req_path)
    if time.time() - req.get("ts", 0) > STALE_SECONDS:
        finish.mark_done(req_path, req, "stale")
        return {"status": "stale"}
    finish.resolve_target(req_path, req)
    if not req.get("session_id"):
        finish.mark_done(req_path, req, "unresolved")
        return {"status": "unresolved"}
    import pins
    pins.record_watcher_set(req["session_id"], req["model"], req.get("id"))  # before the set, so the Watcher's own change is never a hand change
    out = {"status": "ok", "path": req_path, "session_id": req["session_id"], "model": req["model"],
           "set_model": req.get("current_model") != req["model"], "send": req.get("resend", True)}
    if out["send"]:
        out["message"] = finish.text(req)
    return out


if __name__ == "__main__":
    out = prep(sys.argv[1])
    if out:
        print(json.dumps(out))
