"""Claude Code hook entry point: capture events as they happen, no model calls.

Register for Stop, PreCompact and SessionEnd (and optionally PostToolUse). Claude
Code passes JSON on stdin with transcript_path and cwd; ingest is idempotent, so
running it often only adds the new events. Failures are logged, never raised:
a hook must not break the user's session.

Store location: $ACPMH_STORE, else ~/.acpmh/<project folder name>.
"""
import json
import os
import sys
import time

from . import gitstate, ingest
from .store import Store


def store_for(cwd):
    root = os.environ.get("ACPMH_STORE") or os.path.join(
        os.path.expanduser("~/.acpmh"), os.path.basename(os.path.abspath(cwd)) or "default")
    return Store(root)


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    cwd = data.get("cwd") or os.getcwd()
    try:
        store = store_for(cwd)
    except Exception as e:  # cannot even open the store: report to stderr only
        sys.stderr.write("acpmh: cannot open store: %r\n" % e)
        return 0
    try:
        path = data.get("transcript_path")
        if path and os.path.exists(path):
            n = ingest.ingest(store, ingest.load(path))
        else:
            n = 0
        gitstate.record(store, cwd)
        if data.get("hook_event_name") == "PreCompact":
            store.append("precompact:%s:%s" % (data.get("session_id"), time.time()),
                         "compaction", "observed", "Context compaction started (%s)"
                         % data.get("trigger", "?"))
        sys.stderr.write("acpmh: +%d events\n" % n)
    except Exception as e:
        with store.db:
            store.db.execute("INSERT INTO capture_failures (ts,source_key,error,payload) "
                             "VALUES (?,?,?,?)", (time.time(), "hook", repr(e),
                                                  json.dumps(data)[:4000]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
