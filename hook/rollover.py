"""Autoswitch v2, Step 4: automatic rollover of a long chat into part 2 (same model, or the model a lasting
switch wants), including overnight work. Off by default.

AUTOSWITCH_ROLLOVER, else the file $AUTOSWITCH_DIR/rollover: `on` turns it on; anything else, or nothing,
is PR #40's behaviour exactly. It also needs AUTOSWITCH_HANDOFF on (the tag delivery, sizes and settings
carry-over are #40's) and AUTOSWITCH_MODE enforce (shadow only logs `would_rollover`).

Triggers (real tokens from sizes.py, all configurable):
  prompt  The user sends a message in a chat over AUTOSWITCH_ROLLOVER_AT (200k). The hook lets it through with an
          instruction, like #40's handoff; part 2 gets the message with the one-time tag. If a lasting model
          switch is due at the same time, part 2 starts on that model: both in one move.
  stop    the agent ends a turn in a chat over the threshold (the Stop hook, stop.py). The hook blocks the
          stop once with an instruction to roll over. If start_session is refused (the turn was not typed by
          the user, Step 2.6), the chat records "deferred" and stops; the rollover then runs on the user's next message,
          and auto-compaction stays the safety net meanwhile. Never two blocked stops in a row for one chat.
  early   the tokens-per-turn growth of the last AUTOSWITCH_ROLLOVER_EARLY_TURNS (4) typed turns predicts
          crossing the threshold within the next turn (chat already over AUTOSWITCH_ROLLOVER_EARLY_FLOOR, 0.6
          of the threshold). AUTOSWITCH_ROLLOVER_EARLY=off disables it.
Guards (all must hold): a quiet moment (no tool call without a result, no failed Edit/Write left unfixed, no
linked session mid-task, no background command running: those have no recreate recipe; a failed Edit or a
background command older than AUTOSWITCH_ROLLOVER_GUARD_EXPIRE, 30 min, no longer blocks, and the reason is
kept in state.json as guards_expired), not the Watcher's own chat (it owns the heartbeat, or runs a watch.sh
Monitor: a Watcher is never moved as a job), no rollover of this chat in the last AUTOSWITCH_ROLLOVER_GAP
(100k) tokens, no open handoff or rollover, and the saving beats the handoff's cost (#40's re-cache figures,
see worth()).

On disk, per rollover, $AUTOSWITCH_DIR/rollovers/<id>/ (plural: `rollover` is the on/off file):
  state.json   status + a timestamp for every step reached:
               created -> built -> started -> delivered -> confirmed -> linked [-> shown] [-> archived]
               off the path: deferred, superseded, failed (with the step and why)
  jobs.json    each cron job, monitor, loop or linked session of part 1: kind, recreate recipe, one-time id,
               state part1 -> moving -> created -> part2 (or kept: stays with part 1 until it finishes)
  handoff.md   the ACPMH handoff plus the current task and the jobs
Every hook run (prompt and stop) calls recover(): a rollover started but not delivered within
AUTOSWITCH_ROLLOVER_CONFIRM_WAIT (600 s) is failed (part 1 keeps the work and its jobs, a one-line note);
delivered but never confirmed is confirmed with the unmoved jobs kept by part 1. A late copy of a failed
rollover's first message is stopped in part 2, so two agents never do the same work, and part 1 of a
confirmed rollover is told on every turn that the chat continued in part N.

CLI (run by the chats, see instruction() and part2_prompt()):
  python3 rollover.py build|started|deferred|fail|wait|jobs|job-created|job-deleted|confirm|linked|shown|archive
"""
import contextlib
import datetime
import fcntl
import glob
import json
import os
import re
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sbqueue as sb  # noqa: E402
from policy import _f, cfg, tier_of  # noqa: E402

TAG = "[autoswitch-rollover:"
_TAG = re.compile(r"\[autoswitch-rollover:([0-9a-f]{12})\]")
_RID = re.compile(r"^[0-9a-f]{12}$")
_SID = re.compile(r"session_id: (local_[0-9a-f-]{36})")
OPEN = ("created", "built", "started")             # part 2 does not have the work yet
LIVE = OPEN + ("delivered",)                      # not settled
CONTINUED = ("confirmed", "linked", "shown", "archived")
STEPS = ("created", "built", "started", "delivered", "confirmed", "linked", "shown", "archived")
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def enabled():
    v = os.environ.get("AUTOSWITCH_ROLLOVER")
    if v is None:
        try:
            with open(sb.path("rollover")) as f:
                v = f.read().strip()
        except OSError:
            v = ""
    return v.strip().lower() in ("on", "1", "true", "yes")


def active():
    """Rollover on, on top of #40's handoff (whose tag delivery and settings carry-over it reuses)."""
    if not enabled():
        return False
    import handoff
    return handoff.enabled()


def threshold():
    return _f("AUTOSWITCH_ROLLOVER_AT", "200000")


def gap():
    return _f("AUTOSWITCH_ROLLOVER_GAP", "100000")


def confirm_wait():
    return _f("AUTOSWITCH_ROLLOVER_CONFIRM_WAIT", "600")


def host_session_id():
    sid = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID", "")
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop" and \
            re.match(r"^local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", sid):
        return sid
    return None


# ---------- size and growth ----------

def measure(transcript):
    """{"tokens", "method", "marks": [tokens at the start of each typed user message]}. A file that can't hold
    AUTOSWITCH_ROLLOVER_EARLY_FLOOR x threshold real tokens even at one character per byte is not counted."""
    import sizes
    try:
        size = os.path.getsize(transcript)
    except (OSError, TypeError, ValueError):
        return {"tokens": None, "method": "unknown", "marks": []}
    if size / 4.0 * sizes.factor() < threshold() * _f("AUTOSWITCH_ROLLOVER_EARLY_FLOOR", "0.6"):
        return {"tokens": sizes.cheap(transcript), "method": "cheap", "marks": []}
    marks = []
    r = sizes.text_chars(transcript, marks)
    if r is None:
        return {"tokens": None, "method": "unknown", "marks": []}
    img = _f("AUTOSWITCH_IMAGE_TOKENS", "1500")
    tok = lambda c, i: c / 4.0 * sizes.factor() + i * img  # noqa: E731
    return {"tokens": tok(*r), "method": "precise", "marks": [tok(c, i) for c, i in marks]}


