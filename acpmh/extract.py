"""Incremental knowledge extraction with a cheap model (Haiku by default).

Only events not yet incorporated are read. The model proposes facts with the
event numbers that support them; this module validates every proposal before
it is stored. A model can never mark something "verified": that status is only
set by verify.py from observed evidence.
"""
import json
import re

from .store import FACT_KINDS, INSTRUCTION_KINDS, TEXT_KINDS

DEFAULT_MODEL = "claude-haiku-5-5"
CHUNK_CHARS = 60000

PROMPT = """You maintain the project memory for a software project worked on by AI coding models.
Below are journal events #{lo}-#{hi} (each starts with its number), followed by the facts already stored.

Extract ONLY knowledge that would change what a model taking over this project should do:
- requirement: something the user asked for or wants (status "stated")
- constraint: a rule/limit the user set or the environment imposes (never do X, must keep Y)
- decision: a design or approach choice that was made, with its reason
- rejected: an approach that was tried or proposed and rejected, with why
- task: the current objective / what is in progress / the next step
- bug: a known problem, failure or open bug
- fact: an important project fact (names, paths, numbers, settings, results, URLs, IDs) a successor would need
- open_question: something unresolved or uncertain

Rules:
- Be thorough: capture every concrete fact a successor could need or be asked about,
  especially what exists or was created, counts, statuses (done / in progress / not
  started), results, and the state reached at the END of these events.
- Be specific: keep exact names, numbers, file paths, thresholds, commands, commit/PR ids.
- When the task or a status changes, add the new one and supersede the old fact.
- One fact per item, written so it stands alone.
- "sources": the event numbers that support it (required, from #{lo}-#{hi}).
- "status": "stated" if it comes from the user's words (or a user_prompt/agent_message instruction), "observed" if from real tool output, "claimed" if only the assistant said it.
- If a new fact replaces or contradicts an existing one, list the old fact id in "supersedes".
- Do not repeat facts already stored unless they changed.
- Also write "segment": one dense line describing what happened in these events.

Reply with JSON only:
{{"add":[{{"kind":"...","text":"...","status":"...","sources":[1],"supersedes":[]}}],"segment":"..."}}

EXISTING FACTS:
{facts}

EVENTS:
{events}"""


def event_line(e, store, body_chars):
    line = "#%d [%s/%s] %s" % (e["seq"], e["kind"], e["status"], e["summary"])
    if e["kind"] in TEXT_KINDS and e["body_ref"]:
        body = store.get_blob(e["body_ref"])
        line = "#%d [%s/%s] %s" % (e["seq"], e["kind"], e["status"], body[:body_chars])
    return line


def chunks(store, after, upto=None):
    cur, size = [], 0
    for e in store.events(after=after, upto=upto):
        if e["kind"] in ("file_read",):
            continue  # reads change nothing; still searchable in the journal
        line = event_line(e, store, 6000)
        if cur and size + len(line) > CHUNK_CHARS:
            yield cur
            cur, size = [], 0
        cur.append((e["seq"], line))
        size += len(line)
    if cur:
        yield cur


def facts_text(store):
    rows = store.active_facts()
    return "\n".join("F%d [%s/%s] %s" % (f["fact_id"], f["kind"], f["status"], f["text"])
                     for f in rows) or "(none yet)"


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    data = json.loads(m.group(0))
    if not isinstance(data.get("add"), list):
        raise ValueError("reply has no 'add' list")
    return data


def apply(store, data, lo, hi, user_seqs):
    """Validate and store proposals. Returns (added, rejected_reasons)."""
    added, rejected = 0, []
    for item in data.get("add", []):
        text = (item.get("text") or "").strip()
        srcs = [s for s in item.get("sources", []) if isinstance(s, int) and lo <= s <= hi]
        if not text or not srcs:
            rejected.append("no text or no valid sources: %r" % item)
            continue
        status = item.get("status", "claimed")
        if status == "verified":
            status = "claimed"  # only evidence can verify
        if status == "stated" and not any(s in user_seqs for s in srcs):
            status = "claimed"  # "stated" must point at the user's own words
        kind = item.get("kind") if item.get("kind") in FACT_KINDS else "fact"
        fid = store.add_fact(kind, text, status, srcs)
        for old in item.get("supersedes", []) or []:
            try:
                old = int(str(old).lstrip("F"))
            except ValueError:
                continue
            if store.db.execute("SELECT 1 FROM facts WHERE fact_id=?", (old,)).fetchone():
                store.supersede(old, fid)
        added += 1
    if data.get("segment"):
        store.add_segment(lo, hi, data["segment"].strip())
    return added, rejected


def run(store, client, model=DEFAULT_MODEL, upto=None, log=print):
    """Incorporate all events after last_incorporated_seq (up to `upto`)."""
    after = int(store.get_meta("last_incorporated_seq", "0"))
    total_added = 0
    for chunk in chunks(store, after, upto):
        lo, hi = chunk[0][0], chunk[-1][0]
        user_seqs = {r[0] for r in store.db.execute(
            "SELECT seq FROM events WHERE kind IN (?,?) AND seq BETWEEN ? AND ?",
            INSTRUCTION_KINDS + (lo, hi))}
        prompt = PROMPT.format(lo=lo, hi=hi, facts=facts_text(store),
                               events="\n".join(l for _, l in chunk))
        data = None
        for attempt in range(2):
            resp = client.messages.create(
                model=model, max_tokens=16000,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": prompt}])
            store.record_usage("maintenance", model, resp.usage)
            text = "".join(b.text for b in resp.content if b.type == "text")
            try:
                data = parse_json(text)
                break
            except ValueError:
                log("extract: bad JSON for #%d-#%d, retrying" % (lo, hi))
        if data is None:
            # Leave last_incorporated_seq where it is so this chunk is retried.
            raise RuntimeError("extraction failed for events #%d-#%d" % (lo, hi))
        added, rejected = apply(store, data, lo, hi, user_seqs)
        total_added += added
        for r in rejected:
            log("extract: rejected " + r[:200])
        store.set_meta("last_incorporated_seq", str(hi))
        log("extract: #%d-#%d -> %d facts" % (lo, hi, added))
    end = upto if upto is not None else store.last_seq()
    store.set_meta("last_incorporated_seq", str(max(end, after)))
    return total_added
