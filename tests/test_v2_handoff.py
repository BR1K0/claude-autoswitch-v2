"""Tests for Autoswitch v2 (AUTOSWITCH_HANDOFF). Every test runs in its own temp AUTOSWITCH_DIR.

  python3 tests/sb_test_handoff.py
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SB = os.path.join(REPO, "hook")
sys.path.insert(0, SB)
sys.path.insert(0, HERE)

from v2_helpers import ID, ID2, _read_json, _read_jsonl, _stop, _write_json, _write_text, fake_clipimg  # noqa: E402


def _read_text(path):
    with open(path) as f:
        return f.read()


def entry(kind, text=None, blocks=None, **kw):
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, **kw}, separators=(",", ":")) + "\n"


def transcript(path, user_msgs=(), filler_chars=0, extra=""):
    """A transcript with the given user messages, an assistant reply after each, and `filler_chars` of
    tool-result text (counted as conversation)."""
    with open(path, "w") as f:
        if filler_chars:
            f.write(entry("assistant", blocks=[{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}}]))
            f.write(entry("user", blocks=[{"type": "tool_result", "tool_use_id": "t1", "content": "y" * filler_chars}]))
        for m in user_msgs:
            f.write(entry("user", m))
            f.write(entry("assistant", "ok, noted."))
        f.write(extra)
    return path


class Scorer:
    """Fake scorer: probs chosen by a function of the text, so the context call can differ from the message's."""
    def __init__(self, test, fn):
        calls = self.calls = []
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(s):
                body = json.loads(s.rfile.read(int(s.headers["Content-Length"])))
                calls.append(body["text"])
                probs = fn(body["text"])
                s.send_response(200); s.end_headers()
                s.wfile.write(json.dumps({"label": max(probs, key=probs.get), "probs": probs}).encode())
            def log_message(s, *a): pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        test.addCleanup(_stop, self.srv)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"


def fixed(h, s, o):
    return lambda text: {"haiku": h, "sonnet": s, "opus": o}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.env = {**os.environ, "AUTOSWITCH_DIR": self.d, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s"),
                    "CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_HOST_SESSION_ID": ID,
                    "AUTOSWITCH_CLIPIMG": fake_clipimg(False), "AUTOSWITCH_HANDOFF": "on",
                    "AUTOSWITCH_PROJECTS": os.path.join(self.d, "projects")}
        for k in ("AUTOSWITCH_BUDGET", "AUTOSWITCH_LIMIT", "AUTOSWITCH_CONTEXT", "AUTOSWITCH_CALIBRATION",
                  "AUTOSWITCH_MODE", "AUTOSWITCH_SPEED", "AUTOSWITCH_SPEED", "ANTHROPIC_API_KEY"):
            self.env.pop(k, None)
        os.makedirs(os.path.join(self.d, "s"), exist_ok=True)
        _write_text(os.path.join(self.d, "watcher.alive"), "")
        self._saved = dict(os.environ)
        os.environ.clear(); os.environ.update(self.env)
        self.addCleanup(self._restore)
        import importlib
        import sbqueue
        importlib.reload(sbqueue)
        for m in ("handoff", "budget", "sizes", "pins", "finish"):
            if m in sys.modules:
                importlib.reload(sys.modules[m])

    def _restore(self):
        os.environ.clear(); os.environ.update(self._saved)

    def record(self, model="claude-sonnet-5-5", cli="c1", sid=ID, **kw):
        p = os.path.join(self.d, "s", "a", "b"); os.makedirs(p, exist_ok=True)
        _write_json(os.path.join(p, sid + ".json"), {"sessionId": sid, "cliSessionId": cli, "model": model,
                                                     "title": "Router work", "effort": "high",
                                                     "permissionMode": "acceptEdits", **kw})

    def hook(self, prompt, scorer, tr=None, cli="c1", mode="enforce", env=None, cwd="/tmp/proj"):
        if tr is None:
            tr = transcript(os.path.join(self.d, "small.jsonl"), ["hello there"])
        e = {**self.env, "AUTOSWITCH_MODE": mode, "AUTOSWITCH_SCORER": scorer.url, **(env or {})}
        out = subprocess.run([sys.executable, os.path.join(SB, "hint.py")], capture_output=True, text=True, env=e,
                             input=json.dumps({"prompt": prompt, "session_id": cli, "transcript_path": tr,
                                               "cwd": cwd, "permission_mode": "bypassPermissions"}))
        self.assertEqual(out.returncode, 0, out.stderr)
        try:
            return json.loads(out.stdout) if out.stdout.strip() else {}
        except ValueError:
            self.fail("hook printed non-JSON: %r" % out.stdout)

    def shadow(self):
        p = os.path.join(self.d, "shadow.jsonl")
        return _read_jsonl(p) if os.path.exists(p) else []

    def pending(self):
        p = os.path.join(self.d, "pending")
        return sorted(os.listdir(p)) if os.path.isdir(p) else []

    def handoffs(self):
        import handoff
        return handoff.all_records()

    def long_tr(self, msgs=("we are tuning the router thresholds",), name="long.jsonl"):
        # 300k characters of tool output: 120k real tokens, well above every break-even
        return transcript(os.path.join(self.d, name), msgs, filler_chars=300000)


# ---------- 1. sizes ----------

