"""Family 1: a direct dependency removed from a manifest."""
from __future__ import annotations

import hashlib

from scarwright import imports as imp
from scarwright import manifests
from scarwright.detectors import in_scope, vendored
from scarwright.gitio import Change, Commit
from scarwright.harvest import Ctx, slug
from scarwright.models import Decision
from scarwright.redact import clean, safe_path


def harvest(ctx: Ctx, commit: Commit, changes: list[Change]) -> list[Decision]:
    sha, parent = commit.sha, ctx.repo.parent_of(commit.sha)
    out: list[Decision] = []
    manifest_changes = [c for c in changes if manifests.ecosystem_of(c.path)]
    for ch in manifest_changes:
        if ch.status != "M" or not safe_path(ch.path):
            continue
        eco = manifests.ecosystem_of(ch.path)
        before = ctx.index.manifest(parent, ch.path)
        after = ctx.index.manifest(sha, ch.path)
        if before is None or after is None:
            continue
        added = sorted(after - before)
        after_bytes = ctx.repo.read(sha, ch.path)
        if eco == "pip" and manifests.include_count(after_bytes) > manifests.include_count(ctx.repo.read(parent, ch.path)):
            continue  # requirements were split into an included file; what looks removed may just have moved there
        for name in sorted(before - after):
            if _moved(ctx, parent, sha, eco, name, ch.path, manifest_changes):
                continue
            if manifests.still_mentioned(after_bytes, name):
                continue  # still named in the file (another section, a URL, an override): not a removal
            lang, modules = manifests.import_names(eco, name)
            scope = manifests.manifest_scope(ch.path)
            usage = _usage_files(ctx, parent, sha, changes, lang, modules, scope) if lang else []
            statement = f"`{name}` was removed from {ch.path}"
            basis = []
            if usage:
                statement += f" and {len(usage)} file(s) stopped importing it"
                where = f"under {scope}/" if scope else "in the repository"
                basis.append(f"{len(usage)} file(s) stopped importing it in the same commit, and no source file Scarwright could read {where} imported it afterwards")
            elif lang is None:
                basis.append(f"usage in code is not checked for {eco} yet")
            if 1 <= len(added) <= 3:
                basis.append("added in the same change: " + ", ".join(f"`{a}`" for a in added))
            suffix = "" if "/" not in ch.path else "-" + hashlib.sha256(ch.path.encode()).hexdigest()[:6]
            out.append(Decision(
                id=f"dep-{eco}-{slug(name)}{suffix}-{sha[:8]}", family="dependency", statement=statement + ".",
                commit=sha, date=commit.date, author=clean(commit.author, 60), subject=clean(commit.subject),
                detector={"kind": "dependency", "ecosystem": eco, "name": name, "manifest": ch.path, "import_names": modules,
                          "import_lang": lang, "guard_imports": bool(usage)},
                basis=basis, intent={"usage_files": len(usage)},
            ))
    return out


def _moved(ctx: Ctx, parent: str, sha: str, eco: str, name: str, path: str, manifest_changes: list[Change]) -> bool:
    """The dependency reappears in another manifest in the same commit: a move, not a removal."""
    for other in manifest_changes:
        if other.path == path or manifests.ecosystem_of(other.path) != eco or other.status == "D":
            continue
        now = ctx.index.manifest(sha, other.path) or set()
        was = ctx.index.manifest(parent, other.old_path or other.path) or set()
        if name in now and (other.status != "M" or name not in was):
            return True
    return False


def _usage_files(ctx: Ctx, parent: str, sha: str, changes: list[Change], lang: str, modules: list[str], scope: str) -> list[str]:
    files = []
    for c in changes:
        if c.status not in ("M", "R") or imp.lang_of(c.path) != lang or not in_scope(c.path, scope) or vendored(c.path):
            continue
        before = ctx.index.keys(parent, c.old_path or c.path)
        after = ctx.index.keys(sha, c.path)
        used = any(imp.matches(k, m, lang) for k in before for m in modules)
        still = any(imp.matches(k, m, lang) for k in after for m in modules)
        if used and not still:
            files.append(c.path)
    if files and ctx.index.importers(sha, scope, lang, modules):
        return []
    return files
