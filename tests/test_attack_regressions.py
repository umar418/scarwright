"""Every defect found by adversarial testing before the first release, kept as a test."""
from __future__ import annotations

import pytest

from scarwright import imports as imp
from scarwright import ledger, manifests, receipt
from scarwright.check import check
from scarwright.gitio import Repo, agent_in_trailers
from scarwright.scan import scan

from conftest import Sandbox, package_json


def _js_migration(sb: Sandbox, old="axios", new="ky", later=5, folder="src"):
    for i in range(3):
        sb.write(f"{folder}/a{i}.js", f"import client from '{old}';\nexport const v{i} = client.get;\n")
    sb.commit("init")
    for i in range(3):
        sb.write(f"{folder}/a{i}.js", f"import fetcher from '{new}';\nexport const v{i} = fetcher.get;\n")
    sha = sb.commit(f"replace {old} with {new}")
    sb.touch_commits(f"{folder}/extra.js", later)
    return sha


def _keys(path, text, mode="all"):
    data = text if isinstance(text, bytes) else text.encode()
    return set(imp.resolve(path, imp.parse(path, data), mode=mode))


# ---- blocker 1: "no file imports X" must not be claimed when an importer exists that we used to miss

HIDDEN_JS = {
    "src/App.vue": "<script>\nimport axios from 'axios'\nexport default {}\n</script>\n",
    "src/App.svelte": "<script>\n  import axios from 'axios';\n</script>\n",
    "src/lazy.js": "export const load = () => import(/* webpackChunkName: 'http' */ 'axios');\n",
    "src/multi.js": "const axios = require(\n  'axios'\n);\n",
    "src/tick.js": "const axios = require(`axios`);\n",
    "src/build/client.js": "import axios from 'axios';\n",
    "src/vendor/client.js": "import axios from 'axios';\n",
    "src/bom.js": b"\xef\xbb\xbfimport axios from 'axios';\n",
    "src/page.html": "<script type=\"module\">\nimport axios from 'axios';\n</script>\n",
    "src/big.js": "import axios from 'axios';\n" + "// padding\n" * 200_000,
}


@pytest.mark.parametrize("path", sorted(HIDDEN_JS))
def test_remaining_js_importer_blocks_the_claim(sandbox, path):
    sandbox.write(path, HIDDEN_JS[path])
    _js_migration(sandbox)
    result = scan(Repo(sandbox.root))
    assert not [d for d in result.tier("proven") if d.detector.get("module") == "axios"]


HIDDEN_PY = {
    "app/dyn.py": "import importlib\nrequests = importlib.import_module('requests')\n",
    "app/dunder.py": "requests = __import__('requests')\n",
    "app/stub.pyi": "import requests\n",
    "app/fast.pyx": "import requests\n",
    "app/dist/fetch.py": "import requests\n",
    "app/bom.py": b"\xef\xbb\xbfimport requests\n",
    "app/wide.py": "import requests\n".encode("utf-16"),
    "app/py2.py": "import os; import requests\nprint 'old'\n",
}


@pytest.mark.parametrize("path", sorted(HIDDEN_PY))
def test_remaining_python_importer_blocks_the_claim(sandbox, path):
    sandbox.write(path, HIDDEN_PY[path])
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import requests\n\nV = requests.get\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import httpx\n\nV = httpx.get\n")
    sandbox.commit("replace requests with httpx")
    sandbox.touch_commits("app/extra.py", 5)
    result = scan(Repo(sandbox.root))
    assert not [d for d in result.tier("proven") if d.detector.get("module") == "requests"]


# ---- blocker 2: method calls are not imports

