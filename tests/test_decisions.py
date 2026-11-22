"""End-to-end: build a real git history, scan it, and check what is and is not proven."""
from __future__ import annotations

from scarwright import ledger, receipt
from scarwright.check import check
from scarwright.gitio import Repo
from scarwright.scan import scan

from conftest import Sandbox, package_json


def _moment_migration(sb: Sandbox, later_manifest_changes: int = 3) -> str:
    sb.write("package.json", package_json({"moment": "^2", "left-pad": "1"}))
    for i in range(3):
        sb.write(f"src/a{i}.js", "import moment from 'moment';\nexport const x = moment();\n")
    sb.commit("init")
    sb.write("package.json", package_json({"date-fns": "^3", "left-pad": "1"}))
    for i in range(3):
        sb.write(f"src/a{i}.js", "import {format} from 'date-fns';\nexport const x = format(new Date());\n")
    decided = sb.commit("replace moment with date-fns")
    for i in range(later_manifest_changes):
        sb.write("package.json", package_json({"date-fns": "^3", "left-pad": "1", f"extra-{i}": "1"}))
        sb.commit(f"add extra-{i}")
    return decided


def _proven(result, family=None):
    return [d for d in result.tier("proven") if family is None or d.family == family]


def test_dependency_migration_is_proven(sandbox):
    decided = _moment_migration(sandbox)
    repo = Repo(sandbox.root)
    result = scan(repo)
    [d] = _proven(result)
    assert d.family == "dependency" and d.detector["name"] == "moment" and d.commit == decided
    assert d.backtest.fires_before and d.backtest.silent_after and d.backtest.exposure == 3
    assert "3 file(s) stopped importing it" in d.statement
    repo.close()


def test_too_little_history_is_not_proven(sandbox):
    _moment_migration(sandbox, later_manifest_changes=1)
    result = scan(Repo(sandbox.root))
    assert not _proven(result)
    [d] = result.tier("candidate")
    assert "too little history" in d.reason


def test_removal_without_code_change_is_not_proven(sandbox):
    sandbox.write("package.json", package_json({"unused-thing": "1", "keep": "1"}))
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"keep": "1"}))
    sandbox.commit("drop unused dep")
    for i in range(4):
        sandbox.write("package.json", package_json({"keep": "1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    result = scan(Repo(sandbox.root))
    assert not _proven(result)
    assert "no sign the code was moved off it" in result.tier("candidate")[0].reason


def test_dependency_moved_between_manifests_is_not_a_removal(sandbox):
    sandbox.write("package.json", package_json({"shared": "1"}))
    sandbox.write("packages/a/package.json", package_json({}))
    sandbox.commit("init")
    sandbox.write("package.json", package_json({}))
    sandbox.write("packages/a/package.json", package_json({"shared": "1"}))
    sandbox.commit("move shared into package a")
    result = scan(Repo(sandbox.root))
    assert result.decisions == []


def test_check_flags_readding_with_citation_and_respects_levels(sandbox):
    decided = _moment_migration(sandbox)
    repo = Repo(sandbox.root)
    ledger.write(repo.root, scan(repo))
    [stored] = ledger.load(repo.root)

    assert check(repo, "staged") == []
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1", "moment": "^2"}))
    sandbox.git("add", "package.json")
    [f] = check(repo, "staged")
    assert f.decision.id == stored.id and f.decision.commit == decided and f.path == "package.json" and f.line
    assert not f.blocking
    assert [x.path for x in check(repo, "worktree")] == ["package.json"]

    assert ledger.set_field(repo.root, stored.id, "enforcement", "block")
    assert check(repo, "staged")[0].blocking
    assert ledger.set_field(repo.root, stored.id, "status", "rejected")
    assert check(repo, "staged") == []
    repo.close()


def test_preexisting_use_is_not_reported_as_new(sandbox):
    """A file that already broke a rule before the change is not blamed on the change."""
    _moment_migration(sandbox)
    repo = Repo(sandbox.root)
    ledger.write(repo.root, scan(repo))
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1", "moment": "^2"}))
    sandbox.commit("moment sneaks back")
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "2", "moment": "^2"}))
    sandbox.git("add", "package.json")
    assert check(repo, "staged") == []
    repo.close()


