import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_cli(home, base=None):
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home  # what os.path.expanduser reads on Windows
    if base:
        os.environ["AUTOSWITCH_DIR"] = base
    else:
        os.environ.pop("AUTOSWITCH_DIR", None)
    loader = importlib.machinery.SourceFileLoader("autoswitch_cli", os.path.join(ROOT, "bin", "autoswitch"))
    spec = importlib.util.spec_from_loader("autoswitch_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


OLD = {"model": "opus", "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/x/hint.py"}]},
                         {"hooks": [{"type": "command", "command": "echo mine"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}}



def _rt(*a, **k):
    with open(*a, **k) as f:
        return f.read()


def _wt(text, *a, **k):
    with open(*a, **k) as f:
        f.write(text)

class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude"))
        self.path = os.path.join(self.home, ".claude", "settings.json")
        self.cli = load_cli(self.home)

    def read(self):
        return json.loads(_rt(self.path))

    def test_install_is_idempotent_and_replaces_old_hint(self):
        _wt(json.dumps(OLD), self.path, "w")
        self.cli.settings_install("/usr/bin/python3")
        once = self.read()
        cmds = [h["command"] for g in once["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertEqual(len(cmds), 2)
        self.assertIn("echo mine", cmds)
        self.assertFalse(any("~/x/hint.py" in c for c in cmds))
        self.cli.settings_install("/usr/bin/python3")
        self.assertEqual(self.read(), once)
        self.assertEqual(len([f for f in os.listdir(os.path.dirname(self.path)) if "backup" in f]), 1)

    def test_uninstall_restores_original(self):
        _wt(json.dumps(OLD), self.path, "w")
        self.cli.settings_install("/usr/bin/python3")
        self.cli.settings_uninstall()
        back = self.read()
        self.assertEqual(back["model"], "opus")
        self.assertEqual(back["hooks"]["Stop"], OLD["hooks"]["Stop"])
        self.assertEqual(sorted(json.dumps(g) for g in back["hooks"]["UserPromptSubmit"]),
                         sorted(json.dumps(g) for g in OLD["hooks"]["UserPromptSubmit"]))
        self.assertNotIn("PostModelSwitch", back["hooks"])

    def test_invalid_json_is_left_alone(self):
        _wt("{not json", self.path, "w")
        with self.assertRaises(SystemExit):
            self.cli.settings_install("/usr/bin/python3")
        self.assertEqual(_rt(self.path), "{not json")

    def test_fresh_home_then_uninstall_leaves_no_hooks(self):
        os.remove(self.path) if os.path.exists(self.path) else None
        self.cli.settings_install("/usr/bin/python3")
        self.assertIn("UserPromptSubmit", self.read()["hooks"])
        self.cli.settings_uninstall()
        self.assertFalse(os.path.exists(self.path))  # it did not exist before install

    def raw(self):
        return _rt(self.path)

    GROUPED = ('{\n    "model": "opus",\n    "hooks": {\n        "UserPromptSubmit": [\n'
               '            {"matcher": "x", "hooks": [{"type": "command", "command": "python3 ~/old-router/hint.py"},'
               ' {"type": "command", "command": "other-tool"}]},\n'
               '            {"hooks": [{"type": "command", "command": "echo last"}]}\n        ]\n    }\n}')

    def test_uninstall_is_byte_identical_when_untouched(self):
        _wt(self.GROUPED, self.path, "w")
        self.cli.settings_install("/usr/bin/python3")
        self.assertNotEqual(self.raw(), self.GROUPED)
        self.assertTrue(self.raw().startswith('{\n    "model"'))  # original 4-space indent kept
        self.cli.settings_uninstall()
        self.assertEqual(self.raw(), self.GROUPED)

    def test_uninstall_restores_in_place_after_other_edits(self):
        _wt(self.GROUPED, self.path, "w")
        self.cli.settings_install("/usr/bin/python3")
        d = self.read()
        d["theme"] = "dark"  # the user changed something else after install
        _wt(json.dumps(d, indent=4), self.path, "w")
        self.cli.settings_uninstall()
        back = self.read()
        want = json.loads(self.GROUPED)
        want["theme"] = "dark"
        self.assertEqual(back, want)  # same groups, same order, matcher kept
        self.assertTrue(self.raw().startswith('{\n    "model"'))

    def test_emptied_group_is_reinserted_at_its_index(self):
        orig = {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "a"}]},
            {"matcher": "m", "hooks": [{"type": "command", "command": "~/old/hint.py"}]},
            {"hooks": [{"type": "command", "command": "b"}]}]}}
        _wt(json.dumps(orig, indent=2), self.path, "w")
        self.cli.settings_install("/usr/bin/python3")
        d = self.read()
        d["x"] = 1
        _wt(json.dumps(d, indent=2), self.path, "w")
        self.cli.settings_uninstall()
        back = self.read()
        back.pop("x")
        self.assertEqual(back, orig)

    def test_legacy_list_state_still_restores(self):
        _wt(json.dumps(OLD), self.path, "w")
        self.cli.settings_install("/usr/bin/python3")
        st = json.loads(_rt(os.path.join(self.home, ".autoswitch", "displaced.json")))
        _wt(json.dumps([{"event": d["event"], "hook": d["hook"]} for d in st["displaced"]]), os.path.join(self.home, ".autoswitch", "displaced.json"), "w")
        self.cli.settings_uninstall()
        cmds = [h["command"] for g in self.read()["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertEqual(sorted(cmds), ["echo mine", "python3 ~/x/hint.py"])


class CustomDirTest(unittest.TestCase):
    def test_custom_autoswitch_dir_is_fully_removed(self):
        home = tempfile.mkdtemp()
        base = os.path.join(home, "elsewhere", "as")
        os.makedirs(os.path.join(home, ".claude"))
        path = os.path.join(home, ".claude", "settings.json")
        _wt(json.dumps(OLD), path, "w")
        cli = load_cli(home, base)
        try:
            self.assertTrue(cli.MARK.startswith(base))
            cli.settings_install("/usr/bin/python3")
            self.assertTrue(cli.hook_installed())
            # the hook command uses forward slashes on Windows ("C:/.../hint.py")
            self.assertIn(base.replace("\\", "/"), " ".join(h["command"] for g in json.loads(_rt(path))["hooks"]["UserPromptSubmit"] for h in g["hooks"]).replace("\\", "/"))
            d = json.loads(_rt(path))
            d["y"] = 2
            _wt(json.dumps(d), path, "w")
            cli.settings_uninstall()
            self.assertNotIn(base.replace("\\", "/"), json.dumps(json.loads(_rt(path))).replace("\\\\", "/"))
            self.assertNotIn("PostModelSwitch", json.loads(_rt(path))["hooks"])
        finally:
            os.environ.pop("AUTOSWITCH_DIR", None)


@unittest.skipIf(sys.platform == "win32", "runs uninstall.sh (bash + launchctl stub); Windows uses uninstall.ps1, run in the windows-install CI job")
class UninstallScriptTest(unittest.TestCase):
    def run_uninstall(self, settings_text):
        home = tempfile.mkdtemp()
        base = os.path.join(home, "as dir")
        os.makedirs(os.path.join(home, ".claude"))
        os.makedirs(base)
        _wt("[]", os.path.join(base, "displaced.json"), "w")
        _wt(settings_text, os.path.join(home, ".claude", "settings.json"), "w")
        stub = os.path.join(home, "stub")
        os.makedirs(stub)
        _wt("#!/bin/sh\nexit 0\n", os.path.join(stub, "launchctl"), "w")
        os.chmod(os.path.join(stub, "launchctl"), 0o755)
        env = {**os.environ, "HOME": home, "AUTOSWITCH_DIR": base, "PATH": stub + os.pathsep + os.environ["PATH"]}
        r = subprocess.run(["bash", os.path.join(ROOT, "uninstall.sh")], env=env, capture_output=True, text=True)
        return r, base

    def test_failed_restore_aborts_before_deleting(self):
        r, base = self.run_uninstall("{not json")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(base, "displaced.json")))

    def test_successful_uninstall_removes_dir(self):
        r, base = self.run_uninstall("{}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(base))


if __name__ == "__main__":
    unittest.main()
