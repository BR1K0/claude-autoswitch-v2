"""Tests for Autoswitch Step 4, automatic rollover (AUTOSWITCH_ROLLOVER). Every test runs in its own temp
AUTOSWITCH_DIR, with a fake scorer and a fake ACPMH; nothing calls an API.

  python3 tests/sb_test_rollover.py
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SB = os.path.join(REPO, "hook")
sys.path.insert(0, SB)
sys.path.insert(0, HERE)

from v2_helpers import ID, ID2, _read_jsonl, _write_json, _write_text  # noqa: E402
from test_v2_handoff import (FAKE_CLI, FAKE_HANDOFF, FAKE_STORE, Base, Scorer, entry, fixed,  # noqa: E402,F401
                             transcript)

ID9 = "local_99999999-2222-3333-4444-555555555555"


def iso(t=None):
    return datetime.datetime.fromtimestamp(time.time() if t is None else t, datetime.timezone.utc).isoformat()


_n = [0]


def tool(name, inp, result="ok", tur=None, err=False, answered=True, ts=None):
    """An assistant tool_use and (if answered) the user tool_result with its toolUseResult."""
    _n[0] += 1
    tid = "toolu_%d" % _n[0]
    out = json.dumps({"type": "assistant", "timestamp": iso(ts), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}) + "\n"
    if answered:
        r = {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result, **({"is_error": True} if err else {})}]}}
        if tur is not None:
            r["toolUseResult"] = tur
        out += json.dumps(r) + "\n"
    return out


def note(text):
    return json.dumps({"type": "user", "timestamp": iso(), "message": {"role": "user", "content": text}}) + "\n"


CRON = tool("CronCreate", {"cron": "*/20 * * * *", "prompt": "check slack"}, "Scheduled recurring job fbb69d77",
            {"id": "fbb69d77", "recurring": True, "durable": False})
MONITOR = tool("Monitor", {"command": "bash tail-ci.sh", "description": "CI results", "persistent": True},
               "Monitor started (task bq9vcog81)", {"taskId": "bq9vcog81", "persistent": True})
WATCH = tool("Monitor", {"command": "bash '/Users/me/claude-autoswitch/hook/watch.sh'",
                         "description": "autoswitch requests", "persistent": True},
             "Monitor started (task bwatch001)", {"taskId": "bwatch001", "persistent": True})
BG = tool("Bash", {"command": "sleep 999", "run_in_background": True}, "started", {"backgroundTaskId": "bhl53j9bb"})
BG_DONE = note("<task-notification>\n<task-id>bhl53j9bb</task-id>\n<status>completed</status>\n</task-notification>")
LINK = tool("mcp__ccd_session__start_session", {"title": "Helper", "prompt": "do x"},
            [{"type": "text", "text": "Started session \"Helper\" (session_id: %s, name: h). It runs on its own." % ID9}])
LINK_BACK = note("<side-sessions-event>Session %s finished its turn.</side-sessions-event>" % ID9)
SEQ = ("created", "built", "started", "delivered", "confirmed", "linked")


class RBase(Base):
    def setUp(self):
        super().setUp()
        self.env.update(AUTOSWITCH_ROLLOVER="on", AUTOSWITCH_ROLLOVER_AT="100000")
        os.environ.update(AUTOSWITCH_ROLLOVER="on", AUTOSWITCH_ROLLOVER_AT="100000")
        for k in [k for k in os.environ if k.startswith("AUTOSWITCH_ROLLOVER_") and k != "AUTOSWITCH_ROLLOVER_AT"]:
            os.environ.pop(k); self.env.pop(k, None)
        import importlib
        for m in ("rollover", "acpmh_bridge"):
            if m in sys.modules:
                importlib.reload(sys.modules[m])
        # fake ACPMH (no API): ingest + build only
        self.acpmh = os.path.join(self.d, "acpmh_repo"); pkg = os.path.join(self.acpmh, "acpmh"); os.makedirs(pkg)
        _write_text(os.path.join(pkg, "__init__.py"), "")
        _write_text(os.path.join(pkg, "cli.py"), FAKE_CLI)
        _write_text(os.path.join(pkg, "handoff.py"), FAKE_HANDOFF)
        _write_text(os.path.join(pkg, "store.py"), FAKE_STORE)
        extra = dict(AUTOSWITCH_ACPMH=self.acpmh, AUTOSWITCH_ACPMH_HOME=os.path.join(self.d, "acpmh_home"),
                     FAKE_ACPMH_LOG=os.path.join(self.d, "acpmh.log"))
        self.env.update(extra); os.environ.update(extra)
        self.proj = os.path.join(self.d, "projects", "-Users-kem-proj"); os.makedirs(self.proj)

    def big(self, extra="", msgs=("we are building the rollover",), chars=300000, name="c1.jsonl"):
        """120k real tokens with the default 300k characters: over the 100k test threshold."""
        return transcript(os.path.join(self.proj, name), msgs, filler_chars=chars, extra=extra)

    def stop(self, tr, cli="c1", active=False, env=None, sid=ID):
        e = {**self.env, "AUTOSWITCH_MODE": "enforce", "CLAUDE_CODE_HOST_SESSION_ID": sid, **(env or {})}
        out = subprocess.run([sys.executable, os.path.join(SB, "stop.py")], capture_output=True, text=True, env=e,
                             input=json.dumps({"session_id": cli, "transcript_path": tr, "cwd": "/Users/me/proj",
                                               "permission_mode": "acceptEdits", "hook_event_name": "Stop",
                                               "stop_hook_active": active}))
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout) if out.stdout.strip() else {}

    def cli(self, *args):
        out = subprocess.run([sys.executable, os.path.join(SB, "rollover.py")] + list(args), capture_output=True,
                             text=True, env=os.environ)
        return json.loads(out.stdout)

    def recs(self):
        import rollover
        return rollover.all_records()

    def sc(self):
        return Scorer(self, fixed(0.1, 0.8, 0.1))  # Sonnet, on a Sonnet chat: no model switch wanted

    def as_part2(self):
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID2
        self.env["CLAUDE_CODE_HOST_SESSION_ID"] = ID2

    def as_part1(self):
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID
        self.env["CLAUDE_CODE_HOST_SESSION_ID"] = ID

    def rolled_by_file(self):
        """Turned on the documented way, with the on/off file instead of the environment."""
        self.env.pop("AUTOSWITCH_ROLLOVER", None); os.environ.pop("AUTOSWITCH_ROLLOVER", None)
        _write_text(os.path.join(self.d, "rollover"), "on\n")
        return self.rolled()

    def rolled(self, extra=""):
        """A rollover started by the user's message on a big Sonnet chat."""
        self.record()
        out = self.hook("next step on the rollover please", self.sc(), tr=self.big(extra))
        (rec,) = self.recs()
        return rec, out


