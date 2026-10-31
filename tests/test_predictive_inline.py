"""Speeds `predictive` and `inline`, the stop-text option and the same-tier upgrade.

predictive: switch while the user types (hook/typeahead.py), so the message goes out on the right model with no
stop and no wait. A hook cannot change the model of the message it is holding (measured live), so the switch has to
happen before Enter. inline: the autoswitch-inline plugin picks each turn's model itself; the hook stays silent.
"""
import importlib
import json
import os
import shutil
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

from test_autoswitch import ID, ID3, _read_json, _write_json, passes_clean, stops  # noqa: E402
from v2_helpers import _write_text  # noqa: E402


def scorer(test, label, prob):
    """A local stand-in for the scorer service that always answers `label` at `prob`."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(s):
            s.rfile.read(int(s.headers["Content-Length"]))
            s.send_response(200)
            s.end_headers()
            s.wfile.write(json.dumps({"label": label, "probs": {label: prob}}).encode())

        def log_message(s, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return f"http://127.0.0.1:{srv.server_port}"


class PredictiveInlineTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.saved = {k: os.environ.get(k) for k in ("AUTOSWITCH_DIR", "AUTOSWITCH_SESSIONS", "AUTOSWITCH_SPEED",
                                                     "AUTOSWITCH_TYPEAHEAD_DRY", "AUTOSWITCH_MODE", "AUTOSWITCH_HANDOFF",
                                                     "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_HOST_SESSION_ID")}
        for k in ("AUTOSWITCH_SPEED", "AUTOSWITCH_TYPEAHEAD_DRY", "AUTOSWITCH_MODE", "AUTOSWITCH_HANDOFF"):
            os.environ.pop(k, None)
        os.environ["AUTOSWITCH_DIR"] = self.d
        os.environ["AUTOSWITCH_SESSIONS"] = os.path.join(self.d, "s")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID
        _write_text(os.path.join(self.d, "watcher.alive"), "")
        _write_text(os.path.join(self.d, "mode"), "enforce\n")
        _write_text(os.path.join(self.d, "speed"), "predictive\n")
        os.makedirs(os.path.join(self.d, "s", "a", "b"))
        self._on("claude-opus-5-5")
        import sbqueue
        import typeahead
        self.q = importlib.reload(sbqueue)
        self.t = importlib.reload(typeahead)
        self.want = {"target": "claude-haiku-5-5", "reason": "haiku at 90%", "label": "haiku", "confidence": 0.9}
        self.t.decide = lambda app_id, rec, text: dict(self.want)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _on(self, model):
        _write_json(os.path.join(self.d, "s", "a", "b", ID + ".json"),
                    {"sessionId": ID, "cliSessionId": "cli1", "model": model, "title": "Some chat"})

    def _pending(self):
        p = os.path.join(self.d, "pending")
        return [_read_json(os.path.join(p, f)) for f in sorted(os.listdir(p)) if f.endswith(".json")] if os.path.isdir(p) else []

    def _hook(self, prompt, url, **env):
        tr = os.path.join(self.d, "t.jsonl")
        _write_text(tr, "x" * 400)
        return subprocess.run([sys.executable, os.path.join(SB, "hint.py")], capture_output=True, text=True,
                              encoding="utf-8", env={**os.environ, "AUTOSWITCH_SCORER": url, **env},
                              input=json.dumps({"prompt": prompt, "session_id": "cli1", "transcript_path": tr}))

    def _dry(self, prompt, url, **env):
        return json.loads(self._hook(prompt, url, AUTOSWITCH_DRY="1", **env).stdout)

    # --- speed setting ---
    def test_speed_values(self):
        self.assertEqual((self.q.speed(), self.q.never_holds()), ("predictive", True))
        _write_text(self.q.path("speed"), "instant\n")
        self.assertEqual((self.q.speed(), self.q.never_holds()), ("instant", True))
        _write_text(self.q.path("speed"), "Predictive \n")
        self.assertEqual(self.q.speed(), "predictive")
        _write_text(self.q.path("speed"), "inline\n")
        self.assertEqual((self.q.speed(), self.q.never_holds()), ("inline", True))
        _write_text(self.q.path("speed"), "fast\n")
        self.assertEqual((self.q.speed(), self.q.never_holds()), ("seamless", False))

    def test_inline_marker(self):
        self.assertFalse(self.q.inline_marked("cli1"))
        os.makedirs(self.q.path("inline"))
        _write_text(self.q.path("inline", "cli1"), "1\n")
        self.assertTrue(self.q.inline_marked("cli1"))
        self.assertFalse(self.q.inline_marked(None))
        self.assertFalse(self.q.inline_marked("../inline/cli1"))  # never a path outside the marker folder

    # --- typeahead drafts ---
    def test_draft_queues_a_switch_only_request(self):
        self.assertEqual(self.t.draft(ID, "whats 7x2", "Some chat"), "queued claude-haiku-5-5")
        (req,) = self._pending()
        self.assertEqual((req["session_id"], req["model"], req["resend"], req["typeahead"], req["cli_session_id"]),
                         (ID, "claude-haiku-5-5", False, True, "cli1"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", req["id"])))  # nothing is ever re-sent
        self.assertEqual(self.t.draft(ID, "whats 7x2 pls", "Some chat"), "already queued")
        self.assertEqual(len(self._pending()), 1)

    def test_a_changed_draft_replaces_the_unclaimed_switch(self):
        self.t.draft(ID, "whats 7x2", None)
        self.want = {"target": "claude-sonnet-5-5", "reason": "x", "label": "sonnet", "confidence": 0.8}
        self.assertEqual(self.t.draft(ID, "whats 7x2 and refactor the parser", None), "queued claude-sonnet-5-5")
        self.assertEqual([r["model"] for r in self._pending()], ["claude-sonnet-5-5"])
        self.want = {"target": None, "reason": "already on target model"}
        self.assertEqual(self.t.draft(ID, "whats 7x2 and refactor the parser into modules", None), "stay")
        self.assertEqual(self._pending(), [])  # the draft no longer wants a switch: the queued one is withdrawn

    def test_draft_never_withdraws_a_hook_request(self):
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "sent prompt", "haiku", 0.9, resend=False)
        self.want = {"target": None, "reason": "x"}
        self.t.draft(ID, "next draft", None)
        self.assertEqual([r["id"] for r in self._pending()], [rid])

    def test_draft_does_nothing_unless_it_should(self):
        _write_text(self.q.path("speed"), "instant\n")
        self.assertEqual(self.t.draft(ID, "whats 7x2", None), "off")
        _write_text(self.q.path("speed"), "predictive\n")
        _write_text(self.q.path("mode"), "shadow\n")
        self.assertEqual(self.t.draft(ID, "whats 7x2", None), "off")
        _write_text(self.q.path("mode"), "enforce\n")
        self.assertEqual(self.t.draft(ID, "hi", None), "skip")              # too short to score
        self.assertEqual(self.t.draft(ID, "/model haiku", None), "skip")    # a command
        self.assertEqual(self.t.draft(ID, "whats 7x2", "Other chat"), "title mismatch")
        self.assertEqual(self.t.draft(ID3, "whats 7x2", None), "no record")
        for d in ({"target": "claude-haiku-5-5", "screenshot": True}, {"target": None, "handoff_to": "claude-haiku-5-5"}):
            self.want = d
            self.assertEqual(self.t.draft(ID, "whats 7x2", None), "stay")
        os.remove(os.path.join(self.d, "watcher.alive"))
        self.assertEqual(self.t.draft(ID, "whats 7x2", None), "no watcher")
        self.assertEqual(self._pending(), [])

    def test_dry_run_only_logs(self):
        _write_text(self.q.path("speed"), "instant\n")
        os.environ["AUTOSWITCH_TYPEAHEAD_DRY"] = "1"
        self.assertEqual(self.t.draft(ID, "whats 7x2", None), "would queue claude-haiku-5-5 (haiku at 90%)")
        self.assertEqual(self._pending(), [])

    @unittest.skipUnless(sys.platform == "darwin", "the typeahead reader is macOS-only")
    def test_spawn_starts_the_reader_once_and_records_its_pid(self):
        os.makedirs(self.q.path("bin"))
        fake = self.q.path("bin", "typeahead")
        _write_text(fake, "#!/bin/sh\nexec sleep 30\n")
        os.chmod(fake, 0o755)
        os.utime(fake, (time.time() + 60, time.time() + 60))  # newer than the source: no rebuild
        first = self.t.spawn()
        self.assertTrue(first.startswith("started"), first)
        with open(self.q.path("typeahead.pid")) as f:
            pid = int(f.read())
        self.addCleanup(lambda: os.kill(pid, 9))
        self.assertEqual(self.t.spawn(), "running")
        _write_text(self.q.path("speed"), "instant\n")
        self.assertEqual(self.t.spawn(), "off")

    # --- the hook in dry mode and at submit ---
    def test_hook_dry_run_prints_the_decision_and_changes_nothing(self):
        d = self._dry("what is 2+2", scorer(self, "haiku", 0.95))
        self.assertEqual((d["app_id"], d["label"], d["current"], d["target"]),
                         (ID, "haiku", "claude-opus-5-5", "claude-haiku-5-5"))
        self.assertEqual(self._pending(), [])
        # no shadow log, no pin observation, no queue: only the files this test wrote
        self.assertEqual(set(os.listdir(self.d)), {"watcher.alive", "mode", "speed", "s", "t.jsonl"})

    def test_hook_dry_run_switches_nothing_unless_switching_is_on(self):
        _write_text(self.q.path("mode"), "shadow\n")
        d = self._dry("what is 2+2", scorer(self, "haiku", 0.95))
        self.assertIsNone(d["target"])
        self.assertIn("shadow", d["reason"])

    def test_submit_in_predictive_never_holds_and_does_not_queue_twice(self):
        url = scorer(self, "haiku", 0.99)
        dry = self._dry("what is 2+2", url)
        self.assertTrue(dry["target"])
        self.t.decide = lambda app_id, rec, text: dry
        self.assertTrue(self.t.draft(ID, "what is 2+2", None).startswith("queued"))
        out = self._hook("what is 2+2", url)
        self.assertTrue(passes_clean(out))                  # no stop, no wait
        self.assertEqual(len(self._pending()), 1)          # the draft's switch is already on its way

    def test_predictive_holds_only_when_it_is_worth_the_wait(self):
        url = scorer(self, "haiku", 0.99)
        self.assertTrue(passes_clean(self._hook("what is 2+2", url)))         # short, only cheaper: no wait
        shutil.rmtree(os.path.join(self.d, "pending"), ignore_errors=True)
        big = "please go through every file in the repo and rename the helper " + "x" * 400
        out = self._hook(big, url)
        self.assertTrue(stops(out))                                           # big: held, switched, re-sent
        (req,) = self._pending()
        self.assertTrue(req["resend"])

    def test_predictive_holds_an_up_switch_and_withdraws_the_drafts_request(self):
        self._on("claude-haiku-5-5")
        self.want = {"target": "claude-opus-5-5", "reason": "x", "label": "opus", "confidence": 0.99}
        self.t.draft(ID, "design the storage", None)
        url = scorer(self, "opus", 0.99)
        self.assertTrue(stops(self._hook("design the storage layer", url)))
        (req,) = self._pending()
        self.assertTrue(req["resend"])
        self.assertNotIn("typeahead", req)

    def test_inline_speed_stops_nothing_queues_nothing_shows_nothing(self):
        _write_text(self.q.path("speed"), "inline\n")
        os.makedirs(self.q.path("inline"))
        _write_text(self.q.path("inline", "cli1"), "1\n")  # the plugin runs in this chat
        for label in ("haiku", "opus"):
            out = self._hook("what is 2+2 " + label, scorer(self, label, 0.99))
            self.assertTrue(passes_clean(out))
            self.assertEqual(out.stdout.strip(), "")       # no notice at all: the plugin switches the turn itself
        self.assertEqual(self._pending(), [])
        self.assertEqual(self.t.draft(ID, "whats 7x2", None), "off")  # the plugin handles this chat at Enter

    def test_inline_needs_no_watcher_in_a_chat_with_the_plugin(self):
        _write_text(self.q.path("speed"), "inline\n")
        os.makedirs(self.q.path("inline"))
        _write_text(self.q.path("inline", "cli1"), "1\n")
        os.remove(os.path.join(self.d, "watcher.alive"))
        url = scorer(self, "haiku", 0.99)
        out = self._hook("what is 2+2", url)
        self.assertEqual(out.stdout.strip(), "")           # no "no Watcher" note: inline does not use one
        self.assertEqual(self._dry("what is 2+2", url)["target"], "claude-haiku-5-5")

    def test_inline_without_the_plugin_falls_back_to_predictive(self):
        _write_text(self.q.path("speed"), "inline\n")  # no inline/cli1 marker: a chat opened before the plugin
        url = scorer(self, "haiku", 0.99)
        dry = self._dry("what is 2+2", url)
        self.assertTrue(dry["target"])
        self.t.decide = lambda app_id, rec, text: dry
        self.assertTrue(self.t.draft(ID, "what is 2+2", None).startswith("queued"))   # switches while typing
        out = self._hook("what is 2+2", url)
        self.assertTrue(passes_clean(out))
        self.assertIn("Switching", json.loads(out.stdout)["systemMessage"])          # predictive's submit path

    # --- stop text ---
    def test_stop_text_can_be_hidden(self):
        _write_text(self.q.path("speed"), "seamless\n")
        url = scorer(self, "haiku", 0.99)
        self.assertEqual(json.loads(self._hook("some prompt", url).stdout)["stopReason"], "↪ Switched to Haiku")
        shutil.rmtree(os.path.join(self.d, "pending"), ignore_errors=True)
        _write_text(self.q.path("stoptext"), "hide\n")
        out = self._hook("some prompt", url)
        self.assertTrue(stops(out))
        self.assertEqual(json.loads(out.stdout)["stopReason"], " ")  # never empty: no reason means no stop

    # --- same tier, newer model ---
    def test_latest_in_tier(self):
        import policy
        self.assertEqual(policy.latest_in_tier("claude-opus-4-7"), "claude-opus-5-5")
        self.assertEqual(policy.latest_in_tier("claude-sonnet-5"), "claude-sonnet-5-5")
        self.assertEqual(policy.latest_in_tier("claude-haiku-4-5"), "claude-haiku-5-5")
        for m in ("claude-opus-5-5", "claude-opus-5-5[1m]", None, "", "some-other-model"):
            self.assertIsNone(policy.latest_in_tier(m), m)

    def test_an_older_model_moves_to_its_tiers_newest(self):
        self._on("claude-opus-4-7")
        d = self._dry("design the storage layer", scorer(self, "opus", 0.99))
        self.assertEqual(d["target"], "claude-opus-5-5")
        self.assertIn("newer model", d["reason"])
        self._on("claude-opus-5-5")
        d = self._dry("design the storage layer", scorer(self, "opus", 0.99))
        self.assertIsNone(d["target"])                                    # already the newest: nothing to do

    def test_no_upgrade_in_a_long_chat_or_a_pinned_one(self):
        self._on("claude-opus-4-7")
        d = self._dry("design the storage layer", scorer(self, "opus", 0.99), AUTOSWITCH_UPGRADE_MAX_TOKENS="10")
        self.assertIsNone(d["target"])                                    # the first message would re-read it all
        import pins
        importlib.reload(pins)
        pins.pin(ID, "claude-opus-4-7", "hand change")
        self.assertIsNone(self._dry("design the storage layer", scorer(self, "opus", 0.99))["target"])

    def test_new_chat_on_an_older_model_moves_to_the_newest(self):
        os.remove(os.path.join(self.d, "s", "a", "b", ID + ".json"))     # no record yet: a brand-new chat
        d = self._dry("design the storage layer", scorer(self, "opus", 0.30),
                      AUTOSWITCH_CURRENT_MODEL="claude-opus-4-7")
        self.assertEqual(d["target"], "claude-opus-5-5")

    # --- the CLI ---
    def _cli(self, *args, **env):
        return subprocess.run([sys.executable, os.path.join(REPO, "bin", "autoswitch"), *args], capture_output=True,
                              text=True, encoding="utf-8", env={**os.environ, **env})

    def test_cli_speed_accepts_all_four(self):
        for word in ("predictive", "instant", "seamless"):
            out = self._cli("speed", word)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(self._cli("speed").stdout.strip(), word)
        self.assertEqual(self._cli("speed", "turbo").returncode, 2)

    def test_cli_speed_inline_installs_the_plugin(self):
        log = os.path.join(self.d, "claude-args.txt")
        fake = os.path.join(self.d, "fake-claude")
        _write_text(fake, '#!/bin/sh\necho "$@" >> "%s"\n' % log)
        os.chmod(fake, 0o755)
        out = self._cli("speed", "inline", AUTOSWITCH_CLAUDE=fake)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self._cli("speed").stdout.strip(), "inline")
        with open(log) as f:
            calls = f.read().splitlines()
        self.assertEqual(calls, ["plugin marketplace add " + REPO,
                                 "plugin install autoswitch-inline@claude-autoswitch --scope user"])
        self.assertTrue(os.path.isdir(os.path.join(self.d, "inline")))
        self.assertIn("installed", out.stdout)

    def test_cli_speed_inline_prints_the_commands_when_it_cannot_install(self):
        out = self._cli("speed", "inline", AUTOSWITCH_CLAUDE="x", AUTOSWITCH_NO_PLUGIN_INSTALL="1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("claude plugin marketplace add", out.stdout)
        self.assertIn("claude plugin install autoswitch-inline@claude-autoswitch --scope user", out.stdout)

    def test_marketplace_lists_the_plugin(self):
        with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            m = json.load(f)
        (entry,) = [e for e in m["plugins"] if e["name"] == "autoswitch-inline"]
        self.assertEqual(m["name"], "claude-autoswitch")
        self.assertTrue(os.path.isfile(os.path.join(REPO, entry["source"], ".claude-plugin", "plugin.json")))


if __name__ == "__main__":
    unittest.main()
