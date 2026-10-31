# Measured trade-offs per budget mode

What each budget mode (save / balanced / quality) saves and what it risks, measured on labelled prompts that the
scorer was not trained on. The short version is in the [README](../README.md#what-each-mode-costs-you); this page has
the raw numbers. Reproduce the public-scorer numbers with:

```bash
python3 train/train.py --out /tmp/models          # the models install.sh trains
python3 scorer/scorer.py --port 8799 --models /tmp/models &
python3 bench/modes.py --scorer http://127.0.0.1:8799 --data data/design_test.jsonl
```

## Method

- **Decision path.** Each prompt is scored once by a running scorer, with the same request the hook sends
  (`{"text", "input": model_input(text)}`). For each mode and each starting model (a chat on Opus 5.5, a chat on
  Haiku 5.5) the score goes through `policy.decide` with that mode's gates from `budget.gates`, exactly as
  `hint._decide_v2` does for an in-place switch. The chat is a known, short one (5k tokens of history), so the
  cost check (dwell guard) and handoffs never block, and an unclear prompt gets the "unclear: default Sonnet" rule.
  Default gates: save 0.60 down / 0.60 up, balanced 0.70 / 0.50, quality 0.80 / 0.40; lean-to-Sonnet 0.40.
- **Label.** The cheapest model the prompt needs. *Too weak* = sent to a weaker tier than its label (split
  opus->haiku, opus->sonnet, sonnet->haiku); *too strong* = overspend; *hard Qs on a weaker model* = share of the
  opus-labelled prompts that did not end on Opus.
- **Cost per message**, steady state on the chosen model; the one-time re-cache after a switch is not counted.
  Typical message: 30k cached context + 1k new input + 800 output. Long: 150k cached context, same input/output.

  | per MTok | input | output | cache read | per typical message (30k) | per long message (150k) |
  |---|---|---|---|---|---|
  | Opus 5.5 | $4 | $20 | $0.20 | $0.0260 | $0.0500 |
  | Sonnet 5.5 | $2 | $10 | $0.20 | $0.0160 | $0.0400 |
  | Haiku 5.5 (<=100k) | $0.10 | $0.50 | $0.01 (assumed) | $0.0008 | - |
  | Haiku 5.5 (>100k) | $0.50 | $2.50 | $0.05 (assumed) | - | $0.0100 |

  Haiku 5.5's cache-read price is not in our pricing reference; we assume the usual 0.1x of input. Haiku 5.5's
  tokenizer also counts the same text as about 30% more tokens, which is not applied here.
  Note that at these prices Sonnet costs 62% of Opus per typical message (cache reads cost the same on both), so a
  switch from Opus to Sonnet saves much less than one to Haiku.

## Prompt sets (held-out only)

| set | prompts | labels | where from | in this repo |
|---|---|---|---|---|
| design test | 36 | 6 haiku, 13 sonnet, 17 opus | `data/design_test.jsonl`: hand-labelled design/architecture prompts, the held-out half of the design set (`design_train.jsonl` is used for training and is not used here) | yes |
| coding tasks | 30 | 30 haiku | the maintainer's local coding benchmark: 10 easy, 10 medium, 10 hard small coding tasks with hidden tests; Haiku passed all 30 in a measured run, so each is labelled haiku | no (prompts not published) |
| combined | 66 | 36 haiku, 13 sonnet, 17 opus | both of the above | partly |

Not used: `data/synthetic_train.jsonl` and `data/design_train.jsonl` (training data) and `data/swe_labeled.jsonl`
(150 SWE-bench issues, training data for the public scorer's second model). No design-test prompt appears verbatim
in any training file we checked.

## Two scorers

- **Public scorer**: `scorer/scorer.py` with models trained by `train/train.py` from this repo's data (what
  `install.sh` sets up). The README table uses this one.
- **Personal scorer**: the maintainer's own scorer, half trained on the same kind of synthetic prompts and half on
  their own past messages (not public). Shown because it behaves differently, which says something about how much
  the results depend on the scorer.

## Public scorer

### Combined (66)

66 prompts, labels {'haiku': 36, 'sonnet': 13, 'opus': 17}, scorer top-label accuracy 62%

| run | exact | too weak | opus->haiku | opus->sonnet | sonnet->haiku | too strong | hard Qs on a weaker model | saved vs Opus (30k) | saved vs Opus (150k) | x Haiku cost (30k) |
|---|---|---|---|---|---|---|---|---|---|---|
| always haiku | 55% | 45% | 26% | 0% | 20% | 0% | 100% | 97% | 80% | 1.0x |
| always sonnet | 20% | 26% | 0% | 26% | 0% | 55% | 100% | 38% | 20% | 20.0x |
| always opus | 26% | 0% | 0% | 0% | 0% | 74% | 0% | 0% | 0% | 32.5x |
| save, start opus | 58% | 2% | 0% | 2% | 0% | 41% | 6% | 29% | 19% | 23.1x |
| save, start haiku | 61% | 3% | 0% | 3% | 0% | 36% | 12% | 35% | 24% | 21.1x |
| balanced, start opus | 53% | 2% | 0% | 2% | 0% | 45% | 6% | 26% | 16% | 24.0x |
| balanced, start haiku | 62% | 2% | 0% | 2% | 0% | 36% | 6% | 33% | 23% | 21.7x |
| quality, start opus | 48% | 2% | 0% | 2% | 0% | 50% | 6% | 24% | 14% | 24.8x |
| quality, start haiku | 62% | 2% | 0% | 2% | 0% | 36% | 6% | 32% | 22% | 22.2x |

### Design test (36)

36 prompts, labels {'haiku': 6, 'sonnet': 13, 'opus': 17}, scorer top-label accuracy 94%

| run | exact | too weak | opus->haiku | opus->sonnet | sonnet->haiku | too strong | hard Qs on a weaker model | saved vs Opus (30k) | saved vs Opus (150k) | x Haiku cost (30k) |
|---|---|---|---|---|---|---|---|---|---|---|
| always haiku | 17% | 83% | 47% | 0% | 36% | 0% | 100% | 97% | 80% | 1.0x |
| always sonnet | 36% | 47% | 0% | 47% | 0% | 17% | 100% | 38% | 20% | 20.0x |
| always opus | 47% | 0% | 0% | 0% | 0% | 53% | 0% | 0% | 0% | 32.5x |
| save, start opus | 94% | 3% | 0% | 3% | 0% | 3% | 6% | 28% | 19% | 23.3x |
| save, start haiku | 92% | 6% | 0% | 6% | 0% | 3% | 12% | 31% | 20% | 22.6x |
| balanced, start opus | 92% | 3% | 0% | 3% | 0% | 6% | 6% | 27% | 17% | 23.8x |
| balanced, start haiku | 94% | 3% | 0% | 3% | 0% | 3% | 6% | 29% | 19% | 22.9x |
| quality, start opus | 89% | 3% | 0% | 3% | 0% | 8% | 6% | 25% | 16% | 24.3x |
| quality, start haiku | 94% | 3% | 0% | 3% | 0% | 3% | 6% | 28% | 19% | 23.3x |

### Coding tasks (30)

30 prompts, labels {'haiku': 30, 'sonnet': 0, 'opus': 0}, scorer top-label accuracy 23%

| run | exact | too weak | opus->haiku | opus->sonnet | sonnet->haiku | too strong | hard Qs on a weaker model | saved vs Opus (30k) | saved vs Opus (150k) | x Haiku cost (30k) |
|---|---|---|---|---|---|---|---|---|---|---|
| always haiku | 100% | 0% | 0% | 0% | 0% | 0% | - | 97% | 80% | 1.0x |
| always sonnet | 0% | 0% | 0% | 0% | 0% | 100% | - | 38% | 20% | 20.0x |
| always opus | 0% | 0% | 0% | 0% | 0% | 100% | - | 0% | 0% | 32.5x |
| save, start opus | 13% | 0% | 0% | 0% | 0% | 87% | - | 30% | 19% | 22.9x |
| save, start haiku | 23% | 0% | 0% | 0% | 0% | 77% | - | 41% | 28% | 19.3x |
| balanced, start opus | 7% | 0% | 0% | 0% | 0% | 93% | - | 26% | 15% | 24.2x |
| balanced, start haiku | 23% | 0% | 0% | 0% | 0% | 77% | - | 38% | 27% | 20.2x |
| quality, start opus | 0% | 0% | 0% | 0% | 0% | 100% | - | 22% | 11% | 25.4x |
| quality, start haiku | 23% | 0% | 0% | 0% | 0% | 77% | - | 35% | 25% | 21.0x |

## Personal scorer

### Combined (66)

66 prompts, labels {'haiku': 36, 'sonnet': 13, 'opus': 17}, scorer top-label accuracy 65%

| run | exact | too weak | opus->haiku | opus->sonnet | sonnet->haiku | too strong | hard Qs on a weaker model | saved vs Opus (30k) | saved vs Opus (150k) | x Haiku cost (30k) |
|---|---|---|---|---|---|---|---|---|---|---|
| always haiku | 55% | 45% | 26% | 0% | 20% | 0% | 100% | 97% | 80% | 1.0x |
| always sonnet | 20% | 26% | 0% | 26% | 0% | 55% | 100% | 38% | 20% | 20.0x |
| always opus | 26% | 0% | 0% | 0% | 0% | 74% | 0% | 0% | 0% | 32.5x |
| save, start opus | 59% | 3% | 0% | 3% | 0% | 38% | 12% | 34% | 23% | 21.4x |
| save, start haiku | 42% | 26% | 0% | 26% | 0% | 32% | 100% | 52% | 34% | 15.7x |
| balanced, start opus | 55% | 3% | 0% | 3% | 0% | 42% | 12% | 32% | 20% | 22.2x |
| balanced, start haiku | 55% | 14% | 0% | 14% | 0% | 32% | 53% | 46% | 31% | 17.6x |
| quality, start opus | 47% | 3% | 0% | 3% | 0% | 50% | 12% | 27% | 15% | 23.7x |
| quality, start haiku | 65% | 3% | 0% | 3% | 0% | 32% | 12% | 39% | 27% | 19.8x |

### Design test (36)

36 prompts, labels {'haiku': 6, 'sonnet': 13, 'opus': 17}, scorer top-label accuracy 94%

| run | exact | too weak | opus->haiku | opus->sonnet | sonnet->haiku | too strong | hard Qs on a weaker model | saved vs Opus (30k) | saved vs Opus (150k) | x Haiku cost (30k) |
|---|---|---|---|---|---|---|---|---|---|---|
| always haiku | 17% | 83% | 47% | 0% | 36% | 0% | 100% | 97% | 80% | 1.0x |
| always sonnet | 36% | 47% | 0% | 47% | 0% | 17% | 100% | 38% | 20% | 20.0x |
| always opus | 47% | 0% | 0% | 0% | 0% | 53% | 0% | 0% | 0% | 32.5x |
| save, start opus | 92% | 6% | 0% | 6% | 0% | 3% | 12% | 31% | 20% | 22.6x |
| save, start haiku | 53% | 47% | 0% | 47% | 0% | 0% | 100% | 48% | 30% | 16.8x |
| balanced, start opus | 92% | 6% | 0% | 6% | 0% | 3% | 12% | 31% | 20% | 22.6x |
| balanced, start haiku | 75% | 25% | 0% | 25% | 0% | 0% | 53% | 40% | 26% | 19.6x |
| quality, start opus | 86% | 6% | 0% | 6% | 0% | 8% | 12% | 27% | 17% | 23.6x |
| quality, start haiku | 94% | 6% | 0% | 6% | 0% | 0% | 12% | 32% | 22% | 22.0x |

### Coding tasks (30)

30 prompts, labels {'haiku': 30, 'sonnet': 0, 'opus': 0}, scorer top-label accuracy 30%

| run | exact | too weak | opus->haiku | opus->sonnet | sonnet->haiku | too strong | hard Qs on a weaker model | saved vs Opus (30k) | saved vs Opus (150k) | x Haiku cost (30k) |
|---|---|---|---|---|---|---|---|---|---|---|
| always haiku | 100% | 0% | 0% | 0% | 0% | 0% | - | 97% | 80% | 1.0x |
| always sonnet | 0% | 0% | 0% | 0% | 0% | 100% | - | 38% | 20% | 20.0x |
| always opus | 0% | 0% | 0% | 0% | 0% | 100% | - | 0% | 0% | 32.5x |
| save, start opus | 20% | 0% | 0% | 0% | 0% | 80% | - | 39% | 26% | 20.0x |
| save, start haiku | 30% | 0% | 0% | 0% | 0% | 70% | - | 56% | 38% | 14.3x |
| balanced, start opus | 10% | 0% | 0% | 0% | 0% | 90% | - | 33% | 20% | 21.9x |
| balanced, start haiku | 30% | 0% | 0% | 0% | 0% | 70% | - | 53% | 37% | 15.1x |
| quality, start opus | 0% | 0% | 0% | 0% | 0% | 100% | - | 27% | 14% | 23.8x |
| quality, start haiku | 30% | 0% | 0% | 0% | 0% | 70% | - | 47% | 33% | 17.2x |

## What the numbers say

- **No mode ever sent a hard (opus-labelled) prompt to Haiku**, on either scorer. Every quality miss is opus->sonnet.
- **The savings are modest: about a quarter to a third of Opus's cost per typical message.** The scorer rarely
  picks Haiku: it rated only 7 of the 30 Haiku-solvable coding tasks as Haiku work (the personal scorer 9), so most easy work still runs
  on Sonnet or Opus, and Sonnet costs 62% of Opus here. Overspend, not quality loss, is the main failure.
- **On the public scorer the three modes barely differ** (24-35% saved, 1-2 of 17 hard prompts on Sonnet). Its
  confidence on these prompts is mostly above all the gates, so the gates rarely bite.
- **Starting model matters more than the mode.** A chat that starts on Opus mostly stays there unless the scorer is
  confident; a chat that starts on Haiku moves up to Sonnet when unsure.
- **Always-Sonnet saves 38%, more than any mode on the public scorer**, but puts every hard prompt on Sonnet. The
  router's value here is keeping hard prompts on Opus, not saving more than Sonnet would.
- **Save mode on the personal scorer, starting on Haiku, is a bad deal for hard work**: all 17 hard design prompts
  ended on Sonnet. That scorer's confidence on them is 0.40-0.64, under save's 0.60 up-gate, so the lean rule sends
  them one tier up to Sonnet instead of Opus. Balanced put 9 of 17 on Sonnet. Only quality (up-gate 0.40) kept them on
  Opus. With a less confident scorer, save mode should not be used for hard work in a chat that starts on Haiku.

## Limits

- **66 prompts, 17 of them hard.** One hard prompt is 6 percentage points. Treat the percentages as rough.
- **Not real usage.** Hand-written design prompts and small coding tasks, not your messages. Your mix decides the saving.
- **Labels are coarse.** The design labels are a person's judgement; the coding labels only say Haiku passed the
  hidden tests, not that its answer was as good. No prompt in the sets was run on all three models in this measurement.
- **The scorer is the weak link.** On the design test it picks the right label 94% of the time, but those prompts are
  written in a similar style to its synthetic training prompts; on the coding tasks it is right 23-30% of the time.
- **Short chats only.** A 5k-token history; in a long chat the cost check and handoffs keep the model more often.
- **Per-message steady-state cost**, no re-cache cost, no thinking tokens, one fixed message shape.
