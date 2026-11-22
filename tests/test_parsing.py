from __future__ import annotations

from scarwright import imports as imp
from scarwright import manifests


def test_package_json_sections():
    data = b'{"dependencies": {"a": "1"}, "devDependencies": {"@s/b": "2"}, "scripts": {"x": "y"}}'
    assert manifests.parse_manifest("package.json", data) == {"a", "@s/b"}


def test_pyproject_all_layouts():
    text = b"""
[project]
dependencies = ["Requests>=2", "PyYAML ; python_version < '3.13'", "pkg[extra]==1.0"]
[project.optional-dependencies]
dev = ["pytest"]
[dependency-groups]
lint = ["ruff", {include-group = "dev"}]
[tool.poetry.dependencies]
python = "^3.10"
Typing_Extensions = "*"
[tool.uv]
dev-dependencies = ["mypy"]
"""
    assert manifests.parse_manifest("pyproject.toml", text) == {"requests", "pyyaml", "pkg", "pytest", "ruff", "typing-extensions", "mypy"}


def test_requirements_skips_options_and_urls():
    text = b"-r base.txt\nflask==3.0  # web\n# comment\nhttps://example.com/x.whl\ngit+https://example.com/r.git#egg=My_Pkg\nname @ https://example.com/n.whl\n-e .\n"
    assert manifests.parse_manifest("requirements-dev.txt", text) == {"flask", "my-pkg", "name"}


def test_cargo_and_go():
    cargo = b'[dependencies]\nserde = "1"\n[dev-dependencies]\ntokio = { version = "1" }\n[target.\'cfg(unix)\'.dependencies]\nlibc = "0.2"\n'
    assert manifests.parse_manifest("Cargo.toml", cargo) == {"serde", "tokio", "libc"}
    go = b"module x\n\nrequire (\n\tgithub.com/a/b v1.0.0\n\tgithub.com/c/d v1.0.0 // indirect\n)\nrequire github.com/e/f v2.0.0\n"
    assert manifests.parse_manifest("go.mod", go) == {"github.com/a/b", "github.com/c/d", "github.com/e/f"}  # indirect is still required


def test_unreadable_manifests_are_none_not_empty():
    for data in (b"{not json", b"\xff\xfe\x00", b"[" * 100_000, b"null"):
        result = manifests.parse_manifest("package.json", data)
        assert result is None or result == set()
    assert manifests.parse_manifest("pyproject.toml", b"[project\n") is None
    assert manifests.parse_manifest("package.json", None) is None


def test_vendored_manifests_ignored():
    assert manifests.ecosystem_of("node_modules/x/package.json") is None
    assert manifests.ecosystem_of("packages/x/package.json") == "npm"


def _keys(path: str, text: str, roots=("",)) -> set[str]:
    return set(imp.resolve(path, imp.parse(path, text.encode()), roots))


def test_python_absolute_relative_and_names():
    keys = _keys("src/pkg/api/views.py", "import os.path\nfrom ..core import db as d\nfrom . import helpers\nfrom pkg.x import *\n", ("src",))
    assert {"os.path", "pkg.core", "pkg.core.db", "pkg.api", "pkg.api.helpers", "pkg.x"} <= keys


def test_python_attribute_use_counts_as_use():
    """`import httpx` then `httpx.Client()` uses httpx.Client just as `from httpx import Client` does."""
    keys = _keys("t.py", "import httpx\nimport numpy as np\nfrom pkg import config\nc = httpx.Client()\nx = np.float(1)\ny = config.Timeout\n")
    assert {"httpx.Client", "numpy.float", "pkg.config.Timeout"} <= keys


def test_python_unparseable_file_still_yields_imports():
    keys = _keys("old.py", "import requests\nfrom a.b import c\nprint 'python 2'\n")
    assert {"requests", "a.b", "a.b.c"} <= keys


def test_js_import_forms_and_relative_resolution():
    text = """import a from 'react';
import {
  b,
  c,
} from "@scope/pkg/sub";
import './side.css';
export * from '../lib/api.ts';
const d = require('fs');
const e = await import('./lazy/index.js');
// from 'not-an-import'
"""
    keys = _keys("src/app/main.ts", text)
    assert keys >= {"react", "@scope/pkg/sub", "./src/app/side.css", "./src/lib/api", "fs", "./src/app/lazy"}
    assert "not-an-import" not in keys


def test_matching_is_by_whole_component():
    assert imp.matches("requests.adapters", "requests", "python")
    assert not imp.matches("requests_toolbelt", "requests", "python")
    assert imp.matches("lodash/fp", "lodash", "js")
    assert not imp.matches("lodash-es", "lodash", "js")


def test_python_defines():
    text = "from x import *\n"
    assert imp.python_defines("def a(): pass\nB = 1\nimport os\n", "a") is True
    assert imp.python_defines("def a(): pass\n", "gone") is False
    assert imp.python_defines(text, "maybe") is None
