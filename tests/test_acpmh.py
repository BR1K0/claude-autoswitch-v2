import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from acpmh import extract, handoff, images, ingest, redact, tools, verify  # noqa: E402
from acpmh.store import Store  # noqa: E402


def entry(uuid, typ, content, model=None):
    msg = {"role": typ, "content": content}
    if model:
        msg["model"] = model
    return {"type": typ, "uuid": uuid, "sessionId": "s1", "timestamp": "t",
            "message": msg}


def transcript(prefix="a"):
    return [
        entry(prefix + "1", "user", "Build a login page. Never store passwords in plain text."),
        entry(prefix + "2", "assistant", [{"type": "text", "text": "I'll use bcrypt."},
                                          {"type": "tool_use", "id": prefix + "t1",
                                           "name": "Edit",
                                           "input": {"file_path": "src/login.py",
                                                     "old_string": "a", "new_string": "b"}}],
              model="claude-sonnet-5-5"),
        entry(prefix + "3", "user", [{"type": "tool_result", "tool_use_id": prefix + "t1",
                                      "content": "ok"}]),
        entry(prefix + "4", "assistant", [{"type": "tool_use", "id": prefix + "t2",
                                           "name": "Bash",
                                           "input": {"command": "python3 -m pytest -q"}}]),
        entry(prefix + "5", "user", [{"type": "tool_result", "tool_use_id": prefix + "t2",
                                      "content": "3 passed in 0.1s"}]),
        entry(prefix + "6", "assistant", "All tests pass now."),
    ]