# ---------- triggers ----------

class TriggerTest(RBase):
    def test_switch_file_and_state_folder_do_not_clash(self):
        rec, out = self.rolled_by_file()
        self.assertTrue(os.path.isfile(os.path.join(self.d, "rollover")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "rollovers", rec["id"], "state.json")))
        self.assertIn("Rolling over to a new chat (part 2)", out["systemMessage"])

    def test_prompt_over_threshold_rolls_over_on_the_same_model(self):
        rec, out = self.rolled()
        self.assertNotIn("continue", out)                                      # never held
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Rolling over to a new chat (part 2)", out["systemMessage"])
        self.assertEqual((rec["trigger"], rec["model"], rec["switch"], rec["status"]),
                         ("size", "claude-sonnet-5-5", False, "created"))
        self.assertEqual(rec["prompt"], "next step on the rollover please")
        for s in ("rollover.py' build", "start_session", '"user_asked"', 'model "claude-sonnet-5-5"', 'effort "high"',
                  'permission_mode "acceptEdits"', "started %s" % rec["id"], "wait %s" % rec["id"], "job-deleted",
                  "detach_session", "linked %s" % rec["id"], "open_session_in", "Do not archive", "answer the user's message here yourself"):
            self.assertIn(s, ctx)
        self.assertTrue(os.path.exists(os.path.join(self.d, "rollovers", rec["id"], "jobs.json")))
        self.assertEqual(self.handoffs(), []); self.assertEqual(self.pending(), [])
        self.assertEqual(self.shadow()[-1]["would_rollover"], "claude-sonnet-5-5")

    def test_lasting_switch_and_rollover_in_one_move(self):
        self.record(model="claude-opus-5-5")
        sc = Scorer(self, fixed(0.05, 0.9, 0.05))
        tr = self.big()
        self.hook("tidy up the router threshold docs", sc, tr=tr)               # one-off: stays (would roll over)
        self.assertEqual(len(self.recs()), 1)                                   # ...but the size alone rolls it over
        rec = self.recs()[0]
        self.assertEqual(rec["model"], "claude-opus-5-5")
        import shutil
        shutil.rmtree(os.path.join(self.d, "rollovers"))
        out = self.hook("and the router gates docs too", sc, tr=tr)             # lasting down-switch + over threshold
        (rec,) = self.recs()
        self.assertEqual((rec["model"], rec["switch"]), ("claude-sonnet-5-5", True))
        self.assertIn("on Sonnet (part 2)", out["systemMessage"])
        self.assertEqual(self.handoffs(), [])                                   # one move, not a handoff too

    def test_below_threshold_nothing(self):
        self.record()
        out = self.hook("next step", self.sc(), tr=self.big(chars=100000))     # 40k tokens
        self.assertEqual(self.recs(), []); self.assertNotIn("hookSpecificOutput", out)
        self.assertIsNone(self.shadow()[-1]["rollover_trigger"])

    def test_early_trigger_from_growth(self):
        import rollover
        P = lambda t, marks: {"tokens": t, "method": "precise", "marks": marks}   # noqa: E731
        self.assertEqual(rollover.trigger(P(95000, [50000, 65000, 80000])), "early")   # +15k a turn: 110k next
        self.assertIsNone(rollover.trigger(P(95000, [93000, 94000, 94500])))           # slow growth
        self.assertIsNone(rollover.trigger(P(50000, [10000, 30000, 40000])))           # under the floor (60k)
        self.assertEqual(rollover.trigger(P(100000, [])), "size")
        self.assertIsNone(rollover.trigger({"tokens": 150000, "method": "cheap", "marks": []}))  # never on the cheap guess
        self.assertEqual(rollover.trigger(P(10, []), deferred=True), "deferred")
        os.environ["AUTOSWITCH_ROLLOVER_EARLY"] = "off"
        self.assertIsNone(rollover.trigger(P(95000, [50000, 65000, 80000])))
        self.assertEqual(rollover.growth(P(95000, [50000, 65000, 80000])), 15000)

    def test_early_trigger_through_the_hook(self):
        self.record()
        extra = "".join(entry("user", "step %d" % i) + entry("assistant", "z" * 40000) for i in range(3))
        tr = self.big(extra=extra, chars=150000)                                # 60k, then +16k a turn: 108k
        os.environ["AUTOSWITCH_ROLLOVER_AT"] = self.env["AUTOSWITCH_ROLLOVER_AT"] = "115000"
        out = self.hook("and the next step", self.sc(), tr=tr)
        (rec,) = self.recs()
        self.assertEqual(rec["trigger"], "early"); self.assertIn("hookSpecificOutput", out)

    def test_measure_marks_and_compaction(self):
        import rollover
        p = transcript(os.path.join(self.d, "m.jsonl"), ["a" * 1000, "b" * 1000])
        m = rollover.measure(p)
        self.assertEqual(m["method"], "cheap")                                  # small file: never counted
        os.environ["AUTOSWITCH_ROLLOVER_AT"] = "100"
        m = rollover.measure(p)
        self.assertEqual(m["method"], "precise"); self.assertEqual(len(m["marks"]), 2)
        self.assertLess(m["marks"][0], m["marks"][1]); self.assertLess(m["marks"][1], m["tokens"])
        comp = json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n"
        p2 = transcript(os.path.join(self.d, "m2.jsonl"), ["a" * 1000], extra=comp + entry("user", "c"))
        self.assertEqual(len(rollover.measure(p2)["marks"]), 1)


