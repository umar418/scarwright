"""Masking for anything Scarwright prints or writes.

Commit messages and code lines can contain credentials. Redaction here is
deliberately aggressive: over-masking costs nothing, a leak costs trust.
Adapted from Glasswing's redaction pass.
"""
from __future__ import annotations

import html
import json
import math
import re
from collections import Counter
from functools import lru_cache
from importlib import resources
from typing import Iterable
from urllib.parse import urlsplit

import yaml

REDACTED = "<redacted>"
REDACTED_ID = "<redacted-id>"

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX_RE = re.compile(r"[0-9a-fA-F-]+")
_URL_RE = re.compile(r"[a-z][a-z0-9+.-]{1,20}://[^\s\"'<>)\]]+", re.I)
_USERPASS_RE = re.compile(r"((?:^|\s)(?:-u|--user)[=\s]+[^\s:]+:)(\S+)")
_SAID_RE = re.compile(r"(?i)(\b" + r"(?:api[-_ ]?key|secret|token|password|passphrase|passwd|pwd)" + r"(?:\s+(?:is|was|=|:))?\s+)([^\s\"',;)]{6,})")
_BASE64_RE = re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40,}={0,2}(?![A-Za-z0-9/+=])")
_ENV_REF_RE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%")
_OPAQUE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")

CRED_WORD = r"(?:api[-_]?key|apikey|access[-_]?key|secret|token|password|passphrase|passwd|pass|pwd|webhook|authorization|credential|private[-_]?key)"
# Field names that hold a credential -- but not counters/limits like "max_tokens",
# and not fields that hold the *name* of an env var or a helper command.
_CRED_KEY_RE = re.compile(CRED_WORD, re.I)
_NOT_CRED_KEY_RE = re.compile(
    r"(?:max|min|num|count|limit|budget|usage|total|input|output|context)[-_ ]?tokens?|tokens?[-_ ]?(?:limit|count|budget|usage)"
    r"|env[-_ ]?var|envvar|helper|[-_ ](?:file|path|url|uri|name|type|id)$",
    re.I,
)
_BEARER_RE = re.compile(r"\b(Bearer|Basic)\s+([A-Za-z0-9._~+/=-]{6,})", re.I)
_ASSIGN_RE = re.compile(
    r"((?:--?|\b)[A-Za-z0-9_.-]*" + CRED_WORD + r"[A-Za-z0-9_-]*[\"']?\s*[:=]\s*[\"']?(?:(?:Bearer|Basic)\s+)?)([^\s\"'&,;)]{4,})",
    re.I,
)
_FLAG_VALUE_RE = re.compile(r"(--[A-Za-z0-9-]*" + CRED_WORD + r"[A-Za-z0-9-]*\s+)([^\s\"'-][^\s\"']{3,})", re.I)
_CRED_FLAG_RE = re.compile(r"^--?[A-Za-z0-9-]*" + CRED_WORD + r"[A-Za-z0-9-]*$", re.I)
_PLACEHOLDER_WORDS = ("your", "example", "changeme", "change-me", "placeholder", "todo", "dummy", "insert", "replace")


def mask_secret(value: str) -> str:
    """Short, non-reversible preview. Short values are hidden completely."""
    value = value.strip()
    if len(value) < 20:
        return REDACTED
    return f"{value[:4]}…{value[-4:]}"


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def is_placeholder(value: str) -> bool:
    """True for values that are references or dummies rather than a literal credential."""
    v = value.strip()
    v = re.sub(r"^(Bearer|Basic)\s+", "", v, flags=re.I).strip()
    if len(v) < 3:
        return True
    # In a tool that only prints, hiding a harmless value costs nothing and showing a real one costs trust,
    # so only an unmistakable reference or dummy is let through.
    if _ENV_REF_RE.fullmatch(v) and (v.startswith(("${", "%")) or v[1:].isupper()):
        return True
    low = v.lower()
    has_digit = any(c.isdigit() for c in v)
    if v.startswith("<") and v.endswith(">") and not has_digit:
        return True
    if any(w in low for w in _PLACEHOLDER_WORDS) and not has_digit:
        return True
    if len(set(low)) <= 2:  # "xxxx", "****", "0000"
        return True
    if low in {"true", "false", "none", "null", "oauth", "oauth2", "basic", "bearer", "disabled", "enabled"}:
        return True
    return False


def is_credential_key(key: str) -> bool:
    return bool(_CRED_KEY_RE.search(key)) and not _NOT_CRED_KEY_RE.search(key)


