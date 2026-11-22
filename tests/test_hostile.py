"""Inputs designed to break the tool, leak a secret, or put words in its mouth."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from scarwright import ledger, receipt
from scarwright.check import check
from scarwright.cli import app
from scarwright.gitio import GitError, Repo
from scarwright.scan import scan

from conftest import Sandbox, package_json

SECRET = "sk-ant-" "api03-Zq8Lw2Nf7Xc4Vb9Tm1Kj6Hg3Dp5Sa0Yr"
runner = CliRunner()


def _all_outputs(root, result) -> str:
    report = receipt.build(result)
    files = "".join(p.read_text(encoding="utf-8") for p in (root / ".scarwright").rglob("*") if p.is_file())
    return files + json.dumps(ledger.to_json(result)) + receipt.to_html(report) + receipt.to_markdown(report) + json.dumps(report)


def _migration_with_subject(sb: Sandbox, subject: str) -> None:
    sb.write("package.json", package_json({"moment": "^2"}))
    sb.write("a.js", "import moment from 'moment';\n")
    sb.commit("init")
    sb.write("package.json", package_json({"dayjs": "^1"}))
    sb.write("a.js", "import dayjs from 'dayjs';\n")
    sb.commit(subject)
    for i in range(3):
        sb.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sb.commit(f"x{i}")
    sb.write("package.json", package_json({"dayjs": "^1", "moment": "^2"}))
    sb.commit(subject + " again")
    sb.write("package.json", package_json({"dayjs": "^1"}))
    sb.commit("fix")


def test_commit_message_cannot_inject_markup_or_leak_a_secret(sandbox):
    evil = f"drop moment \x1b[31mRED\x1b[0m <script>alert(1)</script> [bold red]x[/] key={SECRET} ‮gnp.exe"
    _migration_with_subject(sandbox, evil)
    repo = Repo(sandbox.root)
    result = scan(repo)
    assert result.tier("proven")
    ledger.write(repo.root, result)
    blob = _all_outputs(repo.root, result)
    assert SECRET not in blob
    assert "\x1b" not in blob and "‮" not in blob
    html = receipt.to_html(receipt.build(result))
    assert "<script>" not in html and "&lt;script&gt;" in html
    repo.close()


def test_reverted_secret_is_never_written_anywhere(sandbox):
    """A reverted commit may be the one that added a credential. The ledger stores line fingerprints, not lines."""
    sandbox.write("config.py", "DEBUG = False\n")
    sandbox.commit("init")
    lines = "\n".join(f"SETTING_NUMBER_{i} = load_value_from_env({i})" for i in range(4))
    sandbox.write("config.py", f"DEBUG = False\nANTHROPIC_API_KEY = \"{SECRET}\"\n{lines}\n")
    tried = sandbox.commit("add settings")
    sandbox.revert(tried)
    for i in range(5):
        sandbox.write("config.py", f"DEBUG = False\n# pass {i}\n")
        sandbox.commit(f"pass {i}")
    repo = Repo(sandbox.root)
    result = scan(repo)
    assert [d.family for d in result.tier("proven")] == ["revert"]
    ledger.write(repo.root, result)
    blob = _all_outputs(repo.root, result)
    assert SECRET not in blob and "load_value_from_env" not in blob
    repo.close()


def test_cli_output_is_clean_for_hostile_history(sandbox):
    _migration_with_subject(sandbox, f"drop moment [link=file:///etc/passwd]click[/link] {SECRET}")
    for args in (["scan", str(sandbox.root)], ["receipt", str(sandbox.root)], ["scan", str(sandbox.root), "--json", "--no-write"],
                 ["decisions", "list", str(sandbox.root)], ["decisions", "list", str(sandbox.root), "--all"]):
        res = runner.invoke(app, args)
        assert res.exit_code == 0, res.output
        assert SECRET not in res.output and "\x1b]8" not in res.output
    payload = runner.invoke(app, ["scan", str(sandbox.root), "--json", "--no-write"])
    assert json.loads(payload.stdout)["decisions"]


def _awkward(sandbox, with_unreadable_source: bool):
    sandbox.write("package.json", package_json({"moment": "^2"}))
    sandbox.write("src/my file é.js", "import moment from 'moment';\n")
    sandbox.write("-dash.js", "import moment from 'moment';\n")
    if with_unreadable_source:
        sandbox.write("blob.js", b"\x00\x01\x02import moment from 'moment'\xff")
    sandbox.write("img.png", bytes(range(256)) * 50)
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    sandbox.write("src/my file é.js", "import dayjs from 'dayjs';\n")
    sandbox.write("-dash.js", "import dayjs from 'dayjs';\n")
    sandbox.commit("to dayjs")
    for i in range(3):
        sandbox.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    return scan(Repo(sandbox.root))


def test_awkward_paths(sandbox):
    result = _awkward(sandbox, with_unreadable_source=False)
    assert result.errors == []
    [d] = result.tier("proven")
    assert d.intent["usage_files"] == 2


def test_source_file_that_cannot_be_read_blocks_a_nobody_uses_it_claim(sandbox):
    result = _awkward(sandbox, with_unreadable_source=True)
    assert result.errors == [] and not result.tier("proven")
    assert result.unreadable_sources == ["blob.js"]


def test_broken_manifest_in_history_is_skipped_not_guessed(sandbox):
    sandbox.write("package.json", package_json({"moment": "^2", "keep": "1"}))
    sandbox.write("a.js", "import moment from 'moment';\n")
    sandbox.commit("init")
    sandbox.write("package.json", '{"dependencies": {"keep": "1",}')
    sandbox.commit("broken json")
    sandbox.write("package.json", package_json({"keep": "1"}))
    sandbox.write("a.js", "export {};\n")
    sandbox.commit("fixed json, moment gone")
    for i in range(4):
        sandbox.write("package.json", package_json({"keep": "1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    result = scan(Repo(sandbox.root))
    assert result.errors == []
    assert result.unparsed_manifests == ["package.json"]
    assert not result.tier("proven")  # the removal happened across an unreadable version; nothing is claimed about it


def test_huge_and_deeply_nested_inputs_do_not_hang(sandbox):
    sandbox.write("package.json", "[" * 50_000)
    sandbox.write("deep.py", "x = " + "(" * 5000 + "1" + ")" * 5000 + "\nimport os\n")
    sandbox.write("big.js", "import a from 'a';\n" + "x;\n" * 200_000)
    sandbox.commit("init")
    sandbox.write("package.json", package_json({}))
    for name in ("deep.py", "big.js"):
        sandbox.write(name, "\n")
    sandbox.commit("replace")
    result = scan(Repo(sandbox.root))
    assert result.errors == []


def test_empty_repository_reports_cleanly(tmp_path):
    sb = Sandbox(tmp_path / "empty")
    res = runner.invoke(app, ["scan", str(sb.root)])
    assert res.exit_code == 2 and "Traceback" not in res.output


def test_not_a_repository_reports_cleanly(tmp_path):
    res = runner.invoke(app, ["scan", str(tmp_path)])
    assert res.exit_code == 2 and "Traceback" not in res.output
    with pytest.raises(GitError):
        Repo(tmp_path / "does-not-exist")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not allow folder names ending in a space")
def test_folder_name_ending_in_a_space(tmp_path):
    sb = Sandbox(tmp_path / "project ")
    sb.write("a.txt", "x\n")
    sb.commit("init")
    repo = Repo(sb.root)
    assert repo.root.name == "project " and scan(repo).commits_scanned == 0
    repo.close()


def test_shallow_clone_is_reported(sandbox, tmp_path):
    for i in range(4):
        sandbox.write("a.txt", f"{i}\n")
        sandbox.commit(f"c{i}")
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "2", sandbox.root.as_uri(), str(clone)], check=True, capture_output=True)
    result = scan(Repo(clone))
    assert result.shallow
    assert any("shallow" in line for line in receipt.build(result)["limits"])


def test_malformed_ledger_files_never_produce_findings(sandbox):
    sandbox.write("package.json", package_json({"a": "1"}))
    sandbox.commit("init")
    folder = sandbox.root / ".scarwright" / "decisions"
    folder.mkdir(parents=True)
    (folder / "garbage.yaml").write_text("{{{ not yaml", encoding="utf-8")
    (folder / "wrong-shape.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    (folder / "unknown-kind.yaml").write_text(
        "id: made-up-decision\ntier: proven\nstatus: active\nenforcement: block\ndetector: {kind: telepathy}\n", encoding="utf-8")
    (folder / "missing-fields.yaml").write_text(
        "id: half-a-decision\ntier: proven\nstatus: active\nenforcement: block\ndetector: {kind: dependency}\n", encoding="utf-8")
    (folder / "bomb.yaml").write_text("a: &a [x, x]\nb: &b [*a, *a]\nc: &c [*b, *b]\nid: bomb-decision\ndetector: {kind: revert}\n", encoding="utf-8")
    sandbox.write("package.json", package_json({"a": "1", "b": "1"}))
    sandbox.git("add", "package.json")
    repo = Repo(sandbox.root)
    assert check(repo, "staged") == []
    repo.close()


def test_ledger_ids_cannot_escape_the_ledger_folder(sandbox):
    sandbox.write("a.txt", "x\n")
    sandbox.commit("init")
    target = sandbox.root / "victim.yaml"
    target.write_text("id: victim\n", encoding="utf-8")
    assert not ledger.set_field(sandbox.root, "../victim", "status", "rejected")
    assert target.read_text(encoding="utf-8") == "id: victim\n"


def test_symlink_in_worktree_is_not_followed(sandbox, tmp_path):
    if sys.platform == "win32":
        pytest.skip("symlinks need privileges on Windows")
    outside = tmp_path / "outside.json"
    outside.write_text(package_json({"moment": "^2"}), encoding="utf-8")
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
    (sandbox.root / "package.json").unlink()
    (sandbox.root / "package.json").symlink_to(outside)
    assert check(repo, "worktree") == []
    repo.close()
