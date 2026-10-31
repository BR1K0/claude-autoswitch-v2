"""What each budget mode (save / balanced / quality) saves and what it risks, on labelled held-out prompts.

  python3 bench/modes.py [--scorer http://127.0.0.1:8765] [--data data/design_test.jsonl ...] [--json out.json]

Each prompt is scored once by a running scorer (the same request hint.py sends: {"text", "input"}). Then, for
every mode and two starting models (a chat on Opus 5.5, a chat on Haiku 5.5), the prompt goes through the real
decision path of a known, short chat: policy.decide with that mode's gates from budget.gates, exactly as
hint._decide_v2 does for an in-place switch (cost check on, a short history so it never blocks). The tier the
chat ends on is compared with the prompt's label (the cheapest model it needs).

Cost per message, steady state on the chosen model (the one-time re-cache on a switch is not counted):
  typical: 30k cached context + 1k new input + 800 output; long: 150k cached context, same input/output.
Prices per MTok (input / output / cache read): Opus 5.5 4 / 20 / 0.20, Sonnet 5.5 2 / 10 / 0.20,
Haiku 5.5 0.10 / 0.50 / 0.01 up to 100k context and 0.50 / 2.50 / 0.05 above. Haiku's cache-read price is not
published in our reference: we assume the usual 0.1x of input.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hook"))

for k in [k for k in os.environ if k.startswith("AUTOSWITCH_")]:
    if k != "AUTOSWITCH_SCORER":
        del os.environ[k]  # default gates and costs only: a local override must not change the numbers

import budget  # noqa: E402
import policy  # noqa: E402
from context import model_input  # noqa: E402
from hint import score  # noqa: E402

TIERS = ("haiku", "sonnet", "opus")
RANK = {t: i for i, t in enumerate(TIERS)}
PRICE = {  # $/MTok: (input, output, cache read), (<=100k context, >100k context)
    "opus": ((4.0, 20.0, 0.20), (4.0, 20.0, 0.20)),
    "sonnet": ((2.0, 10.0, 0.20), (2.0, 10.0, 0.20)),
    "haiku": ((0.10, 0.50, 0.01), (0.50, 2.50, 0.05)),
}
SCENARIOS = {"typical (30k context)": 30_000, "long (150k context)": 150_000}
NEW_IN, OUT = 1_000, 800
HISTORY_TOKENS = 5_000  # a short chat: the cost check passes and an unclear prompt gets the Sonnet default


def cost(tier, context):
    i, o, r = PRICE[tier][context + NEW_IN > 100_000]
    return (context * r + NEW_IN * i + OUT * o) / 1e6


def rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def chosen(label, conf, mode, start):
    g = budget.gates(mode)
    ov = {"down": g["down"], "up": g["up"], "max_down_tokens": float("inf")}
    d = policy.decide(label, conf, policy.MODELS[start], HISTORY_TOKENS, enforce=True, overrides=ov)
    return policy.tier_of(d["target"]) or start


def summarize(truth, picks):
    n = len(truth)
    pct = lambda k: 100.0 * k / n if n else 0.0  # noqa: E731
    pairs = list(zip(truth, picks))
    hard = [p for t, p in pairs if t == "opus"]
    s = {
        "n": n,
        "exact": pct(sum(t == p for t, p in pairs)),
        "under": pct(sum(RANK[p] < RANK[t] for t, p in pairs)),
        "opus->haiku": pct(sum(t == "opus" and p == "haiku" for t, p in pairs)),
        "opus->sonnet": pct(sum(t == "opus" and p == "sonnet" for t, p in pairs)),
        "sonnet->haiku": pct(sum(t == "sonnet" and p == "haiku" for t, p in pairs)),
        "over": pct(sum(RANK[p] > RANK[t] for t, p in pairs)),
        "hard_n": len(hard),
        "hard_wrong": 100.0 * sum(p != "opus" for p in hard) / len(hard) if hard else None,
        "picks": {t: picks.count(t) for t in TIERS},
        "cost": {},
    }
    for name, ctx in SCENARIOS.items():
        avg = sum(cost(p, ctx) for p in picks) / n
        s["cost"][name] = {"avg": avg, "saved_vs_opus": 100.0 * (1 - avg / cost("opus", ctx)),
                           "x_haiku": avg / cost("haiku", ctx)}
    return s


def run(scorer, paths):
    data = []
    for p in paths:
        for r in rows(p):
            res = score(scorer, json.dumps({"text": r["text"], "input": model_input(r["text"])}).encode(), timeout=10)
            lab = res["label"]
            data.append({"set": os.path.basename(p), "label": r["label"], "scored": lab, "conf": res["probs"][lab]})
    out = {"scorer_accuracy": 100.0 * sum(d["label"] == d["scored"] for d in data) / len(data), "results": {}}
    truth = [d["label"] for d in data]
    for t in TIERS:
        out["results"][f"always {t}"] = summarize(truth, [t] * len(data))
    for mode in ("save", "balanced", "quality"):
        for start in ("opus", "haiku"):
            picks = [chosen(d["scored"], d["conf"], mode, start) for d in data]
            out["results"][f"{mode}, start {start}"] = summarize(truth, picks)
    out["n"] = len(data)
    out["labels"] = {t: truth.count(t) for t in TIERS}
    out["gates"] = {m: budget.gates(m) for m in ("save", "balanced", "quality")}
    return out


def table(out):
    f = lambda x: "-" if x is None else f"{round(x)}%"  # noqa: E731
    lines = [f"{out['n']} prompts, labels {out['labels']}, scorer top-label accuracy {out['scorer_accuracy']:.0f}%", "",
             "| run | exact | too weak | opus->haiku | opus->sonnet | sonnet->haiku | too strong | hard Qs on a weaker model "
             "| saved vs Opus (30k) | saved vs Opus (150k) | x Haiku cost (30k) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in out["results"].items():
        c1, c2 = s["cost"]["typical (30k context)"], s["cost"]["long (150k context)"]
        lines.append(f"| {k} | {f(s['exact'])} | {f(s['under'])} | {f(s['opus->haiku'])} | {f(s['opus->sonnet'])} "
                     f"| {f(s['sonnet->haiku'])} | {f(s['over'])} | {f(s['hard_wrong'])} | {f(c1['saved_vs_opus'])} "
                     f"| {f(c2['saved_vs_opus'])} | {c1['x_haiku']:.1f}x |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scorer", default=os.environ.get("AUTOSWITCH_SCORER", "http://127.0.0.1:8765"))
    ap.add_argument("--data", nargs="+", default=[os.path.join(ROOT, "data", "design_test.jsonl")])
    ap.add_argument("--json", help="also write the raw results here")
    a = ap.parse_args()
    for name, ctx in SCENARIOS.items():
        print(f"{name}: per message Opus ${cost('opus', ctx):.4f}, Sonnet ${cost('sonnet', ctx):.4f}, "
              f"Haiku ${cost('haiku', ctx):.4f}")
    out = run(a.scorer, a.data)
    print(table(out))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
