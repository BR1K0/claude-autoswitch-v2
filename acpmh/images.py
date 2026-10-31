"""Screenshots in transcripts: decode, keep the image file, read its text.

Claude Code stores pasted images (and images returned by tools) as base64 blocks.
Each one is kept as blobs/<sha256>.<ext> so the next model can open it, and its
text is read locally with macOS Vision (ocr.swift, built once with swiftc) so it
is searchable and shows up in the handoff. No network, no model calls. Without
swiftc (or off macOS) the image is still stored, just without text.
"""
import base64
import binascii
import json
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "ocr.swift")
BIN = os.path.expanduser(os.environ.get("ACPMH_OCR_BIN", "~/.acpmh/bin/acpmh-ocr"))
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif",
       "image/webp": "webp", "image/tiff": "tiff", "image/heic": "heic"}
OCR_CHARS = 4000
RETRY_BUILD = 86400  # after a failed build, try again after a day


def decode(block):
    """(bytes, media_type) from an image block. Raises ValueError if unusable."""
    src = block.get("source") or {}
    if src.get("type") != "base64" or not src.get("data"):
        raise ValueError("no embedded image data (source type %r)" % src.get("type"))
    try:
        data = base64.b64decode(src["data"], validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
        raise ValueError("bad base64: %s" % e)
    if not data:
        raise ValueError("empty image")
    return data, src.get("media_type") or "image/png"


def ext_for(media_type):
    return EXT.get(media_type, "img")


def _binary():
    """Path to the OCR helper, building it if needed. None if OCR is unavailable."""
    if os.environ.get("ACPMH_OCR") == "off":
        return None
    if os.path.exists(BIN):
        return BIN
    failed = BIN + ".failed"
    if os.path.exists(failed) and time.time() - os.path.getmtime(failed) < RETRY_BUILD:
        return None
    try:
        os.makedirs(os.path.dirname(BIN), exist_ok=True)
        r = subprocess.run(["swiftc", "-O", SRC, "-o", BIN + ".tmp"],
                           capture_output=True, timeout=300)
        if r.returncode == 0:
            os.replace(BIN + ".tmp", BIN)
            return BIN
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        open(failed, "w").close()
    except OSError:
        pass
    return None


def ocr(paths, timeout_each=20):
    """{path: text} for the images that could be read; {} if OCR is unavailable."""
    b = _binary() if paths else None
    if not b:
        return {}
    try:
        r = subprocess.run([b] + list(paths), capture_output=True, text=True,
                           timeout=timeout_each * len(paths) + 10)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("ok"):
            out[d["file"]] = d.get("text", "")
    return out


def ocr_available():
    return _binary() is not None
