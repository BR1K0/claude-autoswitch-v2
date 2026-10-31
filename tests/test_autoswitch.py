import json, os, subprocess, sys, tempfile, threading, time, unittest


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f]


HERE = os.path.dirname(os.path.abspath(__file__))
SB = os.path.join(os.path.dirname(HERE), "hook")
sys.path.insert(0, SB)


ID = "local_11111111-2222-3333-4444-555555555555"
ID2 = "local_aaaaaaaa-2222-3333-4444-555555555555"
ID3 = "local_bbbbbbbb-2222-3333-4444-555555555555"


def fake_clipimg(image, text="", count=1):
    """A stand-in for the clipimg binary, so tests never read the real clipboard."""
    f = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    f.write("#!/bin/sh\ncat <<'EOF'\n" + json.dumps({"count": count, "image": image, "text": text}) + "\nEOF\n"); f.close()
    os.chmod(f.name, 0o755)
    return f.name


os.environ["AUTOSWITCH_CLIPIMG"] = fake_clipimg(False)  # default: no image on the clipboard


def passes_clean(out):
    """The hook let the prompt through: no stop of any kind (no continue:false, no decision key)."""
    try:
        d = json.loads(out.stdout)
    except ValueError:
        return True
    return isinstance(d, dict) and d.get("continue") is not False and "decision" not in d and "stopReason" not in d


def stops(out):
    """True if the hook stopped the turn (the switch path). Accepts only the stopReason form."""
    try:
        d = json.loads(out.stdout)
    except ValueError:
        return False
    return d.get("continue") is False and "stopReason" in d



def _rt(path):
    with open(path) as f:
        return f.read()


def _wt(path, text):
    with open(path, "w") as f:
        f.write(text)


def _rj(path):
    with open(path) as f:
        return json.load(f)


def _wj(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)

