"""Retrieval tools the incoming model can call. Results are short and targeted:
search returns references and snippets, and the model fetches full events by id."""
import json
import os
import re

from . import gitstate

TOOLS = [
    {"name": "search_history",
     "description": "Search the full project journal (every user message, assistant "
                    "message, command, test run, file edit) and stored facts by keywords. "
                    "Returns event/fact ids with snippets. Use exact names, numbers, "
                    "paths or error text when you have them.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "limit": {"type": "integer", "description": "default 8, max 25"}},
         "required": ["query"]}},
    {"name": "get_event",
     "description": "Read one journal event in full (e.g. evt_000123), including the "
                    "original text or tool output. Long bodies are paged with offset.",
     "input_schema": {"type": "object", "properties": {
         "event_id": {"type": "string"},
         "offset": {"type": "integer", "description": "character offset, default 0"}},
         "required": ["event_id"]}},
    {"name": "list_events",
     "description": "List event summaries in a range of the journal, optionally only "
                    "one kind (user_prompt, agent_message, assistant_text, notification, command, test_passed, "
                    "test_failed, file_modified, git_commit, ...). Max 80 per call.",
     "input_schema": {"type": "object", "properties": {
         "start": {"type": "string", "description": "evt id or number"},
         "end": {"type": "string"},
         "kind": {"type": "string"}},
         "required": ["start", "end"]}},
    {"name": "get_image",
     "description": "Open a screenshot or image event (e.g. evt_000123) to look at the "
                    "image itself. get_event shows the text read from it; use this when "
                    "the layout, colours or anything else not in that text matters.",
     "input_schema": {"type": "object", "properties": {"event_id": {"type": "string"}},
                      "required": ["event_id"]}},
    {"name": "file_history",
     "description": "All journal events that edited or read a file path (substring match).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
]

GET_EVENT_CHARS = 8000
IMAGE_REF = re.compile(r"^image: (\S+)$", re.M)


def image_file(store, event_id):
    """(absolute path, media type) of an image event's file, or None."""
    e = store.event("evt_%06d" % _num(event_id))
    if not e or not e["body_ref"]:
        return None
    m = IMAGE_REF.search(store.get_blob(e["body_ref"]))
    if not m:
        return None
    path = store.blob_path(m.group(1))
    ext = m.group(1).rsplit(".", 1)[-1]
    media = {"jpg": "image/jpeg"}.get(ext, "image/" + ext)
    return (path, media) if os.path.exists(path) else None


def _same_message(store, e):
    """Other events from the same transcript message (a screenshot and its words)."""
    msg = e["source_key"].split(":")[0]
    rows = store.db.execute("SELECT event_id, kind FROM events WHERE source_key LIKE ? "
                            "AND seq != ? ORDER BY seq", (msg + ":%", e["seq"])).fetchall()
    return ", ".join("%s [%s]" % (r[0], r[1]) for r in rows)


def _num(v):
    return int(str(v).replace("evt_", "").lstrip("#") or 0)


def call(store, name, args, repo=None):
    try:
        if name == "search_history":
            rows = store.search(args["query"], min(int(args.get("limit") or 8), 25))
            out = []
            for r in rows:
                if r["ref"].startswith("evt_"):
                    e = store.event(r["ref"])
                    out.append("%s [%s] %s\n   …%s…" % (r["ref"], e["kind"], e["summary"],
                                                    r["snip"]))
                else:
                    f = store.db.execute("SELECT * FROM facts WHERE fact_id=?",
                                         (int(r["ref"][5:]),)).fetchone()
                    out.append("F%d [%s/%s] %s (sources %s)" % (
                        f["fact_id"], f["kind"], f["status"], f["text"], f["source_seqs"]))
            return "\n".join(out) or "No matches. Try other keywords or list_events."
        if name == "get_event":
            e = store.event("evt_%06d" % _num(args["event_id"]))
            if not e:
                return "No such event."
            body = store.get_blob(e["body_ref"]) if e["body_ref"] else ""
            off = int(args.get("offset") or 0)
            part = body[off:off + GET_EVENT_CHARS]
            more = len(body) - off - len(part)
            img = image_file(store, e["event_id"])
            same = _same_message(store, e)
            return "%s [%s/%s] %s %s\n%s%s%s%s" % (
                e["event_id"], e["kind"], e["status"], e["ts"] or "", e["summary"], part,
                "\n…[%d more chars, call again with offset=%d]" % (more, off + len(part))
                if more > 0 else "",
                "\nImage file: %s (look at it with get_image)" % img[0] if img else "",
                "\nSame message: %s" % same if same else "")
        if name == "get_image":
            img = image_file(store, args["event_id"])
            return ("Image file: %s (%s)" % img) if img else "No image stored for that event."
        if name == "list_events":
            lo, hi = _num(args["start"]), _num(args["end"])
            kinds = (args["kind"],) if args.get("kind") else None
            rows = store.events(after=lo - 1, upto=hi, kinds=kinds)[:80]
            return "\n".join("%s [%s] %s" % (e["event_id"], e["kind"], e["summary"])
                             for e in rows) or "No events in range."
        if name == "file_history":
            rows = store.db.execute(
                "SELECT * FROM events WHERE paths LIKE ? OR summary LIKE ? ORDER BY seq",
                ("%" + args["path"] + "%", "%" + args["path"] + "%")).fetchall()[:60]
            return "\n".join("%s [%s] %s" % (e["event_id"], e["kind"], e["summary"])
                             for e in rows) or "No events for that path."
        if name == "git_state" and repo:
            return json.dumps(gitstate.state(repo), indent=1)
        return "Unknown tool."
    except Exception as ex:  # tools must answer, not crash the agent loop
        return "Tool error: %r" % ex
