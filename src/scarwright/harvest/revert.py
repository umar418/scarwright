"""Family 6: a change that was tried and explicitly reverted."""
from __future__ import annotations

import math
import re

from scarwright.detectors import generated, line_hash, norm_line, prose, vendored
from scarwright.gitio import Commit
from scarwright.harvest import Ctx
from scarwright.models import Decision
from scarwright.redact import clean, safe_path

_REVERTS_RE = re.compile(r"(?m)^This reverts commit ([0-9a-f]{7,64})\b")
_MAX_FILES = 30
_MAX_LINES_PER_FILE = 200


def harvest(ctx: Ctx, commit: Commit) -> list[Decision]:
    m = _REVERTS_RE.search(commit.body)
    if not m or commit.is_merge:
        return []
    target = ctx.repo.resolve(m.group(1))
    if not target or not ctx.repo.is_ancestor(target, commit.sha):
        return []
    original = ctx.repo.commit(target)
    if original.is_merge or _REVERTS_RE.search(original.body):
        return []  # reverting a revert re-lands the change; that is not a rejection

    sha, parent = commit.sha, ctx.repo.parent_of(commit.sha)
    signature: dict[str, list[str]] = {}
    original_lines = still_there = 0
    for ch in ctx.repo.changes(target)[: _MAX_FILES * 4]:
        if ch.status not in ("A", "M", "R") or not safe_path(ch.path) or vendored(ch.path) or generated(ch.path) or prose(ch.path):
            continue
        added, _ = ctx.repo.diff_lines(ctx.repo.parent_of(target), target, ch.path)
        before = ctx.index.line_hashes(parent, ch.path)
        after = ctx.index.line_hashes(sha, ch.path)
        keep: list[str] = []
        for line in added:
            n = norm_line(line)
            if not n:
                continue
            h = line_hash(n)
            if h not in before:
                continue
            original_lines += 1
            if h in after:
                still_there += 1
            elif h not in keep:
                keep.append(h)
        if keep:
            signature[ch.path] = keep[:_MAX_LINES_PER_FILE]
        if len(signature) >= _MAX_FILES:
            break
    total = sum(len(v) for v in signature.values())
    s = ctx.settings
    if total < s.min_signature_lines:
        return []
    if still_there > 0.2 * original_lines:
        return []  # most of the change is still in place: this undid part of it, which is not "the change was reverted"

    basis = [f"reverted explicitly; fingerprinted by {total} distinctive lines across {len(signature)} file(s)"]
    reason = _reason(commit.body)
    if reason:
        basis.append(f'reason given: "{reason}"')
    return [Decision(
        id=f"rev-{target[:8]}", family="revert",
        statement=f"The change “{clean(original.subject, 100)}” ({target[:8]}) was reverted.",
        commit=sha, date=commit.date, author=clean(commit.author, 60), subject=clean(commit.subject),
        detector={
            "kind": "revert", "reverted": target, "signature": signature,
            "threshold": max(s.min_signature_lines, math.ceil(s.signature_match * total)),
        },
        basis=basis, intent={},
    )]


def _reason(body: str) -> str:
    lines = [l.strip() for l in body.splitlines()]
    kept = [l for l in lines if l and not _REVERTS_RE.search(l) and not re.match(r"(?i)^(signed-off-by|co-authored-by|change-id):", l)]
    return clean(" ".join(kept), 200)
