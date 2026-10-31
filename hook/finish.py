"""Watcher helpers.

  finish.py target <req.json>          print the app session id to switch (waits for a new chat's record)
  finish.py text <req.json>            print "[autoswitch:<id>] <prompt>" to send_message
  finish.py settle <req.json>          close the request once the re-sent prompt reached the hook (run by watch)
  finish.py done <req.json> [status]   move working/<id>.json to done/ (status: ok|refused|error|unresolved|stale)
prep.py uses the same functions in one call; these CLI forms stay for manual use.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbqueue as q

WAIT_BUDGET = 60  # seconds a new chat's record may take to appear, counted from when the request was queued


def load(req_path, tries=40):
    for i in range(tries):
        try:
            with open(req_path, encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:  # Windows: another closer is renaming it right now; retry for up to ~2 s
            if i == tries - 1:
                raise
            time.sleep(0.05)


def save(req_path, req, tries=40):
    """Write atomically: a temp file in the same dir, then os.replace() onto the name, so a reader
    never sees a half-written file (another closer may have it open)."""
    tmp = "%s.%d.tmp" % (req_path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f)
    for i in range(tries):
        try:
            os.replace(tmp, req_path)
            return
        except PermissionError:  # Windows: a reader has it open right now; retry for up to ~2 s
            if i == tries - 1:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.05)


def resolve_target(req_path, req):
    """Fill req["session_id"] (and req["current_model"]) from the app record. Returns the app record or None."""
    from resolve import resolve

    # the budget runs from when the request was queued, so a request that waited behind
    # another unresolved one does not wait a second full minute
    # the app id may already be known from the environment; then only the current model is wanted,
    # and it is a bonus: never wait for it
    wait = 0 if req.get("session_id") else max(3, min(WAIT_BUDGET, req.get("ts", 0) + WAIT_BUDGET - time.time()))
    info = resolve(req.get("cli_session_id"), wait=wait) if req.get("cli_session_id") else None
    if info:
        req["session_id"] = info["sessionId"]
        req["current_model"] = info["model"]
        save(req_path, req)
    return info


def text(req):
    return f"{q.MARK}{req['id']}] {req['prompt']}"


def _claim(src, dst, tries=40):
    """Atomically take src by renaming it to dst. False if another process took it first (it is gone).
    Windows: os.replace raises PermissionError while another process has the file open (the Watcher's `done`,
    an antivirus scan); retry for up to ~2 s, then give up rather than overwrite."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            if i == tries - 1:
                return False
            time.sleep(0.05)
    return False


