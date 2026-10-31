import json
import re

FOLLOWUP_WORDS = re.compile(
    r"\b(do it|keep going|keep working|go ahead|continue|proceed|yes|yeah|yep|yup|sure|ok|okay|okd|go|again|same|that|this|it|them|those|redo|try again|done)\b"
)
QUESTION_START = re.compile(r"^(what|whats|what's|where|how|why|when|who|which|is|are|does|did)\b")
SKIP_PREFIXES = (
    "<local-command", "<command-name>", "<bash-", "<system-reminder>", "<task-notification",
    "Caveat:", "[Request interrupted",
)


def clean(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<ide_(opened_file|selection)>.*?</ide_\1>", "", text, flags=re.S)
    return text.strip()


def is_followup(text):
    t = clean(text).lower()
    words = t.split()
    if not words or len(words) > 8 or t.endswith("?") or QUESTION_START.match(t):
        return False
    return bool(FOLLOWUP_WORDS.search(t))


def model_input(text, prev_user="", prev_assistant=""):
    text = clean(text)
    if not is_followup(text):
        return text
    return f"{text} [replying to] {prev_assistant[-400:]} [earlier] {prev_user[-300:]}"


def _user_text(entry):
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = clean(content or "")
    if not text or text.startswith(SKIP_PREFIXES):
        return None
    return text


def _assistant_text(entry):
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return None
    content = (entry.get("message") or {}).get("content") or []
    text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
    return text or None


def walk(entries):
    """Yield (index, user_text, prev_user, prev_assistant) for each typed user message in a transcript."""
    prev_user, prev_assistant = "", ""
    for i, e in enumerate(entries):
        a = _assistant_text(e)
        if a:
            prev_assistant = a
            continue
        u = _user_text(e)
        if u:
            yield i, u, prev_user, prev_assistant
            prev_user = u


def last_context(transcript_path):
    """Previous typed user message and last assistant reply at the end of a transcript."""
    prev_user, prev_assistant = "", ""
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            entries = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError, TypeError):
        return prev_user, prev_assistant
    for e in entries:
        a = _assistant_text(e)
        if a:
            prev_assistant = a
            continue
        u = _user_text(e)
        if u:
            prev_user = u
    return prev_user, prev_assistant
