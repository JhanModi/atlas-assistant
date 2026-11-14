"""Output validation: everything the model says passes through clean_output() before it is shown."""

from __future__ import annotations

import re

import nh3

_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub classic tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),  # GitHub fine-grained
    re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),  # Google OAuth access token
    re.compile(r"1//[A-Za-z0-9_\-]{30,}"),  # Google refresh token
    re.compile(r"\b\d{3,6}~[A-Za-z0-9]{40,}\b"),  # Canvas access token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),  # generic API keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
]
_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_REF_IMAGE = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.MULTILINE)  # reference-style link targets


def redact_secrets(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def clean_output(text: str | None) -> str:
    """Remove reasoning blocks, HTML, Markdown images and secret-looking strings.

    The dashboard also renders with textContent, so this is defense in depth: even if some markup
    slipped through, it would never be interpreted and no remote resource would be fetched.
    """
    if not text:
        return ""
    text = _THINK.sub("", text)
    if "</think>" in text.lower():  # reasoning without an opening tag (some templates add <think> in the prompt)
        text = text[text.lower().rindex("</think>") + len("</think>"):]
    text = _MD_IMAGE.sub("[image removed]", text)
    text = _MD_REF_IMAGE.sub("", text)
    text = nh3.clean(text, tags=set(), attributes={}, strip_comments=True)
    # nh3 escapes &, <, > for HTML. We render as plain text, so turn the entities back into characters.
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    text = redact_secrets(text)
    return text.strip()


def wrap_untrusted(text: str | None, limit: int = 1500) -> str | None:
    """Mark third-party free text as data. Truncates to keep small models focused and limit injection surface."""
    if text is None:
        return None
    text = text.replace("<untrusted_content>", "").replace("</untrusted_content>", "")
    if len(text) > limit:
        text = text[:limit] + " …[truncated]"
    return f"<untrusted_content>{text}</untrusted_content>"