# ---------- guards ----------

class GuardTest(RBase):
    def blocked(self, extra="", env=None):
        self.record()
        self.hook("next step on the rollover please", self.sc(), tr=self.big(extra), env=env)
        return self.shadow()[-1].get("rollover_blocked") or [], self.recs()

    def test_tool_call_running(self):
        why, recs = self.blocked(tool("Bash", {"command": "make"}, answered=False))
        self.assertEqual(recs, []); self.assertIn("tool call running (Bash)", why[0])

    def test_half_done_edit_then_fixed(self):
        bad = tool("Edit", {"file_path": "/x.py"}, "error", err=True)
        why, recs = self.blocked(bad)
        self.assertEqual(recs, []); self.assertIn("half-done edit (/x.py)", why[0])
        why, recs = self.blocked(bad + tool("Edit", {"file_path": "/x.py"}))
        self.assertEqual(len(recs), 1)

    def test_linked_session_mid_task_then_back(self):
        why, recs = self.blocked(LINK)
        self.assertEqual(recs, []); self.assertIn("linked session mid-task", why[0])
        why, recs = self.blocked(LINK + LINK_BACK)
        self.assertEqual(len(recs), 1)
        import rollover
        (j,) = rollover.jobs_of(recs[0]["id"])
        self.assertEqual((j["kind"], j["orig_id"], j["movable"]), ("linked", ID9, True))

    def test_background_command_without_recipe(self):
        why, recs = self.blocked(BG)
        self.assertEqual(recs, []); self.assertIn("background command running", why[0])
        why, recs = self.blocked(BG + BG_DONE)
        self.assertEqual(len(recs), 1)

    def test_failed_edit_and_lost_background_expire_after_30_min(self):
        old = time.time() - 31 * 60
        bad = tool("Edit", {"file_path": "/x.py"}, "error", err=True, ts=old)
        bg = tool("Bash", {"command": "sleep 9", "run_in_background": True}, "started",
                  {"backgroundTaskId": "bold00001"}, ts=old)
        why, recs = self.blocked(bad + bg)
        self.assertEqual(len(recs), 1, why)
        exp = recs[0]["guards_expired"]
        self.assertEqual(len(exp), 2)
        self.assertIn("failed edit of /x.py", exp[0]); self.assertIn("bold00001", exp[1])
        self.assertIn("guard expired", exp[1])
        import rollover
        self.assertEqual(rollover.jobs_of(recs[0]["id"]), [])                 # an expired command is not moved

    def test_watcher_chat_never_rolls_over(self):
        _write_text(os.path.join(self.d, "watcher.alive"), ID)                 # this chat owns the heartbeat
        why, recs = self.blocked()
        self.assertEqual(recs, []); self.assertIn("this chat runs the Watcher (it owns the heartbeat)", why)
        self.assertIsNone(self.stop(self.big()).get("decision"))               # the Stop trigger too
        self.assertEqual(self.recs(), [])

    def test_watch_sh_monitor_is_never_a_movable_job(self):
        import rollover
        s = rollover.scan(self.big(WATCH + CRON))
        self.assertEqual([j["orig_id"] for j in s["jobs"]], ["fbb69d77"]); self.assertEqual(s["watcher"], ["bwatch001"])
        why, recs = self.blocked(WATCH)                                        # heartbeat owned by nobody
        self.assertEqual(recs, []); self.assertTrue(any("watch.sh Monitor bwatch001" in w for w in why), why)
        stopped = note("<task-notification>\n<task-id>bwatch001</task-id>\n<status>completed</status>\n</task-notification>")
        self.assertEqual(rollover.scan(self.big(WATCH + stopped))["watcher"], [])

    def test_gap_since_the_last_rollover(self):
        import rollover
        self.record()
        tr = self.big()
        rollover.save("aaaaaaaaaaaa", {"id": "aaaaaaaaaaaa", "status": "failed", "session_id": ID, "tokens": 60000, "ts": 1})
        self.hook("next", self.sc(), tr=tr)
        self.assertEqual(len(self.recs()), 1)                                   # 120k - 60k < 100k
        self.assertIn("rollover 60k tokens ago", self.shadow()[-1]["rollover_blocked"][0])
        rollover.save("aaaaaaaaaaaa", {"id": "aaaaaaaaaaaa", "status": "deferred", "session_id": ID, "tokens": 60000, "ts": 1})
        self.hook("next", self.sc(), tr=tr)                                     # a deferred one doesn't count
        self.assertEqual(len(self.recs()), 2)

    def test_saving_must_beat_the_cost(self):
        import rollover
        self.assertTrue(rollover.worth("sonnet", "sonnet", 200000)[0])
        self.assertFalse(rollover.worth("sonnet", "sonnet", None)[0])
        why, recs = self.blocked(env={"AUTOSWITCH_ROLLOVER_EXPECTED_REQUESTS": "2"})
        self.assertEqual(recs, []); self.assertIn("saving $", why[0])

    def test_open_handoff_or_rollover(self):
        rec, _ = self.rolled()
        self.hook("again", self.sc(), tr=self.big())
        self.assertEqual(len(self.recs()), 1)
        self.assertIn("is open", self.shadow()[-1]["rollover_blocked"][0])

    def test_shadow_mode_logs_only(self):
        self.record()
        out = self.hook("next", self.sc(), tr=self.big(), mode="shadow")
        self.assertEqual(self.recs(), []); self.assertNotIn("hookSpecificOutput", out)
        self.assertEqual(self.shadow()[-1]["would_rollover"], "claude-sonnet-5-5")

    def test_scan_jobs(self):
        import rollover
        old = time.time() - 3600
        extra = (CRON + MONITOR + tool("CronCreate", {"cron": "* * * * *"}, "x", {"id": "deadbeef"})
                 + tool("CronDelete", {"id": "deadbeef"}, "Cancelled", {"id": "deadbeef"})
                 + tool("Monitor", {"command": "x"}, "x", {"taskId": "short1", "timeoutMs": 30000}, ts=old)
                 + tool("Monitor", {"command": "y"}, "x", {"taskId": "stopped1", "persistent": True})
                 + tool("TaskStop", {"task_id": "stopped1"}, "ok")
                 + tool("CronCreate", {"cron": "x"}, "denied", err=True))
        s = rollover.scan(self.big(extra))
        self.assertEqual(sorted(j["orig_id"] for j in s["jobs"]), ["bq9vcog81", "fbb69d77"])
        cron = next(j for j in s["jobs"] if j["kind"] == "cron")
        self.assertEqual(cron["recipe"], {"tool": "CronCreate", "args": {"cron": "*/20 * * * *", "prompt": "check slack"}})
        self.assertEqual(cron["remove"], {"tool": "CronDelete", "args": {"id": "fbb69d77"}})
        self.assertEqual(s["task"]["user"], ["we are building the rollover"])
        self.assertIsNone(rollover.scan(os.path.join(self.d, "missing")))
        self.assertFalse(rollover.quiet(None)[0])


