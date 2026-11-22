"""Deterministic detectors. Each can report its state on any commit's tree and on a proposed file change.

No model is involved anywhere in this module.
"""
from __future__ import annotations

import hashlib
import math
import posixpath
import re
from dataclasses import dataclass
from typing import Callable

from scarwright import imports as imp
from scarwright import manifests
from scarwright.gitio import Repo

# Only directories that are third-party by convention. `build`, `dist`, `vendor` and `target` are ordinary
# source folders in plenty of projects, and skipping them made "nobody imports this" claims false.
_VENDORED = {"node_modules", "site-packages", ".venv", "third_party", "bower_components"}
_GENERATED_RE = re.compile(r"(?:\.lock|\.min\.(?:js|css)|\.map|\.snap|\.svg|\.ipynb)$|(?:^|/)(?:package-lock\.json|pnpm-lock\.yaml|uv\.lock|go\.sum)$", re.I)
_BOILERPLATE_RE = re.compile(
    r"^(?:from\s+\S+\s+import\b|import\b|export\s+\*|(?:const|let|var)\s+[^=]+=\s*require\(|#\s*include\b|using\s+\S+;|package\s+\S+|"
    r"(?:logger|log|LOGGER|LOG)\s*=\s*\S*(?:getLogger|get_logger)\b|[\w.]+\s*=\s*logging\.getLogger\b|module\.exports\s*=|"
    r"['\"]use strict['\"]|@\w+(?:\(\))?$|(?:pass|return|break|continue|else:|try:|finally:)$)"
)
_OPAQUE_EXT = {"python": (".ipynb",), "js": ()}


def in_scope(path: str, scope: str) -> bool:
    return not scope or path == scope or path.startswith(scope + "/")


def vendored(path: str) -> bool:
    return any(part in _VENDORED for part in path.split("/")[:-1])


def generated(path: str) -> bool:
    return bool(_GENERATED_RE.search(path))


def prose(path: str) -> bool:
    stem, _, ext = path.rsplit("/", 1)[-1].lower().rpartition(".")
    if not stem:
        stem, ext = ext, ""
    return ext in ("md", "rst", "txt", "adoc") or (not ext and stem in ("license", "changelog", "changes", "authors", "readme", "notice"))


def norm_line(line: str) -> str | None:
    """A line worth fingerprinting: long enough to be distinctive and not boilerplate any file might contain."""
    s = " ".join(line.split())
    if len(s) < 12 or not re.search(r"[A-Za-z0-9]{3}", s) or _BOILERPLATE_RE.match(s):
        return None
    return s


def line_hash(norm: str) -> str:
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:16]


def hash_lines(data: bytes | None) -> dict[str, int]:
    """hash -> first line number, for every fingerprintable line."""
    out: dict[str, int] = {}
    text = imp.decode(data) if data else None
    if text is None:
        return out
    for i, line in enumerate(text.splitlines(), 1):
        n = norm_line(line)
        if n:
            out.setdefault(line_hash(n), i)
    return out


