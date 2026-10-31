"""How big a chat really is, in real tokens (Autoswitch v2, used only when AUTOSWITCH_HANDOFF is on).

Step 1 measured real tokens = characters / 4 x 1.6, one factor for Haiku, Sonnet and Opus
(AUTOSWITCH_TOKEN_FACTOR). The old estimate, transcript bytes / 4, runs about 5x too high on a typical chat
and up to 100x on chats with big tool logs, because the transcript also stores API request copies, metadata,
file snapshots and base64 images.

Two paths:
  cheap    file bytes / 4 / AUTOSWITCH_FILE_OVERCOUNT (5): free, only used for small files.
  precise  the conversation text itself: message text, tool calls and tool results of the main chat
           (sidechains, thinking, metadata and API request copies left out), restarting at each compaction
           boundary, plus a fixed AUTOSWITCH_IMAGE_TOKENS (1500) per image. A 64 MB transcript takes
           about 0.15 s on an M-series Mac.
The precise count runs when the file is at least AUTOSWITCH_PRECISE_BYTES (120 KB), or when asked for
(the zone around break-even). Why 120 KB: a file can never hold more real tokens than bytes x 1.6 / 4
(every byte a character), so below 120 KB a chat is under 48k real tokens, below every break-even zone, and
the cheap number is only used for the in-place cost check. The 5x overcount does not hold for every chat
(a chat of long pasted text is close to 1x), so the cheap number never decides near break-even.
Both numbers are returned so shadow.jsonl can log them side by side.
"""
import json
import os

from policy import _f


def factor():
    return _f("AUTOSWITCH_TOKEN_FACTOR", "1.6")


def cheap(path):
    """Real tokens guessed from the file size, or None if the file can't be read."""
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError, ValueError):
        return None
    return size / 4.0 / max(_f("AUTOSWITCH_FILE_OVERCOUNT", "5"), 0.01)


def _result_chars(content):
    if isinstance(content, str):
        return len(content), 0
    chars = imgs = 0
    for x in content or []:
        if not isinstance(x, dict):
            continue
        if x.get("type") == "text":
            chars += len(x.get("text") or "")
        elif x.get("type") == "image":
            imgs += 1
    return chars, imgs


def _typed(c):
    """A user entry that is a message (text), not a tool result."""
    if isinstance(c, str):
        return True
    blocks = [b for b in c or [] if isinstance(b, dict)]
    return any(b.get("type") == "text" for b in blocks) and not any(b.get("type") == "tool_result" for b in blocks)


def text_chars(path, marks=None):
    """(characters, images) of the conversation the model re-reads, or None if unreadable.
    marks: if a list, gets (characters, images) so far at the start of each typed user message (rollover's
    growth rate); cleared at a compaction like the count."""
    chars = imgs = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                if b'"compact_boundary"' in line and b'"system"' in line:
                    try:
                        if json.loads(line).get("subtype") == "compact_boundary":
                            chars = imgs = 0  # the model only re-reads what follows the compaction
                            if marks is not None:
                                del marks[:]
                    except ValueError:
                        pass
                    continue
                if b'"user"' not in line and b'"assistant"' not in line:
                    continue  # api-request copies, snapshots, metadata: never re-read by the model
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") not in ("user", "assistant") or d.get("isSidechain"):
                    continue
                c = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
                if marks is not None and d.get("type") == "user" and _typed(c):
                    marks.append((chars, imgs))
                if isinstance(c, str):
                    chars += len(c)
                    continue
                for b in c or []:
                    if not isinstance(b, dict):
                        continue
                    t = b.get("type")
                    if t == "text":
                        chars += len(b.get("text") or "")
                    elif t == "tool_use":
                        chars += len(json.dumps(b.get("input"))) + len(b.get("name") or "")
                    elif t == "tool_result":
                        n, i = _result_chars(b.get("content"))
                        chars += n
                        imgs += i
                    elif t == "image":
                        imgs += 1
    except (OSError, TypeError, ValueError):
        return None
    return chars, imgs


def precise(path):
    """Real tokens counted from the conversation text, or None if unreadable."""
    r = text_chars(path)
    if r is None:
        return None
    chars, imgs = r
    return chars / 4.0 * factor() + imgs * _f("AUTOSWITCH_IMAGE_TOKENS", "1500")


def estimate(path, force_precise=False):
    """{"tokens", "cheap", "precise", "method"}: tokens is the number to decide with (real tokens), None if unknown.
    method: "cheap" | "precise" | "unknown"."""
    c = cheap(path)
    if c is None:
        return {"tokens": None, "cheap": None, "precise": None, "method": "unknown"}
    p = None
    try:
        big = os.path.getsize(path) >= _f("AUTOSWITCH_PRECISE_BYTES", "120000")
    except OSError:
        big = False
    if big or force_precise:
        p = precise(path)
    if p is not None:
        return {"tokens": p, "cheap": c, "precise": p, "method": "precise"}
    return {"tokens": c, "cheap": c, "precise": None, "method": "cheap"}
