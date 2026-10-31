# Handoff benchmark

How the v2 defaults were chosen. Every number below was measured on real Claude Code sessions (not
published: they are private chats), at list prices.

## Method

- **Switch points:** five cuts of real sessions, 36k to about 1M tokens. One is agent-driven coding (86% tool
  calls and output), the others are conversations with a person.
- **Questions:** Opus 5.5 wrote 50 questions per cut (40 at 36k) with gold answers from the full history. The
  questions ask what the incoming model must know to carry on: current state, decisions and why, details, early
  instructions.
- **Methods:** re-read the whole chat (switch in place), Claude Code's compaction summary, and ACPMH handoffs:
  - v3: facts + 20k recent tail;
  - **v4**: facts + the whole conversation word for word (trimmed) + 8k tail (`full_log=True`);
  - **v5**: facts + every user and lead-agent message word for word, newest first, about 40k tokens (`user_log=True`);
  - **v5b**: v5 with a 20k tail.
- **Grading:** one Opus pass per answer, blind to the method. Score is relative to re-reading.
- **Selection rules, fixed before any result:** a gap of 2/50 or less is a tie; floors quality 95%, balanced 90%,
  save 85% of re-read; then the lowest cost per point wins; coding counts double.

## Results, Sonnet 5.5 taking over (score vs re-read, cost per switch)

| Size | Re-read | Compaction | v4 | v5 | v5b |
|---|---|---|---|---|---|
| 36k | 100%, 10c | 91%, 15c | 99%, 14c | 98%, 14c | 100%, 16c |
| 134k | 100%, 30c | 80%, 36c | 99%, 19c | 85%, 12c | 95%, 19c |
| 260k (agent coding) | 100%, 55c | 68%, 60c | 94%, 28c | 80%, 16c | 87%, 24c |
| 260k, 50 new questions | 100%, 55c | - | 94%, 22c | 75%, 12c | 82%, 19c |
| 266k | 100%, 56c | 79%, 62c | 94%, 27c | 91%, 14c | 94%, 19c |
| ~1M | 100%, $1.88 | not run | 95%, 99c | 86%, 26c | 94%, 35c |

**Haiku 5.5 taking over** scores close to Sonnet on the same handoffs at about a tenth of the cost (266k: re-read
97% for 16c, v4 94% for 2.4c). **Opus 5.5 taking over** at 266k: re-read 50/50 for $1.18, v4 97% for 58c.

- Compaction never wins: it scores lower and costs as much as re-reading or more.
- v5 drops tool activity, so on agent-driven coding chats it misses the reasons behind code decisions (75-80%).
  v4 holds at 94-95% on every chat.
- Break-even (a handoff costs less than re-reading): about 70k tokens with Sonnet or Opus taking over, 55k with
  Haiku. Below it, v2 switches in place.

## Defaults this sets

| Chat size | balanced | save | quality |
|---|---|---|---|
| under ~70k | switch in place | switch in place | switch in place |
| 70k-300k | v4 handoff | v4 handoff | switch in place |
| over 300k | v5b handoff | v5 handoff | v4 handoff |

## Screenshots

10 questions answerable only from pasted screenshots (~1M cut): re-read with the images 10/10 for $1.89; v5 with
ACPMH's screenshot support (images stored, text read locally) 10/10 for 15c; without it 3/10.

## Coding tasks

8 real tasks from an agent's session, each continued in a sandboxed copy of the repository at the commit the agent
started from. Done = the agent's own reference tests pass and Opus approves the diff.

| Method | Done | Cost per task done |
|---|---|---|
| Re-read, Sonnet | 4/8 | $1.17 |
| v4, Sonnet | 4/8 | 56c |
| v5, Sonnet | 4/8 | 41c |
| Re-read, Haiku | 4/8 | 14c |
| v5, Haiku | 5/8 | 4c |

A handoff never lowered the success rate; it cost a half to a third of re-reading. Small sample: one task moves a
method by 12 points.

## Scorer and context

- On 150 SWE-bench Verified issues the scorer's p(haiku) had no ranking power (AUC 0.50), so the budget modes are
  confidence gates (save 60% / balanced 70% / quality 80% down; 60/50/40% up), not a probability formula.
  `AUTOSWITCH_CALIBRATION` can plug in a calibration later.
- Context-aware scoring (short messages scored together with the current task and the last 3 user messages, 70/30
  blend, at most +0.05 towards Haiku, topic changes scored alone) cut wrong down-switches from 6 to 0 on 29 real
  messages, labelled by hand.