def test_receipt_records_reintroduction_with_agent_trailer(sandbox):
    _moment_migration(sandbox)
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1", "moment": "^2"}))
    broke = sandbox.commit("use moment for formatting\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1"}))
    fixed = sandbox.commit("remove moment again", days=2)
    result = scan(Repo(sandbox.root))
    [d] = _proven(result, "dependency")
    [r] = d.backtest.reintroductions
    assert (r.commit, r.corrected_by, r.agent, r.survived_days, r.how) == (broke, fixed, "Claude", 2.0, "changed")
    report = receipt.build(result)
    assert len(report["events"]) == 1 and report["agent_events"] == 1 and report["median_days"] == 2.0
    assert "moment" in receipt.to_markdown(report)


def test_long_lived_return_is_a_reversal_not_drift(sandbox):
    _moment_migration(sandbox)
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1", "moment": "^2"}))
    sandbox.commit("we need moment after all")
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1"}))
    sandbox.commit("drop moment years later", days=400)
    result = scan(Repo(sandbox.root))
    assert not [d for d in _proven(result) if d.backtest.reintroductions]
    assert receipt.build(result)["events"] == []


def test_recent_return_is_reported_as_open(sandbox):
    _moment_migration(sandbox)
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1", "moment": "^2"}))
    broke = sandbox.commit("moment again")
    sandbox.write("README", "later\n")
    sandbox.commit("unrelated", days=10)
    result = scan(Repo(sandbox.root))
    [d] = _proven(result)
    assert d.backtest.open.commit == broke and d.backtest.open.survived_days == 10.0
    assert len(receipt.build(result)["open"]) == 1


def _python_migration(sb: Sandbox, later: int = 5) -> str:
    sb.write("pkg/__init__.py", "")
    sb.write("pkg/old.py", "def helper():\n    return 1\n")
    sb.write("pkg/new.py", "def helper2():\n    return 2\n")
    for i in range(3):
        sb.write(f"app/a{i}.py", "from pkg.old import helper\n\nVALUE = helper()\n")
    sb.commit("init")
    for i in range(3):
        sb.write(f"app/a{i}.py", "from pkg.new import helper2\n\nVALUE = helper2()\n")
    decided = sb.commit("move callers to pkg.new")
    sb.touch_commits("app/extra.py", later)
    return decided


def test_import_replacement_is_proven_and_guarded(sandbox):
    decided = _python_migration(sandbox)
    repo = Repo(sandbox.root)
    result = scan(repo)
    [d] = _proven(result, "boundary")
    assert d.commit == decided and d.detector["module"] == "pkg.old" and d.detector["replaced_by"] == "pkg.new.helper2"
    ledger.write(repo.root, result)

    sandbox.write("app/direct.py", "from pkg.old import helper\n")
    sandbox.write("app/via_attribute.py", "import pkg.old\n\nX = pkg.old.helper()\n")
    sandbox.write("app/fine.py", "from pkg.new import helper2\n")
    found = {f.path: f.line for f in check(repo, "worktree")}
    assert found == {"app/direct.py": 1, "app/via_attribute.py": 1}
    repo.close()


def test_unused_import_cleanup_is_not_a_decision(sandbox):
    """Imports that vanish with nothing taking their place are cleanup, not a rule."""
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import os\nimport json\n\nVALUE = json.dumps({})\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import json\n\nVALUE = json.dumps({})\n")
    sandbox.commit("remove unused imports")
    sandbox.touch_commits("app/extra.py", 6)
    result = scan(Repo(sandbox.root))
    assert not _proven(result)
    assert all("cleanup" in d.reason for d in result.tier("candidate"))


def test_rename_is_not_a_decision(sandbox):
    """When the old module is gone, importing it again simply fails: there is nothing to guard."""
    sandbox.write("pkg/__init__.py", "")
    sandbox.write("pkg/utils.py", "def f():\n    return 1\n")
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "from pkg.utils import f\n")
    sandbox.commit("init")
    sandbox.remove("pkg/utils.py")
    sandbox.write("pkg/helpers.py", "def f():\n    return 1\n")
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "from pkg.helpers import f\n")
    sandbox.commit("rename utils to helpers")
    sandbox.touch_commits("app/extra.py", 6)
    assert scan(Repo(sandbox.root)).decisions == []


def test_decision_about_code_deleted_later_is_retired(sandbox):
    _python_migration(sandbox)
    sandbox.remove("pkg/old.py")
    sandbox.commit("delete pkg.old")
    result = scan(Repo(sandbox.root))
    assert not _proven(result)
    assert [d.tier for d in result.decisions] == ["moot"]


