"""Import extraction for Python and JavaScript/TypeScript, and resolution to comparable module keys.

A module key is a dotted name for Python ("pkg.core.db") and a specifier for JS
("react", "@scope/pkg/sub", or "./src/lib/api" for a repo-relative file).

Keys come in three strengths:
  import  - written in an import statement (or a literal dynamic import)
  use     - reached by attribute through an imported name, e.g. `httpx.Client`
  weak    - a use whose base name is also a local variable or parameter somewhere in the file
Counting "who still uses X" takes all three, so it errs towards "still used".
Decisions are only ever derived from import keys, and a change is only flagged for import or use keys.
"""
from __future__ import annotations

import ast
import bisect
import codecs
import posixpath
import re
import warnings
from dataclasses import dataclass
from typing import Iterable

PY_EXT = (".py", ".pyi", ".pyx")
JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".astro", ".mdx", ".html", ".htm")
_JS_STRIP_EXT = (".d.ts", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".json")
_JS_FILE_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".json", ".d.ts")
_ALIAS = ("@/", "~/", "#/")
SEP = {"python": ".", "js": "/"}

_PY_IMPORT_RE = re.compile(r"(?:^|;)[ \t]*import[ \t]+([^\n#;]+)", re.M)
_PY_FROM_RE = re.compile(r"(?:^|;)[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+([^\n#;]+)", re.M)
_LIT = '"\x00(\\d+)\x00"'
_JS_FROM_RE = re.compile(r"(?:^|[\n;}>])[ \t]*(?:import|export)\b([^;\"]{0,2000}?)\bfrom\s*" + _LIT)
_KEY_RE = re.compile(r"^[\w@$./:~#+-]{1,200}$")
MAX_SOURCE_CHARS = 1_000_000
MAX_IMPORTS = 5000


class TooComplex(Exception):
    """A source file too large or too dense to analyse in bounded time. Callers treat it as unreadable."""
_JS_SIDE_RE = re.compile(r"(?:^|[\n;}>])[ \t]*import\s*" + _LIT)
_JS_CALL_RE = re.compile(r"\b(?:require|import)\s*\(\s*" + _LIT + r"\s*[,)]")
_JS_BIND_RE = re.compile(r"(?:const|let|var|import)\s+([^=;\n]+?)=\s*(?:await\s+)?$")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_JS_NOT_NAMES = {"as", "type", "typeof", "default", "from", "import", "export"}


@dataclass(frozen=True)
class RawImport:
    module: str
    level: int
    names: tuple[str, ...]
    line: int
    attrs: tuple[str, ...] = ()  # set for a use such as `httpx.Client` reached through an imported name
    weak: bool = False           # the base name is also bound locally somewhere in the file


def lang_of(path: str) -> str | None:
    low = path.lower()
    if low.endswith(PY_EXT):
        return "python"
    if low.endswith(JS_EXT):
        return "js"
    return None