def looks_opaque(token: str, min_len: int = 32) -> bool:
    """A random-looking token: long, mixes letters and digits, not just a hash/UUID."""
    if len(token) < min_len or not re.fullmatch(r"[A-Za-z0-9_+=-]+", token):
        return False  # paths, URLs and sentences are not tokens
    if _HEX_RE.fullmatch(token):  # hashes, commit ids, UUIDs: identifiers, not credentials
        return False
    core = re.sub(r"[^A-Za-z0-9]", "", token)
    letters = re.sub(r"[^A-Za-z]", "", core)
    digit_ratio = (len(core) - len(letters)) / max(len(core), 1)
    if not letters or digit_ratio == 0 or _entropy(token) < 3.8:
        return False
    if letters.islower() or letters.isupper():
        # single-case: random base32/36 is digit-heavy; names like "prod-replica-eu-west-1" are not
        mixes = sum(1 for a, b in zip(core, core[1:]) if a.isdigit() != b.isdigit())
        return digit_ratio >= 0.15 and mixes / max(len(core) - 1, 1) >= 0.2  # digits interleaved, not "name-2024"
    # mixed case: random text flips case constantly; CamelCaseWords2024 rarely does
    pairs = [(a, b) for a, b in zip(core, core[1:]) if a.isalpha() and b.isalpha()]
    flips = sum(1 for a, b in pairs if a.islower() != b.islower())
    return digit_ratio >= 0.04 and flips / max(len(pairs), 1) >= 0.3


def worth_masking(token: str) -> bool:
    """Looser than looks_opaque(): used only to decide what to HIDE, never what to report.
    Hides long hashes and anything with digits scattered through it or erratic letter case."""
    if looks_opaque(token):
        return True
    core = re.sub(r"[^A-Za-z0-9]", "", token)
    if len(core) < 28:
        return False
    digits = sum(c.isdigit() for c in core)
    mixes = sum(1 for a, b in zip(core, core[1:]) if a.isdigit() != b.isdigit())
    if digits >= 2 and mixes / (len(core) - 1) >= 0.08:
        return True
    pairs = [(a, b) for a, b in zip(core, core[1:]) if a.isalpha() and b.isalpha()]
    flips = sum(1 for a, b in pairs if a.islower() != b.islower())
    return len(pairs) >= 20 and flips / len(pairs) >= 0.3


@lru_cache(maxsize=1)
def _vendor_patterns() -> list[tuple[dict, re.Pattern]]:
    raw = resources.files("scarwright.registry").joinpath("secret_patterns.yaml").read_text(encoding="utf-8")
    return [(p, re.compile(p["pattern"])) for p in (yaml.safe_load(raw) or [])]


def find_vendor_secrets(text: str) -> list[tuple[int, int, str, dict]]:
    """Non-overlapping (start, end, value, pattern) matches of known credential formats."""
    hits: list[tuple[int, int, str, dict]] = []
    for pattern, rx in _vendor_patterns():
        for m in rx.finditer(text):
            value = m.group(0)
            if pattern.get("random_body"):  # generic-looking prefix: the rest must look random, not like words
                body = value.split("-", 1)[-1]
                if not (re.search(r"[0-9]", body) and re.search(r"[a-z]", body) and re.search(r"[A-Z]", body)):
                    continue
            if any(m.start() < e and s < m.end() for s, e, _, _ in hits):
                continue
            hits.append((m.start(), m.end(), value, pattern))
    return sorted(hits)


# ---------------------------------------------------------------- URLs


def _opaque_path_segment(seg: str) -> bool:
    return bool(_UUID_RE.fullmatch(seg)) or looks_opaque(seg, min_len=20) or (len(seg) >= 32 and bool(re.fullmatch(r"[A-Za-z0-9_-]+", seg)))


