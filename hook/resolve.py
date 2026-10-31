"""Map a hook's CLI session_id to the app's session record (id + current model).

The hook sees a CLI uuid; the session tools want the app id (local_<uuid>).
The app keeps one JSON per session with both, under claude-code-sessions/.
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

SESSIONS = os.path.expanduser(os.environ.get("AUTOSWITCH_SESSIONS") or paths.sessions_dir())


def resolve(cli_session_id, wait=2.5):
    """Return {"sessionId", "model", "title"} for the CLI uuid, or None.

    A brand-new chat's record can land a moment after its first prompt fires the hook,
    so retry briefly before giving up.
    """
    deadline = time.monotonic() + wait
    while True:
        found = _scan(cli_session_id)
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(0.25)


def _scan(cli_session_id):
    for path in glob.glob(os.path.join(SESSIONS, "*", "*", "local_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("cliSessionId") == cli_session_id:
            return {"sessionId": d["sessionId"], "model": d.get("model"), "title": d.get("title")}
    return None


def record(cli_session_id):
    """The whole app record for the CLI uuid (effort, permissionMode, connectors, added folders...), or None.
    Autoswitch v2 only: what part 2 can inherit."""
    for path in glob.glob(os.path.join(SESSIONS, "*", "*", "local_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("cliSessionId") == cli_session_id:
            return d
    return None


if __name__ == "__main__":
    print(json.dumps(resolve(sys.argv[1])))
