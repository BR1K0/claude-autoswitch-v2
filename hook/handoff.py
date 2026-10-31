"""Autoswitch v2: hand a long chat off to a new chat (part 2) instead of switching it in place.

Off by default. AUTOSWITCH_HANDOFF, else the file $AUTOSWITCH_DIR/handoff (same pattern as `speed`):
`on` turns on everything in v2 (real-token sizes, budget modes, context scoring, break-even thresholds,
handoffs, the Watcher safety notes); anything else, or nothing, is today's behaviour, unchanged.
It needs AUTOSWITCH_MODE shadow (log only) or enforce.

Thresholds (real tokens, all configurable):
  below break-even of the incoming model   in-place switch by the Watcher, with today's cost check
  within +-10% of it (AUTOSWITCH_ZONE)    counted precisely; if that count fails: stay put
  above it                                 only a lasting switch moves: the gates want the other tier for
                                           this message AND (the previous user message wanted it too, OR
                                           the message starts a new task). Down: hand off to part 2.
                                           Up: in place (a chat cannot start a session on a higher model).
                                           One-off or unclear messages stay put.
Break-even (Step 1): Sonnet incoming 70k, Haiku 55k (AUTOSWITCH_BREAKEVEN_SONNET / _HAIKU); Opus incoming
was not measured, 70k is assumed (AUTOSWITCH_BREAKEVEN_OPUS).

A handoff never holds the prompt: the hook lets it through with an instruction (additionalContext) for the
current chat, which builds the handoff (acpmh_bridge.py), starts part 2 with the message and a one-time tag
[autoswitch-handoff:<id>], tidies up and replies with one line. The request lives in
$AUTOSWITCH_DIR/handoff/<id>.json until part 2's hook sees the tag (exactly once).
"""
import contextlib
import fcntl
import glob
import json
import os
import re
import time
import uuid

import sbqueue as sb
from policy import RANK, _f, tier_of

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "[autoswitch-handoff:"
_TAG = re.compile(r"\[autoswitch-handoff:([0-9a-f]{12})\]")
_PART = re.compile(r"\s+· part (\d+)(?: \([^)]*\))?\s*$")
OPEN = ("pending", "built", "started")
PERM_RANK = {"plan": 0, "default": 1, "acceptEdits": 2, "auto": 3, "bypassPermissions": 4}


def enabled():
    v = os.environ.get("AUTOSWITCH_HANDOFF")
    if v is None:
        try:
            with open(sb.path("handoff")) as f:
                v = f.read().strip()
        except OSError:
            v = ""
    return v.strip().lower() in ("on", "1", "true", "yes")


def context_enabled():
    return enabled() and os.environ.get("AUTOSWITCH_CONTEXT", "on").strip().lower() != "off"


def name(tier):
    return (tier or "?").capitalize()


# ---------- thresholds ----------

def breakeven(tier):
    defaults = {"sonnet": "70000", "haiku": "55000", "opus": "70000"}
    return _f(f"AUTOSWITCH_BREAKEVEN_{(tier or 'sonnet').upper()}", defaults.get(tier, "70000"))


def plan(target_tier, cur_tier, size, prev_want=None, starts_new_task=False, budget_mode=None):
    """Pure. {"action": "inplace" | "handoff" | "stay", "reason", "zone"}.
    target_tier: the tier the gates want (dwell guard not applied), or None. size: sizes.estimate()."""
    if not target_tier or target_tier == cur_tier:
        return {"action": "stay", "reason": "no switch wanted", "zone": None}
    tokens = size.get("tokens")
    if tokens is None:
        return {"action": "inplace", "reason": "size unknown: today's rules", "zone": None}
    be = breakeven(target_tier)
    z = _f("AUTOSWITCH_ZONE", "0.10")
    zone = "below" if tokens < be * (1 - z) else "above" if tokens > be * (1 + z) else "zone"
    if zone != "below" and size.get("method") != "precise":
        return {"action": "stay", "reason": "near or above break-even, no precise count: staying", "zone": zone}
    if tokens < be:
        return {"action": "inplace", "reason": f"{int(tokens)} real tokens < break-even {int(be)}", "zone": zone}
    lasting = prev_want == target_tier or starts_new_task
    if not lasting:
        return {"action": "stay", "reason": f"long chat ({int(tokens)} > {int(be)}), one-off: staying", "zone": zone}
    why = "new task" if starts_new_task and prev_want != target_tier else "second message in a row"
    if RANK[target_tier] > RANK.get(cur_tier, 0):
        return {"action": "inplace", "reason": f"long chat, lasting up-switch ({why}): in place", "zone": zone}
    # Step 1: in quality mode no handoff reached 95% of re-reading at 260-266k (v4 did at 134k and ~1M,
    # but at 134k re-reading costs little more). So quality re-reads in place up to this size.
    qmax = _f("AUTOSWITCH_QUALITY_REREAD_MAX", "300000")
    if budget_mode == "quality" and tokens < qmax:
        return {"action": "inplace", "reason": f"quality mode, {int(tokens)} < {int(qmax)}: re-read in place",
                "zone": zone}
    return {"action": "handoff", "reason": f"long chat ({int(tokens)} > {int(be)}), lasting ({why}): handoff",
            "zone": zone}


