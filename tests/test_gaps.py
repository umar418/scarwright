"""Behaviours that mutation testing showed were unprotected: each test here fails if its rule is removed."""
from __future__ import annotations

from scarwright import backtest, ledger
from scarwright.check import check
from scarwright.gitio import Repo
from scarwright.models import Backtest, Decision, Settings
from scarwright.scan import scan

from conftest import package_json
from test_decisions import _moment_migration, _python_migration, _reverted_change


def _decision(family: str, bt: Backtest, **intent) -> Decision:
    return Decision(id="x-1", family=family, statement="s", commit="c" * 40, date="2024-01-01", author="a", subject="s",
                    detector={}, intent=intent, backtest=bt)


def test_judge_rejects_a_check_that_does_not_separate_before_and_after():
    for before, after in ((False, True), (True, False), (False, False)):
        d = _decision("revert", Backtest(fires_before=before, silent_after=after, silent_at_landing=True, exposure=99))
        backtest.judge(d, Settings())
        assert d.tier == "rejected"


def test_judge_rejects_a_decision_that_did_not_hold_at_merge():
    d = _decision("revert", Backtest(fires_before=True, silent_after=True, silent_at_landing=False, exposure=99))
    backtest.judge(d, Settings())
    assert d.tier == "rejected"


def test_return_that_is_still_present_after_months_is_a_reversal(sandbox):
    _moment_migration(sandbox)
    sandbox.write("package.json", package_json({"date-fns": "^3", "left-pad": "1", "moment": "^2"}))
    sandbox.commit("moment is back for good")
    sandbox.write("notes", "x\n")
    sandbox.commit("much later", days=200)
    result = scan(Repo(sandbox.root))
    assert not result.tier("proven")
    assert [d.tier for d in result.decisions if d.detector.get("name") == "moment"] == ["reversed"]


def test_import_replacement_needs_later_history(sandbox):
    _python_migration(sandbox, later=1)
    result = scan(Repo(sandbox.root))
    assert not result.tier("proven")
    assert "too little history" in result.tier("candidate")[0].reason


def test_import_already_present_is_not_reported_as_new(sandbox):
    _python_migration(sandbox)
    repo = Repo(sandbox.root)
    ledger.write(repo.root, scan(repo))
    sandbox.write("app/legacy.py", "from pkg.old import helper\n")
    sandbox.commit("legacy file slips in")
    sandbox.write("app/legacy.py", "from pkg.old import helper\n\nVALUE = helper()\n")
    assert check(repo, "worktree") == []
    repo.close()


def test_one_shared_line_is_not_a_return_of_the_reverted_change(sandbox):
    _reverted_change(sandbox, later=5)
    sandbox.write("core.py", "def run():\n    cache_layer_step_0 = compute_expensive_thing(0)\n    return 0\n")
    sandbox.commit("one similar line")
    sandbox.write("core.py", "def run():\n    return 0\n")
    sandbox.commit("gone again")
    [d] = scan(Repo(sandbox.root)).tier("proven")
    assert d.backtest.reintroductions == [] and d.backtest.open is None


def test_dependency_still_imported_elsewhere_has_no_usage_evidence(sandbox):
    sandbox.write("package.json", package_json({"moment": "^2"}))
    for i in range(4):
        sandbox.write(f"src/a{i}.js", "import moment from 'moment';\n")
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    for i in range(3):
        sandbox.write(f"src/a{i}.js", "import dayjs from 'dayjs';\n")
    sandbox.commit("mostly move to dayjs")
    for i in range(4):
        sandbox.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    result = scan(Repo(sandbox.root))
    deps = [d for d in result.decisions if d.detector.get("name") == "moment"]
    assert [d.tier for d in deps] == ["candidate"] and deps[0].intent["usage_files"] == 0


def test_reverting_a_revert_is_not_a_rejection(sandbox):
    tried, reverted = _reverted_change(sandbox, later=0)
    sandbox.revert(reverted)
    for i in range(6):
        sandbox.write("other.py", f"# {i}\n")
        sandbox.commit(f"other {i}")
    result = scan(Repo(sandbox.root))
    assert not any(d.detector.get("reverted") == reverted for d in result.decisions)
    assert not result.tier("proven")


def test_ledger_ids_cannot_reach_files_outside_the_ledger(sandbox):
    sandbox.write("a.txt", "x\n")
    sandbox.commit("init")
    (sandbox.root / ".scarwright" / "decisions").mkdir(parents=True)
    for rel, bad_id in (("victim.yaml", "../../victim"), (".scarwright/victim.yaml", "../victim")):
        target = sandbox.root / rel
        target.write_text("id: victim\ndetector: {kind: revert}\n", encoding="utf-8")
        assert not ledger.set_field(sandbox.root, bad_id, "status", "rejected")
        assert "rejected" not in target.read_text(encoding="utf-8")


# ---- second mutation pass

def test_one_newcomer_cannot_replace_two_different_things(sandbox):
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import alpha_mod\nimport beta_mod\n\nV = alpha_mod.f() + beta_mod.g()\n")
    sandbox.commit("init")
    for i in range(3):
        sandbox.write(f"app/a{i}.py", "import gamma_mod\n\nV = gamma_mod.h() + gamma_mod.h()\n")
    sandbox.commit("collapse alpha and beta into gamma")
    sandbox.touch_commits("app/extra.py", 6)
    result = scan(Repo(sandbox.root))
    assert {d.detector["module"] for d in result.decisions} == {"alpha_mod", "beta_mod"}
    assert not [d for d in result.decisions if d.detector.get("replaced_by")] and not result.tier("proven")


def test_change_that_mostly_survives_was_not_reverted(sandbox):
    sandbox.write("core.py", "def run():\n    return 0\n")
    sandbox.commit("init")
    body = "\n".join(f"    step_number_{i} = compute_expensive_thing({i})" for i in range(20))
    sandbox.write("core.py", f"def run():\n{body}\n    return 0\n")
    tried = sandbox.commit("add twenty steps")
    kept = "\n".join(f"    step_number_{i} = compute_expensive_thing({i})" for i in range(14))
    sandbox.write("core.py", f"def run():\n{kept}\n    return 0\n")
    sandbox.commit(f"Revert \"add twenty steps\"\n\nThis reverts commit {tried}.")
    sandbox.touch_commits("other.py", 6)
    assert not [d for d in scan(Repo(sandbox.root)).decisions if d.family == "revert"]


def test_revert_that_is_later_reverted_is_no_longer_a_decision(sandbox):
    tried, reverted = _reverted_change(sandbox, later=5)
    assert [d.tier for d in scan(Repo(sandbox.root)).decisions if d.family == "revert"] == ["proven"]
    sandbox.revert(reverted)
    [d] = [d for d in scan(Repo(sandbox.root)).decisions if d.detector.get("reverted") == tried]
    assert d.tier == "reversed"


def test_markdown_receipt_escapes_text_from_commits():
    from scarwright import receipt
    event = {"decision": "x", "statement": "`a` was [removed](http://evil) # now", "decided": "c" * 40, "commit": "d" * 40,
             "date": "2024-01-01", "author": "Eve *bold* ![i](http://evil/p.png)", "agent": None,
             "subject": "s", "corrected_by": "e" * 40, "days": 1.0, "how": "changed"}
    report = {"head": "f" * 40, "commits_scanned": 1, "proven": 1, "events": [event], "open": [dict(event)], "broken": 2,
              "agent_events": 0, "median_days": 1.0, "limits": []}
    md = receipt.to_markdown(report)
    assert "](http://evil" not in md and "*bold*" not in md and "\\[removed\\]" in md
