"""Check a proposed change against the proven decisions in the ledger. Deterministic; no model, no network."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scarwright import detectors, ledger
from scarwright.gitio import MAX_BLOB, Repo
from scarwright.models import Decision, Settings


@dataclass
class Finding:
    decision: Decision
    path: str
    line: int | None
    detail: str

    @property
    def blocking(self) -> bool:
        return self.decision.enforcement == "block"

    def to_json(self) -> dict:
        d = self.decision
        return {
            "decision": d.id, "level": d.enforcement, "path": self.path, "line": self.line, "detail": self.detail,
            "statement": d.statement, "evidence": {"commit": d.commit, "date": d.date, "subject": d.subject},
        }


def _worktree_bytes(root: Path, path: str) -> bytes | None:
    target = root / path
    try:
        if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_BLOB:
            return None
        return target.read_bytes()
    except OSError:
        return None


def check(repo: Repo, mode: str = "staged", base: str | None = None, settings: Settings | None = None) -> list[Finding]:
    """mode: "staged" (index vs HEAD), "worktree" (files on disk vs HEAD), or "range" (base..HEAD)."""
    settings = settings or Settings()
    decisions = [d for d in ledger.load(repo.root) if d.enforced]
    if not decisions:
        return []
    index = detectors.Index(repo)
    head = repo.head()
    old_sha = head
    if mode == "staged":
        changes = repo.staged_changes()
        read_new = repo.read_index
    elif mode == "worktree":
        changes = repo.worktree_changes()
        read_new = lambda p: _worktree_bytes(repo.root, p)  # noqa: E731
    elif mode == "range":
        old_sha = repo.resolve(base or "")
        if not old_sha:
            raise ValueError(f"unknown base revision: {base!r}")
        changes = repo.range_changes(old_sha, head)
        read_new = lambda p: repo.read(head, p)  # noqa: E731
    else:
        raise ValueError(f"unknown mode: {mode!r}")

    built = []
    for d in decisions:
        try:
            built.append((d, detectors.build(index, d.detector, settings)))
        except (ValueError, KeyError, TypeError):
            continue  # a malformed ledger entry never produces a finding
    findings: list[Finding] = []
    new_content: dict[str, bytes | None] = {}
    for path, was in changes:
        relevant = [(d, det) for d, det in built if det.relevant(path)]
        if not relevant:
            continue
        # A renamed file is compared with what it was before the rename, so moving a file is not a new violation.
        old, new = repo.read(old_sha, was), read_new(path)
        new_content[path] = new
        root = index.root_for(head, path)
        for d, det in relevant:
            for hit in det.new_hits(path, old, new, root):
                findings.append(Finding(d, hit.path, hit.line, hit.detail))
    for d, det in built:
        if hasattr(det, "tree_hits") and any(det.relevant(p) for p in new_content):
            for hit in det.tree_hits(old_sha, {p: c for p, c in new_content.items() if det.relevant(p)}):
                findings.append(Finding(d, hit.path, hit.line, hit.detail))
    return findings
