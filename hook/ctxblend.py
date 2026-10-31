"""Context-aware scoring, the "limited blend" measured in docs/BENCHMARK.md.

On when AUTOSWITCH_HANDOFF is on, unless AUTOSWITCH_CONTEXT=off. It replaces the follow-up rule (a
follow-up taking the previous request's score) for chats where it is on.
  - only messages of at most 20 words get context; a longer message is scored alone;
  - context = the current task (the chat's title) + the last 3 user messages;
  - p = 0.7 x p(message alone) + 0.3 x p(message + context);
  - context may raise p(haiku) by at most 0.05 (it can freely make a harder tier likelier);
  - a message with 3+ content words that share none with the context is a topic change: scored alone.
On the author's last 29 messages it cut wrong down-switches from 6 to 0 (docs/BENCHMARK.md).
Probabilities are lists in the order haiku, sonnet, opus.
"""
import json
import os
import re

NAMES = ["haiku", "sonnet", "opus"]
WORD = re.compile(r"[a-z]{4,}")
STOP = set("that this what with have from your just like will would could should about "
           "there their them then than when where which while been were they does into "
           "also only some more much very make made need want".split())
MAX_WORDS = 20
TAIL_BYTES = 2 * 1024 * 1024


def content(t):
    return {w for w in WORD.findall(t.lower()) if w not in STOP}


def vec(probs):
    return [float((probs or {}).get(n, 0.0)) for n in NAMES]


def limited(p_alone, p_ctx, text, ctx):
    """Exactly context_test.limited(), on plain lists."""
    if len(text.split()) > MAX_WORDS:
        return list(p_alone)
    words = content(text)
    if len(words) >= 3 and not words & content(ctx):
        return list(p_alone)  # topic change
    p = [0.7 * a + 0.3 * c for a, c in zip(p_alone, p_ctx)]
    if p[0] > p_alone[0] + 0.05:  # context made Haiku likelier: allow only a little
        s = p[1] + p[2]
        rest = [p[1] / s, p[2] / s] if s else [0.5, 0.5]
        h = p_alone[0] + 0.05
        p = [h, rest[0] * (1 - h), rest[1] * (1 - h)]
    return p


def wants_context(text, ctx):
    """True if limited() would use the context score at all (so the second scorer call can be skipped)."""
    if not ctx or len(text.split()) > MAX_WORDS:
        return False
    words = content(text)
    return not (len(words) >= 3 and not words & content(ctx))


def recent_user_messages(transcript_path, n=3):
    """The last n typed user messages, read from the transcript's tail (the current prompt is not in it yet)."""
    try:
        from context import _user_text
    except ImportError:
        return []
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return []
    out = []
    for line in reversed(tail):
        if '"user"' not in line:
            continue
        try:
            u = _user_text(json.loads(line))
        except (ValueError, AttributeError):
            continue
        if u:
            out.append(u)
            if len(out) == n:
                break
    return out[::-1]


def build_context(title, previous):
    task = f"Current task: {title}." if title else ""
    return "\n".join([task] + list(previous)).strip()


def new_task(text, previous, title=None):
    """A message that starts a new task: long, self-contained (not a follow-up) and on a new topic
    (under AUTOSWITCH_NEWTASK_OVERLAP of its content words appear in the recent context)."""
    from policy import _f
    try:
        from context import is_followup
    except ImportError:
        def is_followup(_):
            return False
    if len(text.split()) < _f("AUTOSWITCH_NEWTASK_WORDS", "25") or is_followup(text):
        return False
    words = content(text)
    if len(words) < 3:
        return False
    shared = words & content(build_context(title, previous))
    return len(shared) / len(words) < _f("AUTOSWITCH_NEWTASK_OVERLAP", "0.2")