def readline_within(w, timeout=15, why=""):
    """w.stdout.readline(), but fail after `timeout` s instead of hanging the whole suite (threads: Windows pipes too)."""
    import threading
    out = []
    t = threading.Thread(target=lambda: out.append(w.stdout.readline()), daemon=True)
    t.start(); t.join(timeout)
    if not out:
        raise AssertionError("the Watcher printed nothing within %ss%s" % (timeout, why))
    return out[0]


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["AUTOSWITCH_DIR"] = self.d
        # tests may themselves run inside a desktop chat: pin the environment the hook sees
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID
        import importlib, sbqueue
        self.q = importlib.reload(sbqueue)
        _wt(os.path.join(self.d, "watcher.alive"), "")  # a Watcher is listening

    def test_decide(self):
        import policy
        d = lambda *a, **k: policy.decide(*a, **{"tokens": 5000, **k})["target"]
        self.assertIsNone(d("haiku", 0.69, "claude-sonnet-5-5", enforce=False))  # down needs >=70%
        self.assertEqual(d("opus", 0.53, "claude-haiku-5-5"), "claude-opus-5-5")  # up needs only >=50%
        self.assertEqual(d("sonnet", 0.50, "claude-haiku-5-5"), "claude-sonnet-5-5")
        self.assertIsNone(d("opus", 0.49, "claude-haiku-5-5", enforce=False))
        self.assertIsNone(d("haiku", 0.60, "claude-opus-5-5", enforce=False))
        self.assertIsNone(d("sonnet", 0.9, "claude-sonnet-5-5"))                  # same tier
        self.assertIsNone(d("haiku", 0.9, None))                                  # unknown model
        self.assertEqual(d("haiku", 0.70, "claude-sonnet-5-5"), "claude-haiku-5-5")

    def test_sonnet_default_only_in_enforce(self):
        import policy
        low = ("haiku", 0.40)
        self.assertEqual(policy.decide(*low, "claude-opus-5-5", tokens=5000, enforce=True)["target"], "claude-sonnet-5-5")
        self.assertEqual(policy.decide("opus", 0.40, "claude-haiku-5-5", tokens=5000, enforce=True)["target"], "claude-sonnet-5-5")
        self.assertIsNone(policy.decide(*low, "claude-sonnet-5-5", tokens=5000, enforce=True)["target"])
        self.assertIsNone(policy.decide(*low, "claude-opus-5-5", tokens=5000, enforce=False)["target"])  # shadow: no default
        os.environ["AUTOSWITCH_UNCLEAR"] = "stay"
        try:
            self.assertIsNone(policy.decide(*low, "claude-opus-5-5", tokens=5000, enforce=True)["target"])
        finally:
            del os.environ["AUTOSWITCH_UNCLEAR"]

    def test_lean_opus_goes_to_sonnet(self):
        import policy
        # 40-49% Opus on a Haiku chat: one tier up, also for a brand-new chat (size unknown, is_new)
        for tokens in (5000, None):
            r = policy.decide("opus", 0.48, "claude-haiku-5-5", tokens=tokens, enforce=True, is_new=tokens is None)
            self.assertEqual(r["target"], "claude-sonnet-5-5"); self.assertIn("leaning", r["reason"])
        self.assertIsNone(policy.decide("opus", 0.48, "claude-haiku-5-5", tokens=90000, enforce=True)["target"])  # long chat: stay
        self.assertEqual(policy.decide("opus", 0.50, "claude-haiku-5-5", tokens=None)["target"], "claude-opus-5-5")
        self.assertIsNone(policy.decide("opus", 0.30, "claude-haiku-5-5", tokens=None, enforce=True)["target"])
        self.assertIsNone(policy.decide("opus", 0.48, "claude-sonnet-5-5", tokens=5000, enforce=True)["target"])

    def test_dwell_guard(self):
        import policy
        self.assertEqual(policy.decide("haiku", 0.95, "claude-opus-5-5", tokens=5000)["target"], "claude-haiku-5-5")
        deep = policy.decide("haiku", 0.95, "claude-opus-5-5", tokens=100000)  # over the 60k cap
        self.assertIsNone(deep["target"]); self.assertIn("dwell", deep["reason"])
        os.environ["AUTOSWITCH_MAX_DOWN_TOKENS"] = "1000000"
        os.environ["AUTOSWITCH_SAVE_SONNET_HAIKU"] = "0.001"
        os.environ["AUTOSWITCH_READ_FRACTION"] = "0"
        try:  # saving below re-cache cost
            r = policy.decide("haiku", 0.95, "claude-sonnet-5-5", tokens=400000)
            self.assertIsNone(r["target"]); self.assertIn("re-cache", r["reason"])
        finally:
            del os.environ["AUTOSWITCH_MAX_DOWN_TOKENS"], os.environ["AUTOSWITCH_SAVE_SONNET_HAIKU"], os.environ["AUTOSWITCH_READ_FRACTION"]
        # up-switches are never held back by the dwell guard
        self.assertEqual(policy.decide("opus", 0.9, "claude-haiku-5-5", tokens=900000)["target"], "claude-opus-5-5")

    def test_pin_wins(self):
        import policy, pins, importlib
        importlib.reload(pins)
        self.assertIsNone(policy.decide("haiku", 0.95, "claude-opus-5-5", pinned=True)["target"])
        pins.record_switch({"session_id": "c1", "to_model": "claude-opus-5-5", "source": "picker"})
        self.assertTrue(pins.is_pinned("c1", "local_1", "claude-opus-5-5"))
        pins.record_switch({"session_id": "c2", "to_model": "claude-haiku-5-5", "source": "sdk"})
        self.assertFalse(pins.is_pinned("c2", "local_2", "claude-haiku-5-5"))      # tool-made change: not a pin
        # fallback: Watcher set haiku, now the chat is on opus -> someone changed it by hand
        pins.record_watcher_set("local_3", "claude-haiku-5-5")
        self.assertFalse(pins.is_pinned("c3", "local_3", "claude-haiku-5-5"))
        self.assertTrue(pins.is_pinned("c3", "local_3", "claude-opus-5-5"))
        self.assertFalse(pins.is_pinned("c4", "local_4", "claude-opus-5-5"))       # no record: cannot tell

    def test_mode(self):
        self.assertEqual(self.q.mode(), "off")
        _wt(self.q.path("enabled"), "")
        self.assertEqual(self.q.mode(), "enforce")                                  # legacy flag
        os.environ["AUTOSWITCH_MODE"] = "shadow"
        try:
            self.assertEqual(self.q.mode(), "shadow"); self.assertFalse(self.q.enabled())
            os.environ["AUTOSWITCH_MODE"] = "bogus"
            self.assertEqual(self.q.mode(), "off")
        finally:
            del os.environ["AUTOSWITCH_MODE"]

    def test_shadow_mode_logs_and_never_blocks(self):
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(s):
                s.rfile.read(int(s.headers["Content-Length"])); s.send_response(200); s.end_headers()
                s.wfile.write(json.dumps({"label": "haiku", "probs": {"haiku": 0.95}}).encode())
            def log_message(s, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.makedirs(os.path.join(self.d, "s", "a", "b"))
        _wj(os.path.join(self.d, "s", "a", "b", "local_t.json"), {"sessionId": ID, "cliSessionId": "c1", "model": "claude-opus-5-5"})
        tr = os.path.join(self.d, "t.jsonl"); _wt(tr, "x" * 4000)
        env = {**os.environ, "AUTOSWITCH_MODE": "shadow", "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s"),
               "AUTOSWITCH_SCORER": f"http://127.0.0.1:{srv.server_port}"}
        out = subprocess.run([sys.executable, os.path.join(SB, "hint.py")], capture_output=True, text=True, env=env,
                             input=json.dumps({"prompt": "secret question text", "session_id": "c1", "transcript_path": tr}))
        srv.shutdown(); srv.server_close()
        self.assertFalse(stops(out)); self.assertIn("Router suggests", out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.d, "pending")))           # nothing queued
        rec = json.loads(_rt(os.path.join(self.d, "shadow.jsonl")))
        self.assertEqual((rec["current"], rec["would_target"], rec["prompt_chars"], rec["history_tokens_est"]),
                         ("claude-opus-5-5", "claude-haiku-5-5", 20, 1000))
        self.assertNotIn("secret", json.dumps(rec))                                 # prompt text never logged

    def test_loop_guard_is_one_shot(self):
        rid = self.q.enqueue("local_x", "claude-haiku-5-5", "hi", "haiku", 0.9)
        self.assertFalse(self.q.consume_pass("hi"))
        self.assertTrue(self.q.consume_pass(f"[autoswitch:{rid}] hi"))
        self.assertFalse(self.q.consume_pass(f"[autoswitch:{rid}] hi"))  # spent
        self.assertFalse(self.q.consume_pass("[autoswitch:../../etc] hi"))

    def test_loop_guard_through_send_message_wrapper(self):
        rid = self.q.enqueue("local_x", "claude-haiku-5-5", "what is 2+2", "haiku", 0.9)
        wrapped = f'<cross-session-message from="local_w" from-name="Watcher">\n[autoswitch:{rid}] what is 2+2\n</cross-session-message>'
        self.assertTrue(self.q.consume_pass(wrapped))
        self.assertFalse(os.path.exists(self.q.path("pass", rid)))  # token spent
        self.assertFalse(self.q.consume_pass(wrapped))

    def test_bare_strips_wrapper_and_marker(self):
        w = '<cross-session-message from="a">\n[autoswitch:ab12] what is 2+2\n</cross-session-message>'
        self.assertEqual(self.q.bare(w), "what is 2+2")
        self.assertEqual(self.q.bare("[autoswitch:zz] hi"), "hi")
        self.assertEqual(self.q.bare("plain"), "plain")

    def test_hook_scores_and_queues_bare_text(self):
        # wrapped prompt on a faked Opus chat: hook must send only inner text to the scorer and queue it
        import http.server, threading
        seen = []
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(s):
                body = json.loads(s.rfile.read(int(s.headers["Content-Length"])))
                seen.append(body["text"])
                s.send_response(200); s.end_headers()
                s.wfile.write(json.dumps({"label": "haiku", "probs": {"haiku": 0.95}}).encode())
            def log_message(s, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.makedirs(os.path.join(self.d, "s", "a", "b"))
        _wj(os.path.join(self.d, "s", "a", "b", "local_t.json"), {"sessionId": ID, "cliSessionId": "c1", "model": "claude-opus-5-5"})
        _wt(os.path.join(self.d, "enabled"), "")
        w = '<cross-session-message from="a">\nwhat is 2+2\n</cross-session-message>'
        env = {**os.environ, "AUTOSWITCH_SCORER": f"http://127.0.0.1:{srv.server_port}", "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s"),
               "AUTOSWITCH_ROUTE_AGENT_MESSAGES": "1"}  # trial mode: route a send_message-wrapped prompt
        tr = os.path.join(self.d, "small.jsonl"); _wt(tr, "x" * 400)  # a small, known history
        out = subprocess.run([sys.executable, os.path.join(SB, "hint.py")], input=json.dumps({"prompt": w, "session_id": "c1", "transcript_path": tr}),
                             capture_output=True, text=True, env=env)
        srv.shutdown(); srv.server_close()
        self.assertEqual(seen, ["what is 2+2"])
        self.assertTrue(stops(out))
        req = _rj(os.path.join(self.d, "pending", os.listdir(os.path.join(self.d, "pending"))[0]))
        self.assertEqual(req["prompt"], "what is 2+2")

    def test_bare_strips_system_reminders(self):
        r = "<system-reminder>\nLinked sessions now ...\n</system-reminder>\n"
        self.assertEqual(self.q.bare(r + "what is 2+2"), "what is 2+2")
        self.assertEqual(self.q.bare(r), "")
        self.assertEqual(self.q.bare(r + r), "")
        self.assertEqual(self.q.bare("<system-reminder>unclosed"), "")
        w = '<cross-session-message from="a">\n' + r + '[autoswitch:ab] hi\n</cross-session-message>'
        self.assertEqual(self.q.bare(w), "hi")

    def test_reminder_only_prompt_is_not_blocked_or_queued(self):
        _wt(os.path.join(self.d, "enabled"), "")
        p = "<system-reminder>\nLinked sessions now ...\n</system-reminder>"
        out = subprocess.run([sys.executable, os.path.join(SB, "hint.py")], input=json.dumps({"prompt": p, "session_id": "c1"}),
                             capture_output=True, text=True, env={**os.environ, "AUTOSWITCH_SCORER": "http://127.0.0.1:9"})
        self.assertEqual(out.stdout.strip(), "")
        self.assertFalse(os.path.exists(os.path.join(self.d, "pending")))

    def test_resolve(self):
        import resolve
        p = os.path.join(self.d, "a", "b"); os.makedirs(p)
        _wj(os.path.join(p, "local_1.json"), {"sessionId": "local_1", "cliSessionId": "cli1", "model": "claude-sonnet-5-5"})
        resolve.SESSIONS = self.d
        self.assertEqual(resolve.resolve("cli1")["sessionId"], "local_1")
        self.assertIsNone(resolve.resolve("nope"))

    def test_hook_flag_off_never_blocks_or_queues(self):
        out = subprocess.run([sys.executable, os.path.join(SB, "hint.py")], input=json.dumps({"prompt": "x", "session_id": "s"}),
                             capture_output=True, text=True, env={**os.environ})
        self.assertEqual(out.returncode, 0)
        self.assertFalse(stops(out))
        self.assertFalse(os.path.exists(os.path.join(self.d, "pending")))

    def test_off_is_silent_hint_is_message_only(self):
        sc = self._scorer("haiku", 0.95)
        out = self._hook("what is 2+2", scorer=sc, mode="off")
        self.assertEqual(out.stdout.strip(), "")
        out = self._hook("what is 2+2", scorer=sc, mode="hint")
        self.assertIn("Router suggests: Haiku", out.stdout)
        self.assertFalse(stops(out))
        self.assertFalse(os.path.exists(os.path.join(self.d, "pending")))

    def test_enforce_without_watcher_never_blocks(self):
        os.remove(os.path.join(self.d, "watcher.alive"))
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertFalse(stops(out))
        self.assertIn("no Watcher session is running", out.stdout)
        self.assertEqual(self._pending(), [])

    # --- brand-new chats (PR #16) and finish.py target ---
    def _scorer(self, label, prob):
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(s):
                s.rfile.read(int(s.headers["Content-Length"])); s.send_response(200); s.end_headers()
                s.wfile.write(json.dumps({"label": label, "probs": {label: prob}}).encode())
            def log_message(s, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}"

    def _hook(self, prompt, cli="newchat", transcript=None, scorer=None, mode="enforce"):
        if transcript is None:  # the app always passes one; a small known history keeps down-switches possible
            transcript = os.path.join(self.d, "default.jsonl")
            _wt(transcript, "x" * 400)
        env = {**os.environ, "AUTOSWITCH_MODE": mode, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s"),
               "AUTOSWITCH_SCORER": scorer}
        os.makedirs(os.path.join(self.d, "s"), exist_ok=True)
        return subprocess.run([sys.executable, os.path.join(SB, "hint.py")], capture_output=True, text=True, env=env,
                              input=json.dumps({"prompt": prompt, "session_id": cli, "transcript_path": transcript}))

    def _pending(self):
        p = os.path.join(self.d, "pending")
        return [_rj(os.path.join(p, f)) for f in sorted(os.listdir(p))] if os.path.isdir(p) else []

    def test_new_chat_queues_by_cli_id(self):
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertTrue(stops(out))
        (req,) = self._pending()
        self.assertEqual(req["session_id"], ID); self.assertEqual(req["cli_session_id"], "newchat")
        self.assertEqual(req["model"], "claude-haiku-5-5")

    def test_new_chat_below_gate_passes(self):
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.60))
        self.assertFalse(stops(out)); self.assertEqual(self._pending(), [])

    def test_new_chat_followup_has_no_context_and_passes(self):
        out = self._hook("ok do it", scorer=self._scorer("haiku", 0.99))
        self.assertFalse(stops(out)); self.assertEqual(self._pending(), [])

    def test_two_new_chats_queue_separately(self):
        sc = self._scorer("haiku", 0.95)
        self._hook("what is 2+2", cli="chatA", scorer=sc)
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID3
        self._hook("capital of France?", cli="chatB", scorer=sc)
        reqs = self._pending()
        self.assertEqual(sorted(r["cli_session_id"] for r in reqs), ["chatA", "chatB"])
        self.assertEqual(len({r["id"] for r in reqs}), 2)

    def test_existing_chat_with_unreadable_record_is_not_treated_as_new(self):
        tr = os.path.join(self.d, "t.jsonl"); _wt(tr, '{"type":"user"}\n{"type":"assistant"}\n')
        out = self._hook("what is 2+2", transcript=tr, scorer=self._scorer("haiku", 0.99))
        self.assertFalse(stops(out)); self.assertEqual(self._pending(), [])

    def _finish(self, *args):
        return subprocess.run([sys.executable, os.path.join(SB, "finish.py"), *args], capture_output=True, text=True,
                              env={**os.environ, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s")})

    def _req(self, **kw):
        os.makedirs(os.path.join(self.d, "working"), exist_ok=True)
        r = {"id": "abc123", "session_id": None, "cli_session_id": "newchat", "model": "claude-haiku-5-5",
             "prompt": "what is 2+2", "ts": time.time(), **kw}
        p = os.path.join(self.d, "working", "abc123.json"); _wj(p, r)
        return p

    def _record(self):
        p = os.path.join(self.d, "s", "a", "b"); os.makedirs(p, exist_ok=True)
        _wj(os.path.join(p, "local_new.json"), {"sessionId": ID, "cliSessionId": "newchat", "model": "claude-sonnet-5-5"})

    def test_finish_target_resolved(self):
        self._record(); p = self._req()
        self.assertEqual(self._finish("target", p).stdout.strip(), ID)
        self.assertEqual(_rj(p)["session_id"], ID)  # remembered for `done`

    def test_finish_target_waits_for_late_record(self):
        p = self._req()
        threading.Timer(1.0, self._record).start()
        t0 = time.time(); out = self._finish("target", p).stdout.strip()
        self.assertEqual(out, ID); self.assertGreaterEqual(time.time() - t0, 0.9)

    def test_finish_target_unresolved_and_expired_budget(self):
        p = self._req(ts=time.time() - 59)  # budget nearly spent by waiting behind another request
        t0 = time.time(); out = self._finish("target", p).stdout.strip()
        self.assertEqual(out, "UNRESOLVED"); self.assertLess(time.time() - t0, 10)

    def test_done_refused_after_done_ok_still_leaves_lost_note(self):
        # Review note: WATCHER.md sends and runs "done ok" in one message; if the send then fails,
        # "done refused" must find the file in done/, rewrite its status and leave the lost note
        os.makedirs(os.path.join(self.d, "working"), exist_ok=True)
        req = os.path.join(self.d, "working", "r1.json")
        _wj(req, {"id": "r1", "session_id": ID, "cli_session_id": "chat-7", "model": "m", "prompt": "redo it"})
        fin = os.path.join(SB, "finish.py")
        self.assertEqual(subprocess.run([sys.executable, fin, "done", req, "ok"], capture_output=True).returncode, 0)
        r = subprocess.run([sys.executable, fin, "done", req, "refused"], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_rj(os.path.join(self.d, "done", "r1.json"))["status"], "refused")
        # "error" after "ok" (send failed, no worker started) leaves the lost note
        r = subprocess.run([sys.executable, fin, "done", req, "error"], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.q.take_lost("chat-7"), "redo it")
        self.assertFalse(os.path.exists(req))

    def test_settle_closes_ok_once_the_hook_spent_the_pass_token(self):
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "hi", "haiku", 0.9, cli_session_id="chat-9")
        req = os.path.join(self.d, "working", rid + ".json")
        os.makedirs(os.path.dirname(req), exist_ok=True)
        os.replace(os.path.join(self.d, "pending", rid + ".json"), req)
        p = subprocess.Popen([sys.executable, os.path.join(SB, "finish.py"), "settle", req], env={**os.environ, "AUTOSWITCH_SETTLE_WAIT": "10"})
        time.sleep(0.5); self.assertIsNone(p.poll())  # token still there: the re-send has not arrived
        self.assertTrue(self.q.consume_pass(self.q.MARK + rid + "] hi"))
        self.assertEqual(p.wait(timeout=5), 0)
        self.assertEqual(json.loads(_rt(os.path.join(self.d, "done", rid + ".json")))["status"], "ok")

    def test_settle_times_out_to_error_with_a_lost_note(self):
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "redo it", "haiku", 0.9, cli_session_id="chat-8")
        req = os.path.join(self.d, "working", rid + ".json")
        os.makedirs(os.path.dirname(req), exist_ok=True)
        os.replace(os.path.join(self.d, "pending", rid + ".json"), req)
        r = subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "settle", req], env={**os.environ, "AUTOSWITCH_SETTLE_WAIT": "0.5"}, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(_rt(os.path.join(self.d, "done", rid + ".json")))["status"], "error")
        self.assertEqual(self.q.take_lost("chat-8"), "redo it")

    def test_settle_leaves_a_request_the_watcher_already_closed(self):
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "x", "haiku", 0.9, cli_session_id="chat-7")
        req = os.path.join(self.d, "working", rid + ".json")
        os.makedirs(os.path.dirname(req), exist_ok=True)
        os.replace(os.path.join(self.d, "pending", rid + ".json"), req)
        self.assertEqual(subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "done", req, "refused"]).returncode, 0)
        r = subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "settle", req], env={**os.environ, "AUTOSWITCH_SETTLE_WAIT": "0.5"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(_rt(os.path.join(self.d, "done", rid + ".json")))["status"], "refused")

    # --- races between settle, the Watcher's `done`, the hook and the sweep ---

    def _working(self, rid):
        req = os.path.join(self.d, "working", rid + ".json")
        os.makedirs(os.path.dirname(req), exist_ok=True)
        os.replace(os.path.join(self.d, "pending", rid + ".json"), req)
        return req

    def _done_status(self, rid):
        with open(os.path.join(self.d, "done", rid + ".json")) as f:
            return json.load(f)["status"]


    # --- follow-ups to #8/#9 (review items L1-L4, P1, P2(b), P4) ---

    def test_lost_note_belongs_to_one_request(self):
        import importlib, finish
        finish = importlib.reload(finish)
        a = self.q.enqueue(ID, "claude-haiku-5-5", "prompt A", "haiku", 0.9, cli_session_id="chat-l")
        ra = self._working(a)
        self.assertTrue(finish.mark_done(ra, finish.load(ra), "error", keep_token=True))   # A: settle timed out
        b = self.q.enqueue(ID, "claude-haiku-5-5", "prompt B", "haiku", 0.9, cli_session_id="chat-l")
        rb = self._working(b)
        self.assertTrue(finish.mark_done(rb, finish.load(rb), "error"))                    # B failed: its note
        self.assertEqual(self.q.consume_pass(self.q.MARK + a + "] prompt A"), a)          # A's late re-send
        self.assertIsNone(self.q.take_lost("chat-l", a))                                  # must not eat B's note
        self.assertEqual(self.q.take_lost("chat-l", b), "prompt B")
        # the hook path: A's re-send through hint.py keeps B's note
        self.assertTrue(finish.mark_done(self._working(self.q.enqueue(ID, "m", "prompt C", "haiku", 0.9, cli_session_id="newchat")),
                                         {"id": "c", "prompt": "prompt C", "cli_session_id": "newchat", "session_id": ID}, "error"))
        tok = self.q.enqueue(ID, "m", "prompt D", "haiku", 0.9, cli_session_id="other")
        self._hook(self.q.MARK + tok + "] prompt D", cli="newchat", scorer=self._scorer("haiku", 0.9))
        self.assertEqual(self.q.take_lost("newchat"), "prompt C")

    def test_failed_publish_is_not_put_back_where_a_settle_could_reclose_it(self):
        import importlib, finish
        finish = importlib.reload(finish)
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "x", "haiku", 0.9, cli_session_id="chat-s")
        req = self._working(rid)
        orig, calls = finish._claim, []
        def flaky(src, dst, tries=40):
            calls.append(dst)
            return False if len(calls) == 2 else orig(src, dst, tries)  # the publish (2nd rename) gives up
        finish._claim = flaky
        try:
            self.assertFalse(finish.mark_done(req, finish.load(req), "refused"))
        finally:
            finish._claim = orig
        self.assertFalse(os.path.exists(req))  # a polling settle sees it gone and stops
        left = [f for f in os.listdir(os.path.join(self.d, "done")) if f.endswith(".closing")]
        self.assertEqual(len(left), 1)  # kept for the sweep (see test_sweep_closes_a_stranded_closing_file...)

    def test_sweep_closes_a_stranded_closing_file_and_keeps_the_heartbeat(self):
        os.makedirs(os.path.join(self.d, "done"), exist_ok=True)
        c = os.path.join(self.d, "done", "abc123.json.4242.closing")
        _write_json(c, {"id": "abc123", "session_id": None, "cli_session_id": "newchat", "model": "claude-haiku-5-5",
                        "prompt": "stranded", "ts": time.time() - 700})
        old = time.time() - 11 * 60; os.utime(c, (old, old))
        hb = os.path.join(self.d, "watcher.alive"); os.utime(hb, (old, old))
        self._watch(AUTOSWITCH_SWEEP_EVERY="3")
        self.assertEqual(self._done("abc123")["status"], "error")
        self.assertFalse(os.path.exists(c))
        self.assertIn("stranded", _read_json(os.path.join(self.d, "lost", "newchat.json"))["prompt"])
        self.assertGreater(os.path.getmtime(hb), time.time() - 30)

    def test_sweep_finishes_a_stranded_ok_close_as_ok_without_a_resend_note(self):
        os.makedirs(os.path.join(self.d, "done"), exist_ok=True)
        c = os.path.join(self.d, "done", "okreq.json.4242.closing")
        _write_json(c, {"id": "okreq", "session_id": ID, "cli_session_id": "newchat", "model": "claude-haiku-5-5",
                        "prompt": "stranded ok", "status": "ok", "ts": time.time() - 700})
        old = time.time() - 11 * 60; os.utime(c, (old, old))
        self._watch(AUTOSWITCH_SWEEP_EVERY="3")
        self.assertEqual(self._done("okreq")["status"], "ok")
        self.assertFalse(os.path.exists(c))
        self.assertFalse(os.path.exists(os.path.join(self.d, "lost", "newchat.json")))

    def test_sweep_keeps_a_stranded_refused_close_refused(self):
        os.makedirs(os.path.join(self.d, "done"), exist_ok=True)
        c = os.path.join(self.d, "done", "refreq.json.4242.closing")
        _write_json(c, {"id": "refreq", "session_id": ID, "cli_session_id": "newchat", "model": "claude-haiku-5-5",
                        "prompt": "stranded refused", "status": "refused", "ts": time.time() - 700})
        old = time.time() - 11 * 60; os.utime(c, (old, old))
        self._watch(AUTOSWITCH_SWEEP_EVERY="3")
        self.assertEqual(self._done("refreq")["status"], "refused")
        self.assertFalse(os.path.exists(c))

    def test_mark_done_loses_while_another_closer_holds_the_claim(self):
        import importlib, finish
        finish = importlib.reload(finish)
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "x", "haiku", 0.9, cli_session_id="chat-k", resend=False)
        req = self._working(rid)
        lock = os.path.join(self.d, "done", rid + ".json.lock")
        open(lock, "w").close()  # another closer is mid-close (on Windows its rename does not stop a second one)
        self.assertFalse(finish.mark_done(req, finish.load(req), "refused"))
        self.assertTrue(os.path.exists(req))  # untouched
        os.remove(lock)
        self.assertTrue(finish.mark_done(req, finish.load(req), "refused"))
        self.assertFalse(os.path.exists(lock))  # released
        self.assertEqual(self._done_status(rid), "refused")

    def test_save_is_atomic(self):
        import importlib, finish
        finish = importlib.reload(finish)
        target = os.path.join(self.d, "r.json")
        finish.save(target, {"status": "ok", "pad": "x" * 100000})
        replaced = []
        orig = finish.os.replace
        def spy(src, dst):
            self.assertEqual(finish.load(target)["status"], "ok")  # the old copy is whole until the swap
            with open(src, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["status"], "refused")  # the new copy is complete before it
            replaced.append(dst)
            return orig(src, dst)
        finish.os.replace = spy
        try:
            finish.save(target, {"status": "refused", "pad": "y" * 100000})
        finally:
            finish.os.replace = orig
        self.assertEqual(replaced, [target])
        self.assertEqual(finish.load(target)["status"], "refused")
        self.assertEqual(os.listdir(self.d).count("r.json"), 1)
        self.assertEqual([f for f in os.listdir(self.d) if f.endswith(".tmp")], [])

    def test_done_refused_racing_settle_under_load_stays_refused(self):
        procs = []
        for i in range(8):
            rid = self.q.enqueue(ID, "claude-haiku-5-5", "x%d" % i, "haiku", 0.9, cli_session_id="chat-%d" % i, resend=False)
            req = self._working(rid)
            env = {**os.environ, "AUTOSWITCH_SETTLE_GRACE": "0"}
            procs.append((rid, subprocess.Popen([sys.executable, os.path.join(SB, "finish.py"), "settle", req], env=env),
                          subprocess.Popen([sys.executable, os.path.join(SB, "finish.py"), "done", req, "refused"],
                                           env=env, stderr=subprocess.PIPE, text=True)))
        for rid, s, d in procs:
            self.assertEqual(s.wait(timeout=20), 0)
            err = d.communicate(timeout=20)[1]
            self.assertEqual(d.returncode, 0, err)
            self.assertEqual(self._done_status(rid), "refused")

    def test_late_refusal_does_not_clear_a_newer_watcher_set(self):
        import importlib, pins
        pins = importlib.reload(pins)
        pins.record_watcher_set(ID, "claude-sonnet-5-5", "old1")
        pins.record_watcher_set(ID, "claude-opus-5-5", "new2")              # a newer switch in the same chat
        pins.clear_watcher_set(ID, "claude-sonnet-5-5", "old1", time.time() - 5)
        self.assertEqual(pins._read("lastset", ID)["model"], "claude-opus-5-5")
        pins.clear_watcher_set(ID, "claude-opus-5-5", "new2")
        self.assertIsNone(pins._read("lastset", ID))
        pins._write("lastset", ID, {"model": "claude-opus-5-5", "ts": time.time()})  # an old record without an id
        pins.clear_watcher_set(ID, "claude-sonnet-5-5", "x", time.time() - 5)
        self.assertIsNotNone(pins._read("lastset", ID))
        pins.clear_watcher_set(ID, "claude-opus-5-5", "x", time.time() - 5)
        self.assertIsNone(pins._read("lastset", ID))

    def test_lean_band_needs_a_known_size_or_a_new_chat(self):
        import policy
        d = lambda **k: policy.decide("opus", 0.45, "claude-haiku-5-5", **k)["target"]
        self.assertEqual(d(tokens=5000), "claude-sonnet-5-5")
        self.assertIsNone(d(tokens=50000))
        self.assertIsNone(d(tokens=None))                       # unreadable transcript of an existing chat: stay
        self.assertIsNone(d(tokens=None, is_new=False))
        self.assertEqual(d(tokens=None, is_new=True), "claude-sonnet-5-5")

    def test_lean_band_is_off_when_the_sonnet_default_is_off(self):
        import policy
        os.environ["AUTOSWITCH_UNCLEAR"] = "stay"
        try:
            self.assertIsNone(policy.decide("opus", 0.45, "claude-haiku-5-5", tokens=5000)["target"])
            self.assertEqual(policy.decide("opus", 0.55, "claude-haiku-5-5", tokens=5000)["target"], "claude-opus-5-5")
        finally:
            del os.environ["AUTOSWITCH_UNCLEAR"]

    def test_transcript_is_new(self):
        import hint
        tr = os.path.join(self.d, "t.jsonl")
        self.assertTrue(hint.transcript_is_new(tr))             # not written yet: a brand-new chat
        with open(tr, "w") as f: f.write('{"type":"user"}\n')
        self.assertTrue(hint.transcript_is_new(tr))
        with open(tr, "a") as f: f.write('{"type":"assistant"}\n')
        self.assertFalse(hint.transcript_is_new(tr))
        self.assertFalse(hint.transcript_is_new(self.d))        # unreadable (a directory): not new, just unknown
        self.assertFalse(hint.transcript_is_new(None))

    def test_hook_unreadable_transcript_of_existing_chat_is_not_leaned(self):
        self._record()                                         # an existing chat, now on Haiku
        p = os.path.join(self.d, "s", "a", "b", "local_new.json")
        r = _read_json(p); r["model"] = "claude-haiku-5-5"; _write_json(p, r)
        out = self._hook("refactor this module", transcript=self.d, scorer=self._scorer("opus", 0.45), mode="shadow")
        rec = _read_jsonl(os.path.join(self.d, "shadow.jsonl"))[-1]
        self.assertIsNone(rec["would_target"])

    def test_malformed_float_env_warns_and_keeps_routing(self):
        import policy, io, contextlib
        for bad in ("40%", "0,4", "nan", "inf"):
            os.environ["AUTOSWITCH_LEAN_THRESHOLD"] = bad
            try:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    c = policy.cfg()
                    t = policy.decide("opus", 0.45, "claude-haiku-5-5", tokens=5000)["target"]
            finally:
                del os.environ["AUTOSWITCH_LEAN_THRESHOLD"]
            self.assertEqual(c["lean"], 0.40); self.assertEqual(t, "claude-sonnet-5-5")
            self.assertIn("AUTOSWITCH_LEAN_THRESHOLD", err.getvalue())
        os.environ["AUTOSWITCH_UP_THRESHOLD"] = "half"
        try:
            out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        finally:
            del os.environ["AUTOSWITCH_UP_THRESHOLD"]
        self.assertTrue(stops(out))                            # routing still works; it did not fail open

    def test_late_resend_after_settle_timeout_still_passes_once_and_clears_the_lost_note(self):
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "redo it", "haiku", 0.9, cli_session_id="chat-late")
        req = self._working(rid)
        r = subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "settle", req], env={**os.environ, "AUTOSWITCH_SETTLE_WAIT": "0.3"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._done_status(rid), "error")
        self.assertTrue(os.path.exists(os.path.join(self.d, "pass", rid)))  # kept for a late re-send
        out = self._hook(self.q.MARK + rid + "] redo it", cli="chat-late", scorer=self._scorer("haiku", 0.95))
        self.assertTrue(passes_clean(out), out.stdout)  # the late re-send is let through, not routed again
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", rid)))  # and only once
        self.assertIsNone(self.q.take_lost("chat-late"))  # it did arrive: no stale "please resend"

    def _race_state(self, rid, d, s_rc, s_out):
        """Diagnostics for a failed race: both closers' output and every file left for this request."""
        lines = ["done rc=%s stdout=%r stderr=%r" % (d.returncode, d.stdout, d.stderr), "settle rc=%s out=%r" % (s_rc, s_out)]
        for sub in ("working", "done", "pass", "lost", "lastset"):
            folder = os.path.join(self.d, sub)
            for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                if rid in name or sub in ("lost", "lastset"):
                    try:
                        with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                            body = f.read()[:500]
                    except OSError as e:
                        body = repr(e)
                    lines.append("%s/%s: %s" % (sub, name, body))
        return "\n".join(lines)

    def test_refusal_racing_settle_keeps_refused_and_never_recreates_working(self):
        for _ in range(5):
            rid = self.q.enqueue(ID, "claude-haiku-5-5", "x", "haiku", 0.9, cli_session_id="chat-r", resend=False)
            req = self._working(rid)
            env = {**os.environ, "AUTOSWITCH_SETTLE_GRACE": "0"}
            s = subprocess.Popen([sys.executable, os.path.join(SB, "finish.py"), "settle", req], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            d = self._finish("done", req, "refused")
            s_out, s_err = s.communicate(timeout=10)
            why = self._race_state(rid, d, s.returncode, s_out + s_err)
            self.assertEqual(s.returncode, 0, why)
            self.assertEqual(d.returncode, 0, why)
            self.assertEqual(self._done_status(rid), "refused", why)  # the refusal wins whichever closer ran first
            self.assertFalse(os.path.exists(req))  # nobody wrote the request back into working/
            self.assertEqual([f for f in os.listdir(os.path.join(self.d, "done")) if "closing" in f], [])
            import importlib, pins
            self.assertFalse(os.path.exists(importlib.reload(pins)._p("lastset", ID)))  # no pin for a refused switch

    def test_pass_token_exists_before_the_request_is_visible(self):
        seen = []
        def spy(src, dst, _real=self.q.os.replace if "AUTOSWITCH" == "AUTOSWITCH" else self.q.os.rename):
            seen.append(os.path.exists(os.path.join(self.d, "pass", os.path.basename(dst)[:-5])))
            return _real(src, dst)
        name = "replace" if "AUTOSWITCH" == "AUTOSWITCH" else "rename"
        orig = getattr(self.q.os, name)
        setattr(self.q.os, name, spy)
        try:
            self.q.enqueue(ID, "claude-haiku-5-5", "hi", "haiku", 0.9)
        finally:
            setattr(self.q.os, name, orig)
        self.assertEqual(seen, [True])

    def test_failed_publish_leaves_no_token(self):
        name = "replace" if "AUTOSWITCH" == "AUTOSWITCH" else "rename"
        orig = getattr(self.q.os, name)
        def boom(*a):
            raise OSError("disk full")
        setattr(self.q.os, name, boom)
        try:
            with self.assertRaises(OSError):
                self.q.enqueue(ID, "claude-haiku-5-5", "hi", "haiku", 0.9)
        finally:
            setattr(self.q.os, name, orig)
        self.assertEqual(os.listdir(os.path.join(self.d, "pass")), [])
        self.assertEqual(os.listdir(os.path.join(self.d, "pending")), [])

    def test_refusal_after_no_resend_grace_clears_the_watcher_pin(self):
        import importlib, pins
        pins = importlib.reload(pins)
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "x", "haiku", 0.9, cli_session_id="chat-g", resend=False)
        req = self._working(rid)
        r = subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "settle", req], env={**os.environ, "AUTOSWITCH_SETTLE_GRACE": "0.2"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._done_status(rid), "ok")
        self.assertTrue(os.path.exists(pins._p("lastset", ID)))
        d = self._finish("done", req, "refused")  # the Watcher reports late: the model was not set after all
        self.assertEqual(d.returncode, 0, d.stderr)
        self.assertEqual(self._done_status(rid), "refused")
        self.assertFalse(os.path.exists(pins._p("lastset", ID)))

    def test_two_requests_in_flight_close_independently(self):
        a = self.q.enqueue(ID, "claude-haiku-5-5", "a", "haiku", 0.9, cli_session_id="chat-a")
        b = self.q.enqueue(ID2, "claude-opus-5-5", "b", "opus", 0.9, cli_session_id="chat-b")
        ra, rb = self._working(a), self._working(b)
        env = {**os.environ, "AUTOSWITCH_SETTLE_WAIT": "10"}
        pa = subprocess.Popen([sys.executable, os.path.join(SB, "finish.py"), "settle", ra], env=env)
        pb = subprocess.Popen([sys.executable, os.path.join(SB, "finish.py"), "settle", rb], env=env)
        time.sleep(0.3)
        self.assertTrue(self.q.consume_pass(self.q.MARK + b + "] b"))
        self.assertEqual(pb.wait(timeout=5), 0)
        self.assertIsNone(pa.poll())  # b's re-send does not close a
        self.assertEqual(self._finish("done", ra, "refused").returncode, 0)
        self.assertEqual(pa.wait(timeout=5), 0)
        self.assertEqual((self._done_status(a), self._done_status(b)), ("refused", "ok"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", a)))  # a refusal still drops the token

    def test_claim_retries_a_windows_sharing_violation(self):
        import importlib, finish
        finish = importlib.reload(finish)
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "x", "haiku", 0.9, cli_session_id="chat-w")
        req = self._working(rid)
        calls = []
        orig = finish.os.replace
        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(32, "being used by another process")
            return orig(src, dst)
        finish.os.replace = flaky
        try:
            with open(req) as f:
                body = json.load(f)
            self.assertTrue(finish.mark_done(req, body, "refused"))
        finally:
            finish.os.replace = orig
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(self._done_status(rid), "refused")
        finish.os.replace = lambda s, d: (_ for _ in ()).throw(FileNotFoundError(s))  # another closer won
        try:
            self.assertFalse(finish.mark_done(req, {"id": rid, "status": "ok"}, "error"))
        finally:
            finish.os.replace = orig
        self.assertEqual(self._done_status(rid), "refused")


    def test_watcher_logs_a_settle_that_cannot_start(self):
        import importlib, io, types, watch
        watch = importlib.reload(watch)
        watch.B = self.d
        watch.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout='{"status": "ok", "path": "x"}')
        def nope(*a, **k):
            raise OSError(24, "Too many open files")
        orig, err = watch.subprocess.Popen, sys.stderr
        watch.subprocess.Popen, sys.stderr = nope, io.StringIO()
        try:
            with open(os.devnull, "w") as dn:
                out, sys.stdout = sys.stdout, dn
                try:
                    watch.handle(os.path.join(self.d, "working", "r1.json"))
                finally:
                    sys.stdout = out
            msg = sys.stderr.getvalue()
        finally:
            watch.subprocess.Popen, sys.stderr = orig, err
        self.assertIn("settle did not start for r1.json", msg)
        self.assertIn("Too many open files", msg)
        with open(os.path.join(self.d, "watch.log"), encoding="utf-8") as f:
            self.assertIn("r1.json", f.read())

    def test_sweep_drops_only_old_pass_tokens(self):
        import importlib, watch
        watch = importlib.reload(watch)
        watch.B = self.d
        os.makedirs(os.path.join(self.d, "pass"), exist_ok=True)
        for name, age in (("old", 3600), ("new", 5)):
            f = os.path.join(self.d, "pass", name)
            open(f, "w").close()
            os.utime(f, (time.time() - age, time.time() - age))
        watch.sweep()
        self.assertEqual(os.listdir(os.path.join(self.d, "pass")), ["new"])

    def test_non_object_json_request_goes_down_the_unparseable_path(self):
        # Review note: valid JSON that is not an object (e.g. []) used to crash on req.get
        os.makedirs(os.path.join(self.d, "working"), exist_ok=True)
        for name, body in (("arr.json", "[]"), ("num.json", "42"), ("str.json", '"cli_session_id"')):
            req = os.path.join(self.d, "working", name)
            _wt(req, body)
            r = subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "done", req, "error"], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(os.path.join(self.d, "done", name + ".bad")), name)
            self.assertFalse(os.path.exists(req))
        # salvage still finds the chat id inside a non-object JSON value
        req = os.path.join(self.d, "working", "wrap.json")
        _wj(req, [{"cli_session_id": "chat-5", "prompt": "hello"}])
        r = subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "done", req, "error"], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.q.take_lost("chat-5"), "hello")

    def test_unparseable_request_still_leaves_lost_note(self):
        # a truncated / non-JSON request file used to get no lost note (load failed -> .bad)
        os.makedirs(os.path.join(self.d, "working"), exist_ok=True)
        req = os.path.join(self.d, "working", "abc123.json")
        _wt(req, '{"id": "abc123", "session_id": null, "cli_session_id": "chat-9", "model": "m", "prompt": "fix \\"the\\" bug", "lab')
        r = subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "done", req, "error"], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.d, "done", "abc123.json.bad")))
        self.assertEqual(self.q.take_lost("chat-9"), 'fix "the" bug')
        # no readable chat id: nothing to attach a note to, but the file is still kept, not crashed on
        req2 = os.path.join(self.d, "working", "zzz.json")
        _wt(req2, "garbage")
        r = subprocess.run([sys.executable, os.path.join(SB, "finish.py"), "done", req2, "error"], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.d, "done", "zzz.json.bad")))

    def test_unresolved_prompt_is_surfaced_on_next_prompt_not_lost(self):
        p = self._req()
        self.assertEqual(self._finish("done", p, "unresolved").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", "abc123")))
        out = self._hook("hello again", scorer=self._scorer("sonnet", 0.30))
        self.assertIn("could not re-send", out.stdout); self.assertIn("what is 2+2", out.stdout)
        out = self._hook("hello again", scorer=self._scorer("sonnet", 0.30))
        self.assertNotIn("could not re-send", out.stdout)  # shown once

    def test_done_ok_without_chat_id_is_not_recorded_as_success(self):
        p = self._req()
        self.assertEqual(self._finish("done", p, "ok").returncode, 0)
        self.assertEqual(_rj(os.path.join(self.d, "done", "abc123.json"))["status"], "error")

    # --- T9: seamless switching ---
    def test_block_message_is_short_and_friendly(self):
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertEqual(json.loads(out.stdout), {"continue": False, "stopReason": "\u21aa Switched to Haiku"})

    def test_stop_text_is_exactly_the_clean_phrase_for_every_tier(self):
        import shutil
        for label, name in (("haiku", "Haiku"), ("sonnet", "Sonnet"), ("opus", "Opus")):
            shutil.rmtree(os.path.join(self.d, "pending"), ignore_errors=True)
            out = self._hook("some prompt", scorer=self._scorer(label, 0.99))
            self.assertEqual(json.loads(out.stdout)["stopReason"], "\u21aa Switched to " + name)

    def test_speed_setting(self):
        self.assertEqual(self.q.speed(), "seamless")
        _wt(self.q.path("speed"), "instant\n")
        self.assertEqual(self.q.speed(), "instant")
        os.environ["AUTOSWITCH_SPEED"] = "seamless"
        try:
            self.assertEqual(self.q.speed(), "seamless")  # env wins over the file
            os.environ["AUTOSWITCH_SPEED"] = "bogus"
            self.assertEqual(self.q.speed(), "seamless")
        finally:
            del os.environ["AUTOSWITCH_SPEED"]

    def test_speed_file_corrupt_or_unknown_is_seamless(self):
        with open(self.q.path("speed"), "wb") as f:
            f.write(b"\xff\xfe\x00inst\x80ant")  # not UTF-8
        self.assertEqual(self.q.speed(), "seamless")
        _wt(self.q.path("speed"), "turbo\n")
        self.assertEqual(self.q.speed(), "seamless")
        os.environ["AUTOSWITCH_SPEED"] = "  instant \n"  # env is stripped like the CLI does
        try:
            self.assertEqual(self.q.speed(), "instant")
        finally:
            del os.environ["AUTOSWITCH_SPEED"]

    def test_cli_speed_with_a_corrupt_file_is_seamless(self):
        with open(os.path.join(self.d, "speed"), "wb") as f:
            f.write(b"\xff\xfe\x80")
        env = dict(os.environ); env.pop("AUTOSWITCH_SPEED", None)
        out = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "bin", "autoswitch"), "speed"],
                             capture_output=True, text=True, env=env)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "seamless"))

    def test_instant_mode_without_a_watcher_enqueues_nothing(self):
        os.remove(os.path.join(self.d, "watcher.alive"))
        os.environ["AUTOSWITCH_SPEED"] = "instant"
        try:
            out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        finally:
            del os.environ["AUTOSWITCH_SPEED"]
        self.assertFalse(stops(out))
        self.assertEqual(self._pending(), [])

    def test_instant_mode_never_holds_the_prompt(self):
        os.environ["AUTOSWITCH_SPEED"] = "instant"
        try:
            out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        finally:
            del os.environ["AUTOSWITCH_SPEED"]
        self.assertFalse(stops(out))
        self.assertEqual(json.loads(out.stdout)["systemMessage"], "\u21aa Switching to Haiku")
        (req,) = self._pending()
        self.assertEqual((req["model"], req["resend"]), ("claude-haiku-5-5", False))
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", req["id"])))  # nothing will be re-sent

    # --- instant speed: never hold the prompt, switch for the next message ---
    def _instant(self, *a, **k):
        os.environ["AUTOSWITCH_SPEED"] = "instant"
        try:
            return self._hook(*a, **k)
        finally:
            del os.environ["AUTOSWITCH_SPEED"]

    def test_instant_from_the_speed_file(self):
        _wt(os.path.join(self.d, "speed"), "instant\n")  # what install writes on a fresh install
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertFalse(stops(out))
        (req,) = self._pending(); self.assertFalse(req["resend"])

    def test_instant_known_chat_with_target_queues_a_switch_only(self):
        self._record()                                              # chat known, on sonnet
        out = self._instant("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertTrue(passes_clean(out))
        self.assertEqual(json.loads(out.stdout)["systemMessage"], "\u21aa Switching to Haiku")
        (req,) = self._pending()
        self.assertEqual((req["session_id"], req["model"], req["resend"]), (ID, "claude-haiku-5-5", False))
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", req["id"])))

    def test_instant_no_target_passes_with_a_plain_hint(self):
        out = self._instant("what is 2+2", scorer=self._scorer("haiku", 0.60))  # new chat, below the gate
        self.assertTrue(passes_clean(out))
        self.assertIn("Router suggests: Haiku", json.loads(out.stdout)["systemMessage"])
        self.assertEqual(self._pending(), [])

    def test_instant_already_on_target_queues_nothing(self):
        self._record()                                              # chat already on sonnet
        out = self._instant("what is 2+2", scorer=self._scorer("sonnet", 0.95))
        self.assertTrue(passes_clean(out)); self.assertEqual(self._pending(), [])

    def test_instant_pinned_chat_is_never_switched(self):
        import pins, importlib; importlib.reload(pins)
        self._record()
        pins.pin(ID, "claude-sonnet-5-5", "hand change")
        out = self._instant("what is 2+2", scorer=self._scorer("opus", 0.99))
        self.assertTrue(passes_clean(out)); self.assertEqual(self._pending(), [])
        self.assertNotIn("Switching", out.stdout)

    def test_instant_new_chat_queues_by_cli_id_without_resend(self):
        out = self._instant("what is 2+2", cli="brandnew", scorer=self._scorer("haiku", 0.95))
        self.assertTrue(passes_clean(out))
        (req,) = self._pending()
        self.assertEqual((req["session_id"], req["cli_session_id"], req["resend"]), (ID, "brandnew", False))
        self.assertEqual(os.listdir(os.path.join(self.d, "pass")), [])  # no loop-guard token minted

    def test_instant_request_preps_to_switch_only(self):
        self._record()
        p = self._req(resend=False)
        out = json.loads(subprocess.run([sys.executable, os.path.join(SB, "prep.py"), p], capture_output=True, text=True,
                                        env={**os.environ, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s")}).stdout)
        self.assertEqual((out["status"], out["session_id"], out["send"]), ("ok", ID, False))
        self.assertNotIn("message", out)  # the Watcher only calls set_session_model

    def test_seamless_is_still_the_default(self):
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertTrue(stops(out)); (req,) = self._pending(); self.assertTrue(req["resend"])

    def test_known_chat_already_on_target_model_is_never_blocked(self):
        self._record()  # local_new on claude-sonnet-5-5, cli id "newchat"
        out = self._hook("write a function", scorer=self._scorer("sonnet", 0.95))
        self.assertFalse(stops(out)); self.assertEqual(self._pending(), [])

    def _prep(self, p):
        r = subprocess.run([sys.executable, os.path.join(SB, "prep.py"), p], capture_output=True, text=True,
                           env={**os.environ, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s")})
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_prep_resolves_and_builds_everything_in_one_call(self):
        self._record(); p = self._req()
        out = self._prep(p)
        self.assertEqual((out["session_id"], out["model"], out["set_model"], out["path"]),
                         (ID, "claude-haiku-5-5", True, p))
        self.assertEqual(out["message"], "[autoswitch:abc123] what is 2+2")

    def test_prep_skips_set_model_when_chat_already_on_target(self):
        self._record(); p = self._req(model="claude-sonnet-5-5")
        self.assertFalse(self._prep(p)["set_model"])

    def test_prep_waits_for_late_record(self):
        p = self._req(); threading.Timer(1.0, self._record).start()
        self.assertEqual(self._prep(p)["session_id"], ID)

    def test_prep_closes_stale_and_unresolved_silently(self):
        p = self._req(ts=time.time() - 700)
        self.assertEqual(self._prep(p), {"status": "stale"})
        self.assertEqual(_rj(os.path.join(self.d, "done", "abc123.json"))["status"], "stale")
        p = self._req(ts=time.time() - 59)
        self.assertEqual(self._prep(p), {"status": "unresolved"})
        self.assertEqual(_rj(os.path.join(self.d, "done", "abc123.json"))["status"], "unresolved")
        self.assertTrue(os.path.exists(os.path.join(self.d, "lost", "newchat.json")))  # surfaced to the user later

    @unittest.skipIf(sys.platform == "win32", "runs the POSIX watcher hook/watch.sh with bash; Windows uses hook/watch.py (WatchPyTest)")
    def test_watch_sh_emits_one_compact_line_fast(self):
        self._record()
        env = {**os.environ, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s")}
        w = subprocess.Popen(["bash", os.path.join(SB, "watch.sh")], stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(w.stdout and w.stdout.close or (lambda: None)); self.addCleanup(w.wait); self.addCleanup(w.kill)
        time.sleep(0.5)
        t0 = time.time()
        self.q.enqueue(None, "claude-haiku-5-5", "line one\nline two", "haiku", 0.9, cli_session_id="newchat")
        line = readline_within(w)
        self.assertLess(time.time() - t0, 1.5)
        self.assertTrue(line.startswith("REQUEST "))
        out = json.loads(line[len("REQUEST "):])
        self.assertEqual(out["session_id"], ID); self.assertIn("line one\nline two", out["message"])
        self.assertTrue(os.path.exists(out["path"]))

    # --- env-provided app id and CLI guard ---
    def test_new_chat_uses_host_session_id_from_environment(self):
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID2
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertTrue(stops(out))
        (req,) = self._pending()
        self.assertEqual(req["session_id"], ID2)      # known at block time, no lookup needed
        self.assertEqual(req["cli_session_id"], "newchat")

    def test_record_of_a_different_chat_is_never_used_or_blocked(self):
        self._record()  # record says ID for cli "newchat"
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID2
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertFalse(stops(out)); self.assertTrue(passes_clean(out)); self.assertEqual(self._pending(), [])

    def test_malformed_or_missing_env_id_passes_through(self):
        for sid in ("local_default", "local_" + "g" * 36, ID.upper(), ID + "x", "", None):
            if sid is None: os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
            else: os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = sid
            out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.99))
            self.assertNotIn("stopReason", out.stdout, sid); self.assertTrue(passes_clean(out), sid); self.assertEqual(self._pending(), [])
        self._record()  # a matching record does not make up for a missing env id
        os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.99))
        self.assertNotIn("stopReason", out.stdout); self.assertTrue(passes_clean(out))

    def test_long_or_odd_prompts_pass_through_with_the_hint(self):
        sc = self._scorer("haiku", 0.99)
        for p in ("x " * 3500, "q" * 6001, "has\x00nul", "lone \ud800 surrogate"):
            out = self._hook(p, scorer=sc)
            self.assertNotIn("stopReason", out.stdout, p[:20]); self.assertTrue(passes_clean(out), p[:20]); self.assertIn("Router suggests", out.stdout)
        self.assertEqual(self._pending(), [])
        self.assertTrue(stops(self._hook("y" * 6000, scorer=sc)))  # right at the limit still goes

    def test_terminal_and_headless_claude_are_never_blocked(self):
        for env in ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"},
                    {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_HOST_SESSION_ID": "not-an-app-id"}):
            os.environ.update(env)
            out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.99))
            self.assertNotIn("stopReason", out.stdout, env); self.assertTrue(passes_clean(out), env); self.assertIn("Router suggests", out.stdout)
            self.assertEqual(self._pending(), [])
        # even a CLI chat that has a (matching) record
        self._record(); os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.99))
        self.assertFalse(stops(out))

    def test_prep_does_not_wait_when_app_id_is_known(self):
        p = self._req(session_id=ID2)  # no record exists for this chat
        t0 = time.time(); out = self._prep(p)
        self.assertEqual(out["session_id"], ID2); self.assertTrue(out["set_model"])
        self.assertLess(time.time() - t0, 2)

    def _watch(self, **extra):
        env = {**os.environ, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s"), **extra}
        # watch.sh just execs watch.py; Windows has no bash, so it runs watch.py directly
        cmd = [sys.executable, os.path.join(SB, "watch.py")] if sys.platform == "win32" else ["bash", os.path.join(SB, "watch.sh")]
        w = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        self.addCleanup(w.stdout.close); self.addCleanup(w.wait); self.addCleanup(w.kill); time.sleep(0.5)
        return w

    def _done(self, rid):
        p = os.path.join(self.d, "done", rid + ".json")
        for _ in range(40):
            if os.path.exists(p): return _rj(p)
            time.sleep(0.1)

    def test_awkward_10k_prompt_goes_through_watch_sh_byte_identical(self):
        self._record()
        prompt = ("line one \"quoted\" 'single' back\\slash \t tab\n" + "unicode: caf\u00e9 \u65e5\u672c\u8a9e \U0001f600 \u2028 \u0085 \n\n") * 160
        self.assertGreater(len(prompt), 10000)
        w = self._watch()
        rid = self.q.enqueue(ID, "claude-haiku-5-5", prompt, "haiku", 0.9, cli_session_id="newchat")
        line = readline_within(w)
        self.assertTrue(line.startswith("REQUEST ")); self.assertTrue(line.isascii())
        out = json.loads(line[len("REQUEST "):])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["message"], f"[autoswitch:{rid}] " + prompt)  # byte-identical

    def test_prep_crash_is_never_silent(self):
        w = self._watch()
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "keep me", "haiku", 0.9, cli_session_id="newchat")
        p = os.path.join(self.d, "pending", rid + ".json")
        r = _rj(p); r["ts"] = "not a number"; _wj(p, r)  # makes prep.py raise
        self.assertEqual(self._done(rid)["status"], "error")
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", rid)))
        self.assertIn("keep me", _rj(os.path.join(self.d, "lost", "newchat.json"))["prompt"])

    def test_stale_and_unresolved_emit_no_event_line_and_are_closed(self):
        w = self._watch()
        rid = self.q.enqueue(ID, "claude-haiku-5-5", "old", "haiku", 0.9, cli_session_id="newchat")
        p = os.path.join(self.d, "pending", rid + ".json")
        r = _rj(p); r["ts"] = time.time() - 700; r["session_id"] = None; _wj(p, r)
        self.assertEqual(self._done(rid)["status"], "stale")
        lines = []  # no event line may follow (select() does not work on pipes on Windows, so read in a thread)
        t = threading.Thread(target=lambda: lines.append(w.stdout.readline()), daemon=True); t.start(); t.join(0.5)
        self.assertEqual(lines, [])

    def test_sweeper_closes_requests_stuck_in_working(self):
        os.makedirs(os.path.join(self.d, "working"), exist_ok=True)
        p = self._req()
        old = time.time() - 11 * 60; os.utime(p, (old, old))
        self._watch(AUTOSWITCH_SWEEP_EVERY="3")
        self.assertEqual(self._done("abc123")["status"], "error")
        self.assertTrue(os.path.exists(os.path.join(self.d, "lost", "newchat.json")))

    # --- T17: unclear default only in short chats; unknown history never passes a down-switch ---
    def test_unclear_default_needs_a_short_chat(self):
        import policy
        self.assertEqual(policy.decide("opus", 0.43, "claude-opus-5-5", tokens=5000)["reason"], "already on scored tier")
        self.assertEqual(policy.decide("opus", 0.43, "claude-haiku-5-5", tokens=4700000)["target"], None)  # long: stay
        self.assertEqual(policy.decide("opus", 0.43, "claude-haiku-5-5", tokens=None, is_new=True)["target"], "claude-sonnet-5-5")  # new chat: lean up
        self.assertIsNone(policy.decide("opus", 0.43, "claude-haiku-5-5", tokens=None)["target"])  # existing chat, size unknown: stay
        self.assertEqual(policy.decide("opus", 0.43, "claude-haiku-5-5", tokens=5000)["target"], "claude-sonnet-5-5")

    def test_unknown_history_never_passes_a_down_switch(self):
        import policy
        r = policy.decide("haiku", 0.95, "claude-sonnet-5-5", tokens=None)
        self.assertIsNone(r["target"]); self.assertIn("unknown", r["reason"])

    def _rec(self, model):
        p = os.path.join(self.d, "s", "a", "b"); os.makedirs(p, exist_ok=True)
        _wj(os.path.join(p, ID + ".json"), {"sessionId": ID, "cliSessionId": "newchat", "model": model})

    def _sized(self, n):
        p = os.path.join(self.d, "sized.jsonl"); _wt(p, "x" * n); return p

    def test_hook_long_chat_unclear_prompt_stays(self):
        self._rec("claude-haiku-5-5")
        out = self._hook("what is 2+2", transcript=self._sized(4 * 4700000), scorer=self._scorer("opus", 0.43), mode="enforce")
        recs = [json.loads(l) for l in _rt(os.path.join(self.d, "shadow.jsonl")).splitlines()]
        self.assertIsNone(recs[-1]["would_target"]); self.assertIn("long", recs[-1]["reason"])

    def test_hook_short_chat_unclear_prompt_goes_to_sonnet(self):
        self._rec("claude-haiku-5-5")
        self._hook("what is 2+2", transcript=self._sized(4000), scorer=self._scorer("opus", 0.43), mode="enforce")
        recs = [json.loads(l) for l in _rt(os.path.join(self.d, "shadow.jsonl")).splitlines()]
        self.assertEqual(recs[-1]["would_target"], "claude-sonnet-5-5")

    # --- T17: manual pins from observed model changes ---
    def test_hand_change_pins_and_a_watcher_change_does_not(self):
        import pins, importlib; importlib.reload(pins)
        now = time.time()
        self._lastset(ID, "claude-opus-5-5", now)                  # the Watcher last asked for opus
        pins.observe(ID, "claude-sonnet-5-5")                        # first look: seen sonnet
        self.assertFalse(pins.is_manual(ID))
        pins.observe(ID, "claude-haiku-5-5")                         # changed to haiku, not by the Watcher
        self.assertTrue(pins.is_manual(ID)); self.assertTrue(pins.is_pinned("cx", ID, "claude-haiku-5-5"))
        pins.unpin(ID)
        pins.observe(ID, "claude-opus-5-5")                          # the Watcher's own change, to what it asked for
        pins.observe(ID, "claude-opus-5-5")
        self.assertFalse(pins.is_manual(ID))

    def test_hook_pinned_chat_is_never_switched(self):
        import pins, importlib; importlib.reload(pins)
        self._record()                                              # chat on sonnet
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID
        pins.pin(ID, "claude-sonnet-5-5", "hand change")
        self._hook("what is 2+2", scorer=self._scorer("opus", 0.99), mode="shadow")
        recs = [json.loads(l) for l in _rt(os.path.join(self.d, "shadow.jsonl")).splitlines()]
        self.assertIsNone(recs[-1]["would_target"]); self.assertTrue(recs[-1]["pinned"])
        self.assertEqual(recs[-1]["reason"], "pinned: model was picked by hand")

    def test_unpin_restores_switching(self):
        import pins, importlib; importlib.reload(pins)
        self._record()
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID
        pins.pin(ID, "claude-sonnet-5-5", "hand change")
        self.assertEqual(pins.unpin(ID), 1)
        self.assertFalse(pins.is_pinned("cx", ID, "claude-sonnet-5-5"))
        self._hook("what is 2+2", scorer=self._scorer("opus", 0.99), mode="shadow")
        recs = [json.loads(l) for l in _rt(os.path.join(self.d, "shadow.jsonl")).splitlines()]
        self.assertEqual(recs[-1]["would_target"], "claude-opus-5-5")
        pins.pin(ID, "claude-sonnet-5-5", "x"); pins.pin("local_other", "claude-haiku-5-5", "x")
        self.assertEqual(pins.unpin(), 2)                            # all chats
        self.assertEqual(pins.unpin(), 0)

    def test_prep_records_the_target_before_the_set(self):
        self._record(); p = self._req()
        self._prep(p)
        self.assertEqual(_rj(os.path.join(self.d, "lastset", ID + ".json"))["model"], "claude-haiku-5-5")


    # --- T16: pin detection from the transcript ---
    def _transcript(self, entries):
        import datetime
        p = os.path.join(self.d, "t16.jsonl")
        with open(p, "w") as f:
            for ts, untimed in entries:
                stamp = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.000Z") if ts else None
                f.write(json.dumps({"type": "user", "timestamp": stamp,
                                    "message": {"content": "<command-name>/model</command-name><command-args>x</command-args>"}}) + "\n")
                if untimed: f.write(json.dumps({"type": "user", "message": {"content": "<local-command-caveat>run directly in Claude Code</local-command-caveat>"}}) + "\n")
        return p

    def _lastset(self, app_id, model, ts):
        p = os.path.join(self.d, "lastset"); os.makedirs(p, exist_ok=True)
        _wj(os.path.join(p, app_id + ".json"), {"model": model, "ts": ts})

    def test_hand_pick_equal_to_watcher_last_set_is_pinned(self):
        import pins, importlib; importlib.reload(pins)
        now = time.time()
        self._lastset("local_x", "claude-sonnet-5-5", now - 600)     # the Watcher set sonnet ten minutes ago
        tr = self._transcript([(now - 100, True)])                   # the user picked sonnet again by hand
        self.assertTrue(pins.is_pinned("cx", "local_x", "claude-sonnet-5-5", tr))

    def test_watchers_own_model_entry_is_not_a_hand_pick(self):
        import pins, importlib; importlib.reload(pins)
        now = time.time()
        self._lastset("local_x", "claude-sonnet-5-5", now)
        tr = self._transcript([(now - 1, True)])                     # the Watcher's entry, just before its done record
        self.assertFalse(pins.is_pinned("cx", "local_x", "claude-sonnet-5-5", tr))

    def test_old_hand_pick_is_overtaken_by_a_later_watcher_set(self):
        import pins, importlib; importlib.reload(pins)
        now = time.time()
        self._lastset("local_x", "claude-opus-5-5", now)
        tr = self._transcript([(now - 3600, True)])
        self.assertFalse(pins.is_pinned("cx", "local_x", "claude-opus-5-5", tr))

    def test_no_watcher_record_but_a_model_command_means_pinned(self):
        import pins, importlib; importlib.reload(pins)
        tr = self._transcript([(time.time() - 50, True)])
        self.assertTrue(pins.is_pinned("cx", "local_none", "claude-sonnet-5-5", tr))
        self.assertFalse(pins.is_pinned("cx", "local_none", "claude-sonnet-5-5", None))  # no transcript, no record: cannot tell

    def test_stale_watcher_sdk_pin_record_does_not_hide_a_hand_pick(self):
        import pins, importlib; importlib.reload(pins)
        now = time.time()
        self._lastset("local_x", "claude-sonnet-5-5", now - 600)
        pins.record_switch({"session_id": "cx", "to_model": "claude-sonnet-5-5", "source": "sdk"})
        tr = self._transcript([(now - 100, True)])
        self.assertTrue(pins.is_pinned("cx", "local_x", "claude-sonnet-5-5", tr))

    def test_lastset_without_ts_uses_the_file_mtime(self):
        import pins, importlib; importlib.reload(pins)
        now = time.time()
        p = os.path.join(self.d, "lastset"); os.makedirs(p, exist_ok=True)
        f = os.path.join(p, "local_old.json"); _wj(f, {"model": "claude-sonnet-5-5"})  # pre-T16 record, no ts
        os.utime(f, (now - 3600, now - 3600))
        self.assertFalse(pins.is_pinned("cx", "local_old", "claude-sonnet-5-5", self._transcript([(now - 7200, True)])))
        self.assertTrue(pins.is_pinned("cx", "local_old", "claude-sonnet-5-5", self._transcript([(now - 60, True)])))

    def test_untimed_caveat_twin_is_ignored(self):
        import pins, importlib; importlib.reload(pins)
        now = time.time()
        self._lastset("local_x", "claude-sonnet-5-5", now)
        tr = self._transcript([(None, True)])
        self.assertFalse(pins.is_pinned("cx", "local_x", "claude-sonnet-5-5", tr))

    def test_hook_shadow_record_shows_the_pin(self):
        now = time.time()
        self._record()                                              # chat "newchat" is on sonnet
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID
        self._lastset(ID, "claude-sonnet-5-5", now - 600)
        tr = self._transcript([(now - 100, True)])
        out = self._hook("what is 2+2", transcript=tr, scorer=self._scorer("opus", 0.9), mode="shadow")
        self.assertIn("Router suggests", out.stdout)
        recs = [json.loads(l) for l in _rt(os.path.join(self.d, "shadow.jsonl")).splitlines()]
        self.assertTrue(recs[-1]["pinned"]); self.assertIsNone(recs[-1]["would_target"])
        self.assertEqual(recs[-1]["reason"], "pinned: model was picked by hand")
        self.assertEqual(self._pending(), [])



