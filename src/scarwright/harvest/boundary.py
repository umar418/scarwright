"""Family 5: every file in a scope stops importing a module in one commit, and its uses are rewritten to something else."""
from __future__ import annotations

import math
import posixpath
import re
from collections import Counter, defaultdict

from scarwright import imports as imp
from scarwright.detectors import in_scope, vendored
from scarwright.gitio import Change, Commit
from scarwright.harvest import Ctx, slug
from scarwright.models import Decision
from scarwright.redact import clean, is_plain, safe_path

_NEVER = {"__future__"}
_IMPORT_LINE_RE = re.compile(r"^\s*(?:import\b|from\b.*\bimport\b|export\b.*\bfrom\b|.*\brequire\s*\()")
_LANG_NAME = {"python": "Python", "js": "JavaScript/TypeScript"}


def _ancestors(path: str) -> list[str]:
    out, d = [], posixpath.dirname(path)
    while d:
        out.append(d)
        d = posixpath.dirname(d)
    out.append("")
    return out


def _generalised(keys, lang: str) -> set[str]:
    out: set[str] = set()
    for key in keys:
        out.update(imp.prefixes(key, lang))
    return out


def harvest(ctx: Ctx, commit: Commit, changes: list[Change], skip: set[tuple[str, str]]) -> list[Decision]:
    s = ctx.settings
    sha, parent = commit.sha, ctx.repo.parent_of(commit.sha)
    mods = [c for c in changes if c.status in ("M", "R") and imp.lang_of(c.path) and not vendored(c.path)]
    if len(mods) < s.min_migrated_files:
        return []

    dropped: dict[tuple[str, str], list[str]] = defaultdict(list)
    added: dict[str, set[str]] = {}
    old_path = {c.path: c.old_path or c.path for c in mods}
    for c in mods:
        lang = imp.lang_of(c.path)
        # Decisions come only from what import statements say. Attribute use (`db.session.commit`) is evidence
        # that something is still used, never a thing to ban.
        stated_before = set(ctx.index.keys(parent, old_path[c.path], "imports"))
        stated_after = set(ctx.index.keys(sha, c.path, "imports"))
        all_after = set(ctx.index.keys(sha, c.path))
        added[c.path] = _generalised(stated_after - stated_before, lang)
        for b in _generalised(stated_before - stated_after, lang):
            if not any(imp.matches(k, b, lang) for k in all_after):
                dropped[(lang, b)].append(c.path)

    wide = sum(1 for files in dropped.values() if len(files) >= s.min_migrated_files)
    if wide > s.max_candidates_per_commit:
        return []  # a mass rewrite (rename, reformat, vendoring): no single decision can be read out of it
    accepted: list[tuple[str, str, str, list[str]]] = []
    for (lang, b), files in dropped.items():
        top = b.split(imp.SEP[lang])[0] if not b.startswith("./") else b
        if len(files) < s.min_migrated_files or top in _NEVER or (lang, top) in skip or (lang, b) in skip:
            continue
        scopes = sorted({a for f in files for a in _ancestors(f)}, key=lambda a: (a.count("/") + 1 if a else 0, a))
        for scope in scopes:
            migrated = [f for f in files if in_scope(f, scope)]
            if len(migrated) < s.min_migrated_files:
                continue
            if ctx.index.importers(sha, scope, lang, [b]):
                continue
            accepted.append((lang, scope, b, migrated))
            break

    kept = []
    for lang, scope, b, migrated in accepted:
        if any(
            (l2, s2, b2) != (lang, scope, b) and l2 == lang and in_scope(scope, s2) and imp.matches(b, b2, lang)
            for l2, s2, b2, _ in accepted
        ):
            continue  # a broader decision from the same commit already covers this one
        if not is_plain(b) or not safe_path(scope):
            continue  # a name or path that is not safe to print is never turned into a statement
        internal_before, _ = target_status(ctx, parent, lang, b)
        internal_after, exists_after = target_status(ctx, sha, lang, b)
        if internal_before and not (internal_after and exists_after):
            continue  # deleted or renamed: importing it again would simply fail, nothing to guard
        if lang == "js" and b.startswith(("./", "@/", "~/", "#/")):
            tail = imp.js_tail(b)
            if any(imp.js_tail(k) == tail for f in migrated for k in added[f]):
                continue  # same module, different import style
        in_scope_mods = [c.path for c in mods if imp.lang_of(c.path) == lang and in_scope(c.path, scope)]
        y = _replacement(lang, b, migrated, in_scope_mods, added, s.min_migrated_files)
        if y and not is_plain(y):
            y = None
        if y and not _rewritten(ctx, parent, sha, lang, b, y, migrated, old_path, s.min_migrated_files):
            y = None  # both changed in the same files, but nothing shows one was swapped for the other
        kept.append((lang, scope, b, migrated, y, internal_before))

    # One replacement can stand in for only one thing. If two dropped imports point at the same newcomer, the pairing is a guess.
    claims = Counter((lang, y) for lang, _, _, _, y, _ in kept if y)
    out: list[Decision] = []
    for lang, scope, b, migrated, y, internal in kept:
        replaced_by = y if y and claims[(lang, y)] == 1 else None
        where = f"Under {scope}/, " if scope else ""
        there = f"under {scope}/" if scope else "in the repository"
        left = f"leaving no {_LANG_NAME[lang]} file Scarwright could read {there} importing"
        if replaced_by:
            statement = (f"{where}{len(migrated)} files stopped importing `{b}` and switched to `{replaced_by}` "
                         f"in the same commit, {left} `{b}`.")
        else:
            statement = f"{where}{len(migrated)} files stopped importing `{b}` in the same commit, {left} it."
        statement = statement[0].upper() + statement[1:]
        out.append(Decision(
            id=f"imp-{slug(scope or 'root', 24)}-{slug(b, 32)}-{sha[:8]}", family="boundary", statement=statement,
            commit=sha, date=commit.date, author=clean(commit.author, 60), subject=clean(commit.subject),
            detector={"kind": "import_ban", "lang": lang, "scope": scope, "module": b, "replaced_by": replaced_by},
            basis=[f"{len(migrated)} files changed together in one commit"]
            + (["the same lines that used the old name were rewritten to use the new one"] if replaced_by else []),
            intent={"replaced_by": replaced_by, "migrated_files": len(migrated), "internal": internal},
        ))
    return out


