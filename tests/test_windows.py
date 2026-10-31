"""Windows support. These run on every platform: the Windows behaviour is selected by passing/mocking the platform."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hook"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths
from test_settings import ROOT, load_cli

HOOK = os.path.join(ROOT, "hook")
ID = "local_11111111-2222-3333-4444-555555555555"



def _rt(*a, **k):
    with open(*a, **k) as f:
        return f.read()


def _wt(text, *a, **k):
    with open(*a, **k) as f:
        f.write(text)

class PathsTest(unittest.TestCase):
    def test_windows_uses_appdata(self):
        env = {"APPDATA": r"C:\Users\me\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
        d = paths.claude_data_dir("win32", env, home=r"C:\Users\me")
        self.assertEqual(d, os.path.join(r"C:\Users\me\AppData\Roaming", "Claude"))
        self.assertEqual(paths.sessions_dir("win32", env, home=r"C:\Users\me"), os.path.join(d, "claude-code-sessions"))

    def test_windows_without_appdata_falls_back_to_home(self):
        d = paths.claude_data_dir("win32", {}, home="/h")
        self.assertEqual(d, os.path.join("/h", "AppData", "Roaming", "Claude"))

    def test_windows_store_install_is_found_when_it_exists(self):
        local = tempfile.mkdtemp()
        store = os.path.join(local, "Packages", "Claude_abc123", "LocalCache", "Roaming", "Claude")
        os.makedirs(store)
        env = {"APPDATA": os.path.join(local, "nowhere"), "LOCALAPPDATA": local}
        self.assertEqual(paths.claude_data_dir("win32", env, home="/h"), store)
        roaming = os.path.join(env["APPDATA"], "Claude")
        os.makedirs(roaming)
        self.assertEqual(paths.claude_data_dir("win32", env, home="/h"), roaming)  # the classic location wins

    def test_folder_with_sessions_wins_when_both_exist(self):
        local = tempfile.mkdtemp()
        env = {"APPDATA": os.path.join(local, "roam"), "LOCALAPPDATA": local}
        roaming = os.path.join(env["APPDATA"], "Claude")
        store = os.path.join(local, "Packages", "Claude_abc", "LocalCache", "Roaming", "Claude")
        os.makedirs(roaming); os.makedirs(os.path.join(store, "claude-code-sessions"))
        self.assertEqual(paths.claude_data_dir("win32", env, home="/h"), store)  # only the Store one has sessions
        self.assertEqual(paths.sessions_dir("win32", env, home="/h"), os.path.join(store, "claude-code-sessions"))
        os.makedirs(os.path.join(roaming, "claude-code-sessions"))
        self.assertEqual(paths.claude_data_dir("win32", env, home="/h"), roaming)  # both have it: classic first

    def test_mac_location_unchanged(self):
        self.assertEqual(paths.sessions_dir("darwin", {}, home="/Users/x"),
                         "/Users/x/Library/Application Support/Claude/claude-code-sessions")

    def test_default_follows_sys_platform(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(paths.claude_data_dirs(env={"APPDATA": "A"})[0].endswith("Claude"))
            self.assertIn("A", paths.claude_data_dirs(env={"APPDATA": "A"})[0])
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIn("Library", paths.claude_data_dirs(home="/h")[0])

    def test_resolve_reads_sessions_from_the_windows_location(self):
        appdata = tempfile.mkdtemp()
        sess = os.path.join(appdata, "Claude", "claude-code-sessions", "acct", "org")
        os.makedirs(sess)
        _wt(json.dumps({"sessionId": ID, "cliSessionId": "cli-1", "model": "claude-haiku-5-5", "title": "t"}), os.path.join(sess, ID + ".json"), "w")
        env = {k: v for k, v in os.environ.items() if k != "AUTOSWITCH_SESSIONS"}
        env["APPDATA"] = appdata
        code = ("import sys, json; sys.platform = 'win32'; sys.path.insert(0, %r); import resolve; "
                "print(json.dumps(resolve.resolve('cli-1', wait=0)))" % HOOK)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        self.assertEqual(json.loads(out.stdout)["sessionId"], ID, out.stderr)


class HookCommandTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.cli = load_cli(self.home)
        self.cli.APP = r"C:\Users\me\.autoswitch\app"
        self.cli.MARK = os.path.join(self.cli.APP, "hook") + "\\"

    def test_windows_command_is_quoted_forward_slashes_without_posix_redirect(self):
        cmd = self.cli.hook_command(r"C:\Program Files\Python312\python.exe", "hint.py", platform="win32")
        self.assertEqual(cmd, '"C:/Program Files/Python312/python.exe" "C:/Users/me/.autoswitch/app/hook/hint.py"')
        self.assertNotIn("/dev/null", cmd)
        self.assertNotIn("||", cmd)

    def test_mac_command_unchanged(self):
        cmd = self.cli.hook_command("/usr/bin/python3", "hint.py", platform="darwin")
        self.assertTrue(cmd.startswith('/usr/bin/python3 "'))
        self.assertTrue(cmd.endswith('hint.py" 2>/dev/null || true'))

    def test_our_windows_commands_are_recognised_with_either_slash(self):
        fwd = self.cli.hook_command("python", "hint.py", platform="win32")
        self.assertTrue(self.cli._ours(fwd))
        self.assertTrue(self.cli._ours(r'python "C:\Users\me\.autoswitch\app\hook\hint.py"'))
        self.assertFalse(self.cli._ours('python "C:/other/hint.py"'))

    def test_install_on_windows_replaces_and_stays_idempotent(self):
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings))
        old = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python C:/old/hint.py"}]}]}}
        _wt(json.dumps(old), settings, "w")
        with mock.patch.object(sys, "platform", "win32"):
            self.cli.settings_install(r"C:\Python312\python.exe", "py -3")
            first = _rt(settings, encoding="utf-8")
            self.cli.settings_install(r"C:\Python312\python.exe", "py -3")
            self.assertEqual(_rt(settings, encoding="utf-8"), first)  # second run changes nothing
            self.cli.settings_install(r"D:\Py\python.exe", "py -3")  # a different interpreter replaces ours, not stacks
        data = json.loads(_rt(settings, encoding="utf-8"))
        cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertEqual(len(cmds), 1)
        self.assertTrue(cmds[0].startswith('"D:/Py/python.exe" "'))
        self.assertTrue(cmds[0].endswith('/hook/hint.py"'))
        self.assertEqual(len(data["hooks"]["PostModelSwitch"]), 1)
        self.assertEqual(self.cli.read("python-cmd"), "py -3")
        self.assertTrue(any(n.startswith("settings.json.autoswitch-backup-") for n in os.listdir(os.path.dirname(settings))))

    def test_crlf_settings_survive_a_round_trip(self):
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings))
        raw = '{\r\n  "model": "opus"\r\n}\r\n'
        _wt(raw, settings, "w", newline="")
        self.cli.settings_install("python")
        self.cli.settings_uninstall()
        self.assertEqual(_rt(settings, newline=""), raw)

    def test_bom_settings_install_and_uninstall(self):
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings))
        raw = '\ufeff{\r\n  "model": "opus"\r\n}\r\n'
        with open(settings, "w", encoding="utf-8", newline="") as f:
            f.write(raw)
        with mock.patch.object(sys, "platform", "win32"):
            self.cli.settings_install(r"C:\Python312\python.exe", "py -3")
        b = _rt(settings, "rb")
        self.assertTrue(b.startswith(b"\xef\xbb\xbf"))  # BOM kept on rewrite
        self.assertEqual(b.count(b"\xef\xbb\xbf"), 1)
        self.assertTrue(self.cli.hook_installed())
        data = json.loads(b.decode("utf-8-sig"))
        self.assertEqual(data["model"], "opus")
        self.assertIn("UserPromptSubmit", data["hooks"])
        self.cli.settings_uninstall()
        self.assertEqual(_rt(settings, encoding="utf-8", newline=""), raw)  # byte-exact restore

    def test_bom_settings_edited_after_install_still_uninstall(self):
        settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings))
        with open(settings, "w", encoding="utf-8-sig") as f:
            json.dump({"model": "opus"}, f, indent=2)
        self.cli.settings_install("python")
        data = json.loads(_rt(settings, encoding="utf-8-sig"))
        data["theme"] = "dark"  # user edit, so uninstall must strip instead of restoring bytes
        with open(settings, "w", encoding="utf-8-sig") as f:
            json.dump(data, f, indent=2)
        self.cli.settings_uninstall()
        b = _rt(settings, "rb")
        self.assertTrue(b.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(json.loads(b.decode("utf-8-sig")), {"model": "opus", "theme": "dark"})


class _RollbackBase(unittest.TestCase):
    SEED = ('\ufeff{\r\n    "model": "opus",\r\n    "hooks": {\r\n        "UserPromptSubmit": [{"hooks": [\r\n'
            '            {"type": "command", "command": "python C:/old/hint.py"},\r\n'
            '            {"type": "command", "command": "mine.sh"}]}],\r\n'
            '        "Stop": [{"hooks": [{"type": "command", "command": "stop.sh"}]}]\r\n    }\r\n}\r\n')

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.cli = load_cli(self.home)
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(self.settings))

    def write(self, text):
        with open(self.settings, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read(self, path=None):
        with open(path or self.settings, encoding="utf-8", newline="") as f:
            return f.read()

    def install(self, py="python"):
        self.cli.settings_snapshot()
        self.cli.settings_install(py, "py -3")


class InstallRollbackTest(_RollbackBase):
    """install.ps1 snapshots before step 1b and rolls back (_settings-rollback) when a later step fails."""

    def test_fresh_install_then_failure_restores_the_original_bytes(self):
        self.write(self.SEED)
        self.install()
        self.assertTrue(self.cli.hook_installed())
        self.cli.settings_rollback()  # a later step (pip, training, ...) failed
        self.assertEqual(self.read(), self.SEED)
        for f in ("displaced.json", "python-cmd", "install-snapshot.json"):
            self.assertFalse(os.path.exists(self.cli.p(f)), f)

    def test_failed_reinstall_keeps_the_working_hook(self):
        self.write(self.SEED)
        self.install()
        os.remove(self.cli.p("install-snapshot.json"))  # install #1 completed
        before, displaced = self.read(), self.read(self.cli.p("displaced.json"))
        self.install(r"D:\Py\python.exe")  # upgrade with another interpreter, then a later step fails
        self.assertNotEqual(self.read(), before)
        self.cli.settings_rollback()
        self.assertTrue(self.cli.hook_installed())
        self.assertEqual(self.read(), before)  # byte-identical to before the 2nd run
        self.assertEqual(self.read(self.cli.p("displaced.json")), displaced)
        self.assertEqual(self.cli.read("python-cmd"), "py -3")
        self.cli.settings_uninstall()  # and the original still comes back exactly
        self.assertEqual(self.read(), self.SEED)

    def test_fresh_install_without_settings_then_failure_leaves_no_file(self):
        self.install()
        self.assertTrue(os.path.exists(self.settings))
        self.cli.settings_rollback()
        self.assertFalse(os.path.exists(self.settings))

    def test_rollback_then_install_and_uninstall_keeps_crlf_exact(self):
        self.write(self.SEED)
        self.install()
        self.cli.settings_rollback()
        self.install()
        os.remove(self.cli.p("install-snapshot.json"))
        self.cli.settings_uninstall()
        self.assertEqual(self.read(), self.SEED)

    def test_crlf_kept_when_settings_changed_after_install(self):
        self.write(self.SEED)
        self.install()
        self.write(self.read().replace('"opus"', '"sonnet"'))  # user edit: no byte-exact restore possible
        self.cli.settings_uninstall()
        out = self.read()
        self.assertEqual(out.count("\r\n"), out.count("\n"))  # every line break is still CRLF
        self.assertTrue(out.startswith("\ufeff"))
        cmds = [h["command"] for g in json.loads(out[1:])["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertEqual(cmds, ["python C:/old/hint.py", "mine.sh"])


class RollbackEditsAndStaleSnapshotTest(_RollbackBase):
    """N1: a rollback keeps edits made to settings.json during the install. N2: a snapshot left by a hard kill is
    undone by the next install or uninstall."""

    def test_rollback_keeps_edits_made_during_the_install(self):
        self.write(self.SEED)
        self.install()
        self.write(self.read().replace('"opus"', '"sonnet"'))  # the user (or Claude) edits settings mid-install
        self.cli.settings_rollback()
        out = self.read()
        self.assertIn('"sonnet"', out)
        self.assertFalse(self.cli.hook_installed())
        self.assertTrue(out.startswith("\ufeff") and out.count("\r\n") == out.count("\n"))
        cmds = [h["command"] for g in json.loads(out[1:])["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertEqual(cmds, ["python C:/old/hint.py", "mine.sh"])  # displaced hooks are back
        for f in ("displaced.json", "python-cmd", "install-snapshot.json"):
            self.assertFalse(os.path.exists(self.cli.p(f)), f)

    def test_rollback_after_reinstall_with_edits_keeps_edits(self):
        self.write(self.SEED)
        self.install()
        os.remove(self.cli.p("install-snapshot.json"))
        displaced = self.read(self.cli.p("displaced.json"))
        self.install(r"D:\Py\python.exe")
        self.write(self.read().replace('"opus"', '"sonnet"'))
        self.cli.settings_rollback()
        self.assertIn('"sonnet"', self.read())  # the mid-install edit is kept
        self.assertTrue(self.cli.hook_installed())  # and the earlier working hook is back, not removed
        cmds = [h["command"] for h in self.cli._iter_hooks(self.read()) if self.cli._ours(h["command"])]
        self.assertTrue(cmds)
        self.assertFalse([c for c in cmds if r"D:\Py" in c], cmds)  # this failed run's hook is gone
        self.assertEqual(self.read(self.cli.p("displaced.json")), displaced)
        self.assertEqual(self.cli.read("python-cmd"), "py -3")

    def test_rollback_after_fresh_install_with_edits_removes_the_hook(self):
        self.write(self.SEED)
        self.install()
        self.write(self.read().replace('"opus"', '"sonnet"'))
        self.cli.settings_rollback()
        self.assertIn('"sonnet"', self.read())
        self.assertFalse(self.cli.hook_installed())

    def test_swap_puts_our_hook_back_at_its_old_position(self):
        ours_old = "python " + self.cli.MARK + "hint.py --old"
        ours_new = "python " + self.cli.MARK + "hint.py --new"
        g = lambda c: {"hooks": [{"type": "command", "command": c}]}
        prev = json.dumps({"hooks": {"UserPromptSubmit": [g("a.sh"), g(ours_old), g("b.sh")]}})
        self.write(json.dumps({"model": "sonnet", "hooks": {"UserPromptSubmit": [g("a.sh"), g(ours_new), g("b.sh"), g("c.sh")]}}))
        self.cli._swap_hooks_keep_edits(prev)
        data = json.loads(self.read())
        cmds = [h["command"] for grp in data["hooks"]["UserPromptSubmit"] for h in grp["hooks"]]
        self.assertEqual(cmds, ["a.sh", ours_old, "b.sh", "c.sh"])  # same slot as before, not moved to the top
        self.assertEqual(data["model"], "sonnet")

    def test_committed_snapshot_never_undoes_a_good_install(self):
        self.write(self.SEED)
        self.install()
        good = self.read()
        self.cli.settings_commit()  # install.ps1 success branch
        self.assertFalse(os.path.exists(self.cli.p("install-snapshot.json")))
        self.cli._stale_snapshot()
        self.assertEqual(self.read(), good)
        self.install()  # the next install does not roll the good one back first
        self.assertTrue(self.cli.hook_installed())
        self.assertFalse(os.path.exists(self.cli.p("install-snapshot.done")))

    def test_commit_flag_when_the_snapshot_cannot_be_renamed(self):
        self.write(self.SEED)
        self.install()
        good = self.read()
        orig = self.cli.os.replace
        def deny(src, dst):
            raise PermissionError(5, "Access is denied")
        self.cli.os.replace = deny
        try:
            self.cli.settings_commit()
        finally:
            self.cli.os.replace = orig
        with open(self.cli.p("install-snapshot.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["committed"])
        self.cli.settings_uninstall()  # would have rolled the install back before; now it uninstalls normally
        self.assertEqual(self.read(), self.SEED)
        self.assertFalse(os.path.exists(self.cli.p("install-snapshot.json")))

    def test_next_install_undoes_a_killed_install(self):
        self.write(self.SEED)
        self.install()  # killed here: no finally, snapshot left behind
        self.assertTrue(os.path.exists(self.cli.p("install-snapshot.json")))
        self.install()  # next run first undoes the killed one, then snapshots the original state
        self.cli.settings_rollback()
        self.assertEqual(self.read(), self.SEED)
        self.assertFalse(os.path.exists(self.cli.p("displaced.json")))

    def test_uninstall_after_a_killed_install_restores_original(self):
        self.write(self.SEED)
        self.install()
        self.cli.settings_uninstall()
        self.assertEqual(self.read(), self.SEED)
        self.assertFalse(os.path.exists(self.cli.p("install-snapshot.json")))

    def test_unreadable_stale_snapshot_is_dropped(self):
        self.write(self.SEED)
        os.makedirs(self.cli.BASE, exist_ok=True)
        with open(self.cli.p("install-snapshot.json"), "w") as f:
            f.write("{trunc")
        self.install()
        os.remove(self.cli.p("install-snapshot.json"))
        self.cli.settings_uninstall()
        self.assertEqual(self.read(), self.SEED)


class WatcherPromptTest(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli(tempfile.mkdtemp())
        self.tpl = _rt(os.path.join(ROOT, "WATCHER.md"), encoding="utf-8")

    def test_mac_prompt_uses_python3_and_watch_py(self):
        with mock.patch.object(self.cli, "WIN", False):
            text = self.cli.watcher_text(self.tpl, app="/Users/x/.autoswitch/app")
        self.assertIn("`python3 /Users/x/.autoswitch/app/hook/watch.py`", text)
        self.assertIn("python3 /Users/x/.autoswitch/app/hook/finish.py done <path> refused", text)
        self.assertNotIn("done <path> ok", text)  # the Watcher no longer closes successful requests
        self.assertNotIn("{{", text)

    def test_windows_prompt_uses_launcher_and_forward_slashes(self):
        with mock.patch.object(self.cli, "WIN", True):
            text = self.cli.watcher_text(self.tpl, app=r"C:\Users\me\.autoswitch\app", py="py -3")
        self.assertIn("`py -3 C:/Users/me/.autoswitch/app/hook/watch.py`", text)
        self.assertNotIn("\\", text.split("Re-arm")[0])

    def test_windows_path_with_spaces_is_quoted(self):
        with mock.patch.object(self.cli, "WIN", True):
            self.assertEqual(self.cli.fwd(r"C:\Users\Jo Smith\a"), '"C:/Users/Jo Smith/a"')


class WindowsClipboardTest(unittest.TestCase):
    class User32:
        def __init__(self, formats, seq=7):
            self.formats, self.seq = formats, seq

        def RegisterClipboardFormatW(self, name):
            return 49999

        def GetClipboardSequenceNumber(self):
            return self.seq

        def IsClipboardFormatAvailable(self, fmt):
            return fmt in self.formats

    def test_image_formats_are_detected_without_ocr(self):
        import images
        for fmt in (2, 8, 17, 49999):
            r = images._windows_clipboard(self.User32({fmt}))
            self.assertEqual(r, {"count": 7, "image": True, "text": ""})
        self.assertFalse(images._windows_clipboard(self.User32({1}))["image"])  # plain text only

    def test_failure_returns_none(self):
        import images
        self.assertIsNone(images._windows_clipboard(object()))

    def test_screenshot_prompt_is_fresh_but_unread_on_windows(self):
        import images
        d = tempfile.mkdtemp()
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(images.sb, "BASE", d), \
             mock.patch.object(images, "_windows_clipboard", return_value={"count": 3, "image": True, "text": ""}):
            self.assertEqual(images.fresh_image(now=1000), {"image": True, "text": ""})

    def test_no_clipboard_reader_elsewhere_means_no_image(self):
        import images
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(images.read_clipboard())


class WatchPyTest(unittest.TestCase):
    """hook/watch.py is the same loop on every platform; hook/watch.sh just execs it."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        sess = os.path.join(self.d, "s", "a", "b")
        os.makedirs(sess)
        _wt(json.dumps({"sessionId": ID, "cliSessionId": "newchat", "model": "claude-sonnet-5-5"}), os.path.join(sess, ID + ".json"), "w")
        self.env = {**os.environ, "AUTOSWITCH_DIR": self.d, "AUTOSWITCH_SESSIONS": os.path.join(self.d, "s")}

    def enqueue(self, **kw):
        code = ("import sys; sys.path.insert(0, %r); import sbqueue as q; "
                "q.enqueue(None, 'claude-haiku-5-5', %r, 'haiku', 0.9, cli_session_id='newchat')" % (HOOK, kw.get("prompt", "hi")))
        subprocess.run([sys.executable, "-c", code], env=self.env, check=True)

    def test_prints_request_line_fast_and_keeps_heartbeat(self):
        w = subprocess.Popen([sys.executable, os.path.join(HOOK, "watch.py")], stdout=subprocess.PIPE, text=True, env=self.env)
        self.addCleanup(w.stdout.close)
        self.addCleanup(w.wait)
        self.addCleanup(w.kill)
        time.sleep(0.5)
        self.assertTrue(os.path.exists(os.path.join(self.d, "watcher.alive")))
        t0 = time.time()
        self.enqueue(prompt="line one\nline two \u00e9")
        line = w.stdout.readline()
        self.assertLess(time.time() - t0, 3)
        self.assertTrue(line.startswith("REQUEST "))
        out = json.loads(line[len("REQUEST "):])
        self.assertEqual((out["session_id"], out["set_model"], out["send"]), (ID, True, True))
        self.assertIn("line one\nline two \u00e9", out["message"])
        self.assertTrue(os.path.exists(out["path"]))  # in working/
        self.assertTrue(line.isascii())

    def test_instant_request_is_switch_only_and_settles_ok(self):
        # send:false (instant speed or a screenshot): no message, no pass token, and settle closes it ok by itself
        env = {**self.env, "AUTOSWITCH_SETTLE_GRACE": "0.5"}
        code = ("import sys; sys.path.insert(0, %r); import sbqueue as q; "
                "q.enqueue(None, 'claude-haiku-5-5', 'hi', 'haiku', 0.9, cli_session_id='newchat', resend=False)" % HOOK)
        subprocess.run([sys.executable, "-c", code], env=env, check=True)
        w = subprocess.Popen([sys.executable, os.path.join(HOOK, "watch.py")], stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(w.stdout.close)
        self.addCleanup(w.wait)
        self.addCleanup(w.kill)
        out = json.loads(w.stdout.readline()[len("REQUEST "):])
        self.assertEqual((out["session_id"], out["set_model"], out["send"]), (ID, True, False))
        self.assertNotIn("message", out)
        self.assertEqual(os.listdir(os.path.join(self.d, "pass")), [])
        done = os.path.join(self.d, "done", os.path.basename(out["path"]))
        for _ in range(100):
            if os.path.exists(done):
                break
            time.sleep(0.1)
        with open(done, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "ok")  # a switch-only request never leaves a "please resend" note
        self.assertFalse(os.path.exists(os.path.join(self.d, "lost")) and os.listdir(os.path.join(self.d, "lost")))

    def test_crashing_prep_is_closed_as_error(self):
        # a request that is not valid JSON makes prep.py crash: it must end up in done/, not stay in working/
        for sub in ("pending", "working", "done"):
            os.makedirs(os.path.join(self.d, sub), exist_ok=True)
        _wt("{not json", os.path.join(self.d, "pending", "bad1.json"), "w")
        w = subprocess.Popen([sys.executable, os.path.join(HOOK, "watch.py")], stdout=subprocess.PIPE, text=True, env=self.env)
        self.addCleanup(w.stdout.close)
        self.addCleanup(w.wait)
        self.addCleanup(w.kill)
        for _ in range(50):
            if os.listdir(os.path.join(self.d, "done")):
                break
            time.sleep(0.1)
        self.assertEqual(os.listdir(os.path.join(self.d, "working")), [])
        self.assertTrue(os.listdir(os.path.join(self.d, "done")))


class SbqueueTest(unittest.TestCase):
    def test_enqueue_replaces_an_existing_target_file(self):
        """os.rename fails on Windows when the target exists; os.replace does not."""
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"AUTOSWITCH_DIR": d}):
            import importlib, sbqueue
            importlib.reload(sbqueue)
            rid = sbqueue.enqueue("local_x", "m", "p", "haiku", 0.9)
            target = sbqueue.path("pending", rid + ".json")
            self.assertTrue(os.path.exists(target))
            with mock.patch("uuid.uuid4") as u:
                u.return_value.hex = rid + "ffff"
                _wt("stale", target, "w")
                self.assertEqual(sbqueue.enqueue("local_x", "m", "p2", "haiku", 0.9), rid)
            self.assertEqual(json.loads(_rt(target))["prompt"], "p2")
        importlib.reload(sbqueue)