class SizeTest(Base):
    def test_precise_counts_conversation_text_not_file_bloat(self):
        import sizes
        p = os.path.join(self.d, "t.jsonl")
        bloat = json.dumps({"type": "api-request-blob", "blob": "z" * 400000}) + "\n"
        transcript(p, ["a" * 1000], extra=bloat + entry("user", "x" * 9000, isSidechain=True)
                   + entry("assistant", blocks=[{"type": "thinking", "thinking": "q" * 50000}]))
        est = sizes.estimate(p)
        self.assertEqual(est["method"], "precise")                     # over 120 KB: counted
        chars = 1000 + len("ok, noted.")
        self.assertAlmostEqual(est["precise"], chars / 4 * 1.6)        # sidechain, thinking, blobs left out
        self.assertGreater(est["cheap"], est["precise"] * 5)

    def test_compaction_restarts_the_count_and_images_have_a_fixed_cost(self):
        import sizes
        p = os.path.join(self.d, "t.jsonl")
        boundary = json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n"
        transcript(p, ["b" * 40000], extra=boundary + entry("user", blocks=[{"type": "text", "text": "c" * 400},
                                                                          {"type": "image", "source": {}}]))
        self.assertAlmostEqual(sizes.precise(p), 400 / 4 * 1.6 + 1500)

    def test_small_file_uses_the_cheap_path_and_factor_is_a_setting(self):
        import sizes
        p = transcript(os.path.join(self.d, "t.jsonl"), ["short"])
        est = sizes.estimate(p)
        self.assertEqual(est["method"], "cheap"); self.assertIsNone(est["precise"])
        self.assertAlmostEqual(est["tokens"], os.path.getsize(p) / 20)
        os.environ["AUTOSWITCH_TOKEN_FACTOR"] = "2"
        self.assertAlmostEqual(sizes.estimate(p, force_precise=True)["tokens"], (5 + 10) / 4 * 2)

    def test_unreadable_is_unknown(self):
        import sizes
        self.assertEqual(sizes.estimate(os.path.join(self.d, "nope.jsonl"))["method"], "unknown")
        self.assertIsNone(sizes.estimate(None)["tokens"])

    def test_hook_logs_both_estimates(self):
        self.record()
        sc = Scorer(self, fixed(0.1, 0.8, 0.1))
        self.hook("explain the plan", sc, tr=self.long_tr(), mode="shadow")
        r = self.shadow()[-1]
        self.assertEqual(r["size_method"], "precise")
        self.assertGreater(r["size_cheap"], 0); self.assertGreater(r["size_precise"], 100000)
        self.assertIn("history_tokens_est", r)                          # today's bytes/4 estimate is still logged


# ---------- 2. budget modes and pacing ----------

class BudgetTest(Base):
    def test_mode_gates(self):
        import budget
        self.assertEqual(budget.mode(), "balanced")
        self.assertEqual(budget.gates("balanced"), {"down": 0.70, "up": 0.50})
        self.assertEqual(budget.gates("quality"), {"down": 0.80, "up": 0.40})
        self.assertEqual(budget.gates("save"), {"down": 0.60, "up": 0.60})
        os.environ["AUTOSWITCH_DOWN_THRESHOLD"] = "0.75"                # balanced keeps today's settings
        self.assertEqual(budget.gates("balanced")["down"], 0.75)
        os.environ["AUTOSWITCH_GATE_SAVE_DOWN"] = "0.55"
        self.assertEqual(budget.gates("save")["down"], 0.55)
        _write_text(os.path.join(self.d, "budget"), "save\n")
        self.assertEqual(budget.mode(), "save")
        os.environ["AUTOSWITCH_BUDGET"] = "bogus"
        self.assertEqual(budget.mode(), "balanced")

    def test_limit_parsing(self):
        import budget
        for raw, want in (("5/day", (5.0, "usd", "day")), ("$20 / week", (20.0, "usd", "week")),
                          ("80%/week", (80.0, "pct", "week")), ("3 per day", (3.0, "usd", "day"))):
            os.environ["AUTOSWITCH_LIMIT"] = raw
            lim = budget.limit()
            self.assertEqual((lim["amount"], lim["unit"], lim["period"]), want, raw)
        for raw in ("", "lots", "0/day", "5/month"):
            os.environ["AUTOSWITCH_LIMIT"] = raw
            self.assertIsNone(budget.limit(), raw)

    def _usage(self, **kw):
        _write_json(os.path.join(self.d, "usage.json"), {"ts": time.time(), "period": "week",
                                                        "period_start": time.time() - 3.5 * 86400,
                                                        "period_end": time.time() + 3.5 * 86400, **kw})

    def test_pacing_moves_one_mode(self):
        import budget
        os.environ["AUTOSWITCH_LIMIT"] = "80%/week"
        self.assertEqual(budget.effective_mode()[0], "balanced")         # no usage file: no adjustment
        self._usage(used_pct=60)                                         # half the week, 60 of 80: pace 1.5
        m, note = budget.effective_mode()
        self.assertEqual(m, "save"); self.assertIn("ahead", note)
        self._usage(used_pct=20)                                         # pace 0.5
        self.assertEqual(budget.effective_mode()[0], "quality")
        self._usage(used_pct=40)                                         # on pace
        self.assertEqual(budget.effective_mode()[0], "balanced")
        os.environ["AUTOSWITCH_BUDGET"] = "save"; self._usage(used_pct=79)
        self.assertEqual(budget.effective_mode()[0], "save")             # already at the end
        os.environ["AUTOSWITCH_LIMIT"] = "10/week"; os.environ["AUTOSWITCH_BUDGET"] = "balanced"
        self._usage(used_usd=1)                                          # dollars: 1 of 5 expected
        self.assertEqual(budget.effective_mode()[0], "quality")

    def test_pacing_ignores_stale_other_period_and_early_usage(self):
        import budget
        os.environ["AUTOSWITCH_LIMIT"] = "80%/week"
        _write_json(os.path.join(self.d, "usage.json"), {"ts": time.time() - 7200, "used_pct": 79})
        self.assertIsNone(budget.pace(budget.limit(), budget.usage()))
        _write_json(os.path.join(self.d, "usage.json"), {"ts": time.time(), "period": "day", "used_pct": 79})
        self.assertIsNone(budget.pace(budget.limit(), budget.usage()))
        _write_json(os.path.join(self.d, "usage.json"), {"ts": time.time(), "used_pct": 5,
                                                        "period_start": time.time() - 60, "period_end": time.time() + 7 * 86400})
        self.assertIsNone(budget.pace(budget.limit(), budget.usage()))   # first 5% of the period

    def test_calibration_map_is_off_by_default_and_uses_the_formula_when_set(self):
        import budget
        self.assertIsNone(budget.calibration())
        self.assertEqual(budget.calibrate(0.42, None), 0.42)
        p = os.path.join(self.d, "cal.json"); _write_json(p, {"points": [[0, 0.6], [1, 0.8]]})
        os.environ["AUTOSWITCH_CALIBRATION"] = p
        pts = budget.calibration()
        self.assertAlmostEqual(budget.calibrate(0.5, pts), 0.7)
        g = budget.gates("balanced", "sonnet", "haiku", pts)             # S 0.02, R 0.25, lambda 1
        self.assertAlmostEqual(g["down"], 0.25 / 0.27); self.assertAlmostEqual(g["up"], 0.02 / 0.27)
        self.assertGreater(budget.gates("quality", "sonnet", "haiku", pts)["down"], g["down"])

    def test_save_mode_switches_down_where_balanced_stays(self):
        self.record()
        sc = Scorer(self, fixed(0.65, 0.3, 0.05))
        p = "summarize the following paragraph about the weather in two lines"  # > 20 words? no: no context needed
        self.assertNotIn("continue", self.hook(p, sc))
        self.assertEqual(self.pending(), [])
        out = self.hook(p, sc, env={"AUTOSWITCH_BUDGET": "save"})
        self.assertIs(out.get("continue"), False)
        self.assertEqual(len(self.pending()), 1)


