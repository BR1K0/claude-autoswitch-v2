"""Strip keys, tokens and passwords from text before it is stored.

Used on text ACPMH derives itself (OCR of screenshots). A redacted value is
replaced by [REDACTED <kind>], so the text still says what was there.
"""
import re

PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                               re.S)),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}")),
    ("API key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}")),
    ("xAI key", re.compile(r"\bxai-[A-Za-z0-9]{16,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}|\bxapp-[A-Za-z0-9\-]{10,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{20,}")),
    ("Slack invite", re.compile(r"https?://join\.slack\.com/\S+")),
]
# "password: x", "api_key = x", "token=x": keep the label, drop the value.
LABELLED = re.compile(r"(?i)\b(pass(?:word|wd|code)?|pwd|secret|api[_ -]?key|token|"
                      r"auth[_ -]?key|access[_ -]?key)(\s*[:=]\s*|\s+is\s+)(\S+)")


def redact(text):
    """Return (clean_text, number_of_values_removed)."""
    if not text:
        return text, 0
    n = 0
    for label, rx in PATTERNS:
        text, k = rx.subn("[REDACTED %s]" % label, text)
        n += k

    def labelled(m):
        nonlocal n
        if m.group(3).startswith("[REDACTED"):
            return m.group(0)
        n += 1
        return "%s%s[REDACTED]" % (m.group(1), m.group(2))
    text = LABELLED.sub(labelled, text)
    return text, n