class FakeClient:
    """Stands in for anthropic.Anthropic in extraction tests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = self

    def create(self, **kw):
        text = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "j"))


class JournalTests(Base):
    def test_append_is_idempotent_and_sequenced(self):
        s1, new1 = self.store.append("k1", "note", "claimed", "one")
        s2, new2 = self.store.append("k1", "note", "claimed", "one again")
        s3, _ = self.store.append("k2", "note", "claimed", "two")
        self.assertEqual((s1, new1, s2, new2, s3), (1, True, 1, False, 2))
        self.assertEqual(self.store.verify_chain(), [])

    def test_tampering_is_detected(self):
        self.store.append("k1", "note", "claimed", "one")
        self.store.append("k2", "note", "claimed", "two")
        with self.store.db:
            self.store.db.execute("UPDATE events SET summary='edited' WHERE seq=1")
        self.assertTrue(any("hash mismatch" in p for p in self.store.verify_chain()))

    def test_gap_is_detected(self):
        for i in range(3):
            self.store.append("k%d" % i, "note", "claimed", str(i))
        with self.store.db:
            self.store.db.execute("DELETE FROM events WHERE seq=2")
        self.assertTrue(any("gap" in p for p in self.store.verify_chain()))

    def test_large_body_goes_to_blob_and_is_searchable(self):
        seq, _ = self.store.append("k", "command", "observed", "$ build",
                                   "x" * 50000 + " unique_marker_token")
        e = self.store.event(seq)
        self.assertEqual(len(self.store.get_blob(e["body_ref"])), 50020)
        self.assertEqual(self.store.search("build")[0]["ref"], "evt_000001")

    def test_reopen_persists(self):
        self.store.append("k", "note", "claimed", "persist me")
        again = Store(os.path.join(self.dir, "j"))
        self.assertEqual(again.event(1)["summary"], "persist me")


class IngestTests(Base):
    def test_kinds_and_statuses(self):
        ingest.ingest(self.store, transcript())
        kinds = [(e["kind"], e["status"]) for e in self.store.events()]
        self.assertEqual(kinds, [("user_prompt", "stated"), ("assistant_text", "claimed"),
                                 ("file_modified", "observed"), ("test_passed", "observed"),
                                 ("assistant_text", "claimed")])
        self.assertEqual(json.loads(self.store.event(3)["paths"]), ["src/login.py"])

    def test_reingest_creates_nothing(self):
        ingest.ingest(self.store, transcript())
        self.assertEqual(ingest.ingest(self.store, transcript()), 0)

    def test_system_reminders_stripped(self):
        ingest.ingest(self.store, [entry("x", "user",
                                         "<system-reminder>noise</system-reminder>\nreal ask")])
        self.assertEqual(self.store.get_blob(self.store.event(1)["body_ref"]), "real ask")

    def test_tagged_messages_are_kept(self):
        ingest.ingest(self.store, [
            entry("m1", "user", '<cross-session-message from="lead">Ship PR 2 today</cross-session-message>'),
            entry("m2", "user", '<pasted_content id="x">spec text</pasted_content>'),
            entry("m3", "user", "<local-command-caveat>boilerplate</local-command-caveat>"),
            entry("m4", "user", "<some-new-tag>unknown thing</some-new-tag>"),
        ])
        self.assertEqual([(e["kind"], e["status"]) for e in self.store.events()],
                         [("agent_message", "stated"), ("user_prompt", "stated"),
                          ("notification", "observed")])
        self.assertIn("Ship PR 2 today", self.store.event(1)["summary"])

    def test_injected_meta_text_is_not_an_instruction(self):
        e = entry("s1", "user", "Base directory for this skill: ...long skill body...")
        e["isMeta"] = True
        ingest.ingest(self.store, [e])
        self.assertEqual(self.store.event(1)["kind"], "injected_context")

    def test_in_flight_tool_call_is_recorded(self):
        t = transcript()[:2]  # Edit call made, result not back yet
        ingest.ingest(self.store, t)
        last = self.store.events()[-1]
        self.assertEqual(last["kind"], "tool_started")
        self.assertIn("src/login.py", last["summary"])
        ingest.ingest(self.store, transcript())  # result arrives later
        kinds = [e["kind"] for e in self.store.events()]
        self.assertIn("file_modified", kinds)
        self.assertEqual(kinds.count("tool_started"), 1)

    def test_failed_test_detected(self):
        t = [entry("a", "assistant", [{"type": "tool_use", "id": "t", "name": "Bash",
                                       "input": {"command": "pytest"}}]),
             entry("b", "user", [{"type": "tool_result", "tool_use_id": "t",
                                  "content": "1 failed, 2 passed", "is_error": True}])]
        ingest.ingest(self.store, t)
        self.assertEqual(self.store.event(1)["kind"], "test_failed")

    def test_cut_index(self):
        t = transcript()
        self.assertLess(ingest.cut_index(t, 5), len(t))
        self.assertEqual(ingest.cut_index(t, 10 ** 9), len(t))


class ExtractTests(Base):
    def test_validation_rules(self):
        ingest.ingest(self.store, transcript())
        data = {"add": [
            {"kind": "constraint", "text": "Never store plain passwords", "status": "stated",
             "sources": [1]},
            {"kind": "fact", "text": "Uses bcrypt", "status": "verified", "sources": [2]},
            {"kind": "requirement", "text": "pretend user said", "status": "stated",
             "sources": [2]},
            {"kind": "fact", "text": "bad source", "status": "claimed", "sources": [99]},
        ], "segment": "login work"}
        added, rejected = extract.apply(self.store, data, 1, 5, {1})
        self.assertEqual((added, len(rejected)), (3, 1))
        st = {f["text"]: f["status"] for f in self.store.active_facts()}
        self.assertEqual(st["Never store plain passwords"], "stated")
        self.assertEqual(st["Uses bcrypt"], "claimed")         # model cannot verify
        self.assertEqual(st["pretend user said"], "claimed")   # not the user's words
        self.assertEqual(self.store.segments()[0]["text"], "login work")

    def test_supersede_keeps_history(self):
        ingest.ingest(self.store, transcript())
        extract.apply(self.store, {"add": [{"kind": "decision", "text": "Use cookies",
                                            "status": "claimed", "sources": [2]}]}, 1, 5, set())
        extract.apply(self.store, {"add": [{"kind": "decision", "text": "Use JWT",
                                            "status": "claimed", "sources": [5],
                                            "supersedes": ["F1"]}]}, 1, 5, set())
        active = [f["text"] for f in self.store.active_facts()]
        self.assertEqual(active, ["Use JWT"])
        old = self.store.db.execute("SELECT * FROM facts WHERE fact_id=1").fetchone()
        self.assertEqual((old["status"], old["superseded_by"]), ("superseded", 2))

    def test_run_is_incremental(self):
        ingest.ingest(self.store, transcript())
        client = FakeClient([json.dumps({"add": [{"kind": "task", "text": "login page",
                                                  "status": "stated", "sources": [1]}]})])
        extract.run(self.store, client, log=lambda *_: None)
        self.assertEqual(self.store.get_meta("last_incorporated_seq"), "5")
        # Nothing new: no model call (FakeClient would raise on empty replies).
        extract.run(self.store, client, log=lambda *_: None)
        usage = self.store.db.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
        self.assertEqual(usage, 1)

    def test_failed_extraction_keeps_position(self):
        ingest.ingest(self.store, transcript())
        client = FakeClient(["not json", "still not json"])
        with self.assertRaises(RuntimeError):
            extract.run(self.store, client, log=lambda *_: None)
        self.assertEqual(self.store.get_meta("last_incorporated_seq"), "0")


class VerifyTests(Base):
    def test_claim_verified_by_test_output(self):
        ingest.ingest(self.store, transcript())
        extract.apply(self.store, {"add": [{"kind": "fact", "text": "All tests pass",
                                            "status": "claimed", "sources": [5]}]}, 1, 5, set())
        verify.run(self.store)
        f = self.store.active_facts()[0]
        self.assertEqual(f["status"], "verified")
        self.assertIn("evt_000004", f["note"])

    def test_claim_without_evidence_stays_unverified(self):
        ingest.ingest(self.store, [entry("z", "assistant", "All tests pass, trust me.")])
        extract.apply(self.store, {"add": [{"kind": "fact", "text": "tests pass",
                                            "status": "claimed", "sources": [1]}]}, 1, 1, set())
        verify.run(self.store)
        self.assertEqual(self.store.active_facts()[0]["status"], "claimed")
        self.assertEqual(len(verify.unverified_claims(self.store)), 1)

    def test_uncaptured_user_prompts(self):
        ingest.ingest(self.store, transcript())
        self.store.set_meta("last_incorporated_seq", "5")
        self.assertEqual([e["seq"] for e in verify.uncaptured_user_prompts(self.store)], [1])
        extract.apply(self.store, {"add": [{"kind": "constraint", "text": "no plain pw",
                                            "status": "stated", "sources": [1]}]}, 1, 5, {1})
        self.assertEqual(verify.uncaptured_user_prompts(self.store), [])


class HandoffTests(Base):
    def test_arbitrary_switch_includes_gap(self):
        # Memory synced after session A; session B happens; switch with no new sync.
        ingest.ingest(self.store, transcript("a"))
        extract.apply(self.store, {"add": [{"kind": "constraint", "text": "NO_PLAIN_PW",
                                            "status": "stated", "sources": [1]}]}, 1, 5, {1})
        self.store.set_meta("last_incorporated_seq", "5")
        b = transcript("b")
        b[0] = entry("b1", "user", "Now add GAP_REQUIREMENT_RATE_LIMIT to login.")
        ingest.ingest(self.store, b)
        text, stats = handoff.build(self.store)
        self.assertIn("NO_PLAIN_PW", text)
        self.assertIn("GAP_REQUIREMENT_RATE_LIMIT", text)
        self.assertEqual(stats["gap_events"], 5)
        self.assertEqual(stats["integrity_problems"], [])

    def test_uncommitted_changes_in_live_git_state(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        run = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True)
        run("init", "-q")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "init")
        with open(os.path.join(repo, "new.py"), "w") as f:
            f.write("x")
        text, _ = handoff.build(self.store, repo=repo)
        self.assertIn("new.py", text)

    def test_mandatory_facts_never_dropped_by_budget(self):
        ingest.ingest(self.store, transcript())
        items = [{"kind": "fact", "text": "filler %d %s" % (i, "y" * 200), "status": "claimed",
                  "sources": [2]} for i in range(200)]
        items.append({"kind": "constraint", "text": "CRITICAL_RULE", "status": "stated",
                      "sources": [1]})
        extract.apply(self.store, {"add": items}, 1, 5, {1})
        text, stats = handoff.build(self.store, budget_tokens=500)
        self.assertIn("CRITICAL_RULE", text)
        self.assertIn("older facts omitted", text)
        self.assertLess(stats["facts_in_handoff"], stats["facts"])


class ToolTests(Base):
    def test_search_get_list_file(self):
        ingest.ingest(self.store, transcript())
        self.assertIn("evt_000001", tools.call(self.store, "search_history",
                                               {"query": "passwords"}))
        self.assertIn("3 passed", tools.call(self.store, "get_event", {"event_id": "evt_000004"}))
        self.assertEqual(len(tools.call(self.store, "list_events",
                                        {"start": "1", "end": "5"}).splitlines()), 5)
        self.assertIn("src/login.py", tools.call(self.store, "file_history",
                                                 {"path": "login.py"}))

    def test_get_event_pages_long_bodies(self):
        self.store.append("k", "command", "observed", "$ big", "A" * 20000)
        first = tools.call(self.store, "get_event", {"event_id": "1"})
        self.assertIn("offset=8000", first)
        last = tools.call(self.store, "get_event", {"event_id": "1", "offset": 16000})
        self.assertNotIn("more chars", last)

    def test_bad_input_returns_error_text(self):
        self.assertIn("Tool error", tools.call(self.store, "get_event", {}))


class HookTests(Base):
    def test_hook_ingests_transcript_and_never_fails(self):
        tpath = os.path.join(self.dir, "t.jsonl")
        with open(tpath, "w") as f:
            for e in transcript():
                f.write(json.dumps(e) + "\n")
        env = dict(os.environ, ACPMH_STORE=os.path.join(self.dir, "hookstore"))
        payload = json.dumps({"transcript_path": tpath, "cwd": self.dir,
                              "hook_event_name": "PreCompact", "session_id": "s1"})
        for _ in range(2):  # second run adds nothing new from the transcript
            p = subprocess.run([sys.executable, "-m", "acpmh.hook"], cwd=ROOT, env=env,
                               input=payload, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0)
        s = Store(os.path.join(self.dir, "hookstore"))
        kinds = [e["kind"] for e in s.events()]
        self.assertEqual(kinds.count("user_prompt"), 1)
        self.assertEqual(kinds.count("compaction"), 2)
        self.assertEqual(s.verify_chain(), [])
        # Garbage input still exits 0.
        p = subprocess.run([sys.executable, "-m", "acpmh.hook"], cwd=ROOT, env=env,
                           input="not json", capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)


class McpTests(Base):
    def test_stdio_protocol(self):
        ingest.ingest(self.store, transcript())
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "search_history", "arguments": {"query": "bcrypt"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "prepare_handoff", "arguments": {}}}]
        p = subprocess.run([sys.executable, "-m", "acpmh.mcp_server", "--store",
                            os.path.join(self.dir, "j")], cwd=ROOT, capture_output=True,
                           text=True, input="\n".join(json.dumps(r) for r in reqs) + "\n",
                           timeout=30)
        out = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([o["id"] for o in out], [1, 2, 3, 4])
        names = {t["name"] for t in out[1]["result"]["tools"]}
        self.assertTrue({"search_history", "prepare_handoff", "record_event"} <= names)
        self.assertIn("bcrypt", out[2]["result"]["content"][0]["text"])
        self.assertIn("ACPMH HANDOFF", out[3]["result"]["content"][0]["text"])

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def img(data=PNG, media="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def shot_transcript():
    return [
        entry("u1", "user", [img(), {"type": "text", "text": "what does this error mean?"}]),
        entry("u2", "assistant", [{"type": "tool_use", "id": "t9", "name": "computer",
                                   "input": {"action": "screenshot"}}]),
        entry("u3", "user", [{"type": "tool_result", "tool_use_id": "t9",
                              "content": [{"type": "text", "text": "done"}, img()]}]),
    ]


class ScreenshotTests(Base):
    def setUp(self):
        super().setUp()
        self._ocr, self._avail = images.ocr, images.ocr_available
        images.ocr = lambda paths: {p: "KeyError: 'model' in policy.py line 42\n"
                                       "password: hunter22 key sk-ant-api03-ABCDEFGHIJKLMNOP"
                                    for p in paths}
        images.ocr_available = lambda: True

    def tearDown(self):
        images.ocr, images.ocr_available = self._ocr, self._avail

    def test_image_stored_with_text_and_idempotent(self):
        ingest.ingest(self.store, shot_transcript())
        kinds = [e["kind"] for e in self.store.events()]
        self.assertEqual(kinds.count("screenshot"), 1)
        self.assertEqual(kinds.count("tool_image"), 1)
        path, media = tools.image_file(self.store, "evt_000001")
        self.assertTrue(os.path.exists(path) and media == "image/png")
        blobs = sorted(os.listdir(os.path.join(self.dir, "j", "blobs")))
        self.assertEqual(ingest.ingest(self.store, shot_transcript()), 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "j", "blobs"))), blobs)

    def test_ocr_text_searchable_and_secrets_removed(self):
        ingest.ingest(self.store, shot_transcript())
        self.assertIn("evt_000001", tools.call(self.store, "search_history",
                                               {"query": "KeyError policy.py"}))
        body = tools.call(self.store, "get_event", {"event_id": "evt_000001"})
        self.assertNotIn("hunter22", body)
        self.assertNotIn("ABCDEFGHIJKLMNOP", body)
        self.assertIn("REDACTED", body)

    def test_get_event_and_get_image_give_the_reference(self):
        ingest.ingest(self.store, shot_transcript())
        out = tools.call(self.store, "get_event", {"event_id": "evt_000001"})
        self.assertIn("Image file: ", out)
        self.assertIn("Same message: evt_000002 [user_prompt]", out)
        self.assertIn(".png", tools.call(self.store, "get_image", {"event_id": "1"}))
        self.assertIn("No image", tools.call(self.store, "get_image", {"event_id": "2"}))
        text, _ = handoff.build(self.store)
        self.assertIn("## Screenshots and images", text)
        self.assertIn("KeyError", text)

    def test_broken_or_missing_image_does_not_crash(self):
        t = [entry("b1", "user", [img(data="%%%not base64%%%"),
                                  {"type": "image", "source": {"type": "url", "url": "x"}},
                                  {"type": "image"}, {"type": "text", "text": "see"}])]
        self.assertEqual(ingest.ingest(self.store, t), 4)
        bodies = [tools.call(self.store, "get_event", {"event_id": str(i)}) for i in (1, 2, 3)]
        self.assertTrue(all("could not be stored" in b for b in bodies))

    def test_no_ocr_still_stores_image(self):
        images.ocr, images.ocr_available = (lambda paths: {}), (lambda: False)
        ingest.ingest(self.store, shot_transcript())
        out = tools.call(self.store, "get_event", {"event_id": "1"})
        self.assertIn("not available", out)
        self.assertIsNotNone(tools.image_file(self.store, "1"))

    def test_mcp_returns_image_block(self):
        ingest.ingest(self.store, shot_transcript())
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "get_image", "arguments": {"event_id": "evt_000001"}}}]
        p = subprocess.run([sys.executable, "-m", "acpmh.mcp_server", "--store",
                            os.path.join(self.dir, "j")], cwd=ROOT, capture_output=True,
                           text=True, input=json.dumps(reqs[0]) + "\n", timeout=30)
        content = json.loads(p.stdout)["result"]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["data"], PNG)


class RedactTests(unittest.TestCase):
    def test_keys_and_passwords(self):
        text, n = redact.redact("token=abc123 xoxb-1234567890-abcdef and ghp_" + "a" * 36 +
                                " https://join.slack.com/t/x/shared_invite/zt-1 tokens: 292k")
        self.assertEqual(n, 4)
        self.assertIn("tokens: 292k", text)
        self.assertNotIn("abc123", text)


class V5Tests(Base):
    def _chat(self, n, words=50):
        t = []
        for i in range(n):
            t.append(entry("q%d" % i, "user", "request %d: %s" % (i, "word " * words)))
            t.append(entry("r%d" % i, "assistant", "LONGREPLY %d " % i + "x " * 2000))
        ingest.ingest(self.store, t)

    def test_every_user_message_verbatim_no_assistant_replies(self):
        self._chat(10)
        text, st = handoff.build(self.store, user_log=True, tail_tokens=0)
        for i in range(10):
            self.assertIn("request %d:" % i, text)
        log = text.split("## Every message from the user")[1]
        self.assertNotIn("LONGREPLY", log.split("\n## ")[0])
        self.assertEqual(st["user_log_omitted"], 0)

    def test_size_limit_keeps_newest_and_points_to_older(self):
        self._chat(300, words=200)
        text, st = handoff.build(self.store, user_log=True, tail_tokens=2000, max_tokens=20000)
        self.assertLessEqual(st["tokens_est"], 20000)  # nothing synced yet: gap is bounded
        self.store.set_meta("last_incorporated_seq", "600")  # synced, but no facts cite them
        text, st = handoff.build(self.store, user_log=True, tail_tokens=2000, max_tokens=20000)
        self.assertLessEqual(st["tokens_est"], 20000)
        self.assertIn("request 299:", text)
        self.assertNotIn("request 0:", text)
        self.assertGreater(st["user_log_omitted"], 0)
        self.assertIn("older messages", text)
        self.assertIn("Older instructions no fact captures", text)


if __name__ == "__main__":
    unittest.main()
