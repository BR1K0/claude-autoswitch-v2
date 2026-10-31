"""Screenshot awareness for the Autoswitch hook.

The UserPromptSubmit hook only receives the typed text: no image, no placeholder, and the message is not in
the transcript yet. A block + re-send therefore drops every pasted screenshot. The one signal left on the
Mac is the system pasteboard, which still holds the pasted image when the hook runs. So:
  - clipboard_image() asks clipimg (Swift, Vision OCR, ~0.2s) whether the pasteboard holds an image and what
    text is in it;
  - a "fresh" image (new pasteboard change, or seen within IMAGE_WINDOW seconds) marks the prompt as
    possibly carrying an image. The hook must then NEVER block it; it scores typed text + OCR text instead.
Failure direction: a stale clipboard image only costs a skipped switch; a missed image would cost the image.
Images that did not come through the clipboard (drag and drop, file picker) are not detectable.
"""
import json
import os
import shlex
import subprocess
import sys
import time

import sbqueue as sb

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "clipimg.swift")
IMAGE_WINDOW = float(os.environ.get("AUTOSWITCH_IMAGE_WINDOW", "300"))  # seconds a clipboard image stays "fresh"
OCR_CHARS = int(os.environ.get("AUTOSWITCH_OCR_CHARS", "1500"))
BUDGET = float(os.environ.get("AUTOSWITCH_IMAGE_BUDGET", "1.5"))  # seconds for the whole image step, clipimg + fallback
RETRY_BUILD = 86400  # after a failed swiftc build, try again after a day


def binary():
    return os.environ.get("AUTOSWITCH_CLIPIMG") or sb.path("bin", "clipimg")


LOCK_MAX_AGE = 600  # seconds; a build is killed by `timeout 240` where there is one


def _builder_alive(lock):
    """True while the build that wrote this lock may still be running. The lock holds the build's pid (written right
    after it starts); a hung swiftc keeps its pid alive, so a second build is never started next to it."""
    try:
        with open(lock, encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        pid = 0
    try:
        age = time.time() - os.path.getmtime(lock)
    except OSError:
        return False
    if pid > 0:
        if age >= LOCK_MAX_AGE:
            return False  # older than any build (timeout 240 + slack): a reused pid after a reboot or kill -9
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False  # the build was killed before it could remove its lock
        except OSError:
            return True  # exists, owned by someone else
    return age < 300  # no pid yet (the build is just starting): trust a recent lock


def _build_in_background():
    """Compile clipimg once, detached, so the hook never waits for swiftc. Until it exists, detect only."""
    out = sb.path("bin", "clipimg")
    try:
        os.makedirs(sb.path("bin"), exist_ok=True)
        lock, failed = sb.path("bin", "building"), sb.path("bin", "failed")
        if _builder_alive(lock):
            return
        if os.path.exists(failed) and time.time() - os.path.getmtime(failed) < RETRY_BUILD:
            return  # swiftc missing or failing: do not respawn it on every prompt
        with open(lock, "w"):
            pass
        # the outer sh backgrounds the build (which first records its pid in the lock) and exits at once, so the hook can reap it
        # and never waits for swiftc. `timeout 240` caps a hung swiftc where coreutils has it (not stock macOS);
        # without it the live pid in the lock still keeps a second build from starting.
        build = (f'echo $$ > "{lock}"; T=; command -v timeout >/dev/null 2>&1 && T="timeout 240"; '
                 f'if $T swiftc -O "{SRC}" -o "{out}.tmp" && mv "{out}.tmp" "{out}"; then rm -f "{failed}"; '
                 f'else rm -f "{out}.tmp"; touch "{failed}"; fi; rm -f "{lock}"')
        script = "/bin/sh -c " + shlex.quote(build) + " &"
        subprocess.run(["/bin/sh", "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, start_new_session=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _osascript_has_image(timeout):
    """Detection only (no OCR) for the first prompts after install, before clipimg is built."""
    try:
        r = subprocess.run(["osascript", "-e", "clipboard info"], capture_output=True, text=True, timeout=timeout)
        info = r.stdout
        return {"count": None, "image": any(t in info for t in ("PNGf", "TIFF", "JPEG")), "text": ""}
    except Exception:
        return None


# Windows clipboard formats an image arrives in: CF_BITMAP, CF_DIB, CF_DIBV5 (plus the registered "PNG")
WIN_IMAGE_FORMATS = (2, 8, 17)


def _windows_clipboard(user32=None):
    """Windows: {"count", "image", "text": ""} from the clipboard, via ctypes (no subprocess, a few ms).
    `count` is the clipboard sequence number, so "a new copy" is detected like the Mac change count.
    There is no OCR on Windows (yet): a screenshot is detected, never read, so its prompt is scored on the
    typed text alone ("unclear") and is never blocked. Returns None if it cannot be read (never raises)."""
    try:
        if user32 is None:
            import ctypes
            user32 = ctypes.windll.user32
            user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        formats = list(WIN_IMAGE_FORMATS)
        formats.append(user32.RegisterClipboardFormatW("PNG"))
        return {"count": int(user32.GetClipboardSequenceNumber()) & 0xFFFFFFFF,
                "image": any(user32.IsClipboardFormatAvailable(f) for f in formats), "text": ""}
    except Exception:
        return None


def read_clipboard(ocr=True, deadline=None):
    """{"count", "image", "text"} from the pasteboard, or None if it cannot be read (never raises).
    Every subprocess is bounded by `deadline` (a time.time() value) so the whole step stays within BUDGET."""
    if sys.platform == "win32":
        return _windows_clipboard()
    if sys.platform != "darwin":
        return None  # no clipboard reader: screenshots are not detected, nothing is ever blocked because of them
    deadline = deadline or time.time() + BUDGET
    left = lambda: max(0.05, deadline - time.time())
    b = binary()
    if not os.path.exists(b):
        if not os.environ.get("AUTOSWITCH_CLIPIMG"):
            _build_in_background()
        return _osascript_has_image(left())
    try:
        r = subprocess.run([b, "ocr" if ocr else "info"], capture_output=True, text=True, timeout=left())
        d = json.loads(r.stdout)
        d.setdefault("text", "")
        return d
    except Exception:
        return _osascript_has_image(left()) if time.time() < deadline else None


def _state(new=None):
    p = sb.path("clip.json")
    if new is not None:
        try:
            os.makedirs(sb.BASE, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(new, f)
        except OSError:
            pass
        return new
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def fresh_image(now=None):
    """The clipboard image if it looks pasted into this prompt, else None.

    Fresh = the pasteboard changed since the last prompt we looked at, or the same image was first seen less
    than IMAGE_WINDOW seconds ago (the user may paste the same screenshot into a second prompt).
    Returns {"text": OCR text (capped), "image": True} or None.
    """
    now = now or time.time()
    clip = read_clipboard()
    prev = _state()
    if not clip or not clip.get("image"):
        _state({"count": clip and clip.get("count"), "image": False, "first_seen": None})
        return None
    count = clip.get("count")
    same = count is not None and prev.get("count") == count and prev.get("image")
    first_seen = prev.get("first_seen") if same else now
    _state({"count": count, "image": True, "first_seen": first_seen})
    if same and now - (first_seen or now) > IMAGE_WINDOW:
        return None  # an old screenshot still sitting on the clipboard
    return {"image": True, "text": (clip.get("text") or "").strip()[:OCR_CHARS]}