def growth(m):
    """Average real tokens per typed turn over the last AUTOSWITCH_ROLLOVER_EARLY_TURNS turns, or None."""
    n = int(_f("AUTOSWITCH_ROLLOVER_EARLY_TURNS", "4"))
    pts = (m.get("marks") or []) + ([m["tokens"]] if m.get("tokens") is not None else [])
    pts = pts[-(n + 1):]
    if len(pts) < 3:
        return None
    return max(0.0, (pts[-1] - pts[0]) / (len(pts) - 1))


def trigger(m, deferred=False):
    """'size' | 'early' | 'deferred' | None."""
    t = m.get("tokens")
    if t is not None and m.get("method") == "precise" and t >= threshold():
        return "size"
    if t is not None and m.get("method") == "precise" and \
            os.environ.get("AUTOSWITCH_ROLLOVER_EARLY", "on").strip().lower() != "off" and \
            t >= threshold() * _f("AUTOSWITCH_ROLLOVER_EARLY_FLOOR", "0.6"):
        g = growth(m)
        if g is not None and t + g >= threshold():
            return "early"
    if deferred:
        return "deferred"
    return None


def worth(cur, tgt, tokens):
    """(ok, why). #40's cost figures (policy.cfg: re-cache per 50k on each model, cached reads = READ_FRACTION
    of a write). Cost: part 2 caches the handoff (AUTOSWITCH_ROLLOVER_HANDOFF_TOKENS, 25k) on its model, and
    the rollover turn re-reads part 1 about AUTOSWITCH_ROLLOVER_STEPS (8) times. Saving: each of the next
    AUTOSWITCH_ROLLOVER_EXPECTED_REQUESTS (30) requests re-reads the handoff instead of the whole chat."""
    c = cfg()
    cur, tgt = cur or "sonnet", tgt or cur or "sonnet"
    if tokens is None or cur not in c["recache50k"] or tgt not in c["recache50k"]:
        return False, "size or model unknown"
    w = lambda t: c["recache50k"][t] / 50000.0  # noqa: E731  cache-write $ per token
    r = lambda t: w(t) * c["read_fraction"]     # noqa: E731  cached-read $ per token
    h = _f("AUTOSWITCH_ROLLOVER_HANDOFF_TOKENS", "25000")
    cost = h * w(tgt) + _f("AUTOSWITCH_ROLLOVER_STEPS", "8") * tokens * r(cur)
    saving = _f("AUTOSWITCH_ROLLOVER_EXPECTED_REQUESTS", "30") * (tokens * r(cur) - h * r(tgt))
    if saving <= cost:
        return False, f"saving ${saving:.3f} <= cost ${cost:.3f}"
    return True, f"saving ${saving:.3f} > cost ${cost:.3f}"


# ---------- transcript scan: quiet moment and jobs ----------

def _ts(d):
    try:
        return datetime.datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _text_of(c):
    if isinstance(c, str):
        return c
    out = []
    for b in c or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text") or "")
        elif isinstance(b, dict) and b.get("type") == "tool_result":
            x = b.get("content")
            out.append(x if isinstance(x, str) else " ".join(y.get("text", "") for y in x or [] if isinstance(y, dict)))
    return "\n".join(out)