class Index:
    """Parsing results cached by blob id, so replaying history re-parses only what changed."""

    def __init__(self, repo: Repo):
        self.repo = repo
        self._raw: dict[tuple[str, str], list[imp.RawImport] | None] = {}
        self._roots: dict[str, tuple[str, ...]] = {}
        self._manifest: dict[tuple[str, str], set[str] | None] = {}
        self._hashes: dict[str, dict[str, int]] = {}
        self._mentions: dict[tuple[str, str], bool] = {}
        self._keys: dict[tuple, dict[str, int]] = {}
        self.unparsed_manifests: set[str] = set()
        self.unreadable_sources: set[str] = set()

    def roots(self, sha: str) -> tuple[str, ...]:
        if sha not in self._roots:
            if len(self._roots) > 256:
                self._roots.pop(next(iter(self._roots)))
            self._roots[sha] = imp.python_roots(self.repo.tree(sha))
        return self._roots[sha]

    def root_for(self, sha: str, path: str) -> str:
        """The directory holding this file's top-level Python package: the first ancestor without an __init__.py."""
        d = posixpath.dirname(path)
        while d and self.repo.oid_at(sha, d + "/__init__.py"):
            d = posixpath.dirname(d)
        return d

    def raws(self, sha: str, path: str) -> list[imp.RawImport] | None:
        """Parsed imports of one file; None when the file exists but cannot be read (too large, not text)."""
        lang = imp.lang_of(path)
        oid = self.repo.oid_at(sha, path) if lang else None
        if oid is None:
            return []
        cache_key = (oid, lang)
        if cache_key not in self._raw:
            self._raw[cache_key] = imp.parse_or_none(path, self.repo.blob(oid))
        raws = self._raw[cache_key]
        if raws is None:
            self.unreadable_sources.add(path)
        return raws

    def keys(self, sha: str, path: str, mode: str = "all") -> dict[str, int]:
        raws = self.raws(sha, path)
        if not raws:
            return {}
        root = self.root_for(sha, path) if imp.lang_of(path) == "python" and any(r.level for r in raws) else ""
        cache_key = (id(raws), path, root, mode)
        if cache_key not in self._keys:
            if len(self._keys) > 200_000:
                self._keys.clear()
            self._keys[cache_key] = imp.resolve(path, raws, (root,), mode)
        return self._keys[cache_key]

    def manifest(self, sha: str, path: str) -> set[str] | None:
        oid = self.repo.oid_at(sha, path)
        if oid is None:
            return None
        cache_key = (oid, path.rsplit("/", 1)[-1])
        if cache_key not in self._manifest:
            names = manifests.parse_manifest(path, self.repo.blob(oid))
            if names is None:
                self.unparsed_manifests.add(path)
            self._manifest[cache_key] = names
        return self._manifest[cache_key]

    def line_hashes(self, sha: str, path: str) -> dict[str, int]:
        oid = self.repo.oid_at(sha, path)
        if oid is None:
            return {}
        if oid not in self._hashes:
            if len(self._hashes) > 4096:
                self._hashes.pop(next(iter(self._hashes)))
            self._hashes[oid] = hash_lines(self.repo.blob(oid))
        return self._hashes[oid]

    def _mentions_token(self, oid: str, token: str) -> bool:
        key = (oid, token)
        if key not in self._mentions:
            data = self.repo.blob(oid)
            self._mentions[key] = data is None or token.encode("utf-8", "replace") in data
        return self._mentions[key]

    def file_fires(self, sha: str, path: str, lang: str, modules: list[str]) -> bool:
        """Does this file use any of the modules? A source file that cannot be read counts as a yes."""
        raws = self.raws(sha, path)
        if raws is None:
            return True
        return any(imp.matches(k, m, lang) for k in self.keys(sha, path) for m in modules)

    def importers(self, sha: str, scope: str, lang: str, modules: list[str]) -> list[str]:
        """Files in scope that use a module, erring towards "yes" for anything that cannot be analysed."""
        out = []
        tokens = [re.split(r"[./:]", m)[-1] for m in modules]
        opaque = _OPAQUE_EXT[lang]
        for path, oid in self.repo.tree(sha).items():
            if not in_scope(path, scope) or vendored(path):
                continue
            if imp.lang_of(path) == lang:
                if self.file_fires(sha, path, lang, modules):
                    out.append(path)
            elif opaque and path.lower().endswith(opaque) and any(self._mentions_token(oid, t) for t in tokens):
                out.append(path)  # a notebook that mentions the name: cannot rule it out
        return out


@dataclass
class Hit:
    path: str
    line: int | None
    detail: str


class ImportBanDetector:
    def __init__(self, index: Index, d: dict):
        self.index = index
        self.lang = str(d["lang"])
        self.scope = str(d.get("scope") or "")
        self.modules = [str(m) for m in (d.get("modules") or [d["module"]])]

    def relevant(self, path: str) -> bool:
        return imp.lang_of(path) == self.lang and in_scope(path, self.scope) and not vendored(path)

    def state(self, sha: str) -> frozenset[str]:
        return frozenset(self.index.importers(sha, self.scope, self.lang, self.modules))

    def update(self, prev: frozenset[str], sha: str, changed: list[str]) -> frozenset[str]:
        state = set(prev)
        for path in changed:
            if self.index.repo.oid_at(sha, path) and self.index.file_fires(sha, path, self.lang, self.modules):
                state.add(path)
            else:
                state.discard(path)
        return frozenset(state)

    def _found(self, keys: dict[str, int]) -> list[tuple[int, str]]:
        return sorted((line, k) for k, line in keys.items() if any(imp.matches(k, m, self.lang) for m in self.modules))

    def new_hits(self, path: str, old: bytes | None, new: bytes | None, root: str) -> list[Hit]:
        if not self.relevant(path) or new is None:
            return []
        roots = (root,) if self.lang == "python" else ("",)
        if old is not None and self._found(imp.resolve(path, imp.parse(path, old), roots)):
            return []  # it was already there before this change
        raws = imp.parse(path, new)
        stated = self._found(imp.resolve(path, raws, roots, "imports"))
        if stated:
            return [Hit(path, stated[0][0], f"imports `{stated[0][1]}`")]
        used = self._found(imp.resolve(path, raws, roots, "strict"))
        return [Hit(path, used[0][0], f"uses `{used[0][1]}`")] if used else []


