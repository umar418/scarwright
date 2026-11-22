"""Defects found by the security attack pass before the first release, kept as tests.

Threat model: the repository being scanned is attacker-controlled, including anything committed under .scarwright/.
"""
from __future__ import annotations

import json
import sys
import time

import pytest
from typer.testing import CliRunner

from scarwright import imports as imp
from scarwright import ledger, manifests, receipt
from scarwright.check import check
from scarwright.cli import app
from scarwright.gitio import Repo
from scarwright.redact import clean, is_plain
from scarwright.scan import scan

from conftest import Sandbox, package_json

runner = CliRunner()
unix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")


def _proven_repo(sb: Sandbox, subject: str = "to dayjs") -> None:
    sb.write("package.json", package_json({"moment": "^2"}))
    sb.write("a.js", "import moment from 'moment';\n")
    sb.commit("init")
    sb.write("package.json", package_json({"dayjs": "^1"}))
    sb.write("a.js", "import dayjs from 'dayjs';\n")
    sb.commit(subject)
    for i in range(3):
        sb.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sb.commit(f"x{i}")


# ---- writing outside the repository

@unix_only
@pytest.mark.parametrize("link", [".scarwright", ".scarwright/decisions", ".scarwright/cache", ".scarwright/.gitignore"])
def test_scan_never_writes_through_a_symlink(sandbox, tmp_path, link):
    _proven_repo(sandbox)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    target = sandbox.root / link
    target.parent.mkdir(parents=True, exist_ok=True)
    if link.endswith(".gitignore"):
        target.symlink_to(victim)
    else:
        target.symlink_to(outside, target_is_directory=True)
    res = runner.invoke(app, ["scan", str(sandbox.root)])
    assert "Traceback" not in res.output
    assert list(outside.iterdir()) == []


@unix_only
def test_predictable_ledger_file_symlink_is_not_followed(sandbox, tmp_path):
    _proven_repo(sandbox)
    repo = Repo(sandbox.root)
    [d] = scan(repo).tier("proven")
    repo.close()
    rc = tmp_path / "shellrc"
    rc.write_text("# my shell config\n", encoding="utf-8")
    folder = sandbox.root / ".scarwright" / "decisions"
    folder.mkdir(parents=True)
    (folder / f"{d.id}.yaml").symlink_to(rc)
    res = runner.invoke(app, ["scan", str(sandbox.root)])
    assert res.exit_code == 2 and "Traceback" not in res.output
    assert rc.read_text(encoding="utf-8") == "# my shell config\n"
    for cmd in (["decisions", "reject", d.id, "--repo", str(sandbox.root)], ["decisions", "enforce", d.id, "block", "--repo", str(sandbox.root)]):
        runner.invoke(app, cmd)
        assert rc.read_text(encoding="utf-8") == "# my shell config\n"


# ---- hangs

@pytest.mark.parametrize("text", [
    "var a " * 2500 + "require('a')" * 2500,
    "import a\n" * 60_000,
    "import 'a';\n" * 150_000,
    "import {" + "a," * 50_000,
    "`" * 100_000,
    "/*" * 200_000,
    "require(" * 50_000,
])
def test_hostile_js_is_parsed_in_bounded_time(text):
    started = time.monotonic()
    imp.parse_or_none("x.js", text.encode())
    assert time.monotonic() - started < 10


def test_dense_file_is_reported_unreadable_rather_than_trusted():
    assert imp.parse_or_none("x.js", ("import 'a';\n" * 150_000).encode()) is None
    assert imp.parse_or_none("x.js", b"import a from 'a';\n") is not None


# ---- hostile committed ledger files

BAD_LEDGERS = {
    "markup-tier": "id: markup-tier\ntier: '[/nope]'\nstatus: active\nenforcement: block\ndetector: {kind: dependency, ecosystem: npm, name: a, manifest: package.json}\n",
    "decided-list": "id: decided-list\ntier: proven\ndecided: [1, 2]\ndetector: {kind: dependency, ecosystem: npm, name: a, manifest: package.json}\nstatement: [1]\n",
    "signature-list": "id: signature-list\ntier: proven\ndetector: {kind: revert, reverted: abcdef1, signature: [1, 2]}\n",
    "deep": "id: deep\ndetector: " + "[" * 3000 + "]" * 3000 + "\n",
    "alias-bomb": "a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\nc: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
                  "d: &d [*c, *c, *c, *c, *c, *c, *c, *c, *c]\ne: &e [*d, *d, *d, *d, *d, *d, *d, *d, *d]\n"
                  "f: &f [*e, *e, *e, *e, *e, *e, *e, *e, *e]\ng: &g [*f, *f, *f, *f, *f, *f, *f, *f, *f]\n"
                  "id: alias-bomb\ntier: proven\nstatement: *g\ndetector: {kind: dependency, ecosystem: npm, name: a, manifest: package.json}\n",
    "escape-statement": "id: escape-statement\ntier: proven\nstatus: active\nenforcement: warn\nstatement: \"\\e]8;;http://evil\\e\\\\click\\e]8;;\\e\\\\ \\ec\"\n"
                        "detector: {kind: dependency, ecosystem: npm, name: a, manifest: package.json}\n",
    "wrong-id": "id: some-other-id\ntier: proven\ndetector: {kind: dependency, ecosystem: npm, name: a, manifest: package.json}\n",
    "python-tag": "id: python-tag\ntier: proven\nstatement: !!python/object/apply:os.system ['true']\ndetector: {kind: dependency, ecosystem: npm, name: a, manifest: package.json}\n",
}


