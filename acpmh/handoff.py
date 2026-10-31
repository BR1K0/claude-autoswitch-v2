"""Build a layered handoff from the journal, facts and real git state.

Layer 1 (always): task, next step, every active requirement/constraint/decision,
open bugs, verification evidence, git state, the latest user messages verbatim.
Layer 2 (adaptive): other facts by recency, files touched, recent activity, and
events since the last memory update (that gap is always included in full).
Layer 3 (by reference): a map of the whole history with event ranges, so the
next model knows what exists and can fetch it with the retrieval tools.
"""
import json
from collections import OrderedDict

from . import gitstate, verify
from .store import IMAGE_KINDS, INSTRUCTION_KINDS, TEXT_KINDS, est_tokens

MANDATORY = ("task", "requirement", "constraint", "decision", "rejected", "bug",
             "open_question")


def _fact_line(f):
    refs = ",".join("evt_%06d" % s for s in json.loads(f["source_seqs"])[:3])
    note = " — %s" % f["note"] if f["note"] else ""
    return "- [F%d %s] %s (%s%s)" % (f["fact_id"], f["status"], f["text"], refs, note)


def _clip(text, n):
    return text if len(text) <= n else text[:n] + " …[truncated; full text via get_event]"


def verbatim_tail(store, tokens):
    """The most recent events, verbatim, newest last, within `tokens`."""
    out, used = [], 0
    for e in reversed(store.events()):
        body = store.get_blob(e["body_ref"]) if e["body_ref"] else ""
        if e["kind"] in TEXT_KINDS:
            text = "%s [%s] %s" % (e["event_id"], e["kind"], body or e["summary"])
        else:
            text = "%s [%s] %s%s" % (e["event_id"], e["kind"], e["summary"],
                                     "\n" + _clip(body, 1500) if body else "")
        t = est_tokens(text)
        if used + t > tokens:
            break
        out.append(text)
        used += t
    return list(reversed(out))


def conversation_log(store, msg_chars=6000):
    """Every message verbatim; every tool call as its one-line summary."""
    out = []
    for e in store.events():
        if e["kind"] in TEXT_KINDS:
            body = store.get_blob(e["body_ref"]) if e["body_ref"] else e["summary"]
            out.append("%s [%s] %s" % (e["event_id"], e["kind"], _clip(body, msg_chars)))
        else:
            out.append("%s [%s] %s" % (e["event_id"], e["kind"], e["summary"]))
    return out