def scan(transcript, now=None):
    """One pass over the main chat. {"pending": [tool names without a result], "half_edits": [paths whose last
    Edit/Write failed], "jobs": [live jobs], "busy_linked": [session ids], "background": [running commands],
    "task": {"user": [last typed messages], "assistant": last answer text}}."""
    now = time.time() if now is None else now
    uses, done, jobs, edits, user_msgs = {}, set(), {}, {}, []
    last_out, last_reply = {}, {}
    assistant = ""
    try:
        f = open(transcript, "rb")
    except (OSError, TypeError):
        return None
    with f:
        for line in f:
            if b'"user"' not in line and b'"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") not in ("user", "assistant") or d.get("isSidechain"):
                continue
            ts = _ts(d) or now
            c = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
            if d["type"] == "assistant":
                for b in c or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        uses[b.get("id")] = {"name": b.get("name") or "", "input": b.get("input") or {}, "ts": ts}
                        nm, inp = b.get("name") or "", b.get("input") or {}
                        if nm.endswith("send_message") and isinstance(inp, dict) and inp.get("session_id"):
                            last_out[inp["session_id"]] = ts
                txt = _text_of(c)
                if txt.strip():
                    assistant = txt
                continue
            text = _text_of(c)
            for sid in re.findall(r"local_[0-9a-f-]{36}", text):
                if text.lstrip().startswith("<"):
                    last_reply[sid] = ts  # a machine note about that session: it reported back
            m = re.search(r"<task-id>([^<]+)</task-id>", text)
            if m and re.search(r"<status>(completed|failed|killed|stopped)</status>", text):
                jobs.pop(m.group(1).strip(), None)
            if isinstance(c, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c) \
                    or isinstance(c, str):
                if not text.lstrip().startswith("<"):
                    user_msgs.append(text)
            res = d.get("toolUseResult")
            for b in c or []:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                u = uses.get(b.get("tool_use_id"))
                done.add(b.get("tool_use_id"))
                if not u:
                    continue
                err = bool(b.get("is_error"))
                nm, inp = u["name"], u["input"] if isinstance(u["input"], dict) else {}
                rtext = _text_of([b])
                if nm in EDITS:
                    p = inp.get("file_path") or inp.get("notebook_path")
                    if p:
                        edits[p] = u["ts"] if err else None
                if err:
                    continue
                if nm == "CronCreate" and isinstance(res, dict) and res.get("id"):
                    jobs[res["id"]] = {"kind": "cron", "orig_id": res["id"], "ts": u["ts"],
                                       "recipe": {"tool": "CronCreate", "args": inp},
                                       "remove": {"tool": "CronDelete", "args": {"id": res["id"]}}}
                elif nm == "CronDelete":
                    jobs.pop(inp.get("id"), None)
                elif nm == "Monitor" and isinstance(res, dict) and res.get("taskId") and _is_watcher(inp):
                    jobs[res["taskId"]] = {"kind": "watcher", "orig_id": res["taskId"], "ts": u["ts"], "recipe": None,
                                           "remove": None}  # the Watcher itself: never a job to move
                elif nm == "Monitor" and isinstance(res, dict) and res.get("taskId"):
                    end = None if res.get("persistent") or not res.get("timeoutMs") else u["ts"] + res["timeoutMs"] / 1000.0
                    jobs[res["taskId"]] = {"kind": "monitor", "orig_id": res["taskId"], "ts": u["ts"], "ends": end,
                                           "recipe": {"tool": "Monitor", "args": inp},
                                           "remove": {"tool": "TaskStop", "args": {"task_id": res["taskId"]}}}
                elif nm == "ScheduleWakeup":
                    delay = _num(inp.get("delaySeconds"))
                    jid = "wakeup-%s" % b.get("tool_use_id")
                    jobs[jid] = {"kind": "loop", "orig_id": jid, "ts": u["ts"],
                                 "ends": u["ts"] + delay if delay is not None else None,
                                 "recipe": {"tool": "ScheduleWakeup", "args": inp}, "remove": None}
                elif nm == "Bash" and inp.get("run_in_background") and isinstance(res, dict) and res.get("backgroundTaskId"):
                    jobs[res["backgroundTaskId"]] = {"kind": "background", "orig_id": res["backgroundTaskId"],
                                                     "ts": u["ts"], "recipe": None, "remove": None,
                                                     "command": str(inp.get("command"))[:300]}
                elif nm in ("TaskStop", "KillShell", "KillBash"):
                    jobs.pop(inp.get("task_id") or inp.get("shell_id") or inp.get("id"), None)
                elif nm.endswith("start_session"):
                    m2 = _SID.search(rtext) or _SID.search(json.dumps(res) if res else "")
                    if m2:
                        sid = m2.group(1)
                        last_out[sid] = u["ts"]
                        jobs[sid] = {"kind": "linked", "orig_id": sid, "ts": u["ts"],
                                     "title": inp.get("title"), "recipe": {"tool": "adopt", "args": {"session_id": sid}},
                                     "remove": None}
                elif nm.endswith("archive_session") or nm.endswith("stop_session"):
                    jobs.pop(inp.get("session_id"), None)
    live, expired = [], []
    limit = guard_expire()
    for p, t in sorted(edits.items()):
        if t is not None and now - t > limit:
            expired.append("failed edit of %s left unfixed for %d min: guard expired" % (p, (now - t) // 60))
    for j in jobs.values():
        if j.get("ends") is not None and j["ends"] < now:
            continue
        if j["kind"] == "background" and now - j["ts"] > limit:
            expired.append("background command %s never reported back in %d min: guard expired"
                           % (j["orig_id"], (now - j["ts"]) // 60))
            continue
        if j["kind"] == "loop" and j.get("ends") is not None:
            j["recipe"]["args"] = dict(j["recipe"]["args"], delaySeconds=max(60, int(j["ends"] - now)))
        live.append(j)
    pending = [u["name"] for k, u in uses.items() if k not in done]
    busy = [s for s, t in last_out.items() if t > last_reply.get(s, 0) and s in jobs]
    return {"pending": pending,
            "half_edits": sorted(p for p, t in edits.items() if t is not None and now - t <= limit),
            "jobs": [j for j in live if j["kind"] not in ("background", "watcher")],
            "background": [j for j in live if j["kind"] == "background"], "busy_linked": busy,
            "watcher": [j["orig_id"] for j in live if j["kind"] == "watcher"], "expired": expired,
            "task": {"user": [u[:1500] for u in user_msgs[-3:]], "assistant": assistant[-1500:]}}


def guard_expire():
    """A failed Edit never retried, or a background command whose completion never arrived, stops blocking a
    rollover after AUTOSWITCH_ROLLOVER_GUARD_EXPIRE (1800 s); the reason goes into the rollover's state."""
    return _f("AUTOSWITCH_ROLLOVER_GUARD_EXPIRE", "1800")


def _is_watcher(inp):
    """A Monitor running the Autoswitch Watcher (watch.sh)."""
    return isinstance(inp, dict) and re.search(r"(^|[/\s'\"])watch\.sh\b", str(inp.get("command") or "")) is not None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def quiet(s):
    """(ok, reasons)."""
    if s is None:
        return False, ["transcript unreadable"]
    why = []
    if s["pending"]:
        why.append("tool call running (%s)" % ", ".join(sorted(set(s["pending"]))))
    if s["half_edits"]:
        why.append("half-done edit (%s)" % ", ".join(s["half_edits"]))
    if s["busy_linked"]:
        why.append("linked session mid-task (%s)" % ", ".join(s["busy_linked"]))
    if s["background"]:
        why.append("background command running, no recreate recipe (%s)" % ", ".join(j["orig_id"] for j in s["background"]))
    if s.get("watcher"):
        why.append("this chat runs the Watcher (watch.sh Monitor %s)" % ", ".join(s["watcher"]))
    return not why, why


# ---------- records ----------

def _dir(rid):
    return sb.path("rollovers", rid)


def load(rid, name="state"):
    if not _RID.match(rid or ""):
        return None
    try:
        with open(os.path.join(_dir(rid), name + ".json")) as f:
            r = json.load(f)
        return r if isinstance(r, (dict, list)) else None
    except (OSError, ValueError):
        return None


def save(rid, data, name="state"):
    os.makedirs(_dir(rid), exist_ok=True)
    p = os.path.join(_dir(rid), name + ".json")
    tmp = p + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


@contextlib.contextmanager
def locked(rid):
    os.makedirs(_dir(rid), exist_ok=True)
    with open(os.path.join(_dir(rid), ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def step(rec, name, **kw):
    rec.setdefault("steps", {})[name] = time.time()
    if name in STEPS:
        rec["status"] = name
    rec.update(kw)
    save(rec["id"], rec)
    return rec


def all_records():
    out = []
    for p in glob.glob(sb.path("rollovers", "*", "state.json")):
        r = load(os.path.basename(os.path.dirname(p)))
        if isinstance(r, dict) and r.get("id"):
            out.append(r)
    return sorted(out, key=lambda r: r.get("ts", 0))


def open_for(app_id):
    return next((r for r in all_records() if r.get("session_id") == app_id and r.get("status") in LIVE), None)


def deferred_for(app_id):
    return next((r for r in all_records() if r.get("session_id") == app_id and r.get("status") == "deferred"), None)


def continued(app_id):
    """The confirmed rollover that moved this chat's work to part N, or None."""
    for r in reversed(all_records()):
        if r.get("session_id") == app_id and r.get("status") in CONTINUED:
            return r
    return None


def last_tokens(app_id):
    """Size of this chat at its last rollover attempt that counts for the gap (not deferred or superseded)."""
    for r in reversed(all_records()):
        if r.get("session_id") == app_id and r.get("status") not in ("deferred", "superseded"):
            return r.get("tokens")
    return None


def guards(app_id, m, s, cur, tgt):
    """(ok, reasons) for a chat that has a trigger."""
    import handoff
    ok, why = quiet(s)
    if app_id and handoff.heartbeat_owner() == app_id:
        why.append("this chat runs the Watcher (it owns the heartbeat)")
    if open_for(app_id) or handoff.open_for(app_id):
        why.append("a handoff or rollover of this chat is open")
    if continued(app_id):
        why.append("this chat already continued in a later part")
    lt = last_tokens(app_id)
    if lt is not None and m.get("tokens") is not None and m["tokens"] - lt < gap():
        why.append("rollover %dk tokens ago (< %dk)" % ((m["tokens"] - lt) // 1000, gap() // 1000))
    w_ok, w_why = worth(cur, tgt, m.get("tokens"))
    if not w_ok:
        why.append(w_why)
    return not why, why


def jobs_of(rid):
    j = load(rid, "jobs")
    return j if isinstance(j, list) else []


def create(app_id, cli_id, transcript, cwd, model, prompt, info, record, permission_mode, m, s, trig,
           budget_mode="balanced"):
    """A new rollover in state `created`, with its jobs list. prompt is the user's held message, or None (stop)."""
    import handoff
    rid = uuid.uuid4().hex[:12]
    base, part = handoff.split_title((info or {}).get("title"))
    cur = tier_of((info or {}).get("model"))
    tgt = tier_of(model)
    rec = {
        "id": rid, "kind": "rollover", "trigger": trig, "status": "created", "ts": time.time(),
        "session_id": app_id, "cli_session_id": cli_id, "transcript": transcript, "cwd": cwd,
        "project": os.path.basename(os.path.normpath(cwd)) if cwd else None,
        "from_model": (info or {}).get("model"), "model": model, "switch": cur != tgt,
        "effort": (record or {}).get("effort"),
        "permission_mode": handoff.lower_mode(permission_mode, (record or {}).get("permissionMode")),
        "prompt": prompt, "tokens": None if m.get("tokens") is None else int(m["tokens"]),
        "task": (s or {}).get("task") or {}, "budget_mode": budget_mode, "part": part + 1,
        "guards_expired": (s or {}).get("expired") or [],
        "old_title": f"{base} · part {part} ({handoff.name(cur)})",
        "new_title": f"{base} · part {part + 1} ({handoff.name(tgt)})",
        "base_title": base, "not_carried": handoff.not_carried(record),
        "handoff_path": os.path.join(_dir(rid), "handoff.md"),
    }
    jobs = []
    for j in (s or {}).get("jobs") or []:
        jobs.append({"jid": uuid.uuid4().hex[:12], "kind": j["kind"], "owner": "part1", "state": "part1",
                     "orig_id": j["orig_id"], "recipe": j.get("recipe"), "remove": j.get("remove"),
                     "title": j.get("title"), "movable": bool(j.get("recipe"))})
    save(rid, jobs, "jobs")
    rec["steps"] = {"created": rec["ts"]}
    save(rid, rec)
    for d in [r for r in all_records() if r.get("session_id") == app_id and r.get("status") == "deferred"]:
        d.update(status="superseded", superseded_by=rid)
        save(d["id"], d)
    return rec


def _task_text(rec):
    t = rec.get("task") or {}
    parts = []
    if t.get("user"):
        parts.append("The user's last messages:\n" + "\n---\n".join(t["user"]))
    if t.get("assistant"):
        parts.append("Your last words in part %d:\n%s" % (rec["part"] - 1, t["assistant"]))
    return "\n\n".join(parts) or "(see the handoff)"


def _jobs_text(jobs):
    if not jobs:
        return "none"
    out = []
    for j in jobs:
        what = j.get("title") or (j.get("recipe") or {}).get("args", {}).get("description") or j["orig_id"]
        out.append("%s %s (%s)%s" % (j["kind"], j["orig_id"], what, "" if j["movable"] else ", stays with part 1"))
    return "; ".join(out)


def part2_prompt(rec):
    rid = rec["id"]
    cli = "python3 '%s'" % os.path.join(HERE, "rollover.py")
    head = (f"{TAG}{rid}] This chat continues \"{rec['base_title']}\" (part {rec['part'] - 1}), rolled over "
            f"automatically by Autoswitch because it had grown long. First read the handoff file "
            f"`{rec['handoff_path']}` in full: it says how to look up details of the earlier chat. Then run "
            f"`{cli} jobs {rid}` and recreate exactly the jobs it lists, each with its recipe, running "
            f"`{cli} job-created {rid} <jid> <new id>` after each one; if that prints \"delete_yours\", delete the copy "
            f"you just made. Then run `{cli} confirm {rid}`.\n\n")
    if rec.get("prompt"):
        return head + "Then answer the user's message below as if they had sent it here.\n\nthe user's message:\n" + rec["prompt"]
    return head + "Then continue the work where part %d left off:\n\n%s" % (rec["part"] - 1, _task_text(rec))


def instruction(rec):
    """additionalContext (prompt trigger) or the Stop hook's block reason (stop trigger) for part 1."""
    rid = rec["id"]
    cli = "python3 '%s'" % os.path.join(HERE, "rollover.py")
    import handoff
    tgt = handoff.name(tier_of(rec["model"]))
    effort = f', effort "{rec["effort"]}"' if rec.get("effort") else ""
    stop = rec["trigger"] == "stop"
    what = "their message above" if not stop else "the current task"
    fallback = ("answer the user's message here yourself" if not stop else "carry on with the work here as before")
    refused = (f"If start_session is refused because this turn was not typed by the user: do NOT retry and do not "
               f"propose a session; run `{cli} deferred {rid}` and end the turn with exactly one line: "
               f"\"Rollover deferred until your next message.\" Any other start_session error: run "
               f"`{cli} fail {rid} start` and {fallback}.") if stop else \
              (f"If start_session fails or is refused: run `{cli} fail {rid} start` and {fallback}.")
    archive = ("Leave this chat un-archived (overnight work), and add: \"Left un-archived: say 'archive' to "
               "archive it.\"" if stop else "Do not archive this chat; add: \"Say 'archive' to archive this chat.\"")
    return (
        f"[Autoswitch rollover {rid}] The user turned on automatic rollover in the Autoswitch settings "
        f"(AUTOSWITCH_ROLLOVER=on). This chat is long (about {(rec['tokens'] or 0) // 1000}k tokens), so "
        f"{what} continues in a new chat (part {rec['part']}, {tgt}). Do NOT {'answer the message' if not stop else 'do more work'} "
        f"here. Do exactly these steps, nothing else:\n"
        f"1. Bash (timeout 600000 ms): {cli} build {rid}\n"
        f"   It prints one JSON line. If \"ok\" is false, stop the steps and {fallback}.\n"
        f"2. start_session: initiation \"user_asked\" (the user enabled automatic rollover in settings), context \"fresh\", "
        f"use_worktree false, model \"{rec['model']}\"{effort}, permission_mode \"{rec['permission_mode']}\", "
        f"title \"{rec['new_title']}\", prompt = the JSON's \"prompt\" value character for character, "
        f"background = the JSON's \"background\" value. {refused}\n"
        f"3. Bash: {cli} started {rid} <the new session_id>\n"
        f"4. Bash (timeout 600000 ms): {cli} wait {rid}\n"
        f"   It prints one JSON line. If \"ok\" is false, part 2 never took over: stop the steps, tell the user in one "
        f"line, and {fallback}. Otherwise, for each item of \"delete\": call its tool with its args (nothing for "
        f"tool null), then run `{cli} job-deleted {rid} <jid>`.\n"
        f"5. detach_session on the new session; set_session_title: this chat (\"self\") to \"{rec['old_title']}\", "
        f"the new session to \"{rec['new_title']}\"; list_groups, and if a group named \"{rec['project']}\" exists, "
        f"move_sessions the new session into it. Then Bash: {cli} linked {rid}\n"
        f"6. open_session_in the new session, target \"split\". Then Bash: {cli} shown {rid} yes  (or `no` if it "
        f"was refused, e.g. the window is hidden).\n"
        f"7. Reply here with exactly one line: \"Continued in part {rec['part']} ({tgt}): <link to the new session>. "
        f"Not carried over: {', '.join(rec['not_carried'])}. Jobs still here: <the wait JSON's \"kept\", or none>.\" "
        f"{archive} Then end the turn."
    )


# ---------- the CLI steps ----------

def _get(rid):
    rec = load(rid)
    if not isinstance(rec, dict):
        raise KeyError("unknown rollover")
    return rec


def build(rid):
    import acpmh_bridge
    with locked(rid):
        rec = _get(rid)
        if rec["status"] not in ("created", "built"):
            return {"ok": False, "error": "rollover is %s" % rec["status"]}
    steps = []
    path = acpmh_bridge.build_handoff(rec.get("transcript"), rec.get("cwd"), out=rec["handoff_path"],
                                      budget_mode=rec.get("budget_mode", "balanced"), tokens=rec.get("tokens"), log=steps)
    with locked(rid):
        rec = _get(rid)
        rec["build_steps"] = steps
        if rec["status"] not in ("created", "built"):
            return {"ok": False, "error": "rollover is %s" % rec["status"]}
        if not path:
            _fail(rec, "built", "; ".join(steps))
            return {"ok": False, "error": "; ".join(steps)}
        with open(path, "a") as f:
            f.write("\n\n## Rollover (Autoswitch)\n\nCurrent task:\n\n%s\n\nJobs of part %d: %s\n"
                    % (rec.get("prompt") and "the user's new message (see the first message)" or _task_text(rec),
                       rec["part"] - 1, _jobs_text(jobs_of(rid))))
        step(rec, "built", handoff_path=path)
    return {"ok": True, "handoff": path, "prompt": part2_prompt(rec),
            "background": ("Part %d of the chat \"%s\", continued automatically by Autoswitch because the chat had "
                           "grown long. The handoff file %s summarises the earlier chat and says how to look up "
                           "details." % (rec["part"], rec["base_title"], path))}


def started(rid, session):
    with locked(rid):
        rec = _get(rid)
        if rec["status"] in ("created", "built"):
            step(rec, "started", part2_session=session if str(session).startswith("local_") else None)
            return {"ok": True}
        return {"ok": rec["status"] in ("delivered",) + CONTINUED, "status": rec["status"]}


def deferred(rid):
    with locked(rid):
        rec = _get(rid)
        if rec["status"] in ("created", "built"):
            rec.setdefault("steps", {})["deferred"] = time.time()
            rec["status"] = "deferred"
            save(rid, rec)
            _revert_jobs(rid)
            return {"ok": True}
        return {"ok": False, "status": rec["status"]}


def _revert_jobs(rid):
    """Jobs part 2 did not create go back to part 1 (kept); returns them."""
    jobs = jobs_of(rid)
    kept = []
    for j in jobs:
        if j["state"] in ("part1", "moving"):
            j.update(state="kept" if j["state"] == "moving" or not j["movable"] else "part1", owner="part1")
            kept.append(j)
    save(rid, jobs, "jobs")
    return kept


def _fail(rec, at, why):
    rec.setdefault("steps", {})["failed"] = time.time()
    rec.update(status="failed", failed_at=at, why=why,
               note=" | Autoswitch could not roll this chat over (%s): it keeps going here" % (why or at)[:200])
    save(rec["id"], rec)
    _revert_jobs(rec["id"])
    return rec


def fail(rid, why="failed"):
    with locked(rid):
        rec = _get(rid)
        if rec["status"] in OPEN:
            _fail(rec, rec["status"], why)
            return {"ok": True}
        return {"ok": False, "status": rec["status"]}


def jobs_cmd(rid):
    """Part 2: claims every movable job (part1 -> moving) and lists the recipes."""
    with locked(rid):
        rec = _get(rid)
        if rec["status"] != "delivered":
            return {"ok": False, "error": "rollover is %s: create nothing" % rec["status"], "jobs": []}
        jobs = jobs_of(rid)
        out = []
        for j in jobs:
            if j["movable"] and j["state"] == "part1":
                j.update(state="moving", claimed_ts=time.time())
            if j["state"] == "moving":
                r = j["recipe"]
                out.append({"jid": j["jid"], "kind": j["kind"], "tool": r["tool"], "args": r["args"],
                            "how": ("you now own linked session %s: check it with get_session / list_events; its "
                                    "turn-end notes still reach part %d, which forwards them" % (j["orig_id"], rec["part"] - 1))
                            if r["tool"] == "adopt" else "call %s with these args" % r["tool"]})
        save(rid, jobs, "jobs")
        return {"ok": True, "jobs": out}


def job_created(rid, jid, new_id):
    with locked(rid):
        jobs = jobs_of(rid)
        for j in jobs:
            if j["jid"] != jid:
                continue
            if j["state"] == "moving":
                j.update(state="created", new_id=new_id, created_ts=time.time())
                save(rid, jobs, "jobs")
                return {"ok": True}
            if j["state"] in ("created", "part2") and j.get("new_id") == new_id:
                return {"ok": True}
            return {"ok": False, "delete_yours": True, "state": j["state"]}  # part 1 kept it: no duplicate
        return {"ok": False, "error": "unknown job"}


def _confirm(rec):
    kept = _revert_jobs(rec["id"])
    step(rec, "confirmed", kept=[j["jid"] for j in kept])
    return rec


def confirm(rid):
    with locked(rid):
        rec = _get(rid)
        if rec["status"] == "delivered":
            _confirm(rec)
            return {"ok": True}
        return {"ok": rec["status"] in CONTINUED, "status": rec["status"]}


def _wait_result(rec):
    jobs = jobs_of(rec["id"])
    if rec["status"] not in CONTINUED:
        return {"ok": False, "status": rec["status"]}
    return {"ok": True, "status": rec["status"],
            "delete": [{"jid": j["jid"], "kind": j["kind"], "tool": (j.get("remove") or {}).get("tool"),
                        "args": (j.get("remove") or {}).get("args")} for j in jobs if j["state"] == "created"],
            "kept": [f"{j['kind']} {j['orig_id']}" for j in jobs if j["state"] in ("kept", "part1")]}


def wait(rid, timeout=None, poll=0.5):
    """Part 1: until part 2 has confirmed (or the wait ends). Delivered but unconfirmed at the end: confirmed
    with the unmoved jobs kept here. Never delivered: failed, part 1 keeps the work."""
    timeout = _f("AUTOSWITCH_ROLLOVER_WAIT", "540") if timeout is None else timeout
    end = time.time() + timeout
    while True:
        with locked(rid):
            rec = _get(rid)
            if rec["status"] in CONTINUED or rec["status"] in ("failed", "deferred", "superseded"):
                return _wait_result(rec)
            if time.time() >= end:
                if rec["status"] == "delivered":
                    _confirm(rec)
                else:
                    _fail(rec, rec["status"], "part 2 never received its first message")
                return _wait_result(rec)
        time.sleep(poll)


def job_deleted(rid, jid):
    with locked(rid):
        jobs = jobs_of(rid)
        for j in jobs:
            if j["jid"] == jid and j["state"] == "created":
                j.update(state="part2", owner="part2", deleted_ts=time.time())
                save(rid, jobs, "jobs")
                return {"ok": True}
        return {"ok": False}


def linked(rid):
    with locked(rid):
        rec = _get(rid)
        if rec["status"] == "confirmed":
            step(rec, "linked")
        return {"ok": rec["status"] in ("linked", "shown", "archived")}


def shown(rid, yes):
    with locked(rid):
        rec = _get(rid)
        if rec["status"] not in ("linked", "shown"):
            return {"ok": False, "status": rec["status"]}
        if str(yes).lower() in ("yes", "1", "true"):
            step(rec, "shown")
        else:
            rec["not_shown"] = True
            save(rid, rec)
        return {"ok": True}


def archive(rid, transcript=None):
    """Part 1, after the user said archive: only when every job kept here has finished."""
    with locked(rid):
        rec = _get(rid)
        if rec["status"] not in ("linked", "shown"):
            return {"ok": False, "error": "rollover is %s: do not archive" % rec["status"]}
        kept = [j for j in jobs_of(rid) if j["state"] in ("kept", "part1")]
        if kept:
            s = scan(transcript or rec.get("transcript"))
            live = {j["orig_id"] for j in (s or {}).get("jobs", []) + (s or {}).get("background", [])} if s else None
            still = [j for j in kept if live is None or j["orig_id"] in live]
            if still:
                return {"ok": False, "error": "jobs still running here: " + _jobs_text(still)}
        step(rec, "archived")
        return {"ok": True}


# ---------- hooks ----------

def deliver(raw, app_id, cli_id):
    """Part 2's first message. None if it carries no rollover tag; else {"out": hook output or None}."""
    m = _TAG.search(sb.unwrap(raw))
    if not m:
        return None
    rid = m.group(1)
    if not isinstance(load(rid), dict):
        return None
    with locked(rid):
        rec = _get(rid)
        st = rec["status"]
        if st in ("delivered",) + CONTINUED:
            if rec.get("part2_cli") == cli_id:
                return {"out": None}
            return {"out": {"continue": False,
                            "stopReason": f"↪ Already continued in part {rec.get('part')}: duplicate not sent"}}
        if st not in OPEN:
            return {"out": {"continue": False, "stopReason": f"↪ This rollover was cancelled ({st}): part "
                                                             f"{rec['part'] - 1} kept the work. Nothing to do here."}}
        text = sb.unwrap(raw)
        verbatim = not rec.get("prompt") or " ".join(rec["prompt"].split()) in " ".join(text.split())
        step(rec, "delivered", part2_session=app_id or rec.get("part2_session"), part2_cli=cli_id, verbatim=verbatim)
        if not any(j["movable"] for j in jobs_of(rid)):
            _confirm(rec)  # nothing to move: part 2 has the work, confirmed at once
    if app_id:
        import pins
        pins.record_watcher_set(app_id, rec["model"], rid)  # the model part 2 started on is expected
        pins.observe(app_id, rec["model"])
    import handoff
    handoff.record_want(app_id, tier_of(rec["model"]))
    if not verbatim:
        return {"out": {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                "[Autoswitch] The message above was copied with changes. The user's exact message is:\n" + rec["prompt"]}}}
    return {"out": None}


def recover(now=None):
    """Every hook run: resume or undo unfinished rollovers. Returns the ids changed."""
    now = time.time() if now is None else now
    changed = []
    for r in all_records():
        st = r.get("status")
        if st not in LIVE and st != "deferred":
            continue
        rid = r["id"]
        with locked(rid):
            rec = load(rid)
            if not isinstance(rec, dict):
                continue
            st = rec.get("status")
            since = max([v for k, v in (rec.get("steps") or {}).items() if k in STEPS] or [rec.get("ts", now)])
            if st in OPEN and now - since > confirm_wait():
                _fail(rec, st, "part 2 never received its first message" if st == "started" else "stopped at " + st)
                changed.append(rid)
            elif st == "delivered" and now - since > confirm_wait():
                _confirm(rec)
                changed.append(rid)
            elif st == "deferred" and now - rec.get("ts", now) > _f("AUTOSWITCH_ROLLOVER_DEFER_TTL", "86400"):
                rec["status"] = "superseded"
                save(rid, rec)
                changed.append(rid)
    return changed


def take_note(app_id):
    """One-line notes (failed rollovers) for part 1, shown once."""
    out = ""
    for r in all_records():
        if r.get("session_id") == app_id and r.get("note") and not r.get("noted"):
            with locked(r["id"]):
                rec = load(r["id"])
                if isinstance(rec, dict) and rec.get("note") and not rec.get("noted"):
                    rec["noted"] = time.time()
                    save(r["id"], rec)
                    out += rec["note"]
    return out


def part1_context(rec, machine=False):
    """Every turn of a part 1 whose work moved on: don't do the same work twice."""
    rid = rec["id"]
    cli = "python3 '%s'" % os.path.join(HERE, "rollover.py")
    jobs = jobs_of(rid)
    kept = [j for j in jobs if j["state"] in ("kept", "part1")]
    left = [j for j in jobs if j["state"] == "created"]
    link = rec.get("part2_session") or "part %d" % rec["part"]
    s = (f"[Autoswitch] This chat continued in part {rec['part']} ({link}); don't continue the task here. ")
    if machine:
        s += (f"If this event belongs to a job still owned here ({_jobs_text(kept)}), handle it; otherwise send it "
              f"to {link} with send_message and do nothing else.")
    else:
        s += "Answer the user only if they ask something about this chat itself; otherwise point them to part %d." % rec["part"]
    if left:
        s += (" Unfinished from the rollover: part 2 recreated these jobs, so delete the copies here: " +
              "; ".join("%s %s (then `%s job-deleted %s %s`)" % ((j.get('remove') or {}).get('tool') or "nothing",
                                                                 json.dumps((j.get('remove') or {}).get('args')),
                                                                 cli, rid, j["jid"]) for j in left) + ".")
    if rec["status"] == "confirmed":
        s += f" The chat was not linked yet: rename it to \"{rec['old_title']}\" and run `{cli} linked {rid}`."
    s += f" If the user asks to archive this chat, run `{cli} archive {rid}` first and archive only if it prints ok true."
    return s


def on_prompt(hook, app_id, info, handoff_to, mode, img, new_chat, prompt, bmode):
    """UserPromptSubmit, the user's typed message (machine turns use part1_context only).
    Returns (hook output dict or None, log fields)."""
    log = {}
    tr = hook.get("transcript_path")
    cont = continued(app_id) if app_id else None
    if cont:
        if not cont.get("part1_told"):
            with locked(cont["id"]):
                c = load(cont["id"])
                c["part1_told"] = time.time()
                save(cont["id"], c)
        return {"systemMessage": f"↪ This chat continued in part {cont['part']}",
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": part1_context(cont)}}, {"rollover": "continued"}
    for r in all_records():  # part 2's next message: confirms a rollover it never confirmed itself
        if app_id and r.get("part2_session") == app_id and r.get("status") == "delivered":
            with locked(r["id"]):
                rec = load(r["id"])
                if rec.get("status") == "delivered":
                    _confirm(rec)
    if not app_id or not info or new_chat:
        return None, log
    m = measure(tr)
    trig = trigger(m, deferred=bool(deferred_for(app_id)))
    log.update(rollover_tokens=None if m["tokens"] is None else int(m["tokens"]), rollover_trigger=trig)
    if not trig:
        return None, log
    s = scan(tr)
    cur = tier_of(info.get("model"))
    model = handoff_to or info.get("model")
    ok, why = guards(app_id, m, s, cur, tier_of(model))
    if img:
        ok, why = False, why + ["screenshot on this turn"]
    log.update(would_rollover=model if ok else None, rollover_blocked=why or None,
               rollover_guards_expired=(s or {}).get("expired") or None)
    if not ok or mode != "enforce":
        return None, log
    from resolve import record
    rec = create(app_id, hook.get("session_id"), tr, hook.get("cwd"), model, prompt, info,
                 record(hook.get("session_id")), hook.get("permission_mode"), m, s, trig, bmode)
    log["rollover_id"] = rec["id"]
    import handoff
    sw = f" on {handoff.name(tier_of(model))}" if rec["switch"] else ""
    return {"systemMessage": f"↪ Rolling over to a new chat{sw} (part {rec['part']})",
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": instruction(rec)}}, log


def _stopblock(cli):
    return sb.path("rollovers", "stopblock", re.sub(r"[^A-Za-z0-9_-]", "_", str(cli)) + ".json")


def on_stop(hook, app_id):
    """Stop hook. Returns the hook output dict or None."""
    cli = hook.get("session_id")
    note = take_note(app_id) if app_id else ""
    for r in all_records():  # part 2's first turn ended: confirm what it moved
        if app_id and r.get("part2_session") == app_id and r.get("status") == "delivered":
            with locked(r["id"]):
                rec = load(r["id"])
                if rec.get("status") == "delivered":
                    _confirm(rec)
    out = {"systemMessage": note.lstrip(" |")} if note else None
    if not app_id or not cli:
        return out
    sbp = _stopblock(cli)
    if hook.get("stop_hook_active") or os.path.exists(sbp):
        try:
            os.remove(sbp)
        except OSError:
            pass
        return out  # loop guard: never block two stops in a row
    if continued(app_id) or deferred_for(app_id):
        return out  # moved on, or waiting for the user's next message
    from resolve import record, resolve
    info = resolve(cli, wait=0)
    if not info or info.get("sessionId") != app_id:
        return out
    tr = hook.get("transcript_path")
    m = measure(tr)
    trig = trigger(m)
    if not trig:
        return out
    s = scan(tr)
    ok, why = guards(app_id, m, s, tier_of(info.get("model")), tier_of(info.get("model")))
    _log({"ts": time.time(), "event": "stop", "session": app_id, "tokens": None if m["tokens"] is None else int(m["tokens"]),
          "trigger": trig, "would_rollover": ok, "blocked": why or None,
          "guards_expired": (s or {}).get("expired") or None})
    if not ok:
        return out
    import budget
    rec = create(app_id, cli, tr, hook.get("cwd"), info.get("model"), None, info, record(cli),
                 hook.get("permission_mode"), m, s, "stop", budget.effective_mode()[0])
    os.makedirs(os.path.dirname(sbp), exist_ok=True)
    with open(sbp, "w") as f:
        json.dump({"rid": rec["id"], "ts": time.time()}, f)
    res = {"decision": "block", "reason": instruction(rec)}
    if note:
        res["systemMessage"] = note.lstrip(" |")
    return res


def _log(rec):
    try:
        os.makedirs(sb.BASE, exist_ok=True)
        with open(sb.path("rollover.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def main(argv):
    cmds = {"build": (build, 1), "started": (started, 2), "deferred": (deferred, 1), "fail": (fail, 2),
            "wait": (wait, 1), "jobs": (jobs_cmd, 1), "job-created": (job_created, 3),
            "job-deleted": (job_deleted, 2), "confirm": (confirm, 1), "linked": (linked, 1),
            "shown": (shown, 2), "archive": (archive, 1)}
    if len(argv) < 2 or argv[0] not in cmds or not _RID.match(argv[1]):
        return {"ok": False, "error": "usage: rollover.py <%s> <id> ..." % "|".join(cmds)}
    fn, n = cmds[argv[0]]
    args = argv[1:1 + n]
    if argv[0] == "fail" and len(args) < 2:
        args.append("failed")
    if len(args) != n:
        return {"ok": False, "error": "%s needs %d argument(s)" % (argv[0], n)}
    if not os.path.exists(os.path.join(_dir(argv[1]), "state.json")):
        return {"ok": False, "error": "unknown rollover"}
    return fn(*args)


if __name__ == "__main__":
    try:
        res = main(sys.argv[1:])
    except Exception as e:  # never a traceback for the chat to puzzle over
        res = {"ok": False, "error": type(e).__name__}
    print(json.dumps(res))