def redact_url(url: str) -> str:
    """Keep scheme, host and readable path words; hide everything that could be a credential."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return REDACTED
    if not host:
        return REDACTED
    userinfo = f"{REDACTED}@" if (parts.username or parts.password) else ""
    path = "/".join(REDACTED_ID if _opaque_path_segment(seg) else seg for seg in parts.path.split("/"))
    query = ""
    if parts.query:
        keys = [kv.split("=", 1)[0] for kv in parts.query.split("&") if kv]
        query = "?" + "&".join(f"{k}={REDACTED}" for k in keys)
    return f"{parts.scheme}://{userinfo}{host}{port}{path}{query}"


def url_credential_signals(url: str) -> list[tuple[str, str]]:
    """(description, raw value) for credentials embedded in a URL. Bare UUIDs are
    masked on display but NOT reported: they are usually just identifiers."""
    out: list[tuple[str, str]] = []
    try:
        parts = urlsplit(url)
    except ValueError:
        return out
    if parts.password and not is_placeholder(parts.password):
        out.append(("password in the URL", parts.password))
    for kv in parts.query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            if v and is_credential_key(k) and not is_placeholder(v):
                out.append((f"'{k}' value in the URL", v))
    for seg in parts.path.split("/"):
        if not _UUID_RE.fullmatch(seg) and not is_placeholder(seg) and looks_opaque(seg, min_len=24):
            out.append(("long access token in the URL path", seg))
    return out


def url_host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


# ---------------------------------------------------------------- redaction


def _mask_match(m: re.Match) -> str:
    value = m.group(2)
    return m.group(0) if is_placeholder(value) else m.group(1) + REDACTED


def _mask_bearer(m: re.Match) -> str:
    return m.group(0) if is_placeholder(m.group(2)) else f"{m.group(1)} {REDACTED}"


@lru_cache(maxsize=8192)
def redact_text(text: str) -> str:
    """Hide anything in a free-form string that could be a credential."""
    if not text or len(text) < 6:
        return text
    hits = find_vendor_secrets(text)
    if hits:
        out, last = [], 0
        for start, end, value, _ in hits:
            out.append(text[last:start])
            out.append(mask_secret(value))
            last = end
        out.append(text[last:])
        text = "".join(out)
    text = _URL_RE.sub(lambda m: redact_url(m.group(0)), text)
    text = _ASSIGN_RE.sub(_mask_match, text)
    text = _FLAG_VALUE_RE.sub(_mask_match, text)
    text = _BEARER_RE.sub(_mask_bearer, text)
    text = _USERPASS_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _SAID_RE.sub(lambda m: m.group(1) + REDACTED if any(c.isdigit() for c in m.group(2)) else m.group(0), text)
    text = _OPAQUE_TOKEN_RE.sub(lambda m: mask_secret(m.group(0)) if worth_masking(m.group(0)) else m.group(0), text)
    text = _BASE64_RE.sub(lambda m: mask_secret(m.group(0)) if _random_base64(m.group(0)) else m.group(0), text)
    return text


def _random_base64(token: str) -> bool:
    """Base64 that reads as random (mixed case, digits, high entropy), as opposed to a long file path."""
    return bool(re.search(r"[a-z]", token) and re.search(r"[A-Z]", token) and re.search(r"[0-9]", token)) and _entropy(token) >= 3.6


def safe_args(args: Iterable[str]) -> list[str]:
    """Redact a command's arguments, including a value that follows a credential flag."""
    out: list[str] = []
    hide_next = False
    for arg in args:
        if hide_next and not arg.startswith("-") and not is_placeholder(arg):
            out.append(REDACTED)
        else:
            out.append(redact_text(arg))
        hide_next = bool(_CRED_FLAG_RE.match(arg))
    return out


def safe_command_line(command: str | None, args: Iterable[str]) -> str:
    return " ".join(p for p in [redact_text(command) if command else None, *safe_args(args)] if p)


def scrub(rendered: str, raw_values: Iterable[str]) -> str:
    """Last line of defence: remove any detected raw secret from rendered output,
    in plain, JSON-escaped and HTML-escaped form."""
    for raw in sorted({v for v in raw_values if v and len(v) >= 6}, key=len, reverse=True):
        mask = mask_secret(raw)
        for variant in {raw, json.dumps(raw)[1:-1], html.escape(raw), json.dumps(raw, ensure_ascii=False)[1:-1]}:
            if variant in rendered:
                rendered = rendered.replace(variant, mask)
    return rendered


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def plain(text: object) -> str:
    """Text with control and direction-override characters removed. No masking: use for names that must stay exact."""
    return _CONTROL_RE.sub("", str(text))


def safe_path(path: str) -> bool:
    """A path that can be printed as it is: no control characters and nothing in it that looks like a credential."""
    return plain(path) == path and redact_text(path) == path


def is_plain(text: str) -> bool:
    """True when a name holds nothing that could steer a terminal and nothing that looks like a credential."""
    return bool(text) and plain(text) == text and redact_text(text) == text


def clean(text: str | None, limit: int = 160) -> str:
    """One safe display line: no control or direction-override characters, credentials masked, bounded length."""
    if not text:
        return ""
    raw = str(text)[:20000]
    # A credential wrapped across lines: find it in the unwrapped text, then hide each fragment of it.
    unwrapped = re.sub(r"[ \t]*[\r\n]+[ \t]*", "", raw)
    if unwrapped != raw:
        for _, _, value, _ in find_vendor_secrets(unwrapped):
            for piece in re.split(r"\s+", raw):
                if len(piece) >= 6 and piece in value:
                    raw = raw.replace(piece, REDACTED)
    line = " ".join(_CONTROL_RE.sub(" ", raw).split())
    line = redact_text(line)  # mask first, cut second, so a cut can never expose most of a token
    return line if len(line) <= limit else line[: limit - 1] + "…"