# ---------- 3. context blend ----------

def ref_limited(p_alone, p_ctx, text, ctx):
    """context_test.limited() verbatim, with numpy."""
    import numpy as np
    from ctxblend import content
    p_alone, p_ctx = np.array(p_alone), np.array(p_ctx)
    if len(text.split()) > 20:
        return p_alone
    words = content(text)
    if len(words) >= 3 and not words & content(ctx):
        return p_alone
    p = 0.7 * p_alone + 0.3 * p_ctx
    if p[0] > p_alone[0] + 0.05:
        rest = p[1:] / p[1:].sum() if p[1:].sum() else np.array([0.5, 0.5])
        h = p_alone[0] + 0.05
        p = np.concatenate([[h], rest * (1 - h)])
    return p


class ContextTest(Base):
    def test_matches_the_step1_reference(self):
        import random
        from ctxblend import limited
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed")
        rnd = random.Random(7)
        texts = ["ok do it", "fix the router thresholds now", "what about bananas mangoes papayas", "x " * 30,
                 "router", "tune the thresholds of the router please"]
        ctx = "Current task: router thresholds.\nwe tune the router"
        for _ in range(300):
            a = [rnd.random() for _ in range(3)]; c = [rnd.random() for _ in range(3)]
            a = [x / sum(a) for x in a]; c = [x / sum(c) for x in c]
            t = rnd.choice(texts)
            for x, y in zip(limited(a, c, t, ctx), ref_limited(a, c, t, ctx)):
                self.assertAlmostEqual(x, float(y))

    def test_rules(self):
        from ctxblend import limited
        ctx = "Current task: router thresholds.\nplease tune the router"
        self.assertEqual(limited([0.9, 0.1, 0], [0, 0, 1], "w " * 21, ctx), [0.9, 0.1, 0])       # long: alone
        self.assertEqual(limited([0.9, 0.1, 0], [0, 0, 1], "bananas mangoes papayas", ctx), [0.9, 0.1, 0])  # topic change
        up = limited([0.2, 0.6, 0.2], [1, 0, 0], "ok go", ctx)                                  # raise haiku: +0.05 max
        self.assertAlmostEqual(up[0], 0.25); self.assertAlmostEqual(sum(up), 1)
        down = limited([0.8, 0.2, 0], [0, 0, 1], "ok go", ctx)                                  # harder tier: freely
        self.assertAlmostEqual(down[0], 0.56); self.assertAlmostEqual(down[2], 0.3)

    def test_recent_messages_and_new_task(self):
        import ctxblend
        tr = transcript(os.path.join(self.d, "t.jsonl"), ["one router", "two router", "three router", "four router"])
        self.assertEqual(ctxblend.recent_user_messages(tr), ["two router", "three router", "four router"])
        prev = ["tune the router thresholds", "check the router gates"]
        self.assertFalse(ctxblend.new_task("ok do it", prev))
        self.assertFalse(ctxblend.new_task(" ".join(["router thresholds gates"] * 10), prev))   # same topic
        fresh = ("Separately, please write a birthday poem for my grandmother Margaret, mentioning her garden, "
                 "roses, tulips, chickens, knitting, cooking, and her famous apple crumble recipe from Yorkshire")
        self.assertTrue(ctxblend.new_task(fresh, prev))

    def test_hook_blends_short_messages_and_can_be_switched_off(self):
        self.record()
        # alone the message looks like Haiku 90%; with the context it looks like Opus
        sc = Scorer(self, lambda t: {"haiku": 0.05, "sonnet": 0.15, "opus": 0.8} if "Current task" in t
                    else {"haiku": 0.9, "sonnet": 0.1, "opus": 0.0})
        tr = transcript(os.path.join(self.d, "t.jsonl"), ["design the router threshold model"])
        out = self.hook("ok now the router part", sc, tr=tr)
        self.assertNotIn("continue", out)                                # 0.7*0.9+0.3*0.05 = 0.645 < 0.70: stays
        r = self.shadow()[-1]
        self.assertTrue(r["ctx_blend"]); self.assertAlmostEqual(r["confidence"], 0.645, places=3)
        self.assertIsNone(r["would_target"])
        out = self.hook("ok now the router part", sc, tr=tr, env={"AUTOSWITCH_CONTEXT": "off"})
        self.assertIs(out.get("continue"), False)                        # alone: Haiku 90%, switched in place
        self.assertFalse(self.shadow()[-1]["ctx_blend"])


# ---------- 4. thresholds ----------

