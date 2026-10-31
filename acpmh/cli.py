"""acpmh command line.

  python3 -m acpmh.cli ingest  --store DIR TRANSCRIPT.jsonl   capture events (free)
  python3 -m acpmh.cli sync    --store DIR                    extract facts (Haiku) + verify
  python3 -m acpmh.cli handoff --store DIR [--repo PATH]       print the handoff
  python3 -m acpmh.cli search  --store DIR QUERY               search the journal
  python3 -m acpmh.cli get     --store DIR EVENT_ID [--offset N]  read one event in full
  python3 -m acpmh.cli list    --store DIR START END [--kind K] list event summaries in a range
  python3 -m acpmh.cli file    --store DIR PATH                events that touched a file
  python3 -m acpmh.cli image   --store DIR EVENT_ID            path of a screenshot's image file
  python3 -m acpmh.cli status  --store DIR                     health and freshness
"""
import argparse
import json

from . import extract, handoff, ingest, tools, verify
from .store import Store


def main():
    ap = argparse.ArgumentParser(prog="acpmh")
    ap.add_argument("cmd", choices=["ingest", "sync", "handoff", "search", "get", "list",
                                       "file", "image", "status"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("arg2", nargs="?")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--kind")
    ap.add_argument("--full-log", action="store_true")
    ap.add_argument("--tail", type=int, default=20000)
    ap.add_argument("--store", required=True)
    ap.add_argument("--repo")
    ap.add_argument("--model", default=extract.DEFAULT_MODEL)
    a = ap.parse_intermixed_args()
    store = Store(a.store)

    if a.cmd == "ingest":
        print("+%d events" % ingest.ingest(store, ingest.load(a.arg)))
    elif a.cmd == "sync":
        import anthropic
        extract.run(store, anthropic.Anthropic(), model=a.model)
        print(json.dumps(verify.run(store)))
    elif a.cmd == "handoff":
        text, stats = handoff.build(store, repo=a.repo, full_log=a.full_log,
                                    tail_tokens=a.tail)
        print(text)
        print("<!-- %s -->" % json.dumps(stats))
    elif a.cmd == "search":
        print(tools.call(store, "search_history", {"query": a.arg}))
    elif a.cmd == "image":
        print(tools.call(store, "get_image", {"event_id": a.arg}))
    elif a.cmd == "get":
        print(tools.call(store, "get_event", {"event_id": a.arg, "offset": a.offset}))
    elif a.cmd == "list":
        print(tools.call(store, "list_events", {"start": a.arg, "end": a.arg2,
                                                "kind": a.kind}))
    elif a.cmd == "file":
        print(tools.call(store, "file_history", {"path": a.arg}))
    elif a.cmd == "status":
        total = store.last_seq()
        upto = int(store.get_meta("last_incorporated_seq", "0"))
        fails = store.db.execute("SELECT COUNT(*) FROM capture_failures").fetchone()[0]
        usage = store.db.execute(
            "SELECT purpose, model, SUM(input_tokens), SUM(output_tokens) FROM usage "
            "GROUP BY purpose, model").fetchall()
        print(json.dumps({"events": total, "incorporated": upto,
                          "freshness": round(upto / total, 3) if total else 1.0,
                          "integrity_problems": store.verify_chain(),
                          "capture_failures": fails,
                          "facts": len(store.active_facts()),
                          "uncaptured_instructions": len(verify.uncaptured_user_prompts(store)),
                          "usage": [list(r) for r in usage]}, indent=1))


if __name__ == "__main__":
    main()