def _lock(dest):
    """Exclusive claim marker done/<id>.json.lock (O_EXCL). The rename alone is not exclusive on Windows:
    two processes renaming the same file at once can both succeed, the second moving it from the first's name."""
    try:
        os.close(os.open(dest + ".lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except (FileExistsError, PermissionError):  # PermissionError: Windows, a lock being deleted right now
        return False


def mark_done(req_path, req, status="ok", keep_token=False):
    os.makedirs(q.path("done"), exist_ok=True)
    dest = q.path("done", os.path.basename(req_path))
    if not _lock(dest):
        return False  # another closer is at work; the caller retries or loses, as for a failed claim
    try:
        return _mark_done(req_path, req, status, keep_token)
    finally:
        try:
            os.remove(dest + ".lock")
        except OSError:
            pass


def _mark_done(req_path, req, status="ok", keep_token=False):
    """Close a request: claim it first (atomic rename), then write the status, then publish it in done/.
    Returns False, changing nothing, if another closer (settle or the Watcher's `done`) got there first.
    keep_token: a settle timeout keeps the pass token (removed by the sweeper) so a late re-send still passes once."""
    dest = q.path("done", os.path.basename(req_path))
    claimed = dest + ".%d.closing" % os.getpid()
    if not _claim(req_path, claimed):
        return False
    was_ok = req.get("status") == "ok"
    req["status"] = status
    if req["status"] == "ok" and not req.get("session_id"):
        req["status"] = "error"  # never claim success without a chat id
    import pins
    if req["status"] == "ok":
        pins.record_watcher_set(req["session_id"], req["model"], req.get("id"))  # lets the hook spot later hand changes
    elif was_ok and req.get("session_id"):
        pins.clear_watcher_set(req["session_id"], req.get("model"), req.get("id"), req.get("ts"))  # a refusal after "ok": the Watcher did not set the model
    save(claimed, req)
    if not _claim(claimed, dest):
        # left as done/<id>.json.<pid>.closing: the sweep restores and closes it once it is old. Not put back into
        # working/ here: a settle still polling that path would then close it again with its stale status.
        return False
    # a request that never got re-sent must not leave a live pass token (a timed-out settle keeps it, see above)
    if req["status"] != "ok" and not keep_token:
        tok = q.path("pass", req["id"])
        if os.path.exists(tok):
            os.remove(tok)
    if req["status"] != "ok":
        if req["status"] in ("error", "unresolved", "lost") and req.get("cli_session_id"):
            # the user was told "switching" but nothing was sent: the hook shows this on their next prompt
            os.makedirs(q.path("lost"), exist_ok=True)
            with open(q.path("lost", req["cli_session_id"] + ".json"), "w", encoding="utf-8") as f:
                json.dump({"prompt": req["prompt"][:500], "ts": time.time(), "id": req.get("id")}, f)
    return True


SETTLE_WAIT = float(os.environ.get("AUTOSWITCH_SETTLE_WAIT", "120"))  # seconds the re-sent prompt may take to reach the hook (a busy chat queues it)
SETTLE_GRACE = float(os.environ.get("AUTOSWITCH_SETTLE_GRACE", "30"))  # no-resend requests: time for a refusal to be reported (a Haiku turn can take >5 s)


def settle(req_path):
    """Close a request the Watcher no longer closes itself (it only reports failures, via `done refused`).
    The hook spends the pass token when the re-sent prompt arrives, which is the proof the send worked.
    No token spent within SETTLE_WAIT = nothing arrived yet: close as error, which leaves the "please resend" note,
    but keep the token until the sweep, so a late re-send still passes the hook once (and clears the note).
    If the Watcher closed the request first (a refusal), do nothing: the atomic claim in mark_done loses."""
    try:
        req = load(req_path)
    except (OSError, ValueError):
        return
    if not isinstance(req, dict):
        return
    tok = q.path("pass", str(req.get("id")))
    end = time.time() + (SETTLE_WAIT if req.get("resend", True) else SETTLE_GRACE)
    while os.path.exists(req_path):
        if req.get("resend", True) and not os.path.exists(tok):
            return mark_done(req_path, req, "ok")
        if time.time() >= end:
            resend = req.get("resend", True)
            return mark_done(req_path, req, "error" if resend else "ok", keep_token=resend)
        time.sleep(0.2)


_FIELD = r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"'


def salvage(req_path):
    """For a request file that is not valid JSON: pull out the fields the lost note needs.
    Returns {"cli_session_id", "prompt"} (prompt may be "") or None if no usable chat id."""
    import re
    try:
        with open(req_path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return None
    cli = re.search(_FIELD % "cli_session_id", raw)
    if not cli or not cli.group(1).replace("-", "").isalnum():
        return None
    m = re.search(_FIELD % "prompt", raw)
    try:
        prompt = json.loads('"' + m.group(1) + '"') if m else ""
    except ValueError:
        prompt = m.group(1)
    return {"cli_session_id": cli.group(1), "prompt": prompt}


def done_unparseable(req_path):
    """`done` on a file that will not load: still leave the lost note when the chat id is readable,
    then keep the file as done/<name>.bad. Returns True if a note was written."""
    info = salvage(req_path)
    if info:
        os.makedirs(q.path("lost"), exist_ok=True)
        with open(q.path("lost", info["cli_session_id"] + ".json"), "w", encoding="utf-8") as f:
            json.dump({"prompt": info["prompt"][:500] or "(prompt unreadable)", "ts": time.time()}, f)
    os.makedirs(q.path("done"), exist_ok=True)
    os.replace(req_path, q.path("done", os.path.basename(req_path) + ".bad"))
    return bool(info)


if __name__ == "__main__":
    cmd, req_path = sys.argv[1], sys.argv[2]
    if cmd == "settle":
        settle(req_path)
        sys.exit(0)
    pre = None
    if cmd == "done":
        # settle may be closing it right now (held as done/<id>.json.<pid>.closing): wait until it is readable in
        # working/ or has landed in done/
        moved = q.path("done", os.path.basename(req_path))
        deadline = time.time() + 10
        while time.time() < deadline:
            src = req_path if os.path.exists(req_path) else moved if os.path.exists(moved) else None
            if src:
                try:
                    pre = load(src)  # kept: settle may move the file again before the load below
                    req_path = src
                    break
                except FileNotFoundError:
                    pass
                except (OSError, ValueError):
                    req_path = src  # unreadable for another reason: handled below
                    break
            time.sleep(0.05)
    try:
        req = pre if cmd == "done" and pre is not None else load(req_path)
        if not isinstance(req, dict):  # valid JSON but not an object (e.g. []): treat as unparseable
            raise ValueError("request is not a JSON object")
    except ValueError:
        if cmd != "done":
            raise
        done_unparseable(req_path)
        sys.exit(0)
    if cmd == "target":
        if not req.get("session_id"):
            resolve_target(req_path, req)
        print(req.get("session_id") or "UNRESOLVED")
    elif cmd == "text":
        print(text(req))
    elif cmd == "done":
        status = sys.argv[3] if len(sys.argv) > 3 else "ok"
        moved = q.path("done", os.path.basename(req_path))
        deadline = time.time() + 10
        # settle may be closing the same request right now: it holds it under done/<id>.json.<pid>.closing.
        # Wait for it to land in done/, then re-close that copy, so a refusal is never lost to the race.
        closed = mark_done(req_path, req, status)
        while not closed and time.time() < deadline:
            time.sleep(0.05)
            if os.path.exists(moved):
                req_path = moved
                try:
                    req = load(req_path)
                except (OSError, ValueError):
                    continue  # never re-close with a stale copy; try again
            closed = mark_done(req_path, req, status)
        if not closed:
            sys.exit("could not close %s as %s: another process holds it" % (os.path.basename(req_path), status))