def build(store, repo=None, budget_tokens=None, recent=20, last_user=3,
          tail_tokens=20000, full_log=False, screenshots=12, user_log=False,
          max_tokens=40000, user_msg_chars=3000):
    """Return (markdown, stats). budget_tokens limits only the optional layer 2.

    user_log=True (v5): every message from the user and lead agents verbatim, newest
    first until the whole handoff reaches max_tokens; older ones stay in the journal
    and are listed by id. Assistant replies are not copied, only the recent tail."""
    total = store.last_seq()
    upto = int(store.get_meta("last_incorporated_seq", "0"))
    problems = store.verify_chain()
    failures = store.db.execute("SELECT COUNT(*) FROM capture_failures").fetchone()[0]
    facts = store.active_facts()
    by_kind = OrderedDict((k, [f for f in facts if f["kind"] == k]) for k in MANDATORY)
    S = []

    S.append("# ACPMH HANDOFF\n")
    S.append("Journal: %d events, integrity %s, capture failures: %d. "
             "Memory incorporates events up to #%d (freshness %.0f%%)." % (
                 total, "OK" if not problems else "PROBLEMS: " + "; ".join(problems[:3]),
                 failures, upto, 100.0 * upto / total if total else 100))
    S.append("""
## How to use this handoff
This is NOT the whole history. Every event of the previous work is stored verbatim
in the ACPMH journal. Facts below cite their evidence (evt_NNNNNN). Status meanings:
stated = the user's own words; observed = real tool output; verified = backed by
evidence; claimed = a model said it, unchecked; contradicted = evidence disagrees.
Facts are compressed and can be stale or miss specifics. Before you rely on a detail
(exact values, counts, what exists, why something was decided, earlier attempts),
confirm it in the journal with search_history / get_event / list_events. Prefer the
verbatim recent history and the original events over a fact when they disagree.
""")

    S.append("## Current task and next step")
    for f in reversed(by_kind["task"][-6:]):
        S.append(_fact_line(f))
    S.append("\nLatest instructions from the user or lead agent (verbatim, newest last):")
    for e in store.events(kinds=INSTRUCTION_KINDS)[-last_user:]:
        S.append("> [%s] %s" % (e["event_id"], _clip(
            store.get_blob(e["body_ref"]) if e["body_ref"] else e["summary"], 2500)
            .replace("\n", "\n> ")))

    reports = store.events(kinds=("assistant_text",))[-last_user:]
    if reports:
        S.append("\nLatest status reports from the previous model (claimed, newest last):")
        for e in reports:
            S.append("> [%s] %s" % (e["event_id"], _clip(
                store.get_blob(e["body_ref"]) if e["body_ref"] else e["summary"], 2500)
                .replace("\n", "\n> ")))

    titles = {"requirement": "Requirements (all active)",
              "constraint": "Constraints (all active; do not violate)",
              "decision": "Decisions in force (do not reverse without the user)",
              "rejected": "Rejected approaches (do not retry without new reason)",
              "bug": "Known bugs and failures", "open_question": "Open questions"}
    for k, title in titles.items():
        if by_kind[k]:
            S.append("\n## " + title)
            S.extend(_fact_line(f) for f in by_kind[k])

    S.append("\n## Verification evidence")
    tests = store.events(kinds=("test_passed", "test_failed", "test_run"))[-6:]
    for e in tests:
        S.append("- %s %s" % (e["event_id"], e["summary"]))
    if not tests:
        S.append("- No test runs captured.")
    for eid, why in verify.unverified_claims(store)[-6:]:
        S.append("- UNVERIFIED: %s %s" % (eid, why))

    if repo:
        g = gitstate.state(repo)
        S.append("\n## Git state (live)")
        S.append("```\n%s\n```" % json.dumps(g, indent=1)[:4000])

    missed = verify.uncaptured_user_prompts(store)
    if missed and not user_log:  # v5 carries every instruction in its own log
        S.append("\n## Instructions not captured as facts (verbatim)")
        for e in missed:
            S.append("> [%s] %s" % (e["event_id"], _clip(
                store.get_blob(e["body_ref"]) if e["body_ref"] else e["summary"], 1500)
                .replace("\n", "\n> ")))

    gap = store.events(after=upto)
    if gap:
        S.append("\n## Events since the last memory update (not yet in the facts above)")
        lines, used = [], 0
        for e in reversed(gap):
            body = ""
            if e["kind"] in TEXT_KINDS and e["body_ref"]:
                body = "\n  " + _clip(store.get_blob(e["body_ref"]), 2000).replace("\n", "\n  ")
            line = "- %s %s%s" % (e["event_id"], e["summary"], body)
            if user_log and used + est_tokens(line) > max_tokens // 5:
                S.append("- %d earlier events (evt_%06d..) not yet in the facts and not shown; "
                         "user messages among them are in the verbatim log below; the rest "
                         "via list_events." % (len(gap) - len(lines), gap[0]["seq"]))
                break
            lines.append(line)
            used += est_tokens(line)
        S.extend(reversed(lines))

    core = "\n".join(S)
    core_tokens = est_tokens(core)

    # ---- layer 2: adaptive ----
    L2 = []
    other = [f for f in facts if f["kind"] == "fact"]
    budget = budget_tokens if budget_tokens is not None else max(4000, 12 * len(other) + 3000)
    if user_log:
        budget = min(budget, max_tokens // 4)
    used, kept = 0, []
    for f in reversed(other):  # newest first
        line = _fact_line(f)
        if used + est_tokens(line) > budget * 0.6:
            break
        kept.append(line)
        used += est_tokens(line)
    if other:
        L2.append("\n## Project facts (newest first%s)" % (
            "" if len(kept) == len(other) else
            "; %d older facts omitted, use search_history" % (len(other) - len(kept))))
        L2.extend(kept)

    files = OrderedDict()
    for e in store.events(kinds=("file_modified", "file_written")):
        for p in json.loads(e["paths"] or "[]"):
            if p:
                n, _ = files.get(p, (0, None))
                files[p] = (n + 1, e["event_id"])
    if files:
        L2.append("\n## Files changed during the work (edits, last event)")
        for p, (n, last) in sorted(files.items(), key=lambda kv: kv[1][1], reverse=True)[:40]:
            L2.append("- %s (%d edits, last %s)" % (p, n, last))

    shots = store.events(kinds=IMAGE_KINDS)
    if shots:
        L2.append("\n## Screenshots and images (newest first; %d in all). Text read from "
                  "each is below; to look at one, use get_image or "
                  "`python3 -m acpmh.cli image EVENT_ID` and open the file." % len(shots))
        for e in list(reversed(shots))[:screenshots]:
            body = store.get_blob(e["body_ref"]) if e["body_ref"] else ""
            text = body.split("Text in the image (local OCR):\n", 1)[-1] if body else ""
            L2.append("- %s [%s] %s" % (e["event_id"], e["kind"],
                                        _clip(" ".join(text.split()), 600)))

    L2.append("\n## Recent activity")
    for e in store.events(after=max(0, total - recent)):
        L2.append("- %s %s" % (e["event_id"], e["summary"]))

    if full_log:
        L2.append("\n## Full conversation log (every message verbatim; tool calls as "
                  "one-liners, full output via get_event)")
        L2.extend(conversation_log(store))
    if tail_tokens:
        tail = verbatim_tail(store, tail_tokens)
        if tail:
            L2.append("\n## Most recent history, verbatim (oldest first)")
            L2.extend(tail)

    segs = store.segments()
    if segs:
        L2.append("\n## Map of the full history (fetch any range with list_events)")
        for s in segs:
            L2.append("- evt_%06d..evt_%06d: %s" % (s["start_seq"], s["end_seq"], s["text"]))

    log_stats = {}
    if user_log:
        room = max_tokens - est_tokens(core) - est_tokens("\n".join(L2)) - 200
        lines, log_stats = user_message_log(store, room, user_msg_chars,
                                            {e["seq"] for e in missed})
        core += "\n" + "\n".join(lines)
    text = core + "\n" + "\n".join(L2) + "\n"
    stats = {"tokens_est": est_tokens(text), "core_tokens_est": core_tokens,
             "events": total, "incorporated": upto, "facts": len(facts),
             "facts_in_handoff": len(facts) - len(other) + len(kept),
             "uncaptured_user_prompts": len(missed), "gap_events": len(gap),
             "integrity_problems": problems}
    stats.update(log_stats)
    return text, stats


def user_message_log(store, room, msg_chars, missed_seqs):
    """Every user / lead-agent message and pasted screenshot verbatim, newest first,
    within `room` tokens. Returns (lines, stats)."""
    kinds = INSTRUCTION_KINDS + ("screenshot",)
    rows = store.events(kinds=kinds)
    kept, used = [], 0
    for e in reversed(rows):
        body = store.get_blob(e["body_ref"]) if e["body_ref"] else e["summary"]
        if e["kind"] == "screenshot":
            body = " ".join(body.split("Text in the image (local OCR):\n", 1)[-1].split())
            line = "%s [screenshot] %s" % (e["event_id"], _clip(body, 600))
        else:
            line = "%s [%s] %s" % (e["event_id"], e["kind"], _clip(body, msg_chars))
        t = est_tokens(line)
        if used + t > room:
            break
        kept.append(line)
        used += t
    older = rows[:len(rows) - len(kept)]
    out = ["\n## Every message from the user and lead agents, verbatim (oldest first)"]
    if older:
        out.append("%d older messages (evt_%06d..evt_%06d) are in the journal, not shown; "
                   "use list_events with kind user_prompt, or search_history." % (
                       len(older), older[0]["seq"], older[-1]["seq"]))
        lost = [e for e in older if e["seq"] in missed_seqs]
        if lost:
            out.append("Older instructions no fact captures (fetch with get_event): " +
                       ", ".join(e["event_id"] for e in lost[-60:]))
    out.extend(reversed(kept))
    return out, {"user_log_messages": len(kept), "user_log_omitted": len(older),
                 "user_log_tokens_est": used}