def test_attribute_use_prevents_a_false_everyone_stopped_claim(sandbox):
    """Three files drop `from lib import Thing`, but another still reaches lib.Thing by attribute."""
    sandbox.write("lib.py", "class Thing: pass\nclass Other: pass\n")
    for i in range(3):
        sandbox.write(f"a{i}.py", "from lib import Thing\n")
    sandbox.write("user.py", "import lib\n\nT = lib.Thing()\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"a{i}.py", "from lib import Other\n")
    sandbox.commit("some files switch to Other")
    sandbox.touch_commits("extra.py", 6)
    result = scan(Repo(sandbox.root))
    assert not any(d.detector.get("module") == "lib.Thing" for d in result.decisions)


def _reverted_change(sb: Sandbox, later: int = 5) -> tuple[str, str]:
    sb.write("core.py", "def run():\n    return 0\n")
    sb.commit("init")
    body = "\n".join(f"    cache_layer_step_{i} = compute_expensive_thing({i})" for i in range(6))
    sb.write("core.py", f"def run():\n{body}\n    return 0\n")
    tried = sb.commit("add caching layer to run()")
    reverted = sb.revert(tried)
    for i in range(later):
        sb.write("core.py", f"def run():\n    return 0\n\n# note {i}\n")
        sb.commit(f"note {i}")
    return tried, reverted


def test_reverted_change_is_proven_and_guarded(sandbox):
    tried, reverted = _reverted_change(sandbox)
    repo = Repo(sandbox.root)
    result = scan(repo)
    [d] = _proven(result, "revert")
    assert d.commit == reverted and d.detector["reverted"] == tried
    ledger.write(repo.root, result)

    body = "\n".join(f"    cache_layer_step_{i} = compute_expensive_thing({i})" for i in range(6))
    sandbox.write("core.py", f"def run():\n{body}\n    return 0\n")
    [f] = check(repo, "worktree")
    assert f.path == "core.py" and "reverted change" in f.detail
    sandbox.write("core.py", "def run():\n    cache_layer_step_0 = compute_expensive_thing(0)\n    return 0\n")
    assert check(repo, "worktree") == []
    repo.close()


def test_relanded_revert_is_not_a_decision(sandbox):
    tried, _ = _reverted_change(sandbox, later=0)
    body = "\n".join(f"    cache_layer_step_{i} = compute_expensive_thing({i})" for i in range(6))
    sandbox.write("core.py", f"def run():\n{body}\n    return 0\n")
    sandbox.commit("re-land caching with fix")
    sandbox.touch_commits("other.py", 3)
    sandbox.write("core.py", f"def run():\n{body}\n    return 1\n")
    sandbox.commit("tweak", days=200)
    assert not _proven(scan(Repo(sandbox.root)))


def test_readme_only_revert_is_ignored(sandbox):
    sandbox.write("README.md", "# demo\n")
    sandbox.commit("init")
    sandbox.write("README.md", "# demo\n\n" + "\n".join(f"- sponsor number {i} is thanked here" for i in range(5)) + "\n")
    tried = sandbox.commit("add sponsors")
    sandbox.revert(tried)
    assert scan(Repo(sandbox.root)).decisions == []


def test_merged_branch_work_is_seen_and_attributed(sandbox):
    """A decision made on a branch, and a later violation that arrives through a merge commit."""
    _moment_migration(sandbox)
    sandbox.git("checkout", "-q", "-b", "feature")
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1", "moment": "^2"}))
    culprit = sandbox.commit("feature: bring moment back")
    sandbox.git("checkout", "-q", "main")
    sandbox.write("notes.txt", "x\n")
    sandbox.commit("main moves on")
    sandbox.day += 1
    sandbox.git("merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1"}))
    sandbox.commit("drop moment again")
    result = scan(Repo(sandbox.root))
    [d] = _proven(result, "dependency")
    assert [r.commit for r in d.backtest.reintroductions] == [culprit]


def test_ledger_keeps_hand_set_fields_across_rescans(sandbox):
    _moment_migration(sandbox)
    repo = Repo(sandbox.root)
    assert ledger.write(repo.root, scan(repo)) == (1, 0)
    [stored] = ledger.load(repo.root)
    ledger.set_field(repo.root, stored.id, "enforcement", "block")
    assert ledger.write(repo.root, scan(repo)) == (0, 0)
    assert ledger.load(repo.root)[0].enforcement == "block"
    repo.close()
