"""Check claimed facts against observed evidence. Deterministic, no model calls.

A fact only becomes "verified" here, when real tool output backs it, and becomes
"contradicted" when later evidence disagrees. Everything else stays "claimed".
"""
import json
import re

PASS_CLAIM = re.compile(r"\b(tests? (all )?pass(ed|ing)?|all \d+ tests|green|"
                        r"\d+/\d+ pass)", re.I)
FAIL_CLAIM = re.compile(r"\b(tests? fail(ed|ing)?|failing tests?)\b", re.I)
WINDOW = 25  # events before a claim that can back it


def _tests_near(store, seq):
    return store.db.execute(
        "SELECT seq, kind FROM events WHERE kind IN ('test_passed','test_failed') "
        "AND seq BETWEEN ? AND ? ORDER BY seq DESC", (seq - WINDOW, seq)).fetchall()


def _later_tests(store, seq):
    return store.db.execute(
        "SELECT seq, kind FROM events WHERE kind IN ('test_passed','test_failed') "
        "AND seq > ? ORDER BY seq", (seq,)).fetchall()


def run(store):
    """Update fact statuses from evidence. Returns counts."""
    counts = {"verified": 0, "contradicted": 0}
    for f in store.active_facts():
        if f["status"] != "claimed":
            continue
        srcs = json.loads(f["source_seqs"])
        top = max(srcs)
        if PASS_CLAIM.search(f["text"]) and not FAIL_CLAIM.search(f["text"]):
            near = _tests_near(store, top)
            if near and near[0]["kind"] == "test_passed":
                _set(store, f, "verified", "test output evt_%06d" % near[0]["seq"])
                counts["verified"] += 1
            elif near and near[0]["kind"] == "test_failed":
                _set(store, f, "contradicted", "test output evt_%06d failed"
                     % near[0]["seq"])
                counts["contradicted"] += 1
        # Commit/PR claims backed by an observed commit event.
        m = re.search(r"\bcommit(ted)? ([0-9a-f]{7,40})\b", f["text"], re.I)
        if m:
            hit = store.db.execute(
                "SELECT seq FROM events WHERE kind='git_commit' AND summary LIKE ?",
                ("%" + m.group(2)[:7] + "%",)).fetchone()
            if hit:
                _set(store, f, "verified", "commit evt_%06d" % hit["seq"])
                counts["verified"] += 1
    return counts


def _set(store, fact, status, note):
    with store.db:
        store.db.execute("UPDATE facts SET status=?, note=? WHERE fact_id=?",
                         (status, note, fact["fact_id"]))


def unverified_claims(store, limit=12):
    """Recent assistant statements of success with no matching evidence."""
    out = []
    for e in store.events(kinds=("assistant_text",)):
        text = store.get_blob(e["body_ref"]) if e["body_ref"] else e["summary"]
        if PASS_CLAIM.search(text):
            near = _tests_near(store, e["seq"])
            if not near or near[0]["kind"] != "test_passed":
                out.append((e["event_id"], "claims tests pass, no passing test "
                            "output in the %d events before it" % WINDOW))
    return out[-limit:]


def uncaptured_user_prompts(store):
    """User messages no stored fact cites: the handoff must carry them verbatim."""
    cited = store.cited_seqs()
    upto = int(store.get_meta("last_incorporated_seq", "0"))
    return [e for e in store.events(kinds=("user_prompt", "agent_message"))
            if e["seq"] not in cited and e["seq"] <= upto]