class PlanTest(Base):
    def test_every_rule(self):
        from handoff import plan
        P = lambda t, m="precise": {"tokens": t, "method": m}
        self.assertEqual(plan(None, "sonnet", P(10))["action"], "stay")
        self.assertEqual(plan("haiku", "sonnet", P(None, "unknown"))["action"], "inplace")
        self.assertEqual(plan("haiku", "sonnet", P(30000, "cheap"))["action"], "inplace")          # below: in place
        z = plan("haiku", "sonnet", P(52000, "cheap"))                                               # Haiku BE 55k, zone 49.5-60.5k
        self.assertEqual((z["action"], z["zone"]), ("stay", "zone"))                                 # zone, no precise count
        self.assertEqual(plan("haiku", "sonnet", P(52000))["action"], "inplace")                     # zone, precise, below
        self.assertEqual(plan("haiku", "sonnet", P(58000))["action"], "stay")                        # zone, above, one-off
        self.assertEqual(plan("haiku", "sonnet", P(58000), prev_want="haiku")["action"], "handoff")
        self.assertEqual(plan("sonnet", "opus", P(65000))["action"], "inplace")                      # Sonnet BE 70k
        self.assertEqual(plan("sonnet", "opus", P(90000))["action"], "stay")                         # one-off
        self.assertEqual(plan("sonnet", "opus", P(90000), prev_want="haiku")["action"], "stay")      # previous wanted another tier
        self.assertEqual(plan("sonnet", "opus", P(90000), starts_new_task=True)["action"], "handoff")
        self.assertEqual(plan("sonnet", "opus", P(90000), "sonnet")["action"], "handoff")
        self.assertEqual(plan("opus", "haiku", P(90000))["action"], "stay")                          # up, one-off
        self.assertEqual(plan("opus", "haiku", P(90000), "opus")["action"], "inplace")               # up, lasting: in place
        self.assertEqual(plan("sonnet", "opus", P(200000, "cheap"))["action"], "stay")               # above, cheap only
        os.environ["AUTOSWITCH_BREAKEVEN_SONNET"] = "100000"
        self.assertEqual(plan("sonnet", "opus", P(90000))["action"], "inplace")

    def test_hook_long_chat_one_off_stays_then_lasting_hands_off(self):
        self.record(model="claude-opus-5-5")
        sc = Scorer(self, fixed(0.05, 0.9, 0.05))
        tr = self.long_tr()
        out = self.hook("tidy up the router threshold docs", sc, tr=tr)
        self.assertNotIn("continue", out); self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("one-off", self.shadow()[-1]["reason"]); self.assertEqual(self.pending(), [])
        out = self.hook("and the router gates docs too", sc, tr=tr)                          # second in a row
        self.assertNotIn("continue", out)                                                    # never held
        ctx = out["hookSpecificOutput"]["additionalContext"]
        (rec,) = self.handoffs()
        self.assertEqual(rec["status"], "pending"); self.assertEqual(rec["model"], "claude-sonnet-5-5")
        self.assertIn(rec["id"], ctx); self.assertIn("start_session", ctx); self.assertIn('"user_asked"', ctx)
        self.assertIn("Continuing in a new chat on Sonnet", out["systemMessage"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.shadow()[-1]["would_handoff"], "claude-sonnet-5-5")
        out = self.hook("and the router gates docs too", sc, tr=tr)                          # in progress: no second one
        self.assertEqual(len(self.handoffs()), 1)

    def test_same_prompt_during_a_slow_build_starts_no_second_handoff(self):
        import handoff
        self.record(model="claude-opus-5-5")
        sc = Scorer(self, fixed(0.05, 0.9, 0.05))
        tr = self.long_tr()
        self.hook("tidy up the router threshold docs", sc, tr=tr)
        self.hook("and the router gates docs too", sc, tr=tr)
        (rec,) = self.handoffs()
        rec["ts"] = time.time() - 400; handoff.save(rec)                     # build still running after 400 s
        out = self.hook("and the router gates docs too", sc, tr=tr)
        self.assertNotIn("hookSpecificOutput", out); self.assertEqual(len(self.handoffs()), 1)
        self.assertEqual(handoff.load(rec["id"])["status"], "pending")

    def test_hook_long_chat_new_task_hands_off_at_once(self):
        self.record(model="claude-opus-5-5")
        sc = Scorer(self, fixed(0.05, 0.9, 0.05))
        fresh = ("Separately, please write a birthday poem for my grandmother Margaret, mentioning her garden, "
                 "roses, tulips, chickens, knitting, cooking, and her famous apple crumble recipe from Yorkshire")
        out = self.hook(fresh, sc, tr=self.long_tr())
        self.assertIn("hookSpecificOutput", out)
        self.assertTrue(self.shadow()[-1]["new_task"])

    def test_hook_zone_counts_precisely_even_when_the_cheap_path_was_chosen(self):
        self.record(model="claude-opus-5-5")
        sc = Scorer(self, fixed(0.05, 0.9, 0.05))
        tr = self.long_tr()
        env = {"AUTOSWITCH_PRECISE_BYTES": "1000000000", "AUTOSWITCH_FILE_OVERCOUNT": "1"}  # cheap: about 75k, in the zone
        self.hook("tidy up the router threshold docs", sc, tr=tr, env=env, mode="shadow")
        r = self.shadow()[-1]
        self.assertEqual(r["size_method"], "precise"); self.assertGreater(r["size_precise"], 100000)
        self.assertEqual(r["zone"], "above")

    def test_hook_shadow_mode_never_hands_off(self):
        self.record(model="claude-opus-5-5")
        sc = Scorer(self, fixed(0.05, 0.9, 0.05))
        tr = self.long_tr()
        for _ in range(2):
            out = self.hook("tidy up the router threshold docs", sc, tr=tr, mode="shadow")
            self.assertNotIn("hookSpecificOutput", out)
        self.assertEqual(self.handoffs(), [])
        self.assertEqual(self.shadow()[-1]["would_handoff"], "claude-sonnet-5-5")

    def test_hook_short_chat_still_switches_in_place(self):
        self.record(model="claude-opus-5-5")
        out = self.hook("tidy up the docs", Scorer(self, fixed(0.05, 0.9, 0.05)))
        self.assertIs(out.get("continue"), False); self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.handoffs(), [])