def _plant(sb: Sandbox) -> None:
    folder = sb.root / ".scarwright" / "decisions"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in BAD_LEDGERS.items():
        (folder / f"{name}.yaml").write_text(text, encoding="utf-8")
    (folder / "not-utf8.yaml").write_bytes(b"id: not-utf8\nstatement: \xff\xfe\n")
    (folder / "huge.yaml").write_text("id: huge\nstatement: " + "x" * 9_000_000 + "\n", encoding="utf-8")
    cache = sb.root / ".scarwright" / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "scan.json").write_text(json.dumps({"decisions": [1, "x", {"no": "id"}, {"id": "ok-row", "tier": "[/x]", "statement": ["a"], "reason": "\x1b[2J"}]}), encoding="utf-8")


def test_hostile_ledger_files_cause_no_traceback_no_hang_no_escape(sandbox):
    sandbox.write("package.json", package_json({"a": "1"}))
    sandbox.commit("init")
    _plant(sandbox)
    started = time.monotonic()
    loaded = {d.id: d for d in ledger.load(sandbox.root)}
    assert set(loaded) <= {"escape-statement", "decided-list"}  # malformed files are dropped; usable ones are cleaned
    assert all("\x1b" not in d.statement for d in loaded.values())
    sandbox.write("package.json", package_json({"a": "1", "b": "1"}))
    sandbox.git("add", "package.json")
    repo = Repo(sandbox.root)
    assert check(repo, "staged") == []
    repo.close()
    for args in (["decisions", "list", str(sandbox.root)], ["decisions", "list", str(sandbox.root), "--all"],
                 ["decisions", "show", "ok-row", "--repo", str(sandbox.root)], ["why", "package.json", "--repo", str(sandbox.root)],
                 ["check", str(sandbox.root)], ["scan", str(sandbox.root)], ["receipt", str(sandbox.root)]):
        res = runner.invoke(app, args)
        assert "Traceback" not in res.output and res.exception is None or isinstance(res.exception, SystemExit), (args, res.output[-300:])
        assert "\x1b[2J" not in res.output and "\x1b]8" not in res.output
    assert time.monotonic() - started < 60


# ---- commit metadata

def test_field_separator_bytes_in_author_name_do_not_shift_fields(sandbox):
    _proven_repo(sandbox)
    sandbox.write("z.txt", "z\n")
    sandbox.git("add", "-A")
    sandbox.git("-c", "user.name=Eve\x1f[/bad]\x1f2024\x1e", "commit", "-q", "-m", "odd author [/x]")
    res = runner.invoke(app, ["scan", str(sandbox.root), "--no-write"])
    assert res.exit_code == 0 and "Traceback" not in res.output
    repo = Repo(sandbox.root)
    for c in repo.history():
        assert c.date[:4] == "2024" and len(c.sha) >= 40
    repo.close()


# ---- secrets

