"""UserPromptSubmit hook with the Autoswitch option.

Modes (AUTOSWITCH_MODE, or file $AUTOSWITCH_DIR/mode; legacy file `enabled` = enforce):
  off      the hook does nothing at all.
  hint     a systemMessage suggestion ("Router suggests: Haiku"), nothing else.
  shadow   same hint, plus a line in $AUTOSWITCH_DIR/shadow.jsonl of what enforce would do.
           Never blocks, queues or switches. Prompt text is NOT logged (length only).
  enforce  block the prompt and queue a request; the Watcher switches this chat's model and re-sends it.
Policy (gate, dwell guard, pins, Sonnet default) lives in policy.py. Any failure lets the prompt through.
"""
import json
import os
import re
import sys
import time
import socket

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP_ID = re.compile(r"^local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def env_num(name, default, kind=float):
    """A numeric env setting; a bad or non-positive value falls back to the default (with one note on stderr)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return kind(default)
    try:
        v = kind(raw)
        if v > 0:
            return v
    except ValueError:
        pass
    print(f"autoswitch: ignoring {name}={raw!r}, using {default}", file=sys.stderr)
    return kind(default)


TOKEN_BYTES = env_num("AUTOSWITCH_BYTES_PER_TOKEN", "4")




def score(url, body, timeout=3):
    """POST `body` to the scorer and return its JSON. A bare socket instead of urllib: importing urllib costs
    ~60 ms per prompt on a stock macOS Python, which has no precompiled standard library."""
    host, _, port = url.split("//", 1)[1].rstrip("/").partition(":")
    with socket.create_connection((host, int(port or 80)), timeout=timeout) as s:
        s.sendall(b"POST / HTTP/1.0\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (host.encode(), len(body)) + body)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, payload = data.partition(b"\r\n\r\n")
    if b" 200" not in head.split(b"\r\n", 1)[0]:
        raise ValueError("scorer error")
    return json.loads(payload)


def scorer_url():
    if os.environ.get("AUTOSWITCH_SCORER"):
        return os.environ["AUTOSWITCH_SCORER"]
    try:
        with open(sb_path("port"), encoding="utf-8") as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        port = 8765
    return f"http://127.0.0.1:{port}"


def sb_path(name):
    import sbqueue
    return sbqueue.path(name)


def first_prompt(transcript_path):
    """True if the chat has no assistant turn yet (transcript missing or without one)."""
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            return not any('"type":"assistant"' in line for line in f)
    except (OSError, TypeError):
        return True


def transcript_is_new(transcript_path):
    """Like first_prompt, but only a missing transcript counts as new. An existing chat whose transcript
    can't be read (permissions, a bad path type) is not new: its size is simply unknown."""
    if not transcript_path or not isinstance(transcript_path, str):
        return False
    try:
        with open(transcript_path) as f:
            return not any('"type":"assistant"' in line for line in f)
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False


MACHINE_TAGS = ("<task-notification", "<cross-session-message", "<side-sessions-event", "<ci-monitor-event")


def machine_traffic(raw):
    """True for turns that came from the app or another session, not a person typing.

    Re-sends from the Watcher are wrapped too, but consume_pass() has already let those through.
    """
    if os.environ.get("AUTOSWITCH_ROUTE_AGENT_MESSAGES") == "1":  # trials drive test chats via send_message
        return "<task-notification" in raw
    return any(tag in raw for tag in MACHINE_TAGS)


def history_tokens(transcript_path):
    """Rough history size: transcript bytes / 4, or None if unreadable. Overestimates: the transcript keeps
    the whole history, also after a compaction, so a compacted chat looks longer than it is. Errs towards staying."""
    try:
        return os.path.getsize(transcript_path) / TOKEN_BYTES
    except (OSError, TypeError):
        return None


def host_session_id():
    """The app's own session id, passed to hooks in the environment by the desktop app. None for a
    terminal `claude`, a headless `claude -p`, or anything else that is not a desktop chat: those can
    never be switched by the Watcher, so the hook must never block them."""
    sid = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID", "")
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop" and APP_ID.match(sid):
        return sid
    return None  # never guessed from a session record: that could be a different chat


# prompts that a single Monitor event line may not carry intact are passed through, never blocked
MAX_PROMPT_CHARS = env_num("AUTOSWITCH_MAX_PROMPT_CHARS", "6000", int)
MAX_LINE_CHARS = 16000  # length of the prompt as an ASCII-escaped JSON string


def carryable(prompt):
    try:
        prompt.encode("utf-8")  # lone surrogates and the like
        return len(prompt) <= MAX_PROMPT_CHARS and "\x00" not in prompt and len(json.dumps(prompt)) <= MAX_LINE_CHARS
    except (UnicodeError, ValueError):
        return False


def lost_note(lost):
    return f" | Autoswitch could not re-send your earlier prompt, please send it again: {lost!r}" if lost else ""


def shadow_log(rec):
    try:
        import sbqueue as sb

        os.makedirs(sb.BASE, exist_ok=True)
        with open(sb.path("shadow.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main():
    from context import clean, is_followup, last_context, model_input
    import policy
    import sbqueue as sb

    # Windows pipes default to the ANSI code page; the hook JSON is UTF-8. utf-8-sig because when PowerShell 5.1's
    # $OutputEncoding is UTF-8 with a BOM (the default is US-ASCII, no BOM), piping to a native exe prefixes a BOM,
    # which json.load rejects. utf-8-sig reads BOM-less input exactly like utf-8.
    sys.stdin.reconfigure(encoding="utf-8-sig")
    hook = json.load(sys.stdin)
    if sb.silent():
        return
    mode = sb.mode()
    if mode == "enforce" and os.environ.get("CLAUDE_CODE_ENTRYPOINT") != "claude-desktop":
        mode = "shadow"  # only desktop-app chats can be switched; never block the CLI or headless runs
    no_watcher = False
    if mode == "enforce" and not sb.watcher_alive():
        mode, no_watcher = "shadow", True  # nobody would re-send a blocked prompt: only log, never block
    raw = hook.get("prompt", "")
    rid = sb.consume_pass(raw) if mode == "enforce" else None
    if rid:
        sb.take_lost(hook.get("session_id"), rid)  # it did arrive (maybe after settle timed out): drop "please resend"
        return  # re-sent by the Watcher: loop guard, route nothing
    if machine_traffic(raw):
        return  # agent-to-agent messages and tool/monitor notifications are not the user's prompts
    prompt = sb.bare(raw)  # score and queue the user's text, not the app's wrapper
    if not prompt:
        return  # only app-injected text: nothing to route, never block
    if not clean(prompt) or prompt.lstrip().startswith("/"):
        return
    asked_followup = is_followup(prompt)
    followup = asked_followup
    prev_user = last_context(hook.get("transcript_path"))[0] if followup else ""
    scored = prev_user if prev_user else clean(prompt)
    followup = bool(prev_user)
    img = None
    if mode != "off":
        import images
        img = images.fresh_image()  # a pasted screenshot: never block (the re-send would drop it), score its text too
        if img and img["text"]:
            scored = f"{scored}\n\n[Text in the attached screenshot]\n{img['text']}"
    result = score(scorer_url(), json.dumps({"text": scored, "input": model_input(scored)}).encode())
    label = result["label"]
    confidence = result["probs"][label]
    note = ", from your previous request" if followup else ""
    msg = f"Router suggests: {label.capitalize()} ({confidence:.0%} confident{note})"

    if mode != "off":
        from resolve import resolve
        import pins

        app_id = host_session_id()
        info = resolve(hook.get("session_id"), wait=0)  # fallback source, mainly for the current model
        cur = info and info["model"]
        tokens = history_tokens(hook.get("transcript_path"))
        if app_id and info:
            pins.observe(app_id, cur)  # remembers the model seen; a change nobody here made pins the chat
        new_chat = not info and first_prompt(hook.get("transcript_path"))
        pinned = bool(app_id) and pins.is_pinned(hook.get("session_id"), app_id, cur, hook.get("transcript_path"))
        if not app_id:
            d = {"target": None, "reason": "not a desktop app chat: never blocked"}
        elif pinned:
            d = {"target": None, "reason": "pinned: model was picked by hand"}
        elif info and info["sessionId"] != app_id:
            d = {"target": None, "reason": "record belongs to a different chat: never blocked"}
        elif not carryable(prompt):
            d = {"target": None, "reason": "prompt too long or odd to carry: passed through"}
        elif new_chat:
            # the app writes a chat's record only after its first answer, so neither the chat id nor
            # its starting model is known yet. Only act when the router is confident; the Watcher
            # looks the chat up and sets the model (a no-op if it already matches) before re-sending.
            gate = policy.cfg()["down"]
            if asked_followup and not followup:  # "ok do it" as a first prompt: nothing to inherit a score from
                d = {"target": None, "reason": "new chat, follow-up with no context"}
            elif label in policy.MODELS and confidence >= gate:
                d = {"target": policy.MODELS[label], "reason": f"new chat, {label} {confidence:.0%} confident"}
            else:
                d = {"target": None, "reason": "new chat, below gate"}
        else:
            d = policy.decide(label, confidence, cur, tokens, pinned, enforce=(mode == "enforce"),
                              is_new=not info and transcript_is_new(hook.get("transcript_path")))
        if info and d["target"] == cur:
            d = {"target": None, "reason": "already on target model"}  # never block a chat that needs no switch
        shadow_log({"ts": time.time(), "mode": mode, "session": app_id, "new_chat": new_chat, "current": cur,
                    "label": label, "confidence": round(confidence, 3), "would_target": d["target"],
                    "reason": d["reason"], "prompt_chars": len(prompt), "history_tokens_est": None if tokens is None else int(tokens),
                    "history_bytes": None if tokens is None else int(tokens * TOKEN_BYTES), "pinned": pinned,
                    "screenshot": bool(img), "ocr_chars": len(img["text"]) if img else 0})
        if mode == "enforce" and d["target"] and app_id and (info or new_chat) and img:
            # screenshot: let this turn through untouched and switch the chat for the NEXT one (no re-send)
            sb.enqueue(app_id, d["target"], prompt, label, confidence,
                       cli_session_id=hook.get("session_id"), resend=False)
            msg += f" | screenshot attached: this answer stays on the current model, switching the chat to {policy.tier_of(d['target']).capitalize()} for your next message"
        elif mode == "enforce" and d["target"] and app_id and (info or new_chat) and sb.speed() == "instant":
            # instant: never hold the prompt. This answer runs on the current model, the switch applies from the next message.
            sb.enqueue(app_id, d["target"], prompt, label, confidence,
                       cli_session_id=hook.get("session_id"), resend=False)
            msg = f"\u21aa Switching to {policy.tier_of(d['target']).capitalize()}"
        elif mode == "enforce" and d["target"] and app_id and (info or new_chat):
            sb.enqueue(app_id, d["target"], prompt, label, confidence,
                       cli_session_id=hook.get("session_id"))
            # continue:false stops the turn with no "blocked" wording (the app shows "Operation stopped by hook:").
            # The text is what the user sees; the prompt is kept in the transcript, which is why the re-send is a second turn.
            print(json.dumps({"continue": False, "stopReason": f"\u21aa Switched to {policy.tier_of(d['target']).capitalize()}"}))
            return
    if no_watcher:
        msg += " | Autoswitch is on but no Watcher session is running, so nothing was switched (see `autoswitch watcher`)"
    print(json.dumps({"systemMessage": msg + lost_note(sb.take_lost(hook.get("session_id")))}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # scorer down, no app record, anything: never block the message
        if os.environ.get("AUTOSWITCH_DEBUG"):
            import traceback
            traceback.print_exc()
    sys.exit(0)