# ---------- 5. bridge ----------

FAKE_CLI = r'''
import json, os, sys
log = os.environ["FAKE_ACPMH_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_ACPMH_FAIL") == sys.argv[1]:
    sys.exit(3)
'''
FAKE_HANDOFF = r'''
import json, os
def build(store, **kw):
    with open(os.environ["FAKE_ACPMH_LOG"], "a") as f:
        f.write(json.dumps(["build", kw]) + "\n")
    return "# ACPMH HANDOFF\nstore %s\n" % store.d, {}
'''
FAKE_STORE = "class Store:\n    def __init__(self, d):\n        self.d = d\n"


class BridgeTest(Base):
    def setUp(self):
        super().setUp()
        self.acpmh = os.path.join(self.d, "acpmh_repo"); pkg = os.path.join(self.acpmh, "acpmh"); os.makedirs(pkg)
        _write_text(os.path.join(pkg, "__init__.py"), "")
        _write_text(os.path.join(pkg, "cli.py"), FAKE_CLI)
        _write_text(os.path.join(pkg, "handoff.py"), FAKE_HANDOFF)
        _write_text(os.path.join(pkg, "store.py"), FAKE_STORE)
        self.log = os.path.join(self.d, "acpmh.log")
        os.environ.update(AUTOSWITCH_ACPMH=self.acpmh, AUTOSWITCH_ACPMH_HOME=os.path.join(self.d, "acpmh_home"),
                          FAKE_ACPMH_LOG=self.log)
        proj = os.path.join(self.d, "projects", "-Users-kem-proj"); os.makedirs(proj)
        self.tr = transcript(os.path.join(proj, "c1.jsonl"), ["hello"])

    def calls(self):
        return _read_jsonl(self.log) if os.path.exists(self.log) else []

    def test_without_api_key_no_sync(self):
        import acpmh_bridge as br
        out = br.build_handoff(self.tr, "/Users/me/proj", out=os.path.join(self.d, "h.md"))
        self.assertEqual(out, os.path.join(self.d, "h.md"))
        self.assertIn("acpmh_home/-Users-kem-proj", _read_text(out))
        cmds = [c[0] for c in self.calls()]
        self.assertEqual(cmds, ["ingest", "build"])
        self.assertEqual(self.calls()[0], ["ingest", "--store", os.path.join(self.d, "acpmh_home", "-Users-kem-proj"), self.tr])
        self.assertEqual(self.calls()[1][1], {"full_log": True, "tail_tokens": 8000})  # balanced, size unknown: v4

    def test_with_api_key_syncs(self):
        import acpmh_bridge as br
        os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
        self.assertTrue(br.build_handoff(self.tr, "/x"))
        self.assertEqual([c[0] for c in self.calls()], ["ingest", "sync", "build"])
        os.environ["FAKE_ACPMH_FAIL"] = "sync"                          # a failed sync still builds
        self.assertTrue(br.build_handoff(self.tr, "/x"))

    def test_version_follows_mode_and_size(self):
        import acpmh_bridge as br
        br.build_handoff(self.tr, "/x", budget_mode="quality", tokens=400000)
        self.assertEqual(self.calls()[-1][1], {"full_log": True, "tail_tokens": 8000})
        br.build_handoff(self.tr, "/x", budget_mode="balanced", tokens=400000)
        self.assertEqual(self.calls()[-1][1], {"user_log": True, "tail_tokens": 20000, "max_tokens": 40000})

    def test_errors_give_no_handoff(self):
        import acpmh_bridge as br
        os.environ["FAKE_ACPMH_FAIL"] = "ingest"
        self.assertIsNone(br.build_handoff(self.tr, "/x"))
        os.environ.pop("FAKE_ACPMH_FAIL")
        self.assertIsNone(br.build_handoff(os.path.join(self.d, "missing.jsonl"), "/x"))
        os.environ["AUTOSWITCH_ACPMH"] = os.path.join(self.d, "nowhere")
        self.assertIsNone(br.build_handoff(self.tr, "/x"))

    def test_build_cli_for_a_request(self):
        import handoff
        rec = handoff.create(ID, "c1", self.tr, "/Users/me/proj", "claude-haiku-5-5", 'fix "this" now\nplease',
                             {"model": "claude-sonnet-5-5", "title": "Router work"}, None, "default", {"tokens": 90000})
        out = subprocess.run([sys.executable, os.path.join(SB, "acpmh_bridge.py"), "build", rec["id"]],
                             capture_output=True, text=True, env=os.environ)
        res = json.loads(out.stdout)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["prompt"].startswith("[autoswitch-handoff:%s]" % rec["id"]))
        self.assertTrue(res["prompt"].endswith('fix "this" now\nplease'))
        self.assertIn(rec["handoff_path"], res["prompt"]); self.assertGreaterEqual(len(res["background"]), 80)
        self.assertEqual(handoff.load(rec["id"])["status"], "built")
        bad = subprocess.run([sys.executable, os.path.join(SB, "acpmh_bridge.py"), "build", "nonsense"],
                             capture_output=True, text=True, env=os.environ)
        self.assertFalse(json.loads(bad.stdout)["ok"])
        os.environ["FAKE_ACPMH_FAIL"] = "ingest"
        rec2 = handoff.create(ID, "c1", self.tr, "/x", "claude-haiku-5-5", "m", {}, None, "default", {"tokens": 1})
        res2 = json.loads(subprocess.run([sys.executable, os.path.join(SB, "acpmh_bridge.py"), "build", rec2["id"]],
                                         capture_output=True, text=True, env=os.environ).stdout)
        self.assertFalse(res2["ok"]); self.assertEqual(handoff.load(rec2["id"])["status"], "build_failed")

    def test_late_build_after_timeout_is_never_delivered(self):
        import acpmh_bridge as br
        import handoff
        rec = handoff.create(ID, "c1", self.tr, "/x", "claude-haiku-5-5", "slow words",
                             {"model": "claude-sonnet-5-5", "title": "Router work"}, None, "default", {"tokens": 90000})
        real = br.build_handoff

        def slow(*a, **kw):  # the old chat's next prompt times the request out while the build still runs
            self.assertIn("slow words", handoff.timeout_notes(ID, now=time.time() + 100000))
            return real(*a, **kw)
        br.build_handoff = slow
        self.addCleanup(setattr, br, "build_handoff", real)
        res = br.build_request(rec["id"])
        self.assertFalse(res["ok"]); self.assertIn("timeout_noted", res["error"]); self.assertNotIn("prompt", res)
        self.assertEqual(handoff.load(rec["id"])["status"], "timeout_noted")    # not overwritten with built
        got = handoff.deliver(handoff.part2_prompt(rec), ID2, "c2")              # a part 2 started anyway: stopped
        self.assertIs(got["out"]["continue"], False); self.assertIn("cancelled", got["out"]["stopReason"])
        self.assertEqual(handoff.load(rec["id"])["status"], "timeout_noted")
        self.assertIsNone(handoff.open_for(ID))

    def test_build_has_one_total_limit(self):
        import acpmh_bridge as br
        os.environ["AUTOSWITCH_HANDOFF_BUILD_MAX"] = "0"
        steps = []
        self.assertIsNone(br.build_handoff(self.tr, "/x", log=steps))
        self.assertEqual(steps, ["error TimeoutExpired"]); self.assertEqual(self.calls(), [])


