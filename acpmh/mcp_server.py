"""Minimal MCP stdio server (JSON-RPC 2.0, newline-delimited) exposing ACPMH.

Usage: python3 -m acpmh.mcp_server --store <dir> [--repo <git repo>]
Tools: the retrieval tools in tools.py, plus prepare_handoff, git_state and
record_event (so any MCP-capable model client can read and write the journal).
"""
import argparse
import base64
import json
import sys
import uuid

from . import handoff, tools
from .store import Store

EXTRA = [
    {"name": "prepare_handoff",
     "description": "Build the current ACPMH handoff (task, requirements, decisions, "
                    "evidence, git state, history map) for an incoming model.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "git_state", "description": "Live git branch, head, staged/modified/"
     "untracked files and diffstat.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "record_event",
     "description": "Append an event to the journal (decision, requirement, note). "
                    "Recorded as 'claimed' unless it is user text.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string"}, "summary": {"type": "string"},
         "body": {"type": "string"}}, "required": ["kind", "summary"]}},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--repo")
    a = ap.parse_args()
    store = Store(a.store)
    all_tools = tools.TOOLS + EXTRA

    def reply(id_, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": id_}
        msg["error" if error else "result"] = error or result
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        method, id_, params = req.get("method"), req.get("id"), req.get("params") or {}
        if id_ is None:
            continue  # notification
        if method == "initialize":
            reply(id_, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "acpmh", "version": "0.1"}})
        elif method == "tools/list":
            reply(id_, {"tools": [{"name": t["name"], "description": t["description"],
                                   "inputSchema": t["input_schema"]} for t in all_tools]})
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "prepare_handoff":
                text, _ = handoff.build(store, repo=a.repo)
            elif name == "record_event":
                text = "recorded %s" % store.append(
                    "mcp:" + uuid.uuid4().hex, args["kind"], "claimed",
                    args["summary"], args.get("body"))[0]
            elif name == "get_image" and tools.image_file(store, args.get("event_id", "")):
                path, media = tools.image_file(store, args["event_id"])
                with open(path, "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                reply(id_, {"content": [{"type": "image", "data": data, "mimeType": media},
                                        {"type": "text", "text": "Image file: " + path}]})
                continue
            else:
                text = tools.call(store, name, args, repo=a.repo)
            reply(id_, {"content": [{"type": "text", "text": text}]})
        elif method == "ping":
            reply(id_, {})
        else:
            reply(id_, error={"code": -32601, "message": "unknown method " + str(method)})


if __name__ == "__main__":
    main()
