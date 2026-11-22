"""Dependency manifests: which file is one, and which direct dependencies it declares."""
from __future__ import annotations

import json
import posixpath
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

_SKIP_DIRS = {"node_modules", "vendor", "third_party", "site-packages", ".venv", "venv", "dist", "build", "target", ".git"}
_REQ_FILE_RE = re.compile(r"^requirements[\w.-]*\.(?:txt|in)$", re.I)
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_NPM_ALIAS_RE = re.compile(r"^npm:((?:@[^/@]+/)?[^@]+)@?")
_EGG_RE = re.compile(r"[#&]egg=([A-Za-z0-9][A-Za-z0-9._-]*)")

# Distribution name -> import names, where they differ. Anything not listed is assumed to import under its own name.
PY_IMPORT_ALIASES = {
    "pyyaml": ["yaml"], "pillow": ["PIL"], "beautifulsoup4": ["bs4"], "scikit-learn": ["sklearn"],
    "python-dateutil": ["dateutil"], "opencv-python": ["cv2"], "opencv-python-headless": ["cv2"], "pyjwt": ["jwt"],
    "python-dotenv": ["dotenv"], "attrs": ["attr", "attrs"], "protobuf": ["google.protobuf"], "python-jose": ["jose"],
    "psycopg2-binary": ["psycopg2"], "pymysql": ["pymysql"], "python-multipart": ["multipart"], "pyzmq": ["zmq"],
    "msgpack-python": ["msgpack"], "google-cloud-storage": ["google.cloud.storage"], "typing-extensions": ["typing_extensions"],
    "setuptools": ["setuptools", "pkg_resources"], "python-magic": ["magic"], "pycryptodome": ["Crypto"], "pyserial": ["serial"],
}


_NAME_RE = {
    "npm": re.compile(r"^(?:@[A-Za-z0-9~][A-Za-z0-9._~-]*/)?[A-Za-z0-9~][A-Za-z0-9._~-]*$"),
    "pip": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    "cargo": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
    "go": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~/-]*$"),
}


def valid_name(ecosystem: str, name: str) -> bool:
    """A real package name for its ecosystem. Anything else in a manifest key is ignored, not echoed."""
    from scarwright.redact import is_plain
    rx = _NAME_RE.get(ecosystem)
    return bool(rx) and isinstance(name, str) and len(name) <= 214 and bool(rx.match(name)) and is_plain(name)


def ecosystem_of(path: str) -> str | None:
    parts = path.split("/")
    if any(p in _SKIP_DIRS for p in parts[:-1]):
        return None
    name = parts[-1]
    if name == "package.json":
        return "npm"
    if name == "pyproject.toml":
        return "pip"
    if _REQ_FILE_RE.match(name) or (len(parts) >= 2 and parts[-2] == "requirements" and name.endswith((".txt", ".in"))):
        return "pip"
    if name == "Cargo.toml":
        return "cargo"
    if name == "go.mod":
        return "go"
    return None


def pip_name(raw: str) -> str:
    return re.sub(r"[-_.]+", "-", raw).lower()


def _req_string_name(spec: object) -> str | None:
    if not isinstance(spec, str):
        return None
    m = _REQ_NAME_RE.match(spec)
    return pip_name(m.group(1)) if m else None


