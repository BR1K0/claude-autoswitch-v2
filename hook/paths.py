"""Where the Claude desktop app keeps its data, per platform.

macOS:   ~/Library/Application Support/Claude
Windows: %APPDATA%\\Claude  (a Microsoft Store install may keep it under
         %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude; both are tried: the first one that contains claude-code-sessions wins, else the first
         one that exists)
Linux:   ~/.config/Claude  (not supported by the app; here only so the code never crashes)
"""
import glob
import os
import posixpath
import sys


SESSIONS = "claude-code-sessions"


def _platform(platform):
    return sys.platform if platform is None else platform


def claude_data_dirs(platform=None, env=None, home=None):
    """Candidate folders for the desktop app's data, most likely first."""
    platform = _platform(platform)
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    if platform == "win32":
        roaming = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        local = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        out = [os.path.join(roaming, "Claude")]
        out += sorted(glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude")))
        return out
    j = posixpath.join  # macOS/Linux paths always use "/", even when computed on another OS (tests)
    if platform == "darwin":
        return [j(home, "Library", "Application Support", "Claude")]
    return [j(env.get("XDG_CONFIG_HOME") or j(home, ".config"), "Claude")]


def claude_data_dir(platform=None, env=None, home=None):
    """The app's data folder: the first candidate that has claude-code-sessions in it, else the first that exists,
    else the first candidate. (Both the classic and the Store folder can exist, e.g. after switching installs.)"""
    cands = claude_data_dirs(platform, env, home)
    return (next((c for c in cands if os.path.isdir(os.path.join(c, SESSIONS))), None)
            or next((c for c in cands if os.path.isdir(c)), cands[0]))


def sessions_dir(platform=None, env=None, home=None):
    """Folder with the app's per-chat records (claude-code-sessions)."""
    d = claude_data_dir(platform, env, home)
    return (os.path.join if _platform(platform) == "win32" else posixpath.join)(d, SESSIONS)
