"""The committed ledger: one reviewable file per proven decision under .scarwright/decisions/.

Everything under .scarwright/ may have been committed by someone else, so it is treated as hostile input:
nothing is written through a symlink, files are size-capped and type-checked, and every string is cleaned.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path

import yaml

from scarwright import manifests
from scarwright.models import Decision
from scarwright.redact import clean, plain
from scarwright.scan import ScanResult

DIR = ".scarwright"
MAX_FILE = 256_000
MAX_CACHE = 50_000_000
MAX_FILES = 5000
_USER_FIELDS = ("status", "enforcement", "note")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,120}$")
_HEX_RE = re.compile(r"^[0-9a-f]{7,64}$")
_TIERS = {"proven", "candidate", "covered", "moot", "reversed", "rejected", "stale"}
_STATUSES = {"active", "rejected", "retired", "superseded"}
_LEVELS = {"warn", "block", "off"}


class LedgerError(RuntimeError):
    pass


def decisions_dir(root: Path) -> Path:
    return root / DIR / "decisions"


def _real_dir(root: Path, *parts: str, create: bool = False) -> Path | None:
    """A directory inside the repository that is a real directory all the way down, or an error. Never a symlink."""
    p = root
    for part in parts:
        p = p / part
        if p.is_symlink():
            raise LedgerError(f"{p.relative_to(root)} is a symbolic link; refusing to read or write through it")
        if not p.exists():
            if not create:
                return None
            p.mkdir()
        elif not p.is_dir():
            raise LedgerError(f"{p.relative_to(root)} is not a folder")
    return p


def _write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise LedgerError(f"{path.name} is a symbolic link; refusing to write through it")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as e:
        raise LedgerError(f"cannot write {path.name}: {e.strerror or e}") from e
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _read_text(path: Path, limit: int = MAX_FILE) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read(path: Path) -> dict:
    """One ledger file as a plain mapping, or {} for anything unreadable, oversized, aliased or malformed."""
    text = _read_text(path)
    if text is None:
        return {}
    try:
        if any(isinstance(tok, (yaml.AliasToken, yaml.AnchorToken, yaml.TagToken)) for tok in yaml.scan(text)):
            return {}  # anchors and aliases are how a few bytes of YAML become gigabytes
        data = yaml.safe_load(text)
    except Exception:  # hostile input: any parser failure, including recursion limits, means "not a ledger file"
        return {}
    return data if isinstance(data, dict) else {}


def _str(value, limit: int = 300) -> str | None:
    return value if isinstance(value, str) and len(value) <= limit and plain(value) == value else None


def _valid_detector(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    kind = d.get("kind")
    if kind == "dependency":
        eco, name, manifest = d.get("ecosystem"), _str(d.get("name")), _str(d.get("manifest"))
        names = d.get("import_names") or []
        if eco not in ("npm", "pip", "cargo", "go") or not name or not manifest or not manifests.valid_name(eco, name):
            return None
        if not isinstance(names, list) or not all(_str(n) for n in names) or d.get("import_lang") not in (None, "python", "js"):
            return None
        return {"kind": kind, "ecosystem": eco, "name": name, "manifest": manifest, "import_names": names,
                "import_lang": d.get("import_lang"), "guard_imports": bool(d.get("guard_imports"))}
    if kind == "import_ban":
        module, scope = _str(d.get("module")), d.get("scope") or ""
        if d.get("lang") not in ("python", "js") or not module or _str(scope) is None:
            return None
        replaced = d.get("replaced_by")
        return {"kind": kind, "lang": d["lang"], "scope": scope, "module": module, "replaced_by": _str(replaced) if replaced else None}
    if kind == "revert":
        sig, reverted = d.get("signature"), d.get("reverted")
        if not isinstance(sig, dict) or not isinstance(reverted, str) or not _HEX_RE.match(reverted) or len(sig) > 100:
            return None
        clean_sig = {}
        for path, hashes in sig.items():
            if not _str(path) or not isinstance(hashes, list) or len(hashes) > 1000 or not all(isinstance(h, str) and _HEX_RE.match(h) for h in hashes):
                return None
            clean_sig[path] = hashes
        return {"kind": kind, "reverted": reverted, "signature": clean_sig}
    return None


def _decision(data: dict) -> Decision | None:
    """A Decision from untrusted file content, or None unless every field has the right type and a safe value."""
    decided = data.get("decided") if isinstance(data.get("decided"), dict) else {}
    detector = _valid_detector(data.get("detector"))
    did = data.get("id")
    if detector is None or not isinstance(did, str) or not _ID_RE.match(did):
        return None
    tier, status, level = data.get("tier", "candidate"), data.get("status", "active"), data.get("enforcement", "warn")
    if tier not in _TIERS or status not in _STATUSES or level not in _LEVELS:
        return None
    commit = decided.get("commit")
    basis = data.get("basis") if isinstance(data.get("basis"), list) else []
    return Decision(
        id=did, family=clean(str(data.get("family", "")), 30), statement=clean(str(data.get("statement", "")), 500),
        commit=commit if isinstance(commit, str) and _HEX_RE.match(commit) else "", date=clean(str(decided.get("date", "")), 30),
        author=clean(str(decided.get("author", "")), 60), subject=clean(str(decided.get("subject", "")), 160),
        detector=detector, basis=[clean(str(b), 300) for b in basis[:20]], tier=tier, status=status, enforcement=level,
    )


def load(root: Path) -> list[Decision]:
    out: list[Decision] = []
    try:
        folder = _real_dir(root, DIR, "decisions")
    except LedgerError:
        return out  # a ledger reached through a symlink is not trusted for anything
    if folder is None:
        return out
    for f in sorted(folder.glob("*.yaml"))[:MAX_FILES]:
        d = _decision(_read(f))
        if d is not None and d.id == f.stem:
            out.append(d)
    return out


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def write(root: Path, result: ScanResult) -> tuple[int, int]:
    """Write proven decisions, keep what the user set by hand. Returns (new, updated)."""
    folder = _real_dir(root, DIR, "decisions", create=True)
    ignore = root / DIR / ".gitignore"
    if not ignore.exists() and not ignore.is_symlink():
        _write(ignore, "cache/\n")
    new = updated = 0
    by_id = {d.id: d for d in result.decisions}
    for d in result.decisions:
        path = folder / f"{d.id}.yaml"
        exists = path.exists() or path.is_symlink()
        if d.tier != "proven" and not exists:
            continue
        data = d.to_ledger()
        old = _read(path) if exists else {}
        for key in _USER_FIELDS:
            if key == "note" and isinstance(old.get(key), str):
                data[key] = clean(old[key], 300)
            elif key == "status" and old.get(key) in _STATUSES or key == "enforcement" and old.get(key) in _LEVELS:
                data[key] = old[key]
        if old == data:
            continue
        _write(path, _dump(data))
        new, updated = (new + 1, updated) if not old else (new, updated + 1)
    # A file whose decision this scan no longer finds (history was rewritten, or a later decision replaced it)
    # must not go on being enforced on the strength of an old scan.
    for path in sorted(folder.glob("*.yaml"))[:MAX_FILES]:
        if path.stem in by_id:
            continue
        old = _decision(_read(path))
        if old is not None and old.tier == "proven":
            data = old.to_ledger()
            data.update(tier="stale", note="not found by the latest scan of this history; no longer enforced")
            _write(path, _dump(data))
            updated += 1
    for d in by_id.values():  # reflect hand-set fields on the in-memory result
        old = _read(folder / f"{d.id}.yaml")
        if old.get("status") in _STATUSES:
            d.status = old["status"]
        if old.get("enforcement") in _LEVELS:
            d.enforcement = old["enforcement"]
    cache = _real_dir(root, DIR, "cache", create=True)
    _write(cache / "scan.json", json.dumps(to_json(result), indent=1))
    return new, updated


def set_field(root: Path, decision_id: str, key: str, value: str) -> bool:
    if not _ID_RE.match(decision_id) or key not in _USER_FIELDS:
        return False
    if key == "status" and value not in _STATUSES or key == "enforcement" and value not in _LEVELS:
        return False
    folder = _real_dir(root, DIR, "decisions")
    if folder is None:
        return False
    path = folder / f"{decision_id}.yaml"
    d = _decision(_read(path))
    if d is None or d.id != decision_id:
        return False
    data = d.to_ledger()
    data[key] = clean(value, 300)
    _write(path, _dump(data))
    return True


def to_json(result: ScanResult) -> dict:
    def one(d: Decision) -> dict:
        row = d.to_ledger()
        row["reason"] = d.reason
        row["backtest"] = asdict(d.backtest)
        if row["detector"].get("kind") == "revert":
            row["detector"] = {k: v for k, v in row["detector"].items() if k != "signature"}
        return row

    return {
        "head": result.head,
        "commits_scanned": result.commits_scanned,
        "shallow": result.shallow,
        "unparsed_manifests": result.unparsed_manifests,
        "unreadable_sources": result.unreadable_sources,
        "unchecked_languages": result.unchecked_languages,
        "skipped_commits": result.skipped_commits,
        "errors": result.errors,
        "decisions": [one(d) for d in result.decisions],
    }


def _scrub(value, depth: int = 0):
    """Cache content with every string stripped of control characters; anything oddly shaped becomes None."""
    if depth > 8:
        return None
    if isinstance(value, str):
        return plain(value)[:2000]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value[:5000]]
    if isinstance(value, dict):
        return {plain(str(k))[:100]: _scrub(v, depth + 1) for k, v in list(value.items())[:200]}
    return None


def load_cache(root: Path) -> dict | None:
    """Results of the last scan. It lives in the repository folder, so it is read as untrusted input."""
    try:
        folder = _real_dir(root, DIR, "cache")
        text = _read_text(folder / "scan.json", MAX_CACHE) if folder else None
        data = _scrub(json.loads(text)) if text else None
    except (LedgerError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("decisions")
    data["decisions"] = [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)] if isinstance(rows, list) else []
    return data