def _parse_requirements(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        egg = _EGG_RE.search(line)
        if egg:
            names.add(pip_name(egg.group(1)))
            continue
        if line.startswith("-") or "://" in line.split("@", 1)[0]:
            continue
        name = _req_string_name(line)
        if name:
            names.add(name)
    return names


def _parse_pyproject(text: str) -> set[str]:
    data = tomllib.loads(text)
    names: set[str] = set()

    def add_list(items):
        if isinstance(items, list):
            for item in items:
                n = _req_string_name(item)
                if n:
                    names.add(n)

    def add_keys(table):
        if isinstance(table, dict):
            names.update(pip_name(k) for k in table if isinstance(k, str) and k.lower() != "python")

    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    add_list(project.get("dependencies"))
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group in optional.values():
            add_list(group)
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            add_list(group)
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    add_keys(poetry.get("dependencies"))
    add_keys(poetry.get("dev-dependencies"))
    if isinstance(poetry.get("group"), dict):
        for group in poetry["group"].values():
            if isinstance(group, dict):
                add_keys(group.get("dependencies"))
    for owner in ("uv", "pdm"):
        section = tool.get(owner) if isinstance(tool.get(owner), dict) else {}
        dev = section.get("dev-dependencies")
        add_list(dev)
        if isinstance(dev, dict):  # pdm groups: {group: [requirements]}
            for group in dev.values():
                add_list(group)
    hatch = tool.get("hatch") if isinstance(tool.get("hatch"), dict) else {}
    envs = hatch.get("envs") if isinstance(hatch.get("envs"), dict) else {}
    for env in envs.values():
        if isinstance(env, dict):
            add_list(env.get("dependencies"))
            add_list(env.get("extra-dependencies"))
    return names


def _parse_package_json(text: str) -> set[str]:
    data = json.loads(text)
    names: set[str] = set()
    if isinstance(data, dict):
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            table = data.get(section)
            if isinstance(table, dict):
                for key, value in table.items():
                    if isinstance(key, str):
                        names.add(key)
                    alias = _NPM_ALIAS_RE.match(value) if isinstance(value, str) else None
                    if alias:
                        names.add(alias.group(1))  # "x": "npm:moment@2" installs moment
    return names


def _parse_cargo(text: str) -> set[str]:
    data = tomllib.loads(text)
    names: set[str] = set()

    def add(table):
        if isinstance(table, dict):
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                if isinstance(table.get(section), dict):
                    names.update(k for k in table[section] if isinstance(k, str))

    add(data)
    add(data.get("workspace"))
    if isinstance(data.get("target"), dict):
        for target in data["target"].values():
            add(target)
    return names


def _parse_go_mod(text: str) -> set[str]:
    names, in_block = set(), False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if in_block:
            if line.startswith(")"):
                in_block = False
            elif line:
                names.add(line.split()[0])
        elif line.startswith("require ("):
            in_block = True
        elif line.startswith("require "):
            bits = line.split()
            if len(bits) >= 2:
                names.add(bits[1])
    return names


_PARSERS = {"npm": _parse_package_json, "cargo": _parse_cargo, "go": _parse_go_mod}


def parse_manifest(path: str, data: bytes | None) -> set[str] | None:
    """Direct dependency names, or None when the file is absent or cannot be read as a manifest."""
    if data is None:
        return None
    eco = ecosystem_of(path)
    if eco is None:
        return None
    try:
        text = data.decode("utf-8-sig")
        if eco == "pip":
            names = _parse_pyproject(text) if path.endswith("pyproject.toml") else _parse_requirements(text)
        else:
            names = _PARSERS[eco](text)
        return {n for n in names if valid_name(eco, n)}
    except (UnicodeDecodeError, ValueError, RecursionError, TypeError, AttributeError):
        return None


def import_names(ecosystem: str, name: str) -> tuple[str | None, list[str]]:
    """(language, module names) used to import a dependency. Language None means usage cannot be checked yet."""
    if ecosystem == "npm":
        return "js", [name]
    if ecosystem == "pip":
        return "python", PY_IMPORT_ALIASES.get(name, [name.replace("-", "_")])
    return None, []


def manifest_scope(path: str) -> str:
    return posixpath.dirname(path)


def find_line(ecosystem: str, text: str, name: str) -> int | None:
    """The line that declares a dependency, not merely one that mentions the word."""
    esc = re.escape(name).replace(r"\-", "[-_.]")
    if ecosystem == "npm":
        rx = re.compile(r'"' + re.escape(name) + r'"\s*:|"npm:' + re.escape(name) + r'@')
    elif ecosystem == "go":
        rx = re.compile(r"(?:^|\s)" + re.escape(name) + r"\s+v")
    else:
        rx = re.compile(r"^\s*[\"']?" + esc + r"(?![A-Za-z0-9_.-])", re.I)
    for i, line in enumerate(text.splitlines(), 1):
        if rx.search(line):
            return i
    return None


def still_mentioned(data: bytes | None, name: str) -> bool:
    """The name still appears somewhere in the file as a whole word (another section, a URL, an override)."""
    if not data:
        return False
    esc = re.escape(name).replace(r"\-", "[-_.]")
    # "requests-2.31.0-py3-none-any.whl" names requests; "requests-toolbelt" does not.
    return bool(re.search(r"(?<![A-Za-z0-9_.-])" + esc + r"(?![A-Za-z0-9_]|-[A-Za-z])", data.decode("utf-8", "replace"), re.I))


def include_count(data: bytes | None) -> int:
    """How many other requirement files a pip requirements file pulls in."""
    if not data:
        return 0
    return len(re.findall(r"(?m)^\s*(?:-r|-c|--requirement|--constraint)\b", data.decode("utf-8", "replace")))
