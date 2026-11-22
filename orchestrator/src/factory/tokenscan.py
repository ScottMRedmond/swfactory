"""The token scan: finds token-like strings in text.

Run on agent prose before it's posted (a match makes the result `invalid`) and
on every orchestrator log line (a match is redacted). Matches the Claude token's
prefix (`sk-ant-`), GitHub tokens, JWTs (the App's) and PEM private keys.
"""

import re

PATTERNS: dict[str, re.Pattern[str]] = {
    "Claude token": re.compile(r"sk-ant-[A-Za-z0-9_-]*"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
    "private key": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
    ),
}

REDACTED = "[REDACTED]"


def find(text: str) -> list[str]:
    """The kinds of token found in `text` (empty if none)."""
    return [kind for kind, pattern in PATTERNS.items() if pattern.search(text)]


def redact(text: str) -> str:
    """`text` with every token-like string replaced by `[REDACTED]`."""
    for pattern in PATTERNS.values():
        text = pattern.sub(REDACTED, text)
    return text
