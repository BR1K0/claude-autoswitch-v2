"""Bridge from the router to ACPMH. The router only calls build_handoff(transcript, cwd) -> path (or None).

  python3 acpmh_bridge.py build <request id>    run by the chat that hands off (see handoff.instruction)

ACPMH is run from AUTOSWITCH_ACPMH (default: the acpmh/ package bundled in this repo) as a separate process, so a new ACPMH version needs
no router change:
  1. python3 -m acpmh.cli ingest --store <log> <transcript>      the word-for-word journal (free)
  2. python3 -m acpmh.cli sync --store <log>                     Haiku facts, only if ANTHROPIC_API_KEY is set
  3. the handoff, with the version that won Step 1 for this chat size and budget mode (docs/BENCHMARK.md,
     exactly the options measured there): v4 = full_log, tail 8000; v5 = user_log, tail 8000;
     v5b = user_log, tail 20000 (both v5s capped at 40000 estimated tokens, about 65k real). Balanced: v4
     under 300k real tokens, v5b above. Quality: v4. Save: v4 under 300k, v5 above
     (v5 scored 80-84% on the 260k agent-driven coding session, under save's 85% floor).
     The acpmh CLI has no flags for user_log / max_tokens, so this step calls handoff.build through
     `python3 -c` in the same ACPMH directory.
All steps together are limited to AUTOSWITCH_HANDOFF_BUILD_MAX (540 s, under the instruction's 600 s Bash
timeout); the handoff wait (handoff.deadline) covers that limit, and a request that timed out meanwhile is
never overwritten with `built`.
The project log is ~/.acpmh/<project slug> (AUTOSWITCH_ACPMH_HOME), the slug being the name of the
transcript's project folder (~/.claude/projects/<slug>/<chat>.jsonl). Any error: no handoff (None), never an
exception. A failed sync is not an error: the handoff then has the journal and verbatim log only.
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

V5 = {"user_log": True, "tail_tokens": 8000, "max_tokens": 40000}
V5B = {"user_log": True, "tail_tokens": 20000, "max_tokens": 40000}
V4 = {"full_log": True, "tail_tokens": 8000}
_BUILD = ("import sys, json\n"
          "from acpmh import handoff\n"
          "from acpmh.store import Store\n"
          "text, stats = handoff.build(Store(sys.argv[1]), **json.loads(sys.argv[2]))\n"
          "sys.stdout.write(text)\n")


def acpmh_dir():
    return os.path.expanduser(os.environ.get("AUTOSWITCH_ACPMH") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def slug(transcript, cwd=None):
    src = os.path.basename(os.path.dirname(os.path.abspath(transcript))) if transcript else ""
    if not src and cwd:
        src = os.path.abspath(cwd).replace(os.sep, "-")
    s = re.sub(r"[^A-Za-z0-9._-]", "-", src).strip(".") or "default"
    return s[:200]


def store_dir(transcript, cwd=None):
    home = os.path.expanduser(os.environ.get("AUTOSWITCH_ACPMH_HOME", "~/.acpmh"))
    return os.path.join(home, slug(transcript, cwd))


def options(budget_mode="balanced", tokens=None):
    """Step 1 winners by mode and size (Sonnet incoming, 50 questions + 3 coding tasks)."""
    big = tokens is not None and tokens >= 300000
    if budget_mode == "quality":
        return dict(V4)
    if budget_mode == "save":
        return dict(V5) if big else dict(V4)
    return dict(V5B) if big else dict(V4)


def _run(args, timeout, end=None):
    """One ACPMH step, limited to `timeout` s and to what is left before `end` (the build's total limit)."""
    if end is not None:
        timeout = min(timeout, end - time.time())
        if timeout <= 0:
            raise subprocess.TimeoutExpired(args, 0)
    return subprocess.run([sys.executable] + args, cwd=acpmh_dir(), capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def build_max():
    import handoff
    return handoff.build_max()


def build_handoff(transcript, cwd, out=None, budget_mode="balanced", tokens=None, log=None):
    """Ingest the chat, sync if a key is set, build the handoff and write it to `out`. Returns the path or None."""
    steps = log if log is not None else []
    end = time.time() + build_max()  # all steps together stay under the handoff wait (handoff.deadline)
    try:
        if not transcript or not os.path.isfile(transcript) or not os.path.isdir(os.path.join(acpmh_dir(), "acpmh")):
            steps.append("missing transcript or ACPMH")
            return None
        store = store_dir(transcript, cwd)
        os.makedirs(store, exist_ok=True)
        r = _run(["-m", "acpmh.cli", "ingest", "--store", store, transcript], 600, end)
        steps.append("ingest %d" % r.returncode)
        if r.returncode != 0:
            return None
        if os.environ.get("ANTHROPIC_API_KEY"):
            r = _run(["-m", "acpmh.cli", "sync", "--store", store], 900, end)
            steps.append("sync %d" % r.returncode)  # a failed sync still leaves the verbatim journal
        else:
            steps.append("sync skipped: no ANTHROPIC_API_KEY")
        r = _run(["-c", _BUILD, store, json.dumps(options(budget_mode, tokens))], 300, end)
        steps.append("handoff %d" % r.returncode)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        out = out or os.path.join(store, "handoff-%d.md" % int(time.time()))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w") as f:
            f.write(r.stdout)
        os.replace(tmp, out)
        return out
    except Exception as e:  # never break the caller
        steps.append("error %s" % type(e).__name__)
        return None


def build_request(rid):
    """CLI `build <id>`: build the handoff for a request and print what the chat needs for start_session."""
    import handoff
    rec = handoff.load(rid) if re.fullmatch(r"[0-9a-f]{12}", rid or "") else None
    if not rec:
        return {"ok": False, "error": "unknown handoff request"}
    if rec.get("status") not in ("pending", "built"):
        return {"ok": False, "error": "request is %s" % rec.get("status")}
    steps = []
    path = build_handoff(rec.get("transcript"), rec.get("cwd"), out=rec.get("handoff_path"),
                         budget_mode=rec.get("budget_mode", "balanced"), tokens=rec.get("tokens"), log=steps)
    with handoff.locked(rid):
        rec = handoff.load(rid) or rec
        rec["build_steps"] = steps
        if rec.get("status") not in ("pending", "built"):
            # timed out (or closed) while building: the user was told to resend, never start part 2 now
            handoff.save(rec)
            return {"ok": False, "error": "request is %s (the build took too long)" % rec.get("status")}
        if not path:
            rec["status"] = "build_failed"
            handoff.save(rec)
            return {"ok": False, "error": "; ".join(steps)}
        rec.update(status="built", built_ts=time.time(), handoff_path=path)
        handoff.save(rec)
    return {"ok": True, "handoff": path, "prompt": handoff.part2_prompt(rec),
            "background": ("Part %d of the chat \"%s\", continued automatically by Autoswitch because the chat had "
                           "grown long and the next message fits a cheaper model. The handoff file %s summarises "
                           "the earlier chat and says how to look up details." % (rec["part"], rec["base_title"], path))}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "build":
        try:
            res = build_request(sys.argv[2])
        except Exception as e:
            res = {"ok": False, "error": type(e).__name__}
        print(json.dumps(res))
    else:
        sys.exit(__doc__)