class MachineTrafficTest(unittest.TestCase):
    def test_agent_and_monitor_turns_are_skipped(self):
        import hint
        self.assertTrue(hint.machine_traffic("<task-notification> <task-id>x</task-id> Monitor event"))
        self.assertTrue(hint.machine_traffic('<cross-session-message from="local_x">do the thing</cross-session-message>'))
        self.assertFalse(hint.machine_traffic("what is 15% of 80"))

class ScreenshotTest(unittest.TestCase):
    """A pasted screenshot is invisible to the hook; it must never be blocked (the re-send would drop it)."""
    _scorer = T._scorer
    _hook = T._hook
    _pending = T._pending
    _finish = T._finish
    _req = T._req
    _record = T._record

    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["AUTOSWITCH_DIR"] = self.d
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"; os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = ID
        _wt(os.path.join(self.d, "watcher.alive"), "")
        import importlib, sbqueue
        self.q = importlib.reload(sbqueue)
        self.clip = os.environ["AUTOSWITCH_CLIPIMG"]
        self.addCleanup(lambda: os.environ.__setitem__("AUTOSWITCH_CLIPIMG", self.clip))

    def _with_image(self, text="Traceback KeyError", count=7):
        os.environ["AUTOSWITCH_CLIPIMG"] = fake_clipimg(True, text, count)

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS OCR / clipboard")
    def test_new_chat_with_screenshot_is_not_blocked_but_switch_is_queued(self):
        self._with_image()
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertFalse(stops(out)); self.assertIn("screenshot attached", out.stdout)
        (req,) = self._pending()
        self.assertFalse(req["resend"]); self.assertEqual(req["model"], "claude-haiku-5-5")
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", req["id"])))  # no loop-guard token: nothing re-sent

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS OCR / clipboard")
    def test_instant_with_screenshot_lets_it_through_and_queues_one_switch(self):
        self._with_image()
        os.environ["AUTOSWITCH_SPEED"] = "instant"
        try:
            out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        finally:
            del os.environ["AUTOSWITCH_SPEED"]
        self.assertFalse(stops(out)); self.assertIn("screenshot attached", out.stdout)
        (req,) = self._pending()                                    # exactly one request, switch only
        self.assertFalse(req["resend"]); self.assertEqual(req["model"], "claude-haiku-5-5")
        self.assertFalse(os.path.exists(os.path.join(self.d, "pass", req["id"])))

    def test_without_image_still_blocks_and_resends(self):
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertTrue(stops(out))
        (req,) = self._pending(); self.assertTrue(req["resend"])

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS OCR / clipboard")
    def test_ocr_text_is_sent_to_the_scorer(self):
        import http.server
        seen = []
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(s):
                seen.append(json.loads(s.rfile.read(int(s.headers["Content-Length"]))))
                s.send_response(200); s.end_headers(); s.wfile.write(json.dumps({"label": "opus", "probs": {"opus": 0.9}}).encode())
            def log_message(s, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start(); self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        self._with_image("RaceCondition in scheduler.py line 88")
        self._hook("why does this fail", scorer=f"http://127.0.0.1:{srv.server_port}")
        self.assertIn("RaceCondition in scheduler.py", seen[0]["text"]); self.assertIn("why does this fail", seen[0]["text"])

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS OCR / clipboard")
    def test_stale_clipboard_image_stops_counting_after_the_window(self):
        import images, importlib
        importlib.reload(images)
        self._with_image(count=9)
        self.assertTrue(images.fresh_image(now=1000))          # first sight: fresh
        self.assertTrue(images.fresh_image(now=1100))          # same image, inside the window (re-paste)
        self.assertIsNone(images.fresh_image(now=1000 + images.IMAGE_WINDOW + 1))  # old screenshot left on the clipboard
        self._with_image(count=10)
        self.assertTrue(images.fresh_image(now=5000))          # a new copy is fresh again
        os.environ["AUTOSWITCH_CLIPIMG"] = fake_clipimg(False, count=11)
        self.assertIsNone(images.fresh_image(now=5001))

    def test_unreadable_clipboard_never_raises(self):
        import images
        os.environ["AUTOSWITCH_CLIPIMG"] = "/nonexistent/clipimg"
        images.fresh_image()  # falls back to osascript detection; must not raise

    @unittest.skipIf(sys.platform == "win32", "builds the macOS clipboard helper with swiftc through POSIX tools (which, symlinked touch/rm/mv); not used on Windows")
    def test_failed_build_leaves_marker_and_is_not_retried(self):
        import images, importlib
        importlib.reload(images)
        old = os.environ["PATH"]
        bindir = tempfile.mkdtemp()  # a PATH with the shell basics but no swiftc
        for tool in ("touch", "rm", "mv"): os.symlink(subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip(), os.path.join(bindir, tool))
        os.environ["PATH"] = bindir; self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        images._build_in_background()
        failed, lock = os.path.join(self.d, "bin", "failed"), os.path.join(self.d, "bin", "building")
        for _ in range(50):
            if os.path.exists(failed) and not os.path.exists(lock): break
            time.sleep(0.1)
        self.assertTrue(os.path.exists(failed)); self.assertFalse(os.path.exists(lock))
        images._build_in_background()
        time.sleep(0.5)
        self.assertFalse(os.path.exists(lock))  # no second swiftc within the retry period

    @unittest.skipIf(sys.platform == "win32", "macOS helper build; os.kill(pid, 0) sends Ctrl+C on Windows")
    def test_a_pid_lock_older_than_the_cap_is_stale_even_if_the_pid_lives(self):
        import images
        lock = os.path.join(self.d, "building-cap")
        with open(lock, "w") as f:
            f.write(str(os.getpid()))  # a live pid: e.g. reused after a reboot or a kill -9 during a build
        self.assertTrue(images._builder_alive(lock))
        old = time.time() - images.LOCK_MAX_AGE - 5
        os.utime(lock, (old, old))
        self.assertFalse(images._builder_alive(lock))

    @unittest.skipIf(sys.platform == "win32", "the macOS clipboard helper build; not used on Windows")
    def test_hung_swiftc_is_not_started_twice(self):
        import images, importlib, signal
        importlib.reload(images)
        bindir = tempfile.mkdtemp()
        count = os.path.join(bindir, "count")
        with open(os.path.join(bindir, "swiftc"), "w") as f:  # a swiftc that hangs
            f.write("#!/bin/sh\necho x >> '%s'\nexec sleep 30\n" % count)
        os.chmod(os.path.join(bindir, "swiftc"), 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + old; self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        lock = os.path.join(self.d, "bin", "building")
        t = time.time(); images._build_in_background()
        self.assertLess(time.time() - t, 2)  # never waits for swiftc
        for _ in range(50):
            if os.path.exists(count): break
            time.sleep(0.1)
        with open(lock) as f:
            pid = int(f.read())
        pgid = os.getpgid(pid)  # the detached build's process group: the build shell and the hung swiftc
        def kill():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        self.addCleanup(kill)
        os.utime(lock, (time.time() - 500, time.time() - 500))  # an older lock (under the 600 s cap), build still running
        images._build_in_background()
        time.sleep(0.5)
        with open(count) as f:
            self.assertEqual(f.read().count("x"), 1)  # no second swiftc next to the hung one
        kill()
        for _ in range(50):
            if not images._builder_alive(lock): break
            time.sleep(0.1)
        self.assertFalse(images._builder_alive(lock))  # killed: the next prompt may build again

    def test_whole_image_step_is_capped(self):
        import images, importlib
        f = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
        f.write("#!/bin/sh\nsleep 10\n"); f.close(); os.chmod(f.name, 0o755)
        os.environ["AUTOSWITCH_CLIPIMG"] = f.name; os.environ["AUTOSWITCH_IMAGE_BUDGET"] = "0.6"
        self.addCleanup(lambda: os.environ.pop("AUTOSWITCH_IMAGE_BUDGET", None))
        importlib.reload(images)
        t = time.time(); images.fresh_image()
        self.assertLess(time.time() - t, 1.2)  # clipimg hangs, osascript fallback gets only what is left

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS OCR / clipboard")
    def test_end_to_end_screenshot_prompt_is_never_resent(self):
        """hook -> queue -> watch.sh/prep.py -> Watcher event: switch the model, send nothing, no duplicate."""
        self._with_image()
        self._record()
        env = {**os.environ, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s")}
        w = subprocess.Popen(["bash", os.path.join(SB, "watch.sh")], stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(w.stdout and w.stdout.close or (lambda: None)); self.addCleanup(w.wait); self.addCleanup(w.kill)
        time.sleep(0.5)
        out = self._hook("what is 2+2", scorer=self._scorer("haiku", 0.95))
        self.assertFalse(stops(out))                       # the screenshot prompt went through untouched
        ev = json.loads(readline_within(w, why="; hook stderr: " + out.stderr[-500:])[len("REQUEST "):])
        self.assertTrue(ev["send"] is False and "message" not in ev)  # the Watcher has nothing to send
        self.assertEqual((ev["session_id"], ev["model"]), (ID, "claude-haiku-5-5"))
        self.assertEqual(os.listdir(os.path.join(self.d, "pass")), [])  # no loop-guard token was minted
        self.assertEqual(self._finish("done", ev["path"], "ok").returncode, 0)
        self.assertEqual(_rj(os.path.join(self.d, "done", os.path.basename(ev["path"])))["status"], "ok")
        self.assertEqual(self._pending(), [])                       # nothing else queued: no duplicate

    def test_prep_still_sends_for_normal_requests(self):
        self._record()
        p = self._req()
        out = json.loads(subprocess.run([sys.executable, os.path.join(SB, "prep.py"), p], capture_output=True, text=True,
                                        env={**os.environ, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s")}).stdout)
        self.assertTrue(out["send"]); self.assertIn("what is 2+2", out["message"])



class EnvParseTest(unittest.TestCase):
    def test_bad_numeric_env_falls_back_to_default(self):
        env = {**os.environ, "AUTOSWITCH_BYTES_PER_TOKEN": "four", "AUTOSWITCH_MAX_PROMPT_CHARS": "6k"}
        r = subprocess.run([sys.executable, "-c", "import hint; print(hint.TOKEN_BYTES, hint.MAX_PROMPT_CHARS)"],
                           cwd=SB, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["4.0", "6000"])
        self.assertIn("ignoring AUTOSWITCH_BYTES_PER_TOKEN", r.stderr)

    def test_good_numeric_env_is_used(self):
        env = {**os.environ, "AUTOSWITCH_BYTES_PER_TOKEN": "3.5", "AUTOSWITCH_MAX_PROMPT_CHARS": "100"}
        r = subprocess.run([sys.executable, "-c", "import hint; print(hint.TOKEN_BYTES, hint.MAX_PROMPT_CHARS)"],
                           cwd=SB, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.stdout.split(), ["3.5", "100"])

if __name__ == "__main__":
    unittest.main()
