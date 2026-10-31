"""Run under the Monitor tool. Prints one line per actionable request:
  REQUEST {"status":"ok","path":...,"session_id":...,"model":...,"set_model":true|false,"send":true|false,"message":"..."}
`send` is false, with no `message`, for a switch-only request (instant speed or a screenshot): the Watcher only
sets the model, and `finish.py settle` closes it as ok after the grace period. Same on macOS and Windows.
Costs no tokens while idle: a plain loop polling one small directory every 0.05 s (no file-watcher needed).
A request is claimed by moving it to working/, then prep.py runs in the background so a slow brand-new chat
never delays the next request.

Nothing may be lost silently: if prep.py crashes or prints nothing while the request is still in working/,
it is closed with `finish.py done error` (which leaves a note the user sees on their next prompt).
A sweeper does the same for any request left in working/ for more than 10 minutes.

Works on macOS and Windows (and anywhere else Python runs). hook/watch.sh is a one-line wrapper around this.
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
B = os.path.expanduser(os.environ.get("AUTOSWITCH_DIR") or "~/.autoswitch")
POLL = 0.05  # seconds
HEARTBEAT_EVERY = 50  # polls: about every 2.5 s
SWEEP_EVERY = int(os.environ.get("AUTOSWITCH_SWEEP_EVERY") or 600)  # polls: about every 30 s
SWEEP_AGE = 600  # seconds a request may sit in working/ before it is closed as an error
OK_PREFIX = '{"status": "ok"'
CLOSED = ('{"status": "stale"}', '{"status": "unresolved"}')  # already closed by prep.py

_out = threading.Lock()
_settles = []  # settle children started by this Watcher, reaped as new ones start


def p(*parts):
    return os.path.join(B, *parts)


def _handoff_on():
    v = os.environ.get("AUTOSWITCH_HANDOFF")
    if v is None:
        try:
            with open(p("handoff"), encoding="utf-8") as f:
                v = f.read()
        except OSError:
            v = ""
    return v.strip().lower() in ("on", "1", "true", "yes")


HO = _handoff_on()
WID = os.environ.get("AUTOSWITCH_WATCHER_ID") or os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or ""
TTL = float(os.environ.get("AUTOSWITCH_WATCHER_TTL") or 30)


def heartbeat():
    """The hook only blocks prompts while a Watcher is listening.
    v2 (AUTOSWITCH_HANDOFF on): the heartbeat carries this Watcher's session id, and a second Watcher that finds a
    fresh heartbeat from another id prints DUPLICATE and stops (WATCHER.md: do not re-arm). Off: a plain touch."""
    try:
        if not HO or not WID:
            with open(p("watcher.alive"), "a"):
                pass
            os.utime(p("watcher.alive"), None)
            return
        try:
            with open(p("watcher.alive"), encoding="utf-8") as f:
                other = f.read().strip()
            age = time.time() - os.path.getmtime(p("watcher.alive"))
        except OSError:
            other, age = "", TTL
        if other and other != WID and age < TTL:
            with _out:
                print("DUPLICATE " + other, flush=True)
            os._exit(0)
        tmp = p("watcher.alive.%d" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(WID)
        os.replace(tmp, p("watcher.alive"))
    except OSError:
        pass


def log(msg):
    """A line on stderr (the Monitor shows it) and in watch.log, which outlives the session."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    with _out:
        sys.stderr.write("autoswitch watch: " + msg + "\n")
        sys.stderr.flush()
    try:
        with open(p("watch.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(script, *args, timeout=None):
    return subprocess.run([sys.executable, os.path.join(HERE, script), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


RESTORABLE = ("ok", "refused", "error", "unresolved", "stale")


def close_restored(path):
    """A close that died mid-way already wrote its status: finish it with that status (so an ok close leaves
    no spurious "please resend" note and a refusal stays refused); anything unreadable or unknown closes as error."""
    try:
        with open(path, encoding="utf-8") as f:
            status = json.load(f).get("status")
    except (OSError, ValueError, AttributeError):
        status = None
    if status in RESTORABLE and status != "error":
        try:
            if run("finish.py", "done", path, status).returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    fail(path)


def fail(path):
    """Close a request that could not be handled."""
    if not os.path.exists(path):
        return
    try:
        if run("finish.py", "done", path, "error").returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.replace(path, p("done", os.path.basename(path) + ".bad"))
    except OSError:
        pass


def handle(path):
    try:
        r = run("prep.py", path)
    except (OSError, subprocess.SubprocessError):
        return fail(path)
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return fail(path)
    if out.startswith(OK_PREFIX):
        with _out:
            sys.stdout.write("REQUEST " + out + "\n")
            sys.stdout.flush()
        # the Watcher only reports failures; this closes the request once the re-sent prompt reached the hook.
        # Its own session / process group, so a Monitor that stops the Watcher does not take a pending settle with it.
        detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                  else {"start_new_session": True})
        try:
            settle = subprocess.Popen([sys.executable, os.path.join(HERE, "finish.py"), "settle", path],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      **detach)
            with _out:  # keep a handle so finished settles are reaped, not left to the garbage collector
                _settles[:] = [x for x in _settles if x.poll() is None] + [settle]
        except OSError as e:
            # not fatal (the sweep closes the request), but never silent: the request will sit in working/ for 10 min
            log("settle did not start for %s: %s" % (os.path.basename(path), e))
    elif out not in CLOSED:
        fail(path)


def pending():
    try:
        return sorted(f for f in os.listdir(p("pending")) if f.endswith(".json"))
    except OSError:
        return []


def sweep():
    cutoff = time.time() - SWEEP_AGE
    # pass tokens a timed-out settle kept for a late re-send: valid as long as a request may sit in working/
    try:
        for t in os.listdir(p("pass")):
            try:
                if os.path.getmtime(p("pass", t)) < cutoff:
                    os.remove(p("pass", t))
            except OSError:
                pass
    except OSError:
        pass
    try:
        names = [f for f in os.listdir(p("working")) if f.endswith(".json")]
    except OSError:
        return
    for f in names:
        path = p("working", f)
        try:
            if os.path.getmtime(path) < cutoff:
                heartbeat()  # each close may wait up to ~10 s: keep the hook from dropping to shadow
                fail(path)
        except OSError:
            pass
    # claim markers left by a close that died
    try:
        for f in os.listdir(p("done")):
            if f.endswith(".lock"):
                try:
                    if os.path.getmtime(p("done", f)) < cutoff:
                        os.remove(p("done", f))
                except OSError:
                    pass
    except OSError:
        pass
    # a save that died before its rename leaves a *.tmp beside its target: drop stale ones
    for d in ("working", "done"):
        try:
            for t in os.listdir(p(d)):
                try:
                    if t.endswith(".tmp") and os.path.getmtime(p(d, t)) < cutoff:
                        os.remove(p(d, t))
                except OSError:
                    pass
        except OSError:
            pass
    # a close that died between its two renames leaves done/<id>.json.<pid>.closing: put it back and close it
    try:
        stranded = [f for f in os.listdir(p("done")) if f.endswith(".closing") and ".json." in f]
    except OSError:
        stranded = []
    for f in stranded:
        src, dst = p("done", f), p("working", f.split(".json.", 1)[0] + ".json")
        try:
            if os.path.getmtime(src) < cutoff and not os.path.exists(dst):
                heartbeat()
                os.replace(src, dst)
                close_restored(dst)
        except OSError:
            pass
    heartbeat()


def main():
    for d in ("pending", "working", "done"):
        os.makedirs(p(d), exist_ok=True)
    heartbeat()
    n = 0
    while True:
        for f in pending():
            try:
                os.replace(p("pending", f), p("working", f))  # claim; fails if another Watcher got it first
            except OSError:
                continue
            threading.Thread(target=handle, args=(p("working", f),), daemon=True).start()
        n += 1
        if n % HEARTBEAT_EVERY == 0:
            heartbeat()
        if n % SWEEP_EVERY == 0:
            sweep()
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