# ---------- 6-8. the handoff request, delivery, settings ----------

class DeliveryTest(Base):
    def _rec(self, prompt="please do the thing", **kw):
        import handoff
        rec = handoff.create(ID, "c1", "/t.jsonl", "/Users/me/proj", "claude-haiku-5-5", prompt,
                             {"model": "claude-sonnet-5-5", "title": "Router work"},
                             {"effort": "high", "permissionMode": "acceptEdits",
                              "remoteMcpServersConfig": [{"name": "Claude Docs"}],
                              "sessionPermissionUpdates": [{"type": "addDirectories", "directories": ["/extra"]}]},
                             "bypassPermissions", {"tokens": 120000})
        rec.update(kw); handoff.save(rec)
        return rec

    def test_instruction_and_settings_never_above_part_1(self):
        import handoff
        rec = self._rec()
        self.assertEqual(rec["permission_mode"], "acceptEdits")          # the lower of hook and record
        self.assertEqual(handoff.lower_mode("bypassPermissions", "default"), "default")
        self.assertEqual(handoff.lower_mode(None, None), "default")
        self.assertEqual(handoff.lower_mode("plan", "auto"), "plan")
        self.assertEqual((rec["old_title"], rec["new_title"]), ("Router work · part 1 (Sonnet)", "Router work · part 2 (Haiku)"))
        self.assertEqual(handoff.split_title("Router work · part 2 (Haiku)"), ("Router work", 2))
        ins = handoff.instruction(rec)
        for s in ('initiation "user_asked"', 'model "claude-haiku-5-5"', 'effort "high"', 'permission_mode "acceptEdits"',
                  "detach_session", "set_session_title", 'group named "proj"', "open_session_in", "Continued in part 2 (Haiku)",
                  "connectors (Claude Docs)", "added folders (/extra)", "output style", "Do NOT answer"):
            self.assertIn(s, ins)

    def test_delivery_marks_once_pins_and_never_routes(self):
        import handoff
        rec = self._rec()
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID2                  # part 2's own id
        self.env["CLAUDE_CODE_HOST_SESSION_ID"] = ID2
        sc = Scorer(self, fixed(0.0, 0.0, 1.0))                          # would want Opus: must be ignored
        out = self.hook(handoff.part2_prompt(rec), sc, cli="c2")
        self.assertEqual(out, {}); self.assertEqual(sc.calls, []); self.assertEqual(self.pending(), [])
        r = handoff.load(rec["id"])
        self.assertEqual((r["status"], r["part2_session"], r["verbatim"]), ("delivered", ID2, True))
        self.assertEqual(_read_json(os.path.join(self.d, "lastset", ID2 + ".json"))["model"], "claude-haiku-5-5")
        out = self.hook(handoff.part2_prompt(rec), sc, cli="c9")          # a second copy elsewhere: stopped
        self.assertIs(out.get("continue"), False); self.assertIn("Already continued", out["stopReason"])

    def test_altered_copy_gets_the_exact_message(self):
        import handoff
        rec = self._rec(prompt="exact words here")
        out = self.hook("[autoswitch-handoff:%s] read the handoff. the user's message: other words" % rec["id"],
                        Scorer(self, fixed(1, 0, 0)), cli="c2")
        self.assertIn("exact words here", out["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(handoff.load(rec["id"])["verbatim"])

    def test_unknown_tag_is_routed_normally(self):
        self.record()
        self.hook("[autoswitch-handoff:0123456789ab] what is 2+2", Scorer(self, fixed(0.95, 0.05, 0)))
        self.assertEqual(len(self.shadow()), 1)

    def test_timeout_shows_please_resend_once_and_keeps_the_file(self):
        import handoff
        rec = self._rec(prompt="my lost words", ts=time.time() - 200, status="built", built_ts=time.time() - 121)
        self.record()
        out = self.hook("next question", Scorer(self, fixed(0.1, 0.8, 0.1)))
        self.assertIn("please send it again", out["systemMessage"]); self.assertIn("my lost words", out["systemMessage"])
        self.assertEqual(handoff.load(rec["id"])["status"], "timeout_noted")
        out = self.hook("next question", Scorer(self, fixed(0.1, 0.8, 0.1)))
        self.assertNotIn("my lost words", out["systemMessage"])
        self.assertTrue(os.path.exists(os.path.join(self.d, "handoff", rec["id"] + ".json")))

    def test_slow_build_keeps_the_request_open(self):
        import handoff
        rec = self._rec(prompt="my words", ts=time.time() - 300)              # past the 120 s wait, still building
        self.record()
        out = self.hook("next", Scorer(self, fixed(0.1, 0.8, 0.1)))
        self.assertNotIn("my words", out.get("systemMessage", ""))
        self.assertEqual(handoff.load(rec["id"])["status"], "pending")
        self.assertEqual(handoff.open_for(ID)["id"], rec["id"])
        rec = self._rec(prompt="my words", ts=time.time() - 700)              # past build limit + wait: lost
        self.assertIn("my words", handoff.timeout_notes(ID))
        self.assertEqual(handoff.load(rec["id"])["status"], "timeout_noted")

    def test_no_note_within_the_wait(self):
        self._rec(prompt="my words", ts=time.time() - 60)
        self.record()
        self.assertNotIn("my words", self.hook("next", Scorer(self, fixed(0.1, 0.8, 0.1)))["systemMessage"])


# ---------- 9-10. Watcher safety ----------

class WatcherTest(Base):
    def _held(self, age, resend=True, cli="c7"):
        os.makedirs(os.path.join(self.d, "pending"), exist_ok=True)
        p = os.path.join(self.d, "pending", "aaaaaaaaaaaa.json")
        _write_json(p, {"id": "aaaaaaaaaaaa", "session_id": ID, "cli_session_id": cli, "model": "claude-haiku-5-5",
                        "prompt": "stuck words", "ts": time.time() - age, "resend": resend})
        os.utime(p, (time.time() - age, time.time() - age))
        return p

    def test_stuck_request_without_a_watcher_becomes_a_resend_note(self):
        os.remove(os.path.join(self.d, "watcher.alive"))
        self._held(20)
        self.record(cli="c7")
        out = self.hook("hello again", Scorer(self, fixed(0.1, 0.8, 0.1)), cli="c7")
        self.assertEqual(self.pending(), [])
        self.assertEqual(_read_json(os.path.join(self.d, "done", "aaaaaaaaaaaa.json"))["status"], "lost")
        self.assertIn("stuck words", out["systemMessage"])

    def test_young_or_no_resend_requests(self):
        self._held(5)
        self.hook("hi", Scorer(self, fixed(0.1, 0.8, 0.1)), cli="c1")
        self.assertEqual(self.pending(), ["aaaaaaaaaaaa.json"])                          # under 15 s: left for the Watcher
        self._held(30, resend=False)
        self.hook("hi", Scorer(self, fixed(0.1, 0.8, 0.1)), cli="c1")
        self.assertEqual(_read_json(os.path.join(self.d, "done", "aaaaaaaaaaaa.json"))["status"], "stale")
        self.assertFalse(os.path.exists(os.path.join(self.d, "lost", "c7.json")))

    def test_paused_notice_once_per_chat_and_never_held(self):
        os.remove(os.path.join(self.d, "watcher.alive"))
        self.record()
        sc = Scorer(self, fixed(0.95, 0.05, 0))
        out = self.hook("what is 2+2", sc)
        self.assertNotIn("continue", out); self.assertEqual(self.pending(), [])
        self.assertIn("Autoswitch is paused: the Watcher isn't running", out["systemMessage"])
        self.assertNotIn("paused", self.hook("what is 3+3", sc)["systemMessage"])

    def _watch(self, env):
        return subprocess.Popen(["bash", os.path.join(SB, "watch.sh")], stdout=subprocess.PIPE, text=True,
                                env={**self.env, **env})

    def test_second_watcher_stops(self):
        alive = os.path.join(self.d, "watcher.alive")
        _write_text(alive, "local_other")
        w = self._watch({"AUTOSWITCH_WATCHER_ID": "local_me"})
        try:
            self.assertEqual(w.communicate(timeout=10)[0].strip(), "DUPLICATE local_other")
        finally:
            if w.poll() is None:
                w.kill(); w.communicate()
        self.assertEqual(_read_text(alive), "local_other")

    def test_stale_heartbeat_or_flag_off_keeps_running(self):
        alive = os.path.join(self.d, "watcher.alive")
        for env, stale in (({"AUTOSWITCH_WATCHER_ID": "local_me"}, True),
                           ({"AUTOSWITCH_WATCHER_ID": "local_me", "AUTOSWITCH_HANDOFF": "off"}, False)):
            _write_text(alive, "local_other")
            if stale:
                os.utime(alive, (time.time() - 100, time.time() - 100))
            w = self._watch(env)
            time.sleep(1.5)
            try:
                self.assertIsNone(w.poll(), env)
            finally:
                w.kill(); w.communicate()
            self.assertEqual(_read_text(alive), "local_me" if stale else "local_other")

    def test_big_watcher_chat_shows_a_note_once(self):
        _write_text(os.path.join(self.d, "watcher.alive"), ID2)
        self.record(sid=ID2, cli="wcli", model="claude-haiku-5-5")
        proj = os.path.join(self.d, "projects", "p"); os.makedirs(proj)
        transcript(os.path.join(proj, "wcli.jsonl"), ["watch"], filler_chars=200000)  # 80k real tokens
        self.record()
        sc = Scorer(self, fixed(0.1, 0.8, 0.1))
        self.assertIn("Watcher chat is getting big", self.hook("hi", sc)["systemMessage"])
        os.remove(os.path.join(self.d, "watcher_size.json"))
        self.assertNotIn("Watcher chat", self.hook("hi", sc)["systemMessage"])


# ---------- 12. rollback ----------

V1 = os.environ.get("AUTOSWITCH_V1_REF", "v0.1.1")  # the last release before v2 (tag)


class RollbackTest(Base):
    """AUTOSWITCH_HANDOFF off (or unset) gives exactly the decisions of v0.1 (the last release before v2) on the same inputs."""
    def setUp(self):
        super().setUp()
        root = os.path.join(self.d, "main")
        os.makedirs(root)
        self.main = os.path.join(root, "hook")
        try:
            arch = subprocess.run(["git", "-C", REPO, "archive", V1, "hook"], capture_output=True, check=True)
            subprocess.run(["tar", "-x", "-C", root], input=arch.stdout, check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("v0.1 commit not available (shallow clone?)")

    def _run(self, code_dir, base, prompt, sc, tr, mode, extra):
        env = {**self.env, "AUTOSWITCH_DIR": base, "AUTOSWITCH_SESSIONS": os.path.join(base, "s"),
               "AUTOSWITCH_MODE": mode, "AUTOSWITCH_SCORER": sc.url, **extra}
        env.pop("AUTOSWITCH_HANDOFF")
        if "HO" in extra:
            env["AUTOSWITCH_HANDOFF"] = extra["HO"]; env.pop("HO")
        out = subprocess.run([sys.executable, os.path.join(code_dir, "hint.py")], capture_output=True, text=True, env=env,
                             input=json.dumps({"prompt": prompt, "session_id": "c1", "transcript_path": tr,
                                               "cwd": "/tmp/p", "permission_mode": "default"}))
        state = {}
        for root, _, files in os.walk(base):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), base)
                if rel.startswith("s" + os.sep) or f == "handoff":
                    continue
                with open(os.path.join(root, f)) as fh:
                    raw = fh.read()
                try:
                    data = [json.loads(l) for l in raw.splitlines() if l.strip()] if f.endswith(".jsonl") else json.loads(raw) if raw else raw
                except ValueError:
                    data = raw
                for x in data if isinstance(data, list) else [data]:
                    if isinstance(x, dict):
                        x.pop("ts", None); x.pop("id", None)
                key = rel.split(os.sep)[0] + "/" + ("*" if rel.startswith(("pending", "pass")) else f)
                state.setdefault(key, []).append(data)
        return out.stdout, out.returncode, state

    def test_same_inputs_same_outputs_as_main(self):
        small = transcript(os.path.join(self.d, "small.jsonl"), ["design the router", "ok"])
        long_ = self.long_tr(("tune the router",))
        cases = [
            ("what is 2+2", fixed(0.95, 0.05, 0), small, "claude-sonnet-5-5", "enforce", {}),
            ("what is 2+2", fixed(0.95, 0.05, 0), long_, "claude-opus-5-5", "enforce", {}),
            ("ok do it", fixed(0.95, 0.05, 0), small, "claude-sonnet-5-5", "enforce", {}),
            ("prove this theorem", fixed(0.1, 0.2, 0.7), small, "claude-haiku-5-5", "enforce", {"AUTOSWITCH_SPEED": "instant"}),
            ("hmm", fixed(0.4, 0.35, 0.25), small, "claude-opus-5-5", "enforce", {}),
            ("tidy docs", fixed(0.05, 0.9, 0.05), long_, "claude-opus-5-5", "shadow", {}),
            ("what is 2+2", fixed(0.95, 0.05, 0), small, None, "enforce", {}),
            ("what is 2+2", fixed(0.95, 0.05, 0), small, "claude-sonnet-5-5", "off", {}),
            ("lean", fixed(0.1, 0.45, 0.45), small, "claude-haiku-5-5", "enforce", {"HO": "off"}),
            ("what is 2+2", fixed(0.65, 0.3, 0.05), small, "claude-sonnet-5-5", "enforce", {"HO": "off"}),
        ]
        n = 0
        for prompt, fn, tr, model, mode, extra in cases:
            sc = Scorer(self, fn)
            states = []
            for code in (self.main, SB):
                base = tempfile.mkdtemp(dir=self.d)
                _write_text(os.path.join(base, "watcher.alive"), "")
                _write_text(os.path.join(base, "handoff"), "on")  # the file says on, but the env (or its absence) decides
                if "HO" not in extra:
                    os.remove(os.path.join(base, "handoff"))
                os.makedirs(os.path.join(base, "s"))
                if model:
                    p = os.path.join(base, "s", "a", "b"); os.makedirs(p)
                    _write_json(os.path.join(p, ID + ".json"), {"sessionId": ID, "cliSessionId": "c1", "model": model, "title": "T"})
                states.append(self._run(code, base, prompt, sc, tr, mode, dict(extra)))
            self.assertEqual(states[0], states[1], (prompt, model, mode, extra))
            n += 1
        self.assertEqual(n, len(cases))

    def test_handoff_file_off_and_env_off(self):
        import handoff
        self.assertTrue(handoff.enabled())
        os.environ["AUTOSWITCH_HANDOFF"] = "off"; self.assertFalse(handoff.enabled())
        os.environ.pop("AUTOSWITCH_HANDOFF"); self.assertFalse(handoff.enabled())
        _write_text(os.path.join(self.d, "handoff"), "on\n"); self.assertTrue(handoff.enabled())


if __name__ == "__main__":
    unittest.main()


class HandoffVersionTest(unittest.TestCase):
    def test_step1_winners_by_mode_and_size(self):
        import acpmh_bridge as b
        self.assertEqual(b.options("balanced", 150000), b.V4)
        self.assertEqual(b.options("balanced", 900000), b.V5B)
        self.assertEqual(b.options("quality", 900000), b.V4)
        self.assertEqual(b.options("save", 150000), b.V4)
        self.assertEqual(b.options("save", 260000), b.V4)
        self.assertEqual(b.options("save", 299999), b.V4)
        self.assertEqual(b.options("save", 350000), b.V5)
        self.assertEqual(b.options("balanced", None), b.V4)


class QualityRereadTest(unittest.TestCase):
    def test_quality_rereads_in_place_under_300k(self):
        import handoff
        size = {"tokens": 260000, "method": "precise"}
        self.assertEqual(handoff.plan("haiku", "opus", size, "haiku", False, "quality")["action"], "inplace")
        self.assertEqual(handoff.plan("haiku", "opus", size, "haiku", False, "balanced")["action"], "handoff")
        big = {"tokens": 900000, "method": "precise"}
        self.assertEqual(handoff.plan("haiku", "opus", big, "haiku", False, "quality")["action"], "handoff")