class HookStdinBomTest(unittest.TestCase):
    """When PowerShell 5.1's $OutputEncoding is UTF-8 with a BOM, piping to a native exe prefixes a BOM.
    Both stdin hooks must still read their JSON."""

    def run_hook(self, name, data, home):
        hook = os.path.join(ROOT, "hook", name)
        env = dict(os.environ, HOME=home, USERPROFILE=home, AUTOSWITCH_DIR=os.path.join(home, ".autoswitch"),
                   AUTOSWITCH_DEBUG="1", AUTOSWITCH_MODE="hint", AUTOSWITCH_SCORER="http://127.0.0.1:9")
        return subprocess.run([sys.executable, hook], input=data, capture_output=True, env=env, timeout=30)

    def check_hint(self, data):
        with tempfile.TemporaryDirectory() as home:
            r = self.run_hook("hint.py", data, home)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(b"JSONDecodeError", r.stderr)
        # it got past json.load and reached the (dead) scorer: proof the payload was parsed
        self.assertTrue(b"ConnectionRefusedError" in r.stderr or b"ConnectionError" in r.stderr or b"timed out" in r.stderr, r.stderr)

    def test_hint_accepts_a_bom(self):
        self.check_hint(b'\xef\xbb\xbf{"prompt": "what does git rebase do", "session_id": "s"}')

    def test_hint_without_a_bom_unchanged(self):
        self.check_hint(b'{"prompt": "what does git rebase do", "session_id": "s"}')

    def test_modelswitch_accepts_a_bom(self):
        payload = b'{"session_id": "c1", "to_model": "claude-opus-5-5", "source": "picker"}'
        for data in (b"\xef\xbb\xbf" + payload, payload):
            with tempfile.TemporaryDirectory() as home:
                r = self.run_hook("modelswitch.py", data, home)
                self.assertEqual(r.returncode, 0)
                pins = os.path.join(home, ".autoswitch", "pins")
                self.assertEqual(len(os.listdir(pins)), 1, data)
                with open(os.path.join(pins, os.listdir(pins)[0]), encoding="utf-8") as f:
                    rec = json.load(f)
                self.assertEqual((rec["model"], rec["hand"]), ("claude-opus-5-5", True))


