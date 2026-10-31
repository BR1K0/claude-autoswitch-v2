"""UserPromptSubmit hook with the Autoswitch option.

Modes (AUTOSWITCH_MODE, or file $AUTOSWITCH_DIR/mode; legacy file `enabled` = enforce):
  off      the hook does nothing at all.
  hint     a systemMessage suggestion ("Router suggests: Haiku"), nothing else.
  shadow   same hint, plus a line in $AUTOSWITCH_DIR/shadow.jsonl of what enforce would do.
           Never blocks, queues or switches. Prompt text is NOT logged (length only).
  enforce  block the prompt and queue a request; the Watcher switches this chat's model and re-sends it.
Policy (gate, dwell guard, pins, Sonnet default) lives in policy.py. Any failure lets the prompt through.
Speeds (sbqueue.speed): seamless, instant, predictive (typeahead.py), inline (the autoswitch-inline plugin).
AUTOSWITCH_DRY=1: print the decision as one JSON line and change nothing (used by typeahead.py and the plugin).
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


DRY = os.environ.get("AUTOSWITCH_DRY") == "1"  # typeahead.py and the inline plugin ask for the decision: print it, change nothing
HOLD_CHARS = env_num("AUTOSWITCH_HOLD_CHARS", "400", int)
UPGRADE_MAX_TOKENS = env_num("AUTOSWITCH_UPGRADE_MAX_TOKENS", "100000")
TIERS = ("haiku", "sonnet", "opus")


def predictive_hold(prompt, cur, target):
    """Predictive speed, at Enter, when the switch it wanted has not landed (a new chat's first message, or a send
    within ~3 s of the draft settling): hold this one message like seamless only when it is worth the wait.
    - an up-switch: otherwise the message is answered by a weaker model than it needs;
    - a big message (AUTOSWITCH_HOLD_CHARS, 400+): a long task would run all its turns on the wrong model.
    A short message that only wanted a cheaper model goes through at once; the switch applies from the next one."""
    import policy
    import sbqueue
    if sbqueue.speed() != "predictive":
        return False
    a, b = policy.tier_of(cur), policy.tier_of(target)
    up = a in TIERS and b in TIERS and TIERS.index(b) > TIERS.index(a)
    return up or len(prompt) >= HOLD_CHARS


def stop_text(tier):
    """The text after the app's own "Operation stopped by hook:" prefix. The `stoptext` file set to `hide` leaves
    only the prefix: a single space, because without a stopReason the app does not stop the message at all."""
    try:
        with open(sb_path("stoptext"), encoding="utf-8") as f:
            if f.read().strip().lower() == "hide":
                return " "
    except (OSError, UnicodeError, ValueError):
        pass
    return f"↪ Switched to {tier.capitalize()}"


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
    if sb.speed() == "inline" and not DRY and not sb.inline_marked(hook.get("session_id")):
        os.environ["AUTOSWITCH_SPEED"] = "predictive"  # this chat has no plugin (opened before it was installed)
    inline = sb.speed() == "inline"
    mode = sb.mode()
    ho = mode != "off" and _handoff_on()  # Autoswitch v2: every new path below is behind `ho`
    ro = ho and _rollover_on() and not DRY  # Step 4 rollover: every rollover path is behind `ro` as well
    paused = False
    no_watcher = False
    if mode == "enforce" and os.environ.get("CLAUDE_CODE_ENTRYPOINT") != "claude-desktop":
        mode = "shadow"  # only desktop-app chats can be switched; never block the CLI or headless runs
    if mode == "enforce" and not DRY and not sb.watcher_alive():
        mode, no_watcher = "shadow", True  # nobody would re-send a blocked prompt: only log, never block
        paused = not inline  # inline needs no Watcher in a chat with the plugin: no "paused" note there
    raw = hook.get("prompt", "")
    if ho and not DRY:
        import handoff
        got = handoff.deliver(raw, host_session_id(), hook.get("session_id"))
        if got is not None:  # part 2's first message: delivered exactly once, never routed
            if got["out"]:
                print(json.dumps(got["out"]))
            return
        try:
            handoff.sweep_held()  # held requests no Watcher claimed: turned into "please resend" notes
        except Exception:
            pass
    if ro:
        import rollover
        got = rollover.deliver(raw, host_session_id(), hook.get("session_id"))
        if got is not None:  # part 2's first message of a rollover: delivered exactly once, never routed
            if got["out"]:
                print(json.dumps(got["out"]))
            return
        try:
            rollover.recover()  # unfinished rollovers: resumed or undone
        except Exception:
            pass
    rid = sb.consume_pass(raw) if mode == "enforce" and not DRY else None
    if rid:
        sb.take_lost(hook.get("session_id"), rid)  # it did arrive (maybe after settle timed out): drop "please resend"
        return  # re-sent by the Watcher: loop guard, route nothing
    if machine_traffic(raw):
        if ro:  # a continued part 1 still gets events (monitors, linked sessions): never do part 2's work here
            cont = rollover.continued(host_session_id()) if host_session_id() else None
            if cont:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                         "additionalContext": rollover.part1_context(cont, machine=True)}}))
        return  # agent-to-agent messages and tool/monitor notifications are not the user's prompts
    prompt = sb.bare(raw)  # score and queue the user's text, not the app's wrapper
    if not prompt:
        return  # only app-injected text: nothing to route, never block
    if not clean(prompt) or prompt.lstrip().startswith("/"):
        return
    if ho:
        import handoff
    ctx_on = ho and handoff.context_enabled()
    asked_followup = is_followup(prompt)
    followup = asked_followup
    prev_user = last_context(hook.get("transcript_path"))[0] if followup else ""
    scored = prev_user if prev_user and not ctx_on else clean(prompt)
    followup = bool(prev_user)
    img = None
    if mode != "off":
        import images
        img = images.fresh_image(remember=not DRY)  # a pasted screenshot: never block (the re-send would drop it), score its text too
        if img and img["text"]:
            scored = f"{scored}\n\n[Text in the attached screenshot]\n{img['text']}"
    result = _score(scored, model_input)
    label = result["label"]
    confidence = result["probs"][label]
    ext = {}
    info = None
    if ho:
        from resolve import resolve
        import ctxblend
        info = resolve(hook.get("session_id"), wait=0)
        previous = ctxblend.recent_user_messages(hook.get("transcript_path"))
        ext["ctx_blend"] = False
        if ctx_on:
            ctx = ctxblend.build_context((info or {}).get("title"), previous)
            if ctxblend.wants_context(clean(prompt), ctx):
                p_alone = ctxblend.vec(result["probs"])
                p_ctx = ctxblend.vec(_score(ctx + "\n" + scored, model_input)["probs"])
                p = ctxblend.limited(p_alone, p_ctx, clean(prompt), ctx)
                k = max(range(3), key=lambda j: p[j])
                label, confidence = ctxblend.NAMES[k], p[k]
                ext.update(ctx_blend=True, p_alone=[round(x, 3) for x in p_alone], p_blend=[round(x, 3) for x in p])
            followup = False  # the blend replaces the follow-up rule
        ext["new_task"] = ctxblend.new_task(clean(prompt), previous, (info or {}).get("title"))
    note = ", from your previous request" if followup else ""
    msg = f"Router suggests: {label.capitalize()} ({confidence:.0%} confident{note})"

    if mode != "off":
        from resolve import resolve
        import pins

        app_id = host_session_id()
        if not ho:
            info = resolve(hook.get("session_id"), wait=0)  # fallback source, mainly for the current model
        cur = info and info["model"]
        tokens = history_tokens(hook.get("transcript_path"))
        if app_id and info and not DRY:
            pins.observe(app_id, cur)  # remembers the model seen; a change nobody here made pins the chat
        new_chat = not info and first_prompt(hook.get("transcript_path"))
        pinned = bool(app_id) and pins.is_pinned(hook.get("session_id"), app_id, cur, hook.get("transcript_path"))
        handoff_to = None
        if ho:
            import budget
            import sizes
            size = sizes.estimate(hook.get("transcript_path"))
            bmode, pace_note = budget.effective_mode()
            pts = budget.calibration()
            conf_used = budget.calibrate(confidence, pts)
            ext.update(budget_mode=bmode, pace=pace_note, calibrated=bool(pts), size_cheap=_int(size["cheap"]),
                       size_precise=_int(size["precise"]), size_method=size["method"])
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
            gate = policy.cfg()["down"] if not ho else budget.gates(bmode)["down"]
            if asked_followup and not followup and not (ho and prev_user):  # "ok do it" as a first prompt: nothing to inherit a score from
                d = {"target": None, "reason": "new chat, follow-up with no context"}
            elif label in policy.MODELS and (confidence if not ho else conf_used) >= gate:
                d = {"target": policy.MODELS[label], "reason": f"new chat, {label} {confidence:.0%} confident"}
            else:
                d = {"target": None, "reason": "new chat, below gate"}
        elif not ho:
            d = policy.decide(label, confidence, cur, tokens, pinned, enforce=(mode == "enforce"),
                              is_new=not info and transcript_is_new(hook.get("transcript_path")))
        else:
            d, handoff_to = _decide_v2(label, conf_used, cur, size, pinned, mode, info, hook, bmode, pts, app_id,
                                       ext)
        if info and d["target"] == cur:
            d = {"target": None, "reason": "already on target model"}  # never block a chat that needs no switch
        if (not d["target"] and not pinned and not handoff_to and app_id and carryable(prompt)
                and not (info and info["sessionId"] != app_id)):
            # staying in this tier: move an older version (Opus 4.7, Sonnet 5...) to the tier's current model,
            # which is newer and cheaper. Not in a long chat: the first message on a new model re-reads it all.
            have = cur or os.environ.get("AUTOSWITCH_CURRENT_MODEL")  # the plugin passes the model a new chat runs on
            newer = policy.latest_in_tier(have)
            length = size["tokens"] if ho else tokens
            if newer and (length is None or length < UPGRADE_MAX_TOKENS):
                d = {"target": newer, "reason": f"same tier, newer model ({have} -> {newer})"}
        if DRY:  # typeahead.py / the inline plugin: the decision only, no side effects, nothing queued or shown
            if sb.mode() != "enforce":
                d = {"target": None, "reason": f"mode is {sb.raw_mode()}: nothing is switched"}
            print(json.dumps({"target": d["target"], "reason": d["reason"], "handoff_to": handoff_to,
                              "current": cur, "app_id": app_id, "label": label, "confidence": round(confidence, 3),
                              "screenshot": bool(img), "new_chat": bool(new_chat)}))
            return
        log = {"ts": time.time(), "mode": mode, "session": app_id, "new_chat": new_chat, "current": cur,
               "label": label, "confidence": round(confidence, 3), "would_target": d["target"],
               "reason": d["reason"], "prompt_chars": len(prompt), "history_tokens_est": None if tokens is None else int(tokens),
               "history_bytes": None if tokens is None else int(tokens * TOKEN_BYTES), "pinned": pinned,
               "screenshot": bool(img), "ocr_chars": len(img["text"]) if img else 0}
        ro_out = None
        if ro:
            try:
                ro_mode = "enforce" if sb.mode() == "enforce" and app_id else mode  # the chat rolls itself over: no Watcher needed
                ro_out, ro_log = rollover.on_prompt(hook, app_id, info, handoff_to, ro_mode, img, new_chat, prompt, bmode)
                log.update(ro_log, rollover=ro_log.get("rollover", True))
            except Exception:
                ro_out = None
        if ho:
            log.update(ext, handoff=True, would_handoff=handoff_to)
        shadow_log(log)
        if ho:
            notes = handoff.timeout_notes(app_id) if app_id else ""
            if paused:
                notes += handoff.paused_note(app_id or hook.get("session_id"))
            try:
                notes += handoff.watcher_size_note()
            except Exception:
                pass
            if ro and app_id:
                notes += rollover.take_note(app_id)
            if ro_out:
                ro_out["systemMessage"] = ro_out["systemMessage"] + notes + lost_note(sb.take_lost(hook.get("session_id")))
                print(json.dumps(ro_out))
                return
            if (mode == "enforce" and handoff_to and app_id and info and not img and not new_chat
                    and not handoff.open_for(app_id)):
                from resolve import record
                req = handoff.create(app_id, hook.get("session_id"), hook.get("transcript_path"), hook.get("cwd"),
                                     handoff_to, prompt, info, record(hook.get("session_id")), hook.get("permission_mode"), size, bmode)
                tgt = policy.tier_of(handoff_to).capitalize()
                print(json.dumps({"systemMessage": f"↪ Continuing in a new chat on {tgt} (part {req['part']})" + notes
                                  + lost_note(sb.take_lost(hook.get("session_id"))),
                                  "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                         "additionalContext": handoff.instruction(req)}}))
                return
            msg += notes
        if sb.mode() == "enforce" and inline:
            # inline: the autoswitch-inline plugin sends this turn's requests to the target itself, so nothing is
            # stopped, queued or shown here. Only notes the user must see (lost prompts, handoff timeouts).
            extra = (notes if ho else "") + lost_note(sb.take_lost(hook.get("session_id")))
            if extra.strip(" |"):
                print(json.dumps({"systemMessage": extra.strip(" |")}))
            return
        if mode == "enforce" and d["target"] and app_id and (info or new_chat) and img:
            # screenshot: let this turn through untouched and switch the chat for the NEXT one (no re-send)
            sb.enqueue(app_id, d["target"], prompt, label, confidence,
                       cli_session_id=hook.get("session_id"), resend=False)
            msg += f" | screenshot attached: this answer stays on the current model, switching the chat to {policy.tier_of(d['target']).capitalize()} for your next message"
        elif mode == "enforce" and d["target"] and app_id and (info or new_chat) and sb.never_holds() \
                and not predictive_hold(prompt, cur or os.environ.get("AUTOSWITCH_CURRENT_MODEL"), d["target"]):
            # instant / predictive: never hold the prompt. This answer runs on the current model, the switch applies
            # from the next message. In predictive mode the typed draft usually switched the chat already (then
            # d["target"] is None here); a typeahead switch to this target still in flight is not queued twice.
            if sb.open_switch(app_id) != d["target"]:
                sb.enqueue(app_id, d["target"], prompt, label, confidence,
                           cli_session_id=hook.get("session_id"), resend=False)
            msg = f"↪ Switching to {policy.tier_of(d['target']).capitalize()}"
        elif mode == "enforce" and d["target"] and app_id and (info or new_chat):
            if sb.speed() == "predictive":
                sb.drop_typeahead(app_id)  # this held prompt's request sets the model itself
            sb.enqueue(app_id, d["target"], prompt, label, confidence,
                       cli_session_id=hook.get("session_id"))
            # continue:false stops the turn with no "blocked" wording (the app shows "Operation stopped by hook:").
            # The text is what the user sees; the prompt is kept in the transcript, which is why the re-send is a second turn.
            print(json.dumps({"continue": False, "stopReason": stop_text(policy.tier_of(d['target']))}))
            return
    if DRY:
        return  # hint mode, or no decision to print: the caller leaves the model alone
    if no_watcher and not ho:  # v2 shows its own "paused" note once per chat instead
        msg += " | Autoswitch is on but no Watcher session is running, so nothing was switched (see `autoswitch watcher`)"
    print(json.dumps({"systemMessage": msg + lost_note(sb.take_lost(hook.get("session_id")))}))


def _handoff_on():
    try:
        import handoff
        return handoff.enabled()
    except Exception:
        return False


def _rollover_on():
    try:
        import rollover
        return rollover.active()
    except Exception:
        return False


def _int(x):
    return None if x is None else int(x)


def _score(text, model_input):
    return score(scorer_url(), json.dumps({"text": text, "input": model_input(text)}).encode())


def _decide_v2(label, confidence, cur, size, pinned, mode, info, hook, bmode, pts, app_id, ext):
    """Autoswitch v2 decision for a known chat. Returns (decision, handoff target model or None).
    1. the budget mode's gates pick the tier wanted (dwell guard off);
    2. handoff.plan() applies break-even, the zone and the lasting-switch rule;
    3. an in-place switch then runs today's cost check (dwell guard) with real tokens, no fixed cap."""
    import handoff
    import policy
    import budget
    import sizes
    enforce = mode == "enforce"
    is_new = not info and transcript_is_new(hook.get("transcript_path"))
    want_tier = label if label in policy.MODELS else None
    g = budget.gates(bmode, policy.tier_of(cur), want_tier, pts)
    ov = {"down": g["down"], "up": g["up"], "max_down_tokens": float("inf")}
    tokens = size["tokens"]
    free = policy.decide(label, confidence, cur, tokens, pinned, enforce=enforce, is_new=is_new, overrides=ov,
                         dwell=False)
    tgt_tier = policy.tier_of(free["target"])
    if tgt_tier and size["method"] != "precise" and tokens is not None \
            and tokens >= handoff.breakeven(tgt_tier) * (1 - policy._f("AUTOSWITCH_ZONE", "0.10")):
        size.update(sizes.estimate(hook.get("transcript_path"), force_precise=True))  # the zone: count precisely
        tokens = size["tokens"]
        ext.update(size_precise=_int(size["precise"]), size_method=size["method"])
    prev = handoff.last_want(app_id)
    if not DRY:  # a typed draft or a plugin query is not a request: only sent prompts count toward the lasting-switch rule
        handoff.record_want(app_id, tgt_tier)
    pl = handoff.plan(tgt_tier, policy.tier_of(cur), size, prev, ext.get("new_task", False), bmode)
    ext.update(plan=pl["action"], zone=pl["zone"], prev_want=prev)
    if pl["action"] == "inplace":
        d = policy.decide(label, confidence, cur, tokens, pinned, enforce=enforce, is_new=is_new, overrides=ov)
        return d, None
    if pl["action"] == "handoff":
        return {"target": None, "reason": pl["reason"]}, free["target"]
    return {"target": None, "reason": pl["reason"]}, None


if __name__ == "__main__":
    try:
        main()
    except Exception:  # scorer down, no app record, anything: never block the message
        if os.environ.get("AUTOSWITCH_DEBUG"):
            import traceback
            traceback.print_exc()
    sys.exit(0)
