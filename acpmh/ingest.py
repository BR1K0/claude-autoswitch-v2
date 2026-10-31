"""Turn a Claude Code transcript (JSONL) into journal events, deterministically.

No model calls here. What the user typed is stored word for word ("stated"),
what the assistant said is a "claimed" event, and tool calls with their real
output are "observed". Test runs and commits are recognised from the command.
"""
import json
import re

from . import images
from .redact import redact
from .store import est_tokens

REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
TEST_CMD = re.compile(r"\b(pytest|unittest|npm (run )?test|go test|cargo test|"
                      r"jest|vitest|make test|tox)\b")
FILE_TOOLS = {"Edit": "file_modified", "MultiEdit": "file_modified",
              "Write": "file_written", "NotebookEdit": "file_modified"}
READ_TOOLS = {"Read", "Grep", "Glob"}


def load(path):
    out = []
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") in ("user", "assistant") and not d.get("isSidechain") \
                    and isinstance(d.get("message"), dict):
                out.append(d)
    return out


def flatten(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "tool_reference":
            parts.append("[tool loaded: %s]" % b.get("tool_name"))
        elif b.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def blocks(entry):
    c = entry["message"].get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return c or []


def render(entries):
    """Yield (index, text) for each entry, as a full-history replay would show it."""
    for i, e in enumerate(entries):
        parts = []
        role = e["type"].upper()
        for b in blocks(e):
            t = b.get("type")
            if t == "text" and b.get("text"):
                parts.append("%s: %s" % (role, b["text"]))
            elif t == "tool_use":
                parts.append("TOOL CALL %s: %s" % (b.get("name"),
                                                   json.dumps(b.get("input"))))
            elif t == "tool_result":
                parts.append("TOOL RESULT%s: %s" % (
                    " (error)" if b.get("is_error") else "", flatten(b.get("content"))))
        if parts:
            yield i, "\n".join(parts)


def cut_index(entries, target_tokens):
    """Index of the first entry past target_tokens of rendered history."""
    total = 0
    for i, text in render(entries):
        total += est_tokens(text)
        if total >= target_tokens:
            return i + 1
    return len(entries)


def _short(s, n=240):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def _image_blocks(entries):
    """Yield (source_key, block, origin) for every image, pasted or from a tool."""
    for e in entries:
        for n, b in enumerate(blocks(e)):
            key = "%s:%d" % (e.get("uuid"), n)
            if b.get("type") == "image" and e["type"] == "user":
                yield key, b, "pasted"
            elif b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                for k, x in enumerate(b["content"]):
                    if isinstance(x, dict) and x.get("type") == "image":
                        yield "%s:img%d" % (key, k), x, "tool"


def prepare_images(store, entries):
    """Store new images and read their text. Returns {source_key: (kind, summary, body)}."""
    todo, out = [], {}
    for key, b, origin in _image_blocks(entries):
        if store.db.execute("SELECT 1 FROM events WHERE source_key=?", (key,)).fetchone():
            continue
        try:
            data, media = images.decode(b)
            name = store.put_file(data, images.ext_for(media))
            todo.append((key, origin, name, media, len(data)))
        except (ValueError, OSError) as ex:
            out[key] = (origin, None, None, 0, "could not be stored: %s" % ex)
    texts = images.ocr([store.blob_path(t[2]) for t in todo]) if todo else {}
    ocr_ok = bool(texts) or (todo and images.ocr_available())
    for key, origin, name, media, size in todo:
        text = texts.get(store.blob_path(name))
        out[key] = (origin, name, media, size, text if text is not None else
                    None if ocr_ok else False)
    return {k: _image_event(*v) for k, v in out.items()}


def _image_event(origin, name, media, size, text):
    who = "Screenshot pasted by the user" if origin == "pasted" else "Image returned by a tool"
    kind = "screenshot" if origin == "pasted" else "tool_image"
    label = "Screenshot (pasted)" if origin == "pasted" else "Image from tool"
    if name is None:  # text holds the reason
        return (kind, "observed", "%s, %s" % (label, text), "%s %s." % (who, text))
    if text is False:
        shown, note = "", "(text recognition not available on this machine)"
    else:
        shown, removed = redact((text or "").strip()[:images.OCR_CHARS])
        note = shown or "(no text found)"
        if removed:
            note += "\n(%d key or password value(s) removed)" % removed
    body = ("%s (%s, %d KB).\nimage: %s\nText in the image (local OCR):\n%s" % (
        who, media, max(1, size // 1024), name, note))
    return (kind, "observed", "%s: %s" % (label, _short(shown, 200) if shown else note), body)


def ingest(store, entries, images=True):
    """Append events for these transcript entries. Returns number created.
    images=False skips screenshots (only for comparing against the old behaviour)."""
    pending, created = {}, 0
    imgs = prepare_images(store, entries) if images else {}
    for e in entries:
        base = dict(ts=e.get("timestamp"), session_id=e.get("sessionId"),
                    model=e["message"].get("model"))
        for n, b in enumerate(blocks(e)):
            key = "%s:%d" % (e.get("uuid"), n)
            t = b.get("type")
            ev, extra = None, []
            if t == "image" and e["type"] == "user":
                ev = imgs.get(key)
            elif t == "text" and e["type"] == "user":
                text = REMINDER.sub("", b.get("text", "")).strip()
                if e.get("isMeta") and text:
                    # Injected by the app (skill bodies, caveats), not typed by anyone.
                    ev = ("injected_context", "observed",
                          "Injected context: " + _short(text), text)
                else:
                    ev = _user_text_event(text)
            elif t == "text" and e["type"] == "assistant" and b.get("text", "").strip():
                text = b["text"].strip()
                ev = ("assistant_text", "claimed", "Assistant: " + _short(text), text)
            elif t == "tool_use":
                pending[b.get("id")] = b
            elif t == "tool_result":
                if isinstance(b.get("content"), list):
                    extra = [("%s:img%d" % (key, k), imgs[k2]) for k, k2 in
                             ((k, "%s:img%d" % (key, k)) for k in range(len(b["content"])))
                             if k2 in imgs]
                call = pending.pop(b.get("tool_use_id"), None)
                if call:
                    ev = _tool_event(call, b)
                    if ev and ev[0] in FILE_TOOLS.values():
                        base = dict(base, paths=[call["input"].get("file_path")
                                                 or call["input"].get("notebook_path")])
            if ev:
                kind, status, summary, body = ev
                _, new = store.append(key, kind, status, summary, body, **base)
                created += new
                base.pop("paths", None)
            for ikey, iev in extra:
                _, new = store.append(ikey, *iev, **base)
                created += new
    # Tool calls still waiting for their result at this moment (e.g. a switch
    # mid-write) are recorded as started, with their full input. The completed
    # event is added separately when the result arrives.
    for tid, call in pending.items():
        name, inp = call.get("name"), call.get("input") or {}
        path = inp.get("file_path") or inp.get("notebook_path")
        _, new = store.append("pending:%s" % tid, "tool_started", "observed",
                              "Started (no result yet) %s %s" % (
                                  name, path or _short(json.dumps(inp), 160)),
                              json.dumps(inp)[:20000], paths=[path] if path else None)
        created += new
    return created


# Tagged user-side text: (tag prefix, kind, status, label). Anything tagged that
# is not listed is still kept, as "notification", so nothing is dropped silently.
TAGGED = [
    ("<cross-session-message", "agent_message", "stated", "Message from another agent"),
    ("<pasted_content", "user_prompt", "stated", "User pasted"),
    ("<bash-input>", "user_prompt", "stated", "User ran"),
    ("<command-name>", "local_command", "observed", "Slash command"),
    ("<local-command-stdout>", "local_command", "observed", "Command output"),
    ("<local-command-caveat>", None, None, None),  # boilerplate, no content
]


def _user_text_event(text):
    if not text:
        return None
    if not text.startswith("<"):
        return ("user_prompt", "stated", "User: " + _short(text), text)
    for prefix, kind, status, label in TAGGED:
        if text.startswith(prefix):
            if kind is None:
                return None
            plain = re.sub(r"<[^>]+>", " ", text)
            return (kind, status, "%s: %s" % (label, _short(plain)), text)
    plain = re.sub(r"<[^>]+>", " ", text)
    return ("notification", "observed", "Notification: " + _short(plain), text)


def _tool_event(call, result):
    name, inp = call.get("name"), call.get("input") or {}
    out = flatten(result.get("content"))
    err = bool(result.get("is_error"))
    if name == "Bash":
        cmd = inp.get("command", "")
        body = "$ %s\n%s" % (cmd, out)
        if TEST_CMD.search(cmd):
            failed = err or bool(re.search(r"\b(FAILED|FAIL:|Error|failed)\b", out))
            passed = bool(re.search(r"\b(OK|passed)\b", out)) and not failed
            kind = "test_passed" if passed else "test_failed" if failed else "test_run"
            return (kind, "observed",
                    "Test run (%s): %s" % (kind, _short(cmd, 160)), body)
        if re.search(r"\bgit commit\b", cmd):
            m = re.search(r"\[[^\]]* ([0-9a-f]{7,})\]", out)
            return ("git_commit", "observed", "Commit %s: %s" % (
                m.group(1) if m else "?", _short(cmd, 160)), body)
        return ("command", "observed", "$ %s%s" % (_short(cmd, 160),
                                                    " (error)" if err else ""), body)
    if name in FILE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path")
        return (FILE_TOOLS[name], "observed", "%s %s%s" % (
            name, path, " (error)" if err else ""), json.dumps(inp)[:20000])
    if name in READ_TOOLS:
        target = inp.get("file_path") or inp.get("pattern") or ""
        return ("file_read", "observed", "%s %s" % (name, _short(target, 160)),
                out[:20000])
    return ("tool_call", "observed", "%s %s" % (name, _short(json.dumps(inp), 160)),
            "%s\n%s" % (json.dumps(inp), out[:20000]))