if __name__ == "__main__":
    unittest.main()


class SpeedCliTest(unittest.TestCase):
    """`autoswitch speed` and the speed line in `autoswitch status`, with the macOS/Linux and the Windows CLI."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.base = os.path.join(self.home, ".autoswitch")
        self.cli = load_cli(self.home, self.base)
        self.addCleanup(os.environ.pop, "AUTOSWITCH_DIR", None)
        os.environ.pop("AUTOSWITCH_SPEED", None)

    def run_cli(self, *args, win=False):
        import contextlib, io
        buf = io.StringIO()
        with mock.patch.object(self.cli, "WIN", win), mock.patch.object(sys, "argv", ["autoswitch", *args]), \
             contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                self.cli.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, buf.getvalue()

    def _status(self, win):
        with mock.patch.object(self.cli, "scorer_up", return_value=False), \
             mock.patch.object(self.cli, "service_note", return_value="x"), \
             mock.patch.object(self.cli, "hook_installed", return_value=False):
            return self.run_cli("status", win=win)[1]

    def test_speed_roundtrip_on_both_platforms(self):
        for win in (False, True):
            with self.subTest(win=win):
                self.assertEqual(self.run_cli("speed", win=win), (0, "seamless\n"))  # no file: seamless
                code, out = self.run_cli("speed", "instant", win=win)
                self.assertEqual(code, 0); self.assertIn("INSTANT", out)
                self.assertEqual(_rt(os.path.join(self.base, "speed"), encoding="utf-8"), "instant\n")
                self.assertEqual(self.run_cli("speed", win=win), (0, "instant\n"))
                self.assertIn("speed    instant", self._status(win))
                self.assertEqual(self.run_cli("speed", "SEAMLESS", win=win)[0], 0)
                self.assertEqual(self.run_cli("speed", win=win), (0, "seamless\n"))
                self.assertIn("speed    seamless", self._status(win))
                os.remove(os.path.join(self.base, "speed"))

    def test_bad_value_is_rejected_and_changes_nothing(self):
        for win in (False, True):
            with self.subTest(win=win):
                self.assertEqual(self.run_cli("speed", "fast", win=win)[0], 2)
                self.assertFalse(os.path.exists(os.path.join(self.base, "speed")))

    def test_predictive_is_refused_on_windows_and_keeps_the_speed(self):
        self.assertEqual(self.run_cli("speed", "instant", win=True)[0], 0)
        code, out = self.run_cli("speed", "predictive", win=True)
        self.assertEqual(code, 1)
        self.assertIn("needs macOS", out)
        self.assertIn("left at instant", out)
        self.assertEqual(_rt(os.path.join(self.base, "speed"), encoding="utf-8"), "instant\n")
        self.assertEqual(self.run_cli("speed", "predictive", win=False)[0], 0)  # macOS/Linux: accepted

    def test_inline_on_windows_installs_the_plugin_like_macos(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[1:])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        os.environ["AUTOSWITCH_CLAUDE"] = r"C:\Users\me\AppData\Roaming\npm\claude.cmd"
        self.addCleanup(os.environ.pop, "AUTOSWITCH_CLAUDE", None)
        with mock.patch.object(subprocess, "run", side_effect=run):
            code, out = self.run_cli("speed", "inline", win=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["plugin", "marketplace", "add", self.cli.ROOT],
                                 ["plugin", "install", "autoswitch-inline@claude-autoswitch", "--scope", "user"]])
        self.assertIn("fall back to instant", out)
        self.assertEqual(_rt(os.path.join(self.base, "speed"), encoding="utf-8"), "inline\n")
        self.assertTrue(os.path.isdir(os.path.join(self.base, "inline")))

    def test_inline_on_windows_prints_the_commands_without_claude(self):
        with mock.patch.object(self.cli, "claude_bin", return_value=None):
            code, out = self.run_cli("speed", "inline", win=True)
        self.assertEqual(code, 0)
        self.assertIn("claude plugin marketplace add", out)
        self.assertIn("claude plugin install autoswitch-inline@claude-autoswitch --scope user", out)

    def test_desktop_bundled_claude_is_found_on_windows(self):
        roaming = tempfile.mkdtemp()
        exe = os.path.join(roaming, "Claude", "claude-code", "2.1.309", "abc123", "claude.exe")
        os.makedirs(os.path.dirname(exe))
        _wt("", exe, "w")
        env = {k: v for k, v in os.environ.items() if k != "AUTOSWITCH_CLAUDE"}
        env["APPDATA"] = roaming
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(self.cli, "WIN", True), \
             mock.patch("shutil.which", return_value=None), mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(self.cli.claude_bin(), exe)

    def test_cli_and_hook_agree(self):
        import importlib, sbqueue
        os.environ["AUTOSWITCH_DIR"] = self.base
        q = importlib.reload(sbqueue)
        for word in ("instant", "seamless"):
            self.run_cli("speed", word)
            self.assertEqual(q.speed(), word)
        os.environ["AUTOSWITCH_SPEED"] = "instant"
        try:
            self.assertEqual(self.run_cli("speed")[1], "instant\n")  # the env var wins, as in the hook
        finally:
            del os.environ["AUTOSWITCH_SPEED"]