class DependencyDetector:
    def __init__(self, index: Index, d: dict):
        self.index = index
        self.ecosystem = str(d["ecosystem"])
        self.name = str(d["name"])
        self.manifest = str(d["manifest"])
        self.code: ImportBanDetector | None = None
        if d.get("guard_imports") and d.get("import_lang") and d.get("import_names"):
            self.code = ImportBanDetector(index, {
                "lang": d["import_lang"], "scope": manifests.manifest_scope(self.manifest), "modules": d["import_names"]})

    def relevant(self, path: str) -> bool:
        return path == self.manifest or bool(self.code and self.code.relevant(path))

    def _listed(self, sha: str) -> bool | None:
        """True/False when the manifest can be read; None when it exists but cannot (state unknown)."""
        if self.index.repo.oid_at(sha, self.manifest) is None:
            return False
        names = self.index.manifest(sha, self.manifest)
        return None if names is None else self.name in names

    def state(self, sha: str) -> frozenset[str]:
        out = set(self.code.state(sha)) if self.code else set()
        if self._listed(sha):
            out.add(self.manifest)
        return frozenset(out)

    def update(self, prev: frozenset[str], sha: str, changed: list[str]) -> frozenset[str]:
        state = set(self.code.update(frozenset(prev - {self.manifest}), sha, [p for p in changed if p != self.manifest])) if self.code else set()
        listed = self._listed(sha) if self.manifest in changed else self.manifest in prev
        if listed is None:
            listed = self.manifest in prev  # an unreadable manifest tells us nothing; keep what we knew
        if listed:
            state.add(self.manifest)
        return frozenset(state)

    def new_hits(self, path: str, old: bytes | None, new: bytes | None, root: str) -> list[Hit]:
        if path != self.manifest:
            return self.code.new_hits(path, old, new, root) if self.code else []
        after = manifests.parse_manifest(path, new)
        before = manifests.parse_manifest(path, old) or set()
        if after is None and new is not None and old is not None and self.name not in before and manifests.still_mentioned(new, self.name):
            return [Hit(path, None, f"can no longer be read as a manifest, and it mentions `{self.name}`, so that could not be ruled out")]
        if not after or self.name not in after or self.name in before:
            return []
        line = manifests.find_line(self.ecosystem, new.decode("utf-8", "replace"), self.name) if new else None
        return [Hit(path, line, f"adds dependency `{self.name}`")]


class RevertDetector:
    def __init__(self, index: Index, d: dict, min_lines: int = 3, match: float = 0.6):
        self.index = index
        self.signature = {str(p): set(map(str, hs)) for p, hs in (d.get("signature") or {}).items()}
        self.total = sum(len(hs) for hs in self.signature.values())
        self.need = max(min_lines, math.ceil(match * self.total))

    def relevant(self, path: str) -> bool:
        return path in self.signature

    def _matched(self, hashes_of: Callable[[str], dict[str, int]]) -> tuple[int, dict[str, int]]:
        matched, where = 0, {}
        for path, hashes in self.signature.items():
            present = hashes_of(path)
            found = hashes & present.keys()
            if found:
                matched += len(found)
                where[path] = min(present[h] for h in found)
        return matched, where

    def state(self, sha: str) -> frozenset[str]:
        matched, where = self._matched(lambda p: self.index.line_hashes(sha, p))
        return frozenset(where) if self.total and matched >= self.need else frozenset()

    def update(self, prev: frozenset[str], sha: str, changed: list[str]) -> frozenset[str]:
        return self.state(sha)

    def new_hits(self, path: str, old: bytes | None, new: bytes | None, root: str) -> list[Hit]:
        return []  # judged across all files at once, see tree_hits

    def tree_hits(self, old_sha: str, new_content: dict[str, bytes | None]) -> list[Hit]:
        """The change as a whole brings the reverted lines back, however they are spread across files."""
        if not self.total:
            return []
        before, _ = self._matched(lambda p: self.index.line_hashes(old_sha, p))
        if before >= self.need:
            return []
        after, where = self._matched(
            lambda p: hash_lines(new_content[p]) if p in new_content else self.index.line_hashes(old_sha, p))
        if after < self.need:
            return []
        changed = sorted(p for p in where if p in new_content) or sorted(where)
        return [Hit(changed[0], where[changed[0]], f"brings back {after} of {self.total} lines from the reverted change")]


def build(index: Index, d: dict, settings=None):
    kind = d.get("kind")
    if kind == "dependency":
        return DependencyDetector(index, d)
    if kind == "import_ban":
        return ImportBanDetector(index, d)
    if kind == "revert":
        if settings is not None:
            return RevertDetector(index, d, settings.min_signature_lines, settings.signature_match)
        return RevertDetector(index, d)
    raise ValueError(f"unknown detector kind: {kind!r}")