def _key_ok(k):
    return bool(k) and str(k).replace("-", "").replace("_", "").isalnum()


def last_want(app_id):
    if not _key_ok(app_id):
        return None
    try:
        with open(sb.path("lastscore", app_id + ".json")) as f:
            return json.load(f).get("want")
    except (OSError, ValueError, AttributeError):
        return None


def record_want(app_id, tier):
    if not _key_ok(app_id):
        return
    os.makedirs(sb.path("lastscore"), exist_ok=True)
    with open(sb.path("lastscore", app_id + ".json"), "w") as f:
        json.dump({"want": tier, "ts": time.time()}, f)


# ---------- requests ----------

def _rec_path(rid):
    return sb.path("handoff", rid + ".json")


def load(rid):
    try:
        with open(_rec_path(rid)) as f:
            r = json.load(f)
        return r if isinstance(r, dict) else None
    except (OSError, ValueError):
        return None


def save(rec):
    os.makedirs(sb.path("handoff"), exist_ok=True)
    tmp = _rec_path(rec["id"]) + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, _rec_path(rec["id"]))


@contextlib.contextmanager
def locked(rid):
    """Serialises the status changes of one request (build, timeout note, delivery)."""
    os.makedirs(sb.path("handoff"), exist_ok=True)
    with open(sb.path("handoff", rid + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def all_records():
    out = []
    for p in glob.glob(sb.path("handoff", "*.json")):
        r = load(os.path.basename(p)[:-5])
        if r:
            out.append(r)
    return sorted(out, key=lambda r: r.get("ts", 0))


def open_for(app_id, now=None):
    """A handoff of this chat not yet in a terminal state (delivered, timeout_noted, build_failed), or None.
    Age does not matter here: timeout_notes() is what closes a request that took too long."""
    for r in all_records():
        if r.get("session_id") == app_id and r.get("status") in OPEN:
            return r
    return None


def settle_wait():
    """How long part 2 has to arrive once the handoff is built."""
    return _f("AUTOSWITCH_HANDOFF_WAIT", "120")


def build_max():
    """The build's total time limit (acpmh_bridge.build_handoff), under the instruction's 600 s Bash timeout."""
    return _f("AUTOSWITCH_HANDOFF_BUILD_MAX", "540")


def deadline(rec):
    """When an open request counts as lost: the wait covers the build. Built: built_ts + wait; still
    building: ts + build_max + wait."""
    if rec.get("built_ts"):
        return rec["built_ts"] + settle_wait()
    return rec.get("ts", 0) + build_max() + settle_wait()


def split_title(title):
    """(base title, part number of this chat)."""
    title = (title or "").strip() or "Chat"
    m = _PART.search(title)
    if m:
        return title[:m.start()].rstrip() or "Chat", int(m.group(1))
    return title, 1


def lower_mode(*modes):
    known = [m for m in modes if m in PERM_RANK]
    return min(known, key=PERM_RANK.get) if known else "default"


def not_carried(record):
    """What part 2 can't be given from here (Step 2.9): connectors, added folders, output style."""
    out = []
    names = [s.get("name") for s in (record or {}).get("remoteMcpServersConfig") or [] if isinstance(s, dict)]
    names = [n for n in names if n]
    out.append("connectors" + (f" ({', '.join(names)})" if names else ""))
    dirs = []
    for u in (record or {}).get("sessionPermissionUpdates") or []:
        if isinstance(u, dict) and u.get("type") == "addDirectories":
            dirs += [d for d in u.get("directories") or [] if isinstance(d, str)]
    if dirs:
        out.append("added folders (%s)" % ", ".join(sorted(set(dirs))))
    out.append("output style")
    return out


def create(app_id, cli_id, transcript, cwd, target_model, prompt, info, record, permission_mode, size,
           budget_mode="balanced"):
    rid = uuid.uuid4().hex[:12]
    base, part = split_title((info or {}).get("title"))
    cur = tier_of((info or {}).get("model"))
    tgt = tier_of(target_model)
    rec = {
        "id": rid, "status": "pending", "ts": time.time(),
        "session_id": app_id, "cli_session_id": cli_id, "transcript": transcript, "cwd": cwd,
        "project": os.path.basename(os.path.normpath(cwd)) if cwd else None,
        "from_model": (info or {}).get("model"), "model": target_model,
        "effort": (record or {}).get("effort"),
        "permission_mode": lower_mode(permission_mode, (record or {}).get("permissionMode")),
        "prompt": prompt, "tokens": None if size.get("tokens") is None else int(size["tokens"]),
        "budget_mode": budget_mode, "part": part + 1,
        "old_title": f"{base} · part {part} ({name(cur)})",
        "new_title": f"{base} · part {part + 1} ({name(tgt)})",
        "base_title": base, "not_carried": not_carried(record),
        "handoff_path": sb.path("handoff", rid + ".md"),
    }
    save(rec)
    return rec


def part2_prompt(rec):
    """The exact first message of part 2 (built by acpmh_bridge.py once the handoff exists)."""
    return (f"{TAG}{rec['id']}] This chat continues \"{rec['base_title']}\" (part {rec['part'] - 1}), handed off "
            f"automatically by Autoswitch. First read the handoff file `{rec['handoff_path']}` in full: it says how "
            f"to look up details of the earlier chat. Then answer the user's message below as if they had sent it here.\n\n"
            f"the user's message:\n{rec['prompt']}")


def instruction(rec):
    """additionalContext for the current chat. Short and in order; the tool calls are the app's own."""
    bridge = os.path.join(HERE, "acpmh_bridge.py")
    tgt = name(tier_of(rec["model"]))
    effort = f', effort "{rec["effort"]}"' if rec.get("effort") else ""
    missing = ", ".join(rec["not_carried"])
    return (
        f"[Autoswitch handoff {rec['id']}] The user turned on automatic handoff in the Autoswitch settings "
        f"(AUTOSWITCH_HANDOFF=on). This chat is long (about {(rec['tokens'] or 0) // 1000}k tokens) and their "
        f"message above fits {tgt}, so it continues in a new chat on {tgt}. Do NOT answer the message here. "
        f"Do exactly these steps, nothing else:\n"
        f"1. Bash (timeout 600000 ms): python3 '{bridge}' build {rec['id']}\n"
        f"   It prints one JSON line. If \"ok\" is false, stop the steps: answer the user's message here yourself, after "
        f"one line saying the handoff could not be built.\n"
        f"2. start_session: initiation \"user_asked\" (the user enabled automatic handoff in settings), context \"fresh\", "
        f"use_worktree false, model \"{rec['model']}\"{effort}, permission_mode \"{rec['permission_mode']}\", "
        f"title \"{rec['new_title']}\", prompt = the JSON's \"prompt\" value character for character, "
        f"background = the JSON's \"background\" value.\n"
        f"3. detach_session on the new session.\n"
        f"4. set_session_title: this chat (\"self\") to \"{rec['old_title']}\"; the new session to \"{rec['new_title']}\".\n"
        f"5. list_groups; if a group named \"{rec['project']}\" exists, move_sessions the new session into it.\n"
        f"6. open_session_in the new session, target \"split\". If it is refused (window hidden), skip it.\n"
        f"7. Reply here with exactly one line: \"Continued in part {rec['part']} ({tgt}): <link to the new session>. "
        f"Not carried over: {missing}.\""
    )


def deliver(raw, app_id, cli_id):
    """Part 2's first message. None if it carries no known tag; else the hook output (dict or None) to print.
    Marks the request delivered exactly once, and pins part 2 to the model it started on for this message."""
    m = _TAG.search(sb.unwrap(raw))
    if not m:
        return None
    if not load(m.group(1)):
        return None
    with locked(m.group(1)):
        rec = load(m.group(1))
        if rec.get("status") == "delivered":
            if rec.get("part2_cli") == cli_id:
                return {"out": None}  # the same chat re-ran the hook: never routed
            return {"out": {"continue": False,
                            "stopReason": f"↪ Already continued in part {rec.get('part')}: duplicate not sent"}}
        if rec.get("status") not in OPEN:  # timeout_noted: the user was told to resend, so part 2 must not answer too
            return {"out": {"continue": False,
                            "stopReason": f"↪ This handoff was cancelled ({rec.get('status')}): part "
                                          f"{rec.get('part', 2) - 1} asked the user to send the message again. Nothing to do here."}}
        rec.update(status="delivered", delivered_ts=time.time(), part2_session=app_id, part2_cli=cli_id)
        text = sb.unwrap(raw)
        rec["verbatim"] = " ".join(rec.get("prompt", "").split()) in " ".join(text.split())
        save(rec)
    if app_id:
        import pins
        pins.record_watcher_set(app_id, rec["model"], rec["id"])  # the model part 2 started on is expected, not a hand pick
        pins.observe(app_id, rec["model"])
    record_want(app_id, tier_of(rec["model"]))
    if not rec["verbatim"]:
        return {"out": {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                "[Autoswitch] The message above was copied with changes. The user's exact message is:\n" + rec["prompt"]}}}
    return {"out": None}


def timeout_notes(app_id, now=None):
    """Old chat: requests whose tag never reached part 2 by deadline(). Shown once; the file is kept."""
    now = time.time() if now is None else now
    notes = []
    for r in all_records():
        if r.get("session_id") != app_id or r.get("status") not in OPEN or now <= deadline(r):
            continue
        with locked(r["id"]):
            r = load(r["id"])
            if not r or r.get("status") not in OPEN or now <= deadline(r):
                continue
            r["status"] = "timeout_noted"
            r["noted_ts"] = now
            save(r)
            notes.append(f" | Autoswitch could not continue your message in a new chat, please send it again: "
                         f"{r.get('prompt', '')[:500]!r}")
    return "".join(notes)


# ---------- Watcher safety ----------

def sweep_held(now=None):
    """Held (seamless) requests nobody claimed within AUTOSWITCH_HOLD_TIMEOUT (15 s): the Watcher is gone.
    Closed here, no Watcher needed: a held prompt leaves the "please resend" note for its chat, a no-resend
    request (the prompt already went through) closes quietly as stale."""
    now = time.time() if now is None else now
    limit = _f("AUTOSWITCH_HOLD_TIMEOUT", "15")
    n = 0
    for p in glob.glob(sb.path("pending", "*.json")):
        try:
            if now - os.path.getmtime(p) <= limit:
                continue
            with open(p) as f:
                req = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(req, dict) or now - req.get("ts", now) <= limit:
            continue
        import finish
        if finish.mark_done(p, req, "lost" if req.get("resend", True) else "stale"):
            n += 1
    return n


def heartbeat_owner():
    """The Watcher session id written into watcher.alive by watch.sh, or None."""
    try:
        with open(sb.path("watcher.alive")) as f:
            v = f.read().strip()
        return v if _key_ok(v) else None
    except OSError:
        return None


def paused_note(chat_key):
    """Once per chat per outage: the hook won't hold messages while the Watcher's heartbeat is stale."""
    if not _key_ok(chat_key):
        return ""
    try:
        beat = str(int(os.path.getmtime(sb.path("watcher.alive"))))
    except OSError:
        beat = "none"
    p = sb.path("paused", chat_key)
    try:
        with open(p) as f:
            if f.read().strip() == beat:
                return ""
    except OSError:
        pass
    os.makedirs(sb.path("paused"), exist_ok=True)
    with open(p, "w") as f:
        f.write(beat)
    return " | Autoswitch is paused: the Watcher isn't running"


def _watcher_transcript(watcher_id):
    from resolve import SESSIONS
    for path in glob.glob(os.path.join(SESSIONS, "*", "*", "local_*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("sessionId") == watcher_id and d.get("cliSessionId"):
            projects = os.path.expanduser(os.environ.get("AUTOSWITCH_PROJECTS", "~/.claude/projects"))
            hits = glob.glob(os.path.join(projects, "*", d["cliSessionId"] + ".jsonl"))
            return hits[0] if hits else None
    return None


def watcher_size_note(now=None):
    """Once per Watcher chat: its own chat passed AUTOSWITCH_WATCHER_MAX_TOKENS (50k real tokens). The Watcher
    can't start a fresh chat itself (Step 2.6), so the user is told in their next chat. Checked every 5 minutes."""
    now = time.time() if now is None else now
    wid = heartbeat_owner()
    if not wid or os.path.exists(sb.path("watcher_big", wid)):
        return ""
    cache = sb.path("watcher_size.json")
    try:
        with open(cache) as f:
            c = json.load(f)
        if c.get("id") == wid and now - c.get("ts", 0) < _f("AUTOSWITCH_WATCHER_CHECK_EVERY", "300"):
            return ""
    except (OSError, ValueError, AttributeError):
        pass
    import sizes
    tr = _watcher_transcript(wid)
    tokens = sizes.estimate(tr, force_precise=True)["tokens"] if tr else None
    with open(cache, "w") as f:
        json.dump({"id": wid, "ts": now, "tokens": tokens}, f)
    if tokens is None or tokens <= _f("AUTOSWITCH_WATCHER_MAX_TOKENS", "50000"):
        return ""
    os.makedirs(sb.path("watcher_big"), exist_ok=True)
    open(sb.path("watcher_big", wid), "w").close()
    return " | Watcher chat is getting big: start a fresh one with `autoswitch watcher`"