def decode(data: bytes) -> str | None:
    """Text of a source file, or None when it is not text we can read."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if data.startswith(codecs.BOM_UTF8):
        data = data[3:]
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "replace")


def _python_regex(text: str) -> list[RawImport]:
    """Fallback for files the parser rejects (old syntax, templates). Never drops an import line silently."""
    out = []
    for m in _PY_IMPORT_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if re.fullmatch(r"[\w.]+", name):
                out.append(RawImport(name, 0, (), line))
    for m in _PY_FROM_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        names = tuple(n.strip().split(" as ")[0].strip() for n in m.group(3).strip("() \t\\").split(","))
        out.append(RawImport(m.group(2), len(m.group(1)), tuple(n for n in names if re.fullmatch(r"\w+|\*", n)), line))
    return out


def _dynamic_target(node: ast.Call) -> str | None:
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
    if name not in ("import_module", "__import__") or not node.args:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and re.fullmatch(r"[\w.]+", arg.value) and not arg.value.startswith("."):
        return arg.value
    return None


def parse_python(text: str) -> list[RawImport]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _python_regex(text)
    out = []
    bound: dict[str, tuple[str, int]] = {}  # local name -> (module path it stands for, relative level)
    local: set[str] = set()
    try:
        nodes = list(ast.walk(tree))
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    out.append(RawImport(alias.name, 0, (), node.lineno))
                    if alias.asname:
                        bound[alias.asname] = (alias.name, 0)
                    else:
                        bound[alias.name.split(".")[0]] = (alias.name.split(".")[0], 0)
            elif isinstance(node, ast.ImportFrom):
                level = node.level or 0
                out.append(RawImport(node.module or "", level, tuple(a.name for a in node.names), node.lineno))
                for a in node.names:
                    if a.name != "*":
                        bound[a.asname or a.name] = (f"{node.module}.{a.name}" if node.module else a.name, level)
            elif isinstance(node, ast.Call):
                target = _dynamic_target(node)
                if target:
                    out.append(RawImport(target, 0, (), node.lineno))
            elif isinstance(node, ast.arg):
                local.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
        seen: set[tuple] = set()
        for node in nodes:
            if not isinstance(node, ast.Attribute):
                continue
            chain, cur = [], node
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and cur.id in bound and len(chain) <= 4:
                module, level = bound[cur.id]
                attrs = tuple(reversed(chain))
                if (module, level, attrs) not in seen:
                    seen.add((module, level, attrs))
                    out.append(RawImport(module, level, (), node.lineno, attrs, cur.id in local))
    except RecursionError:
        return _python_regex(text)
    return out


def _mask_js(text: str) -> tuple[str, list[str]]:
    """Drop comments and swap each string literal for a numbered placeholder, keeping line numbers.

    After this, text that only looks like an import because it sits in a comment or inside a string cannot match.
    """
    out: list[str] = []
    lits: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        d = text[i + 1] if i + 1 < n else ""
        if c == "/" and d == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and d == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " + "\n" * text.count("\n", i, j))
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j, n)
            out.append(f'"\x00{len(lits)}\x00"')
            lits.append(text[i + 1:j])
            i = j + 1 if j < n and text[j] == c else j
        elif c == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            j = min(j, n)
            body = text[i + 1:j]
            if "${" in body or "\n" in body:
                out.append("``" + "\n" * body.count("\n"))
            else:
                out.append(f'"\x00{len(lits)}\x00"')
                lits.append(body)
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), lits


def _js_names(clause: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(w for w in _IDENT_RE.findall(clause) if w not in _JS_NOT_NAMES))


def parse_js(text: str) -> list[RawImport]:
    masked, lits = _mask_js(text)
    out = []
    line_starts = [m.start() for m in re.finditer("\n", masked)]

    def add(m: re.Match, group: int, names: tuple[str, ...]):
        spec = lits[int(m.group(group))]
        if spec and "\n" not in spec:
            if len(out) >= MAX_IMPORTS:
                raise TooComplex
            out.append(RawImport(spec, 0, names, bisect.bisect_left(line_starts, m.start(group)) + 1))

    for m in _JS_FROM_RE.finditer(masked):
        add(m, 2, _js_names(m.group(1)))
    for m in _JS_SIDE_RE.finditer(masked):
        add(m, 1, ())
    for m in _JS_CALL_RE.finditer(masked):
        start = max(masked.rfind("\n", 0, m.start()) + 1, m.start() - 300)
        bind = _JS_BIND_RE.search(masked[start:m.start()])
        add(m, 1, _js_names(bind.group(1)) if bind else ())
    return out


def parse_or_none(path: str, data: bytes | None) -> list[RawImport] | None:
    """Imports of a source file, or None when it cannot be analysed (not text, too large, too dense)."""
    lang = lang_of(path)
    if lang is None:
        return []
    text = decode(data) if data is not None else None
    if text is None or len(text) > MAX_SOURCE_CHARS:
        return None
    try:
        raws = parse_python(text) if lang == "python" else parse_js(text)
    except TooComplex:
        return None
    return None if len(raws) > MAX_IMPORTS * 4 else raws


def parse(path: str, data: bytes | None) -> list[RawImport]:
    return parse_or_none(path, data) or []


def python_roots(paths: Iterable[str]) -> tuple[str, ...]:
    """Directories that hold top-level Python packages (e.g. "src", "backend", "")."""
    pkg_dirs = {posixpath.dirname(p) for p in paths if p.endswith("/__init__.py") or p == "__init__.py"}
    roots = {""}
    for d in pkg_dirs:
        top = d
        while posixpath.dirname(top) in pkg_dirs and top:
            top = posixpath.dirname(top)
        roots.add(posixpath.dirname(top))
    return tuple(sorted(roots, key=len, reverse=True))


def _strip_root(dir_path: str, roots: tuple[str, ...]) -> list[str]:
    for root in roots:
        if root and (dir_path == root or dir_path.startswith(root + "/")):
            rest = dir_path[len(root):].strip("/")
            return rest.split("/") if rest else []
    return dir_path.split("/") if dir_path else []


def _js_key(path: str, spec: str) -> str:
    spec = spec.split("?", 1)[0].rstrip("/")
    if not spec.startswith("."):
        return spec
    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    for ext in _JS_STRIP_EXT:
        if target.endswith(ext):
            target = target[: -len(ext)]
            break
    if target.endswith("/index"):
        target = target[: -len("/index")]
    return "./" + target


def module_of(path: str, r: RawImport, roots: tuple[str, ...] = ("",)) -> str | None:
    """The module key one raw import refers to (without any imported names or attributes)."""
    if not _KEY_RE.match(r.module or "x"):
        return None  # not something a real import names; never echoed
    if lang_of(path) == "js":
        return _js_key(path, r.module)
    if r.level:
        pkg = _strip_root(posixpath.dirname(path), roots)
        if r.level - 1 > len(pkg):
            return None
        parts = pkg[: len(pkg) - (r.level - 1)] + (r.module.split(".") if r.module else [])
    else:
        parts = r.module.split(".") if r.module else []
    return ".".join(parts) if parts else None


def resolve(path: str, raws: Iterable[RawImport], roots: tuple[str, ...] = ("",), mode: str = "all") -> dict[str, int]:
    """module key -> first line. mode: "imports" (import statements only), "strict" (plus sure uses), "all"."""
    lang = lang_of(path)
    keys: dict[str, int] = {}

    def add(key: str, line: int):
        if key and key not in keys:
            keys[key] = line

    for r in raws:
        module = module_of(path, r, roots)
        if not module:
            continue
        if lang == "js":
            add(module, r.line)
            continue
        if r.attrs:
            if mode == "imports" or (mode == "strict" and r.weak):
                continue
            for i in range(1, len(r.attrs) + 1):
                add(module + "." + ".".join(r.attrs[:i]), r.line)
            continue
        add(module, r.line)
        for name in r.names:
            if name != "*":
                add(f"{module}.{name}", r.line)
    return keys


def use_names(path: str, raws: Iterable[RawImport], roots: tuple[str, ...], module: str) -> set[str]:
    """The names a file uses to refer to a module or to things imported from it."""
    lang = lang_of(path)
    names = {re.split(r"[./:]", module)[-1]}
    for r in raws:
        if r.attrs:
            continue
        m = module_of(path, r, roots)
        if not m:
            continue
        if lang == "js":
            if matches(m, module, lang):
                names.update(r.names)
            continue
        if matches(m, module, lang):
            names.update(n for n in r.names if n != "*")
            if not r.names:
                names.add(m.split(".")[0])
        else:
            names.update(n for n in r.names if matches(f"{m}.{n}", module, lang))
    return {n for n in names if n}


def matches(key: str, module: str, lang: str) -> bool:
    if key == module or key.startswith(module + SEP[lang]):
        return True
    if lang == "js" and module.startswith("./") and key.startswith(_ALIAS):
        # "@/lib/api" may be the same file as "./src/lib/api"; without the project's alias map, assume it is.
        rest = key[2:]
        parts = module[2:].split("/")
        for i in range(max(1, len(parts) - 1)):  # at least two path components must agree
            suffix = "/".join(parts[i:])
            if rest == suffix or rest.startswith(suffix + "/"):
                return True
    return False


def prefixes(key: str, lang: str, limit: int = 4) -> list[str]:
    """Generalisations of a key, most general first: "a.b.c" -> ["a", "a.b", "a.b.c"]."""
    if lang == "js":
        if key.startswith("./"):
            parts = key[2:].split("/")
            return ["./" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)][-limit:]
        parts = key.split("/")
        head = 2 if key.startswith("@") and len(parts) >= 2 else 1
        return ["/".join(parts[:i]) for i in range(head, min(len(parts), head + limit - 1) + 1)]
    parts = key.split(".")
    return [".".join(parts[:i]) for i in range(1, min(len(parts), limit) + 1)]


def python_module_file(tree_paths, roots: tuple[str, ...], module: str) -> tuple[str | None, str | None]:
    """Locate a dotted name inside the repo.

    Returns (file, symbol): the module file that defines it and, when the last
    component is a name inside that module rather than a module itself, that name.
    (None, None) means the name does not resolve to repo code (external or gone).
    """
    parts = module.split(".")
    for root in roots:
        base = (root + "/" if root else "") + "/".join(parts)
        for cand in (base + ".py", base + "/__init__.py"):
            if cand in tree_paths:
                return cand, None
        if len(parts) >= 2:
            parent = (root + "/" if root else "") + "/".join(parts[:-1])
            for cand in (parent + ".py", parent + "/__init__.py"):
                if cand in tree_paths:
                    return cand, parts[-1]
    return None, None


def python_defines(text: str, symbol: str) -> bool | None:
    """Whether a module defines a top-level name. None when it cannot be told (star imports, unparseable)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    star = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name) and n.id == symbol:
                        return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name == "*":
                    star = True
                elif (a.asname or a.name.split(".")[0]) == symbol:
                    return True
    return None if star else False


def js_module_exists(tree_paths, key: str) -> bool | None:
    """For a repo-relative JS key ("./src/lib/api"): does a file still back it? None for package imports."""
    if not key.startswith("./"):
        return None
    base = key[2:]
    for ext in _JS_FILE_EXT:
        if base + ext in tree_paths or f"{base}/index{ext}" in tree_paths:
            return True
    return base in tree_paths


def js_tail(key: str) -> str:
    """Last two path components without alias prefixes, used to spot import-style rewrites."""
    parts = [p for p in re.sub(r"^(?:\./|@/|~/|#/)", "", key).split("/") if p not in (".", "..")]
    return "/".join(parts[-2:])
