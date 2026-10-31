"""Budget modes, optional limit pacing and the (not yet used) calibration map. Autoswitch v2 only.

Mode: AUTOSWITCH_BUDGET, else the file $AUTOSWITCH_DIR/budget: quality | balanced (default) | save.

Why gates and not the formula: Step 1.9 measured the scorer on 150 SWE-bench issues and its p(haiku) had an
AUC of 0.50, so it is not a probability, and `p x saving > (1 - p) x miss cost x lambda` can't use it yet.
Each mode is therefore a pair of confidence gates (all configurable):
            down-switch gate   up-switch gate
  quality   0.80               0.40     switch down rarely, up readily
  balanced  0.70               0.50     today's gates (AUTOSWITCH_DOWN_THRESHOLD / _UP_THRESHOLD still apply)
  save      0.60               0.60     switch down readily, up reluctantly
Override with AUTOSWITCH_GATE_<MODE>_DOWN / _UP. The cost check (dwell guard, break-even) runs as well.

Calibration map (AUTOSWITCH_CALIBRATION = a JSON file {"points": [[raw, calibrated], ...]}): when set, the
scorer's confidence is mapped through it (piecewise linear) and the gates come from the formula instead:
  down if p > R*lambda / (S + R*lambda),  up if p > S / (S + R*lambda)
with S = the per-answer saving of the pair (AUTOSWITCH_SAVE_*), R = cost of a miss (AUTOSWITCH_MISS_COST,
placeholder $0.25) and lambda save 0.5 / balanced 1 / quality 3. Default: no file = identity = not used.
Step 5 is meant to fit the map from logs.

Limit (optional): AUTOSWITCH_LIMIT, else the file `limit`: "5/day", "$20/week" (dollars) or "80%/week"
(share of the plan). Usage comes from $AUTOSWITCH_DIR/usage.json, written by the Watcher (not built yet):
  {"ts": epoch, "period": "day"|"week", "period_start": epoch, "period_end": epoch, "used_usd": x, "used_pct": y}
pace = used / (limit x elapsed share of the period). Ahead of pace (> 1.10) moves one mode toward save,
behind (< 0.80) one mode toward quality. No file, a stale file (> 1 h), another period, or the first 5% of
the period: no adjustment.
"""
import datetime
import json
import os
import re
import time

import sbqueue as sb
from policy import _f, cfg

MODES = ("quality", "balanced", "save")
DEFAULT_GATES = {"quality": (0.80, 0.40), "save": (0.60, 0.60)}
LAMBDA = {"save": 0.5, "balanced": 1.0, "quality": 3.0}


def _setting(env, fname):
    v = os.environ.get(env)
    if v is None:
        try:
            with open(sb.path(fname)) as f:
                v = f.read().strip()
        except OSError:
            v = ""
    return v.strip()


def mode():
    v = _setting("AUTOSWITCH_BUDGET", "budget").lower()
    return v if v in MODES else "balanced"


_LIMIT = re.compile(r"^\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(%?)\s*(?:/|per\s+)\s*(day|week)$", re.I)


def limit():
    """{"amount", "unit": "usd"|"pct", "period"} or None."""
    m = _LIMIT.match(_setting("AUTOSWITCH_LIMIT", "limit"))
    if not m or float(m.group(1)) <= 0:
        return None
    return {"amount": float(m.group(1)), "unit": "pct" if m.group(2) else "usd", "period": m.group(3).lower()}


def _period_bounds(period, now):
    """Calendar period in local time: today from midnight, this week from Monday 00:00."""
    t = datetime.datetime.fromtimestamp(now)
    start = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= datetime.timedelta(days=start.weekday())
    end = start + datetime.timedelta(days=7 if period == "week" else 1)
    return start.timestamp(), end.timestamp()


def usage():
    try:
        with open(sb.path("usage.json")) as f:
            u = json.load(f)
        return u if isinstance(u, dict) else None
    except (OSError, ValueError):
        return None


def pace(lim, use, now=None):
    """used / (limit x elapsed share), or None when it can't be judged."""
    now = time.time() if now is None else now
    if not lim or not use:
        return None
    try:
        if now - float(use.get("ts", 0)) > _f("AUTOSWITCH_USAGE_MAX_AGE", "3600"):
            return None
        if use.get("period") and use["period"] != lim["period"]:
            return None
        used = use.get("used_pct" if lim["unit"] == "pct" else "used_usd")
        if used is None:
            return None
        start, end = _period_bounds(lim["period"], now)
        if use.get("period_start") and use.get("period_end"):
            start, end = float(use["period_start"]), float(use["period_end"])
        elapsed = (now - start) / (end - start)
        if not 0.05 <= elapsed <= 1:
            return None
        return float(used) / (lim["amount"] * elapsed)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def effective_mode(now=None):
    """(mode, note): the set mode, moved one step by the limit's pace."""
    m = mode()
    p = pace(limit(), usage(), now)
    if p is None:
        return m, None
    i = MODES.index(m)
    if p > _f("AUTOSWITCH_PACE_AHEAD", "1.10") and i < len(MODES) - 1:
        return MODES[i + 1], f"ahead of pace ({p:.2f}): {m} -> {MODES[i + 1]}"
    if p < _f("AUTOSWITCH_PACE_BEHIND", "0.80") and i > 0:
        return MODES[i - 1], f"behind pace ({p:.2f}): {m} -> {MODES[i - 1]}"
    return m, f"on pace ({p:.2f})"


def calibration():
    """Sorted [(raw, calibrated)] points, or None (identity: not used)."""
    p = os.environ.get("AUTOSWITCH_CALIBRATION")
    if not p:
        return None
    try:
        with open(os.path.expanduser(p)) as f:
            pts = sorted((float(a), float(b)) for a, b in json.load(f)["points"])
        return pts or None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def calibrate(p, pts):
    if not pts:
        return p
    if p <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if p <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (p - x0) / (x1 - x0)
    return pts[-1][1]


RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


def gates(m, cur=None, want=None, pts=None):
    """{"down", "up"} for mode m. With a calibration map, the formula's break-even probabilities."""
    c = cfg()
    if pts and cur in RANK and want in RANK and cur != want:
        lo, hi = sorted((cur, want), key=RANK.get)
        S = c["answer_saving"].get((hi, lo), 0.02)
        R = _f("AUTOSWITCH_MISS_COST", "0.25")
        lam = LAMBDA[m]
        return {"down": R * lam / (S + R * lam), "up": S / (S + R * lam)}
    if m == "balanced":
        return {"down": c["down"], "up": c["up"]}
    d, u = DEFAULT_GATES[m]
    return {"down": _f(f"AUTOSWITCH_GATE_{m.upper()}_DOWN", str(d)),
            "up": _f(f"AUTOSWITCH_GATE_{m.upper()}_UP", str(u))}