def test_method_calls_never_become_decisions(sandbox):
    sandbox.write("app/__init__.py", "db = object()\n")
    for i in range(3):
        sandbox.write(f"app/views/v{i}.py", "import logging\nfrom app import db\n\ndef save():\n    logging.warn('x')\n    db.session.commit()\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"app/views/v{i}.py", "import logging\nfrom app import db\n\ndef save():\n    logging.warning('x')\n    db.session.flush()\n")
    sandbox.commit("tidy")
    sandbox.touch_commits("app/views/extra.py", 5)
    assert scan(Repo(sandbox.root)).decisions == []


def test_parameter_shadowing_an_import_is_not_flagged(sandbox):
    keys = imp.resolve("a.py", imp.parse("a.py", b"import config\n\ndef f(config):\n    return config.legacy\n"), mode="strict")
    assert "config.legacy" not in keys
    assert "config.legacy" in imp.resolve("a.py", imp.parse("a.py", b"import config\n\ndef f(config):\n    return config.legacy\n"))


# ---- blocker 3: two imports changing in the same files is not a replacement

def test_lint_cleanup_next_to_an_unrelated_addition_is_not_a_replacement(sandbox):
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import json\n\ndef f():\n    return 1\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import logging\n\nlogger = logging.getLogger(__name__)\n\ndef f():\n    return 1\n")
    sandbox.commit("drop unused json import, add loggers")
    sandbox.touch_commits("app/extra.py", 6)
    repo = Repo(sandbox.root)
    result = scan(repo)
    assert not result.tier("proven")
    ledger.write(repo.root, result)
    sandbox.write("app/new.py", "import json\n")
    assert check(repo, "worktree") == []
    repo.close()


def test_same_names_from_a_new_source_is_a_replacement(sandbox):
    for i in range(3):
        sandbox.write(f"src/a{i}.js", "import {Buffer} from 'buffer';\nexport const b = Buffer.from('x');\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"src/a{i}.js", "import {Buffer} from 'node:buffer';\nexport const b = Buffer.from('x');\n")
    sandbox.commit("use node: protocol")
    sandbox.touch_commits("src/extra.js", 5)
    [d] = scan(Repo(sandbox.root)).tier("proven")
    assert (d.detector["module"], d.detector["replaced_by"]) == ("buffer", "node:buffer")


# ---- blocker 4: comments and strings are not imports

@pytest.mark.parametrize("text", [
    "// const moment = require('moment');\nconst dayjs = require('dayjs');\n",
    "/*\nimport moment from 'moment';\n*/\nimport dayjs from 'dayjs';\n",
    "export const hint = \"do not use require('moment'), use dayjs\";\n",
    "export const VERSION = 2\n// date helpers ported from 'moment'\n",
    "export function f() { throw new Error(`import('moment') is banned`) }\n",
    "const sample = `\nimport moment from 'moment';\n`;\n",
    "/**\n * @example\n * import moment from 'moment'\n */\nexport const a = 1;\n",
])
def test_js_text_that_only_mentions_a_module_is_not_an_import(text):
    assert "moment" not in _keys("x.ts", text)


@pytest.mark.parametrize("text", [
    "import moment from 'moment';\n", "import type { Moment } from 'moment';\n", "import 'moment/locale/fr';\n",
    "export { x } from 'moment';\n", "import m = require('moment');\n", "const m = await import('moment');\n",
])
def test_real_js_imports_are_still_seen(text):
    assert any(imp.matches(k, "moment", "js") for k in _keys("x.ts", text))


# ---- blocker 5: time is measured on the main line

def test_days_count_from_merge_to_fix_not_from_authorship(sandbox):
    sandbox.write("package.json", package_json({"moment": "^2"}))
    for i in range(3):
        sandbox.write(f"src/a{i}.js", "import moment from 'moment';\n")
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    for i in range(3):
        sandbox.write(f"src/a{i}.js", "import dayjs from 'dayjs';\n")
    sandbox.commit("to dayjs")
    sandbox.git("checkout", "-q", "-b", "feature")
    sandbox.write("package.json", package_json({"dayjs": "^1", "moment": "^2"}))
    culprit = sandbox.commit("feature: moment back")
    sandbox.git("checkout", "-q", "main")
    for i in range(3):
        sandbox.write("notes.txt", f"{i}\n")
        sandbox.commit(f"main {i}")
    sandbox.day += 140
    sandbox.git("merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    sandbox.commit("drop moment again", days=1)
    [d] = [d for d in scan(Repo(sandbox.root)).tier("proven") if d.detector.get("name") == "moment"]
    [r] = d.backtest.reintroductions
    assert r.commit == culprit and r.survived_days == 1.0


def test_branch_forked_before_the_decision_is_blamed_not_the_merge(sandbox):
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import requests\n\nV = requests.get\n")
    sandbox.commit("init")
    sandbox.git("checkout", "-q", "-b", "old-branch")
    sandbox.write("app/late.py", "import requests\n")
    culprit = sandbox.commit("add late.py\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    sandbox.git("checkout", "-q", "main")
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import httpx\n\nV = httpx.get\n")
    sandbox.commit("replace requests with httpx")
    sandbox.touch_commits("app/extra.py", 5)
    sandbox.day += 1
    sandbox.git("merge", "-q", "--no-ff", "old-branch", "-m", "merge old branch")
    sandbox.remove("app/late.py")
    sandbox.write("app/late2.py", "import httpx\n")
    sandbox.commit("fix late")
    [d] = scan(Repo(sandbox.root)).tier("proven")
    [r] = d.backtest.reintroductions
    assert (r.commit, r.agent) == (culprit, "Claude")


# ---- blocker 6: people are not agents

@pytest.mark.parametrize("body", [
    "x\n\nCo-authored-by: Claude Dupont <claude.dupont@example.fr>", "x\n\nCo-authored-by: Devin Patel <devin@example.com>",
    "x\n\nCo-authored-by: Gemini Rossi <g@example.it>", "x\n\nCo-authored-by: Jane Doe <jane@anthropic.com>",
    "x\n\nCo-authored-by: Sam <sam@copilot.money>", "This was not generated with Claude Code.",
    "Revert \"y\"\n\nThis reverts commit abc.\n\n    Co-Authored-By: Claude <noreply@anthropic.com>\n\nSigned-off-by: Bob <bob@example.com>",
    "", "Co-Authored-By: Claude <noreply@anthropic.com> and then more prose in a sentence",
])
def test_humans_and_quoted_text_are_not_agents(body):
    assert agent_in_trailers(body) is None


@pytest.mark.parametrize("body,name", [
    ("x\n\nCo-Authored-By: Claude <noreply@anthropic.com>", "Claude"),
    ("x\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n\nCo-Authored-By: Claude Opus 4 <noreply@anthropic.com>", "Claude"),
    ("x\n\nSigned-off-by: A <a@b.c>\nco-authored-by: Cursor Agent <cursoragent@cursor.com>", "Cursor"),
])
def test_known_agent_trailers_are_recognised(body, name):
    assert agent_in_trailers(body) == name


# ---- blocker 7: a removed dependency that code imports again has not "stayed out"

def test_dependency_imported_again_in_code_is_seen_and_flagged(sandbox):
    sandbox.write("package.json", package_json({"moment": "^2"}))
    for i in range(3):
        sandbox.write(f"src/a{i}.js", "import moment from 'moment';\n")
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    for i in range(3):
        sandbox.write(f"src/a{i}.js", "import dayjs from 'dayjs';\n")
    sandbox.commit("to dayjs")
    for i in range(3):
        sandbox.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    repo = Repo(sandbox.root)
    result = scan(repo)
    ledger.write(repo.root, result)
    sandbox.write("src/new.js", "import moment from 'moment';\n")
    [f] = check(repo, "worktree")
    assert f.path == "src/new.js" and "moment" in f.detail
    sandbox.commit("moment imported again, manifest untouched")
    sandbox.write("notes.txt", "x\n")
    sandbox.commit("later", days=200)
    again = scan(repo)
    assert not [d for d in again.tier("proven") if d.detector.get("name") == "moment"]
    repo.close()


# ---- should-fix findings

def test_manifest_moves_and_mentions_are_not_removals():
    go = b"module x\nrequire (\n\tgithub.com/pkg/errors v0.9.1 // indirect\n)\n"
    assert manifests.parse_manifest("go.mod", go) == {"github.com/pkg/errors"}
    pdm = b"[tool.pdm.dev-dependencies]\ntest = [\"requests>=2\"]\n[tool.hatch.envs.default]\ndependencies = [\"httpx\"]\n"
    assert manifests.parse_manifest("pyproject.toml", pdm) == {"requests", "httpx"}
    assert manifests.parse_manifest("package.json", b'\xef\xbb\xbf{"dependencies": {"m": "npm:moment@2"}}') == {"m", "moment"}
    assert manifests.still_mentioned(b"https://example.com/requests-2.31.0-py3-none-any.whl\n", "requests")
    assert manifests.still_mentioned(b'{"resolutions": {"moment": "2.0.0"}}', "moment")
    assert not manifests.still_mentioned(b"requests-toolbelt==1.0\nrequests_cache\n", "requests")
    assert manifests.include_count(b"-r base.txt\nflask\n") == 1


def test_dependency_still_named_elsewhere_in_the_manifest_is_not_removed(sandbox):
    sandbox.write("package.json", '{"dependencies": {"moment": "^2"}}\n')
    sandbox.write("a.js", "import moment from 'moment';\n")
    sandbox.commit("init")
    sandbox.write("package.json", '{"dependencies": {}, "resolutions": {"moment": "2.29.4"}}\n')
    sandbox.write("a.js", "export {};\n")
    sandbox.commit("pin via resolutions")
    assert scan(Repo(sandbox.root)).decisions == []


def test_manifest_line_number_points_at_the_declaration():
    text = '{\n  "description": "A moment in time",\n  "scripts": {"moment": "node x.js"},\n  "dependencies": {\n    "moment": "^2"\n  }\n}\n'
    assert manifests.find_line("npm", text, "moment") in (3, 5)  # a key named moment, never the description
    toml = '[project]\nname = "x"\ndescription = "formerly built on requests"\ndependencies = [\n  "Requests[socks]>=2",\n]\n'
    assert manifests.find_line("pip", toml, "requests") == 5


def test_partial_revert_and_quoted_phrase_are_not_reverts(sandbox):
    sandbox.write("core.py", "def run():\n    return 0\n")
    sandbox.commit("init")
    body = "\n".join(f"    step_number_{i} = compute_expensive_thing({i})" for i in range(10))
    sandbox.write("core.py", f"def run():\n{body}\n    return 0\n")
    tried = sandbox.commit("add steps")
    kept = "\n".join(f"    step_number_{i} = compute_expensive_thing({i})" for i in range(8))
    sandbox.write("core.py", f"def run():\n{kept}\n    return 0\n")
    sandbox.commit(f"trim two steps\n\nThis reverts commit {tried}.")
    sandbox.write("other.py", "x = 1\n")
    sandbox.commit(f"unrelated\n\nAs the old message said, \"This reverts commit {tried}\" was planned.")
    sandbox.touch_commits("core2.py", 5)
    assert scan(Repo(sandbox.root)).decisions == []


def test_boilerplate_lines_are_not_a_fingerprint(sandbox):
    sandbox.write("svc.py", "def fetch(url):\n    return url\n")
    sandbox.commit("init")
    sandbox.write("svc.py", "from __future__ import annotations\nimport logging\nimport time\nlogger = logging.getLogger(__name__)\n\n"
                            "def fetch(url):\n    started_at_monotonic = time.monotonic()\n    return url\n")
    tried = sandbox.commit("add timing")
    sandbox.revert(tried)
    for i in range(5):
        sandbox.write("svc.py", f"def fetch(url):\n    return url\n# {i}\n")
        sandbox.commit(f"n{i}")
    repo = Repo(sandbox.root)
    result = scan(repo)
    ledger.write(repo.root, result)
    sandbox.write("svc.py", "from __future__ import annotations\nimport logging\nlogger = logging.getLogger(__name__)\n\ndef fetch(url):\n    return url\n")
    assert check(repo, "worktree") == []
    repo.close()


def test_reverted_change_spread_across_files_is_caught_when_reapplied(sandbox):
    for i in range(3):
        sandbox.write(f"m{i}.py", "def f():\n    return 0\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"m{i}.py", f"def f():\n    retry_budget_for_module_{i} = 3\n    backoff_seconds_for_module_{i} = 0.5\n    return 0\n")
    tried = sandbox.commit("add retries everywhere")
    sandbox.revert(tried)
    for i in range(5):
        sandbox.write("m0.py", f"def f():\n    return 0\n# {i}\n")
        sandbox.commit(f"n{i}")
    repo = Repo(sandbox.root)
    ledger.write(repo.root, scan(repo))
    for i in range(3):
        sandbox.write(f"m{i}.py", f"def f():\n    retry_budget_for_module_{i} = 3\n    backoff_seconds_for_module_{i} = 0.5\n    return 0\n")
    assert len(check(repo, "worktree")) == 1
    repo.close()


def test_unreadable_manifest_is_neither_a_fix_nor_a_new_break(sandbox):
    sandbox.write("package.json", package_json({"moment": "^2"}))
    sandbox.write("a.js", "import moment from 'moment';\n")
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    sandbox.write("a.js", "import dayjs from 'dayjs';\n")
    sandbox.commit("to dayjs")
    for i in range(3):
        sandbox.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    sandbox.write("package.json", package_json({"dayjs": "^1", "moment": "^2"}))
    broke = sandbox.commit("moment back")
    sandbox.write("package.json", '<<<<<<< HEAD\n{"dependencies": {"dayjs": "^1", "moment": "^2"}}\n=======\n')
    sandbox.commit("oops, conflict markers", days=2)
    sandbox.write("package.json", package_json({"dayjs": "^1", "moment": "^2"}))
    sandbox.commit("repair json", days=1)
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    fixed = sandbox.commit("drop moment", days=20)
    [d] = [d for d in scan(Repo(sandbox.root)).tier("proven") if d.detector.get("name") == "moment"]
    assert [(r.commit, r.corrected_by, r.survived_days) for r in d.backtest.reintroductions] == [(broke, fixed, 23.0)]


def test_receipt_formats_agree_and_count_open_events(sandbox):
    sandbox.write("package.json", package_json({"moment": "^2"}))
    sandbox.write("a.js", "import moment from 'moment';\n")
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    sandbox.write("a.js", "import dayjs from 'dayjs';\n")
    sandbox.commit("to dayjs")
    for i in range(3):
        sandbox.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    sandbox.write("package.json", package_json({"dayjs": "^1", "moment": "^2"}))
    sandbox.commit("moment again\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    report = receipt.build(scan(Repo(sandbox.root)))
    assert (report["broken"], report["agent_events"], len(report["open"])) == (1, 1, 1)
    assert "broken again: 1 (1 still present)" in receipt.to_markdown(report)
    assert ">1</b>times one was broken again (1 still present)" in receipt.to_html(report)


def test_ledger_file_from_rewritten_history_stops_being_enforced(sandbox):
    sandbox.write("package.json", package_json({"moment": "^2"}))
    sandbox.write("a.js", "import moment from 'moment';\n")
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    sandbox.write("a.js", "import dayjs from 'dayjs';\n")
    sandbox.commit("to dayjs")
    for i in range(3):
        sandbox.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    repo = Repo(sandbox.root)
    ledger.write(repo.root, scan(repo))
    [first] = ledger.load(repo.root)
    repo.close()
    sandbox.git("filter-branch", "-f", "--msg-filter", "sed 's/to dayjs/switch to dayjs/'", "HEAD")
    repo = Repo(sandbox.root)
    ledger.write(repo.root, scan(repo))
    tiers = {d.id: d.tier for d in ledger.load(repo.root)}
    assert tiers[first.id] == "stale" and sorted(tiers.values()) == ["proven", "stale"]
    sandbox.write("package.json", package_json({"dayjs": "^1", "x2": "1", "moment": "^2"}))
    assert len(check(repo, "worktree")) == 1
    repo.close()


def test_alias_import_of_a_banned_file_is_caught(sandbox):
    assert imp.matches("@/lib/api", "./src/lib/api", "js")
    assert not imp.matches("@/api", "./src/lib/api", "js")
    assert not imp.matches("@/lib/apix", "./src/lib/api", "js")


def test_relative_import_is_resolved_the_same_way_by_scan_and_check(sandbox):
    sandbox.write("scripts/helpers.py", "def old():\n    return 1\n\ndef new():\n    return 2\n")
    for i in range(3):
        sandbox.write(f"scripts/s{i}.py", "from helpers import old\n\nV = old()\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"scripts/s{i}.py", "from helpers import new\n\nV = new()\n")
    sandbox.commit("use new")
    sandbox.touch_commits("scripts/extra.py", 5)
    repo = Repo(sandbox.root)
    result = scan(repo)
    ledger.write(repo.root, result)
    if result.tier("proven"):
        sandbox.write("scripts/again.py", "from helpers import old\n")
        assert [f.path for f in check(repo, "worktree")] == ["scripts/again.py"]
    repo.close()


def test_moving_a_file_that_already_broke_the_rule_is_not_a_new_violation(sandbox):
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import requests\n\nV = requests.get\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import httpx\n\nV = httpx.get\n")
    sandbox.commit("replace requests with httpx")
    sandbox.touch_commits("app/extra.py", 5)
    repo = Repo(sandbox.root)
    ledger.write(repo.root, scan(repo))
    sandbox.write("app/legacy_client.py", "import requests\n\n\ndef call(url):\n    return requests.get(url, timeout=5)\n")
    sandbox.commit("legacy slips in")
    sandbox.git("mv", "app/legacy_client.py", "app/moved_client.py")
    assert check(repo, "staged") == []
    repo.close()