# ---------- the Stop hook ----------

class StopTest(RBase):
    def test_stop_over_threshold_blocks_once_with_the_instruction(self):
        self.record()
        tr = self.big(CRON)
        out = self.stop(tr)
        self.assertEqual(out["decision"], "block")
        (rec,) = self.recs()
        self.assertEqual((rec["trigger"], rec["prompt"]), ("stop", None))
        for s in ("deferred %s" % rec["id"], "do NOT retry", "Rollover deferred until your next message",
                  "un-archived", "carry on with the work here"):
            self.assertIn(s, out["reason"])
        p2 = __import__("rollover").part2_prompt(rec)
        self.assertIn("continue the work", p2); self.assertIn("we are building the rollover", p2)
        self.assertEqual(self.stop(tr), {})                                     # loop guard: the next stop is never blocked

    def test_stop_hook_active_never_blocks(self):
        self.record()
        self.assertEqual(self.stop(self.big(), active=True), {})
        self.assertEqual(self.recs(), [])

    def test_never_two_blocks_in_a_row_even_after_a_quick_failure(self):
        self.record()
        tr = self.big()
        self.assertEqual(self.stop(tr)["decision"], "block")
        (rec,) = self.recs()
        self.cli("fail", rec["id"], "start")
        out = self.stop(tr)                                                     # loop guard, and the note once
        self.assertNotIn("decision", out); self.assertIn("could not roll this chat over", out["systemMessage"])
        self.assertEqual(self.stop(tr), {})                                     # gap guard: no retry at the same size

    def test_refused_start_defers_to_kems_next_message(self):
        self.record()
        tr = self.big(CRON)
        self.stop(tr)
        (rec,) = self.recs()
        self.assertTrue(self.cli("build", rec["id"])["ok"])
        self.assertEqual(self.cli("deferred", rec["id"]), {"ok": True})          # start_session was refused
        import rollover
        self.assertEqual(rollover.load(rec["id"])["status"], "deferred")
        self.assertTrue(all(j["state"] == "part1" for j in rollover.jobs_of(rec["id"])))  # jobs never moved
        self.assertEqual(self.stop(tr), {})                                     # loop guard
        self.assertEqual(self.stop(tr), {})                                     # deferred: wait for the user
        out = self.hook("good morning, carry on", self.sc(), tr=self.big(CRON, chars=200000))  # 80k: under threshold
        self.assertIn("hookSpecificOutput", out)
        new = [r for r in self.recs() if r["id"] != rec["id"]][0]
        self.assertEqual((new["trigger"], new["prompt"]), ("deferred", "good morning, carry on"))
        self.assertEqual(rollover.load(rec["id"])["status"], "superseded")

    def test_off_or_shadow_or_cli_does_nothing(self):
        self.record()
        tr = self.big()
        for env in ({"AUTOSWITCH_ROLLOVER": "off"}, {"AUTOSWITCH_HANDOFF": "off"}, {"AUTOSWITCH_MODE": "shadow"},
                    {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            self.assertEqual(self.stop(tr, env=env), {})
        self.assertEqual(self.recs(), [])
        self.assertFalse(os.path.exists(os.path.join(self.d, "rollovers")))

    def test_small_chat_or_record_of_another_chat(self):
        self.record()
        self.assertEqual(self.stop(self.big(chars=1000)), {})
        self.assertEqual(self.stop(self.big(), sid=ID9), {})                    # the record belongs to ID
        self.assertEqual(self.recs(), [])


# ---------- the sequence ----------

class SequenceTest(RBase):
    def deliver(self, rec, cli="c2"):
        import rollover
        self.as_part2()
        out = self.hook(rollover.part2_prompt(rec), Scorer(self, fixed(0, 0, 1)), cli=cli)
        self.as_part1()
        return out

    def test_happy_path_every_step_on_disk(self):
        import rollover
        rec, _ = self.rolled(CRON + MONITOR + LINK + LINK_BACK)
        rid = rec["id"]
        jobs = rollover.jobs_of(rid)
        self.assertEqual(sorted(j["kind"] for j in jobs), ["cron", "linked", "monitor"])
        self.assertTrue(all(j["state"] == "part1" and j["owner"] == "part1" for j in jobs))
        res = self.cli("build", rid)                                            # 1
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["prompt"].startswith("[autoswitch-rollover:%s]" % rid))
        self.assertTrue(res["prompt"].endswith("the user's message:\nnext step on the rollover please"))
        with open(rec["handoff_path"]) as f:
            hand = f.read()
        self.assertIn("# ACPMH HANDOFF", hand); self.assertIn("cron fbb69d77", hand)
        self.assertEqual(self.cli("started", rid, ID2), {"ok": True})           # 2
        self.assertEqual(self.deliver(rec), {})                                 # 3: never routed
        r = rollover.load(rid)
        self.assertEqual((r["status"], r["part2_session"], r["verbatim"]), ("delivered", ID2, True))
        self.assertTrue(self.deliver(rec, cli="c9")["stopReason"].startswith("↪ Already continued"))  # a copy elsewhere
        listed = self.cli("jobs", rid)["jobs"]                                  # 4: part 2 claims
        self.assertEqual(len(listed), 3)
        self.assertEqual({j["tool"] for j in listed}, {"CronCreate", "Monitor", "adopt"})
        for j, new in zip(listed, ("c0ffee01", "bnewmon01", ID9)):
            self.assertEqual(self.cli("job-created", rid, j["jid"], new), {"ok": True})
        self.assertEqual(self.cli("confirm", rid), {"ok": True})                # 5
        w = self.cli("wait", rid)
        self.assertTrue(w["ok"]); self.assertEqual(w["kept"], [])
        self.assertEqual({(d["tool"], json.dumps(d["args"])) for d in w["delete"]},
                         {("CronDelete", '{"id": "fbb69d77"}'), ("TaskStop", '{"task_id": "bq9vcog81"}'), (None, "null")})
        for d in w["delete"]:
            self.assertEqual(self.cli("job-deleted", rid, d["jid"]), {"ok": True})
        self.assertTrue(all(j["state"] == "part2" and j["owner"] == "part2" for j in rollover.jobs_of(rid)))
        self.assertEqual(self.cli("linked", rid), {"ok": True})                 # 6
        self.assertEqual(self.cli("shown", rid, "yes"), {"ok": True})           # 7
        steps = rollover.load(rid)["steps"]
        self.assertTrue(all(s in steps for s in SEQ + ("shown",)))
        self.assertEqual(sorted(steps, key=steps.get)[:len(SEQ)], list(SEQ))
        self.assertEqual(self.cli("archive", rid), {"ok": True})
        # part 1 afterwards: told on every turn, never rolled over again
        out = self.hook("what now?", self.sc(), tr=self.big(CRON + MONITOR + LINK + LINK_BACK))
        self.assertIn("continued in part 2", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(len(self.recs()), 1)
        self.assertEqual(self.stop(self.big()), {})

    def test_no_jobs_confirms_at_delivery(self):
        import rollover
        rec, _ = self.rolled()
        self.cli("build", rec["id"]); self.cli("started", rec["id"], ID2)
        self.deliver(rec)
        self.assertEqual(rollover.load(rec["id"])["status"], "confirmed")
        w = self.cli("wait", rec["id"])
        self.assertEqual((w["ok"], w["delete"], w["kept"]), (True, [], []))

    def test_failure_build(self):
        import rollover
        rec, _ = self.rolled(CRON)
        os.environ["FAKE_ACPMH_FAIL"] = "ingest"
        self.assertFalse(self.cli("build", rec["id"])["ok"])
        r = rollover.load(rec["id"])
        self.assertEqual((r["status"], r["failed_at"]), ("failed", "built"))
        self.assertTrue(all(j["state"] == "part1" for j in rollover.jobs_of(rec["id"])))
        out = self.hook("ok", self.sc(), tr=self.big(CRON))                     # one-line note, once
        self.assertIn("could not roll this chat over", out["systemMessage"])
        self.assertEqual(len(self.recs()), 1)                                   # gap guard: no retry right away
        out = self.hook("ok", self.sc(), tr=self.big(CRON))
        self.assertNotIn("could not roll", out["systemMessage"])

    def test_failure_start(self):
        import rollover
        rec, _ = self.rolled()
        self.cli("build", rec["id"])
        self.assertEqual(self.cli("fail", rec["id"], "start"), {"ok": True})
        self.assertEqual(rollover.load(rec["id"])["status"], "failed")
        out = self.deliver(rec)                                                 # a part 2 that started anyway: stopped
        self.assertIs(out.get("continue"), False); self.assertIn("cancelled", out["stopReason"])

    def test_failure_never_delivered_then_late_copy_is_stopped(self):
        import rollover
        rec, _ = self.rolled(CRON)
        self.cli("build", rec["id"]); self.cli("started", rec["id"], ID2)
        w = rollover.wait(rec["id"], timeout=0)
        self.assertFalse(w["ok"]); self.assertEqual(rollover.load(rec["id"])["status"], "failed")
        self.assertIs(self.deliver(rec).get("continue"), False)                 # two agents never do the same work
        self.assertTrue(all(j["state"] == "part1" for j in rollover.jobs_of(rec["id"])))

    def test_failure_jobs_not_moved_are_kept_and_block_archive(self):
        import rollover
        rec, _ = self.rolled(CRON + MONITOR)
        rid = rec["id"]
        self.cli("build", rid); self.cli("started", rid, ID2); self.deliver(rec)
        listed = self.cli("jobs", rid)["jobs"]
        cron = next(j for j in listed if j["tool"] == "CronCreate")
        self.cli("job-created", rid, cron["jid"], "c0ffee01")                   # the monitor never gets recreated
        w = rollover.wait(rid, timeout=0)                                       # delivered: confirmed, monitor kept
        self.assertTrue(w["ok"]); self.assertEqual(w["kept"], ["monitor bq9vcog81"]); self.assertEqual(len(w["delete"]), 1)
        mon = next(j for j in listed if j["tool"] == "Monitor")
        self.assertTrue(self.cli("job-created", rid, mon["jid"], "late1")["delete_yours"])  # too late: no duplicate
        self.cli("job-deleted", rid, w["delete"][0]["jid"]); self.cli("linked", rid)
        self.assertFalse(self.cli("archive", rid)["ok"])                        # the monitor still runs in part 1
        with open(rec["transcript"], "a") as f:
            f.write(tool("TaskStop", {"task_id": "bq9vcog81"}, "ok"))
        self.assertTrue(self.cli("archive", rid)["ok"])

    def test_no_duplicate_jobs(self):
        import rollover
        rec, _ = self.rolled(CRON)
        rid = rec["id"]
        self.cli("build", rid); self.cli("started", rid, ID2); self.deliver(rec)
        (j,) = self.cli("jobs", rid)["jobs"]
        self.assertEqual(self.cli("jobs", rid)["jobs"][0]["jid"], j["jid"])     # listing twice: same job, one claim
        self.assertEqual(self.cli("job-created", rid, j["jid"], "c0ffee01"), {"ok": True})
        self.assertEqual(self.cli("job-created", rid, j["jid"], "c0ffee01"), {"ok": True})   # same copy again: fine
        self.assertTrue(self.cli("job-created", rid, j["jid"], "c0ffee02")["delete_yours"])  # a second copy: deleted
        self.assertEqual(self.cli("jobs", rid)["jobs"], [])
        self.assertFalse(self.cli("job-deleted", rid, "nonexistent0")["ok"])

    def test_jobs_refused_when_not_delivered(self):
        rec, _ = self.rolled(CRON)
        self.assertFalse(self.cli("jobs", rec["id"])["ok"])

    def test_no_lost_message(self):
        import rollover
        rec, out = self.rolled()
        self.assertIn("answer the user's message here yourself", out["hookSpecificOutput"]["additionalContext"])  # failure: part 1 answers
        self.cli("build", rec["id"])
        self.as_part2()
        out = self.hook("[autoswitch-rollover:%s] read it. the user's message: other words" % rec["id"], self.sc(), cli="c2")
        self.assertIn("next step on the rollover please", out["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(rollover.load(rec["id"])["verbatim"])

    def test_cli_errors(self):
        self.assertFalse(self.cli("build", "nonsense")["ok"])
        self.assertFalse(self.cli("build", "0123456789ab")["ok"])
        self.assertFalse(self.cli("frobnicate", "0123456789ab")["ok"])
        rec, _ = self.rolled()
        self.assertFalse(self.cli("started", rec["id"])["ok"])                  # missing argument


# ---------- crash recovery ----------

class RecoveryTest(RBase):
    def aged(self, status, age=700, **kw):
        import rollover
        rec, _ = self.rolled(CRON)
        r = rollover.load(rec["id"])
        r.update(status=status, **kw)
        r["steps"] = {k: time.time() - age for k in SEQ[:SEQ.index(status) + 1]}
        rollover.save(rec["id"], r)
        return r

    def test_started_never_confirmed_is_undone_by_the_next_hook(self):
        import rollover
        r = self.aged("started", part2_session=ID2)
        out = self.hook("still there?", self.sc(), tr=self.big(CRON, chars=1000))  # any hook run
        self.assertEqual(rollover.load(r["id"])["status"], "failed")
        self.assertIn("could not roll this chat over", out["systemMessage"])
        self.assertTrue(all(j["state"] == "part1" for j in rollover.jobs_of(r["id"])))

    def test_crash_while_moving_jobs_moves_them_back(self):
        import rollover
        r = self.aged("delivered")
        jobs = rollover.jobs_of(r["id"]); jobs[0]["state"] = "moving"; rollover.save(r["id"], jobs, "jobs")
        self.assertEqual(rollover.recover(), [r["id"]])
        r2 = rollover.load(r["id"])
        self.assertEqual(r2["status"], "confirmed")                             # part 2 has the work
        self.assertEqual(rollover.jobs_of(r["id"])[0]["state"], "kept")         # the job stays with part 1

    def test_young_rollovers_are_left_alone(self):
        import rollover
        self.aged("started", age=10)
        self.assertEqual(rollover.recover(), [])

    def test_created_but_never_built_is_failed(self):
        import rollover
        r = self.aged("created")
        rollover.recover()
        self.assertEqual(rollover.load(r["id"])["status"], "failed")

    def test_crash_after_confirm_resumes_in_part_1(self):
        import rollover
        rec, _ = self.rolled(CRON)
        rid = rec["id"]
        self.cli("build", rid); self.cli("started", rid, ID2)
        self.as_part2(); self.hook(rollover.part2_prompt(rec), self.sc(), cli="c2"); self.as_part1()
        (j,) = self.cli("jobs", rid)["jobs"]
        self.cli("job-created", rid, j["jid"], "c0ffee01"); self.cli("confirm", rid)
        # part 1 crashed in `wait`: its next turn finishes the job (delete its copy) and the link
        out = self.hook("hello?", self.sc(), tr=self.big(CRON))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CronDelete", ctx); self.assertIn("job-deleted %s %s" % (rid, j["jid"]), ctx)
        self.assertIn("not linked yet", ctx)
        machine = self.hook("<task-notification>\n<task-id>x</task-id>\n<summary>Monitor event</summary>\n</task-notification>",
                            self.sc(), tr=self.big(CRON))
        self.assertIn("send it to %s" % ID2, machine["hookSpecificOutput"]["additionalContext"])

    def test_part2_next_message_confirms(self):
        import rollover
        rec, _ = self.rolled(CRON)
        self.cli("build", rec["id"]); self.cli("started", rec["id"], ID2)
        self.as_part2()
        self.hook(rollover.part2_prompt(rec), self.sc(), cli="c2")
        self.assertEqual(rollover.load(rec["id"])["status"], "delivered")
        self.record(cli="c2", sid=ID2)
        self.hook("thanks", self.sc(), cli="c2")
        self.assertEqual(rollover.load(rec["id"])["status"], "confirmed")
        self.assertEqual(rollover.jobs_of(rec["id"])[0]["state"], "part1")       # never claimed: still part 1's

    def test_part2_stop_confirms(self):
        import rollover
        rec, _ = self.rolled(CRON)
        self.cli("build", rec["id"]); self.cli("started", rec["id"], ID2)
        self.as_part2(); self.hook(rollover.part2_prompt(rec), self.sc(), cli="c2")
        self.stop(self.big(chars=1000, name="c2.jsonl"), cli="c2", sid=ID2)
        self.assertEqual(rollover.load(rec["id"])["status"], "confirmed")

    def test_old_deferred_expires(self):
        import rollover
        r = self.aged("created")
        r.update(status="deferred", ts=time.time() - 90000); rollover.save(r["id"], r)
        rollover.recover()
        self.assertEqual(rollover.load(r["id"])["status"], "superseded")


# ---------- rollback ----------

class RollbackTest(RBase):
    """AUTOSWITCH_ROLLOVER off (or unset) gives exactly the handoff-only decisions on the same inputs.
    Needs AUTOSWITCH_V2_HANDOFF_REF: a commit with the handoff but without rollover (none exists in this repo's
    history; the check was run where that commit exists), else skipped."""
    def setUp(self):
        super().setUp()
        root = os.path.join(self.d, "base")
        os.makedirs(root)
        self.base = os.path.join(root, "hook")
        ref = os.environ.get("AUTOSWITCH_V2_HANDOFF_REF")
        if not ref:
            self.skipTest("AUTOSWITCH_V2_HANDOFF_REF not set")
        try:
            arch = subprocess.run(["git", "-C", REPO, "archive", ref, "hook"], capture_output=True, check=True)
            subprocess.run(["tar", "-x", "-C", root], input=arch.stdout, check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("%s not available" % ref)

    def _run(self, code_dir, base, prompt, sc, tr, mode, ro):
        env = {**self.env, "AUTOSWITCH_DIR": base, "AUTOSWITCH_SESSIONS": os.path.join(base, "s"),
               "AUTOSWITCH_MODE": mode, "AUTOSWITCH_SCORER": sc.url}
        env.pop("AUTOSWITCH_ROLLOVER")
        if ro is not None:
            env["AUTOSWITCH_ROLLOVER"] = ro
        out = subprocess.run([sys.executable, os.path.join(code_dir, "hint.py")], capture_output=True, text=True, env=env,
                             input=json.dumps({"prompt": prompt, "session_id": "c1", "transcript_path": tr,
                                               "cwd": "/tmp/p", "permission_mode": "default"}))
        state = {}
        for root, _, files in os.walk(base):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), base)
                if rel.startswith("s" + os.sep) or f == "rollover":
                    continue
                with open(os.path.join(root, f)) as fh:
                    raw = fh.read()
                try:
                    data = [json.loads(l) for l in raw.splitlines() if l.strip()] if f.endswith(".jsonl") else json.loads(raw) if raw else raw
                except ValueError:
                    data = raw
                for x in data if isinstance(data, list) else [data]:
                    if isinstance(x, dict):
                        for k in ("ts", "id", "handoff_path"):
                            x.pop(k, None)
                key = rel.split(os.sep)[0] + "/" + ("*" if rel.startswith(("pending", "pass", "handoff")) else f)
                state.setdefault(key, []).append(data)
        o = out.stdout
        for rid in __import__("re").findall(r"[0-9a-f]{12}", o):
            o = o.replace(rid, "<id>")
        return o, out.returncode, state

    def test_same_inputs_same_outputs_as_pr40(self):
        small = transcript(os.path.join(self.d, "small.jsonl"), ["design the router", "ok"])
        big = self.big(CRON)
        cases = [
            ("next step", fixed(0.1, 0.8, 0.1), big, "claude-sonnet-5-5", "enforce", None),
            ("next step", fixed(0.1, 0.8, 0.1), big, "claude-sonnet-5-5", "enforce", "off"),
            ("and the docs", fixed(0.05, 0.9, 0.05), big, "claude-opus-5-5", "enforce", None),
            ("what is 2+2", fixed(0.95, 0.05, 0), small, "claude-sonnet-5-5", "enforce", None),
            ("next step", fixed(0.1, 0.8, 0.1), big, "claude-sonnet-5-5", "shadow", "off"),
            ("<task-notification>x</task-notification>", fixed(0.1, 0.8, 0.1), big, "claude-sonnet-5-5", "enforce", None),
            ("[autoswitch-rollover:0123456789ab] hi", fixed(0.1, 0.8, 0.1), big, "claude-sonnet-5-5", "enforce", None),
        ]
        for prompt, fn, tr, model, mode, ro in cases:
            sc = Scorer(self, fn)
            states = []
            for code in (self.base, SB):
                base = tempfile.mkdtemp(dir=self.d)
                _write_text(os.path.join(base, "watcher.alive"), "")
                _write_text(os.path.join(base, "rollover"), "on")                 # file on, env decides when set
                if ro is None:
                    os.remove(os.path.join(base, "rollover"))
                os.makedirs(os.path.join(base, "s"))
                p = os.path.join(base, "s", "a", "b"); os.makedirs(p)
                _write_json(os.path.join(p, ID + ".json"), {"sessionId": ID, "cliSessionId": "c1", "model": model, "title": "T"})
                states.append(self._run(code, base, prompt, sc, tr, mode, ro))
            self.assertEqual(states[0], states[1], (prompt, model, mode, ro))
            self.assertNotIn("rollover", json.dumps(states[1][2]))

    def test_flag(self):
        import rollover
        self.assertTrue(rollover.active())
        os.environ["AUTOSWITCH_HANDOFF"] = "off"; self.assertFalse(rollover.active())
        os.environ["AUTOSWITCH_HANDOFF"] = "on"
        os.environ["AUTOSWITCH_ROLLOVER"] = "off"; self.assertFalse(rollover.enabled())
        os.environ.pop("AUTOSWITCH_ROLLOVER"); self.assertFalse(rollover.enabled())
        _write_text(os.path.join(self.d, "rollover"), "on\n"); self.assertTrue(rollover.enabled())


if __name__ == "__main__":
    unittest.main()
