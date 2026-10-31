"""Pure routing policy: given a score and the chat's state, decide switch / stay. No I/O."""
import os
import sys

MODELS = {  # scorer label -> app model id
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


_NAME = 'autoswitch'


def _f(name, default):
    """A float env setting. A malformed value ("40%", "0,4", nan) warns once on stderr and falls back to the
    default: one bad setting must not raise inside cfg(), which would make the hook fail open and stop all routing."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        v = float(raw)
        if v == v and v not in (float("inf"), float("-inf")):
            return v
    except ValueError:
        pass
    print(f"{_NAME}: ignoring {name}={raw!r}, using {default}", file=sys.stderr)
    return float(default)


def cfg():
    """Read tunables at call time so tests and env changes take effect."""
    return {
        # gate: a wrong down-switch (hard question on a weak model) costs more than a wrong up-switch
        "down": _f("AUTOSWITCH_DOWN_THRESHOLD", os.environ.get("AUTOSWITCH_THRESHOLD", "0.70")),
        "up": _f("AUTOSWITCH_UP_THRESHOLD", "0.50"),
        # an Opus-leaning score between this and `up` is too weak for Opus but still says "harder than Haiku":
        # a chat on Haiku goes up one tier, to Sonnet
        "lean": _f("AUTOSWITCH_LEAN_THRESHOLD", "0.40"),
        # unclear requests (no tier passes its gate): "sonnet" = move the chat to Sonnet, "stay" = leave it
        "unclear": os.environ.get("AUTOSWITCH_UNCLEAR", "sonnet"),
        # dwell guard. Cache-write cost of re-caching 50k tokens on the NEW model. Sonnet/Opus are from
        # Anthropic's published cache-write prices; Haiku is an estimate (about a third of Sonnet).
        "recache50k": {"haiku": _f("AUTOSWITCH_RECACHE_HAIKU", "0.04"),
                       "sonnet": _f("AUTOSWITCH_RECACHE_SONNET", "0.13"),
                       "opus": _f("AUTOSWITCH_RECACHE_OPUS", "0.25")},
        # rough estimates (not measured): dollars saved per answer by running on the cheaper tier,
        "answer_saving": {("opus", "sonnet"): _f("AUTOSWITCH_SAVE_OPUS_SONNET", "0.05"),
                          ("opus", "haiku"): _f("AUTOSWITCH_SAVE_OPUS_HAIKU", "0.08"),
                          ("sonnet", "haiku"): _f("AUTOSWITCH_SAVE_SONNET_HAIKU", "0.02")},
        "read_fraction": _f("AUTOSWITCH_READ_FRACTION", "0.1"),    # cached-read price as a share of write price
        "expected_turns": _f("AUTOSWITCH_EXPECTED_TURNS", "5"),    # turns the cheaper model is expected to serve
        "max_down_tokens": _f("AUTOSWITCH_MAX_DOWN_TOKENS", "60000"),  # never switch down deeper than this
        "short_chat_tokens": _f("AUTOSWITCH_SHORT_CHAT_TOKENS", "20000"),  # the unclear->Sonnet default needs this or less
    }


def tier_of(model):
    m = (model or "").lower()
    return next((t for t in MODELS if t in m), None)


def dwell_ok(cur, tgt, tokens, c=None):
    """True if a down-switch cur->tgt at `tokens` of history is worth the re-cache. Returns (ok, why)."""
    c = c or cfg()
    if tokens is None:
        return False, "history size unknown"
    if tokens > c["max_down_tokens"]:
        return False, f"history {int(tokens)} tokens > cap {int(c['max_down_tokens'])}"
    recache = c["recache50k"][tgt] * tokens / 50000
    per_turn = c["answer_saving"][(cur, tgt)] + (c["recache50k"][cur] - c["recache50k"][tgt]) * (tokens / 50000) * c["read_fraction"]
    saving = per_turn * c["expected_turns"]
    if saving < recache:
        return False, f"saving ${saving:.3f} < re-cache ${recache:.3f}"
    return True, f"saving ${saving:.3f} >= re-cache ${recache:.3f}"


def decide(label, confidence, current_model, tokens=None, pinned=False, enforce=True, is_new=False,
           overrides=None, dwell=True):
    """Return {"target": model id or None, "reason": str}. `enforce` only matters for the Sonnet default.
    `tokens` None means the history size is unknown. `is_new`: the chat has no answer yet (its transcript is
    missing or has no assistant turn); an existing chat whose transcript can't be read is not new.
    Autoswitch v2 only (both default to today's behaviour): `overrides` replaces cfg() entries (the budget
    mode's gates, the cap); dwell=False skips the dwell guard, to learn which tier the gates want."""
    cur = tier_of(current_model)
    if cur is None:
        return {"target": None, "reason": "unknown current model"}
    if pinned:
        return {"target": None, "reason": "pinned: model was picked by hand"}
    c = cfg()
    if overrides:
        c.update(overrides)
    want = label if label in MODELS else None
    lean_up = False
    if want and want != cur:
        need = c["up"] if RANK[want] > RANK[cur] else c["down"]
        if confidence < need:
            if (enforce and c["unclear"] == "sonnet" and want == "opus" and cur == "haiku"
                    and confidence >= min(c["lean"], c["up"])
                    and (tokens <= c["short_chat_tokens"] if tokens is not None else is_new)):  # unknown size: only a brand-new chat
                want, lean_up = "sonnet", True  # leaning Opus but not sure: one tier up
            else:
                want = None
    elif want == cur:
        return {"target": None, "reason": "already on scored tier"}
    if want is None:  # unclear: Sonnet is the default rather than Opus, enforce mode only
        if not enforce or c["unclear"] != "sonnet" or cur == "sonnet":
            return {"target": None, "reason": "below gate, staying"}
        # only a new or short chat: switching a long chat re-caches all of its history on the new model
        if tokens is None or tokens > c["short_chat_tokens"]:
            return {"target": None, "reason": "unclear, chat is long or its size is unknown: staying"}
        want, why = "sonnet", "unclear, short chat: default Sonnet"
    elif lean_up:
        why = f"leaning {label} at {confidence:.0%}: one tier up to Sonnet"
    else:
        why = f"{label} at {confidence:.0%}"
    if dwell and RANK[want] < RANK[cur]:
        ok, dwell = dwell_ok(cur, want, tokens, c)
        if not ok:
            return {"target": None, "reason": f"dwell guard: {dwell}"}
    return {"target": MODELS[want], "reason": why}