def target_status(ctx: Ctx, sha: str, lang: str, b: str) -> tuple[bool, bool]:
    """(is repo code, can still be imported) for a module key at one commit. Outside code is (False, True)."""
    tree = ctx.repo.tree(sha)
    if lang == "python":
        file, symbol = imp.python_module_file(tree, ctx.index.roots(sha), b)
        if file is None:
            return False, True
        if symbol is None:
            return True, True
        data = ctx.repo.read(sha, file)
        return True, bool(data) and imp.python_defines(data.decode("utf-8", "replace"), symbol) is True
    found = imp.js_module_exists(tree, b)
    if found is None:
        return False, True
    if not found:
        prefix = b[2:] + "/"
        found = any(p.startswith(prefix) for p in tree)
    return True, bool(found)


def _replacement(lang: str, b: str, migrated: list[str], in_scope_mods: list[str], added: dict[str, set[str]], min_files: int) -> str | None:
    """Something most of the migrating files took up, and that few other files took up at the same time."""
    counts: Counter[str] = Counter()
    for f in migrated:
        counts.update(added[f])
    need = max(min_files, math.ceil(0.6 * len(migrated)))
    cands = []
    for g, n in counts.items():
        if n < need or imp.matches(g, b, lang) or imp.matches(b, g, lang) or g.split(imp.SEP[lang])[0] in _NEVER:
            continue
        adopters = sum(1 for f in in_scope_mods if g in added[f])
        if n / adopters < 0.6:
            continue  # being added all over the place, not specifically where `b` was dropped
        cands.append((n, g.count(imp.SEP[lang]), g))
    return max(cands)[2] if cands else None


def _word(names: set[str]) -> re.Pattern | None:
    names = {n for n in names if n}
    return re.compile(r"(?<![\w$])(?:" + "|".join(sorted(map(re.escape, names))) + r")(?![\w$])") if names else None


def _rewritten(ctx: Ctx, parent: str, sha: str, lang: str, b: str, y: str, migrated: list[str], old_path: dict[str, str], min_files: int) -> bool:
    """Proof that `y` took the place of `b`, file by file.

    Either the file imports the very same names as before, just from the new place, or some hunk removes a
    line of code using the old name and adds one using the new name. Two imports merely changing in the
    same file (a lint clean-up next to an unrelated addition) is not enough.
    """
    need = max(min_files, math.ceil(0.6 * len(migrated)))
    ok = 0
    for f in migrated:
        raws_old = ctx.index.raws(parent, old_path[f]) or []
        raws_new = ctx.index.raws(sha, f) or []
        root_old = (ctx.index.root_for(parent, old_path[f]),)
        root_new = (ctx.index.root_for(sha, f),)
        old_names = imp.use_names(old_path[f], raws_old, root_old, b)
        new_names = imp.use_names(f, raws_new, root_new, y)
        leaf_b, leaf_y = re.split(r"[./:]", b)[-1], re.split(r"[./:]", y)[-1]
        generic = {leaf_b, leaf_y, b.split(imp.SEP[lang])[0], y.split(imp.SEP[lang])[0]}
        shared = old_names & new_names
        if shared - generic or (leaf_b == leaf_y and shared):
            ok += 1  # the same names are imported as before, only from the new place
            continue
        rx_old, rx_new = _word(old_names), _word(new_names - old_names)
        oid_old, oid_new = ctx.repo.oid_at(parent, old_path[f]), ctx.repo.oid_at(sha, f)
        if not (rx_old and rx_new and oid_old and oid_new):
            continue
        for removed, added_lines in ctx.repo.diff_hunks(oid_old, oid_new):
            if any(rx_old.search(l) and not _IMPORT_LINE_RE.match(l) for l in removed) and \
               any(rx_new.search(l) and not _IMPORT_LINE_RE.match(l) for l in added_lines):
                ok += 1
                break
    return ok >= need