@pytest.mark.parametrize("secret,text", [
    ("S3cr3tP4ssw0rd", "db postgres://admin:S3cr3tP4ssw0rd@db.example.com:5432/app"),
    ("hunter2hunter2", "cache redis://:hunter2hunter2@cache:6379/0"),
    ("p4ssw0rdp4ss", "mongodb+srv://app:p4ssw0rdp4ss@cluster0.example.net/db"),
    ("wJalrXUt" "nFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "aws secret wJalrXUt" "nFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY rotated"),
    ("hunter22pw", "curl -u admin:hunter22pw https://example.com"),
    ("hunter22pw", "DB_PASS=hunter22pw in the env"),
    ("correcthorse1", "passphrase=correcthorse1"),
    ("XXXXXXXXXXXXXXXXXXXXXXXX", "hook https://hooks.slack.com/" "services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"),
    ("9Tm1Kj6Hg3Dp5Sa0Yr", "key sk-ant-" "api03-Zq8Lw2Nf7Xc4Vb\n9Tm1Kj6Hg3Dp5Sa0Yr tail"),
    ("Zq8Lw2Nf7Xc4Vb9Tm1Kj6Hg3Dp5Sa0Yr", "x" * 620 + " sk-ant-" "api03-Zq8Lw2Nf7Xc4Vb9Tm1Kj6Hg3Dp5Sa0Yr"),
])
def test_secret_formats_are_masked(secret, text):
    assert secret not in clean(text, 160) and secret not in clean(text, 4000)


def test_ordinary_text_is_left_readable():
    line = "move src/scarwright/harvest/dependency.py to src/scarwright/harvest/deps/dependency.py (#123)"
    assert clean(line, 200) == line


def test_names_that_are_not_real_package_names_are_dropped():
    data = json.dumps({"dependencies": {
        "ok-pkg": "1", "@scope/ok": "1", "bad\x1b[2Jname": "1", "new\nline": "1", "sk-ant-" "api03-Zq8Lw2Nf7Xc4Vb9Tm1Kj6Hg3Dp5Sa0Yr": "1",
        "rtl\u202ename": "1", "[bold]x[/bold]": "1", "x" * 300: "1"}}).encode()
    assert manifests.parse_manifest("package.json", data) == {"ok-pkg", "@scope/ok"}
    assert not is_plain("a\x1bc") and is_plain("@scope/pkg")


def test_hostile_names_never_reach_any_output(sandbox):
    secret = "sk-ant-" "api03-Zq8Lw2Nf7Xc4Vb9Tm1Kj6Hg3Dp5Sa0Yr"
    evil = {"moment": "^2", "esc\x1bcname": "1", "head\n# Injected": "1", secret: "1"}
    sandbox.write("package.json", package_json(evil))
    sandbox.write("a.js", f"import moment from 'moment';\nimport x from 'pkg\x1b]8;;http://evil\x1b\\\\';\nimport y from '{secret}';\n")
    sandbox.commit("init")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    sandbox.write("a.js", "import dayjs from 'dayjs';\n")
    sandbox.commit("drop [evil](http://evil) ![img](http://evil/x.png) things")
    for i in range(3):
        sandbox.write("package.json", package_json({"dayjs": "^1", f"x{i}": "1"}))
        sandbox.commit(f"x{i}")
    sandbox.write("package.json", package_json({"dayjs": "^1", "moment": "^2"}))
    sandbox.commit("again")
    sandbox.write("package.json", package_json({"dayjs": "^1"}))
    sandbox.commit("gone")
    repo = Repo(sandbox.root)
    result = scan(repo)
    ledger.write(repo.root, result)
    repo.close()
    report = receipt.build(result)
    md = receipt.to_markdown(report)
    blob = md + receipt.to_html(report) + json.dumps(ledger.to_json(result))
    blob += "".join(p.read_text(encoding="utf-8") for p in (sandbox.root / ".scarwright").rglob("*") if p.is_file())
    for args in (["scan", str(sandbox.root)], ["receipt", str(sandbox.root)], ["decisions", "list", str(sandbox.root), "--all"],
                 ["why", "package.json", "--repo", str(sandbox.root)]):
        blob += runner.invoke(app, args).output
    assert secret not in blob and "\x1b" not in blob and "\u202e" not in blob
    assert "\n# Injected" not in md and "](http://evil" not in md


# ---- arguments

@pytest.mark.parametrize("args", [
    ["why", "a.js:²"], ["why", "a.js:" + "9" * 5000], ["why", "x" * 5000], ["why", ":"], ["why", "a.js:0"],
    ["decisions", "show", "../../x"], ["decisions", "enforce", "nope", "block"], ["decisions", "reject", "x" * 500],
    ["check", "--base", "--output=/tmp/x"], ["check", "--base", "nonexistent-rev"], ["receipt", "--format", "pdf"],
])
def test_odd_arguments_give_a_message_not_a_traceback(sandbox, args):
    _proven_repo(sandbox)
    extra = [str(sandbox.root)] if args[0] in ("check", "receipt") else ["--repo", str(sandbox.root)]
    res = runner.invoke(app, args + extra)
    assert "Traceback" not in res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), repr(res.exception)


def test_receipt_output_to_a_folder_or_missing_folder_is_a_clean_error(sandbox, tmp_path):
    _proven_repo(sandbox)
    for target in (tmp_path, tmp_path / "missing" / "r.md"):
        res = runner.invoke(app, ["receipt", str(sandbox.root), "--format", "md", "-o", str(target)])
        assert res.exit_code == 2 and (res.exception is None or isinstance(res.exception, SystemExit))


# ---- repository config that names programs

@unix_only
def test_programs_named_in_repository_config_are_not_run(sandbox, tmp_path):
    _proven_repo(sandbox)
    repo = Repo(sandbox.root)
    ledger.write(repo.root, scan(repo))
    repo.close()
    marker = tmp_path / "ran"
    sandbox.write(".gitattributes", "*.json filter=evil diff=evil\n*.js filter=evil diff=evil\n")
    for key in ("filter.evil.clean", "filter.evil.smudge", "diff.evil.textconv", "diff.evil.command"):
        sandbox.git("config", key, f"sh -c 'touch {marker}; cat'")
    sandbox.git("config", "log.showSignature", "true")
    sandbox.git("config", "gpg.program", f"touch {marker}")
    sandbox.write("package.json", package_json({"dayjs": "^1", "moment": "^2"}))
    for args in (["check", str(sandbox.root), "--worktree"], ["scan", str(sandbox.root), "--no-write"], ["receipt", str(sandbox.root)],
                 ["why", "package.json:2", "--repo", str(sandbox.root)]):
        runner.invoke(app, args)
    assert not marker.exists()
