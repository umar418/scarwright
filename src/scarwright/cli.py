from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from scarwright import __version__, detectors, ledger, receipt
from scarwright import check as check_mod
from scarwright.gitio import GitError, Repo
from scarwright.ledger import LedgerError
from scarwright.models import Settings
from scarwright.redact import plain
from scarwright.scan import ScanResult, scan as run_scan

app = typer.Typer(
    name="scarwright",
    help="Decisions found in your repository's history, proven against that history before they may flag anything.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
decisions_app = typer.Typer(help="Inspect and manage the decision ledger.", no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(decisions_app, name="decisions")
out = Console()
err = Console(stderr=True)


def safe(text: object) -> str:
    """Anything that came from a repository, made safe to print: no control characters, no console markup."""
    return escape(plain(text))


def fail(message: str, code: int = 2):
    err.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(code)


def _repo(path: Path) -> Repo:
    try:
        return Repo(path)
    except (GitError, OSError, ValueError) as e:
        fail(f"Not a usable git repository: {plain(e)[:300]}")


def _version(value: bool):
    if value:
        out.print(__version__)
        raise typer.Exit()


@app.callback()
def main(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version.")):
    pass


def _scan(repo: Repo, quiet: bool) -> ScanResult:
    try:
        if quiet:
            return run_scan(repo, Settings())
        with err.status("Reading history…") as status:
            return run_scan(repo, Settings(), lambda n, total: status.update(f"Reading history… {n}/{total} commits") if n % 25 == 0 else None)
    except GitError as e:
        fail(f"Could not read history: {plain(e)[:300]}")


def _notes(result: ScanResult) -> None:
    if result.shallow:
        err.print("[yellow]This clone is shallow; older decisions cannot be seen.[/yellow]")
    if result.unchecked_languages:
        err.print(f"[dim]Not analysed yet: {safe(', '.join(result.unchecked_languages))} code.[/dim]")
    if result.unparsed_manifests:
        err.print(f"[dim]{len(result.unparsed_manifests)} manifest file(s) could not be read at some point in history; nothing is claimed across those versions.[/dim]")
    if result.unreadable_sources:
        err.print(f"[dim]{len(result.unreadable_sources)} source file(s) were too large or not text; each was treated as possibly still using anything.[/dim]")
    if result.skipped_commits:
        err.print(f"[dim]{len(result.skipped_commits)} very large commit(s) were skipped rather than analysed.[/dim]")
    if result.errors:
        err.print(f"[dim]{len(result.errors)} commit(s) could not be analysed and were skipped.[/dim]")


@app.command()
def scan(
    path: Path = typer.Argument(Path("."), help="Repository to scan."),
    as_json: bool = typer.Option(False, "--json", help="Print results as JSON."),
    no_write: bool = typer.Option(False, "--no-write", help="Do not write the ledger; just report."),
):
    """Find decisions in history, backtest each one, and record the proven ones."""
    repo = _repo(path)
    try:
        result = _scan(repo, as_json)
        new = updated = 0
        if not no_write:
            try:
                new, updated = ledger.write(repo.root, result)
            except (LedgerError, OSError) as e:
                fail(f"Results were not saved: {plain(e)[:300]}")
    finally:
        repo.close()
    if as_json:
        sys.stdout.write(json.dumps(ledger.to_json(result), indent=1) + "\n")
        return
    proven = result.tier("proven")
    out.print(f"Read {result.commits_scanned} commits. [bold]{len(proven)} proven[/bold], "
              f"{len(result.tier('candidate'))} not yet provable, {len(result.tier('reversed'))} later reversed, "
              f"{len(result.tier('moot'))} about code that no longer exists.")
    for d in proven:
        bt = d.backtest
        broke = len(bt.reintroductions) + (1 if bt.open else 0)
        tail = f"  [yellow]broken again {broke}×[/yellow]" if broke else ""
        out.print(f"  [green]✓[/green] {safe(d.statement)} [dim]({safe(d.commit[:8])}, {safe(d.date[:10])})[/dim]{tail}")
    if not no_write and (new or updated):
        out.print(f"[dim]Ledger: {new} new, {updated} updated in {ledger.DIR}/decisions/[/dim]")
    if not proven:
        out.print("[dim]Nothing proven yet. `scarwright decisions list --all` shows what was found and why it falls short.[/dim]")
    _notes(result)


@app.command(name="receipt")
def receipt_cmd(
    path: Path = typer.Argument(Path("."), help="Repository to report on."),
    fmt: str = typer.Option("text", "--format", help="text, md, html or json."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write to this file instead of the terminal."),
):
    """Show every time a proven decision was broken again, and how long it lasted."""
    if fmt not in ("text", "md", "html", "json"):
        fail("--format must be text, md, html or json")
    repo = _repo(path)
    try:
        result = _scan(repo, fmt == "json")
    finally:
        repo.close()
    report = receipt.build(result)
    text = {"json": lambda: json.dumps(report, indent=1) + "\n", "md": lambda: receipt.to_markdown(report),
            "html": lambda: receipt.to_html(report), "text": lambda: None}[fmt]()
    if text is not None:
        if output:
            try:
                output.write_text(text, encoding="utf-8")
            except OSError as e:
                fail(f"Could not write {plain(output)}: {e.strerror or 'error'}")
            err.print(f"Wrote {safe(output)}")
        else:
            sys.stdout.write(text)
        return
    out.print(f"[bold]{report['proven']}[/bold] proven decisions, broken again [bold]{report['broken']}[/bold] time(s); "
              f"{report['agent_events']} in commits carrying an AI agent trailer.")
    for e in report["open"]:
        out.print(f"  [red]open {safe(e['days'])}d[/red] {safe(e['statement'])} [dim]brought back by {safe(e['commit'][:8])} ({safe(e['author'])})[/dim]")
    for e in report["events"]:
        out.print(f"  [yellow]{safe(e['days'])}d[/yellow] {safe(e['statement'])} [dim]brought back by {safe(e['commit'][:8])}, fixed in {safe(e['corrected_by'][:8])}[/dim]")
    for line in report["limits"]:
        err.print(f"[dim]· {safe(line)}[/dim]")


@app.command()
def check(
    path: Path = typer.Argument(Path("."), help="Repository to check."),
    worktree: bool = typer.Option(False, "--worktree", help="Check files on disk instead of what is staged."),
    base: Optional[str] = typer.Option(None, "--base", help="Check everything committed since this revision."),
    as_json: bool = typer.Option(False, "--json", help="Print findings as JSON."),
):
    """Check a change against the proven decisions. Exits 1 only when a decision set to block is broken."""
    repo = _repo(path)
    try:
        mode = "range" if base else "worktree" if worktree else "staged"
        findings = check_mod.check(repo, mode, base)
        nothing_enforced = not findings and not any(d.enforced for d in ledger.load(repo.root))
    except (GitError, ValueError, OSError) as e:
        fail(f"Check failed: {plain(e)[:300]}")
    finally:
        repo.close()
    if as_json:
        sys.stdout.write(json.dumps([f.to_json() for f in findings], indent=1) + "\n")
    else:
        for f in findings:
            d = f.decision
            where = f"{f.path}:{f.line}" if f.line else f.path
            level = "[red]BLOCK[/red]" if f.blocking else "[yellow]WARN[/yellow]"
            out.print(f"{level} {safe(where)} {safe(f.detail)}")
            out.print(f"      {safe(d.statement)}")
            out.print(f"      [dim]evidence: {safe(d.commit[:8])} {safe(d.date[:10])} “{safe(d.subject)}” · {safe(d.id)}[/dim]")
        if nothing_enforced:
            err.print("[dim]Nothing was checked: there are no proven decisions in the ledger yet. Run `scarwright scan` first.[/dim]")
        elif not findings:
            err.print("[dim]No proven decision is broken by this change.[/dim]")
    if any(f.blocking for f in findings):
        raise typer.Exit(1)


@app.command()
def why(target: str = typer.Argument(..., help="path or path:line"), path: Path = typer.Option(Path("."), "--repo", help="Repository.")):
    """Show the decisions that govern a file, with their evidence."""
    file_path, _, line = target.rpartition(":") if target.rpartition(":")[2].isascii() and target.rpartition(":")[2].isdigit() else (target, "", "")
    file_path = file_path.replace("\\", "/")
    while file_path.startswith("./"):
        file_path = file_path[2:]
    line_no = int(line) if line and len(line) <= 7 else None
    repo = _repo(path)
    try:
        index = detectors.Index(repo)
        try:
            blamed = repo.blame_commit(file_path, line_no) if line_no else None
        except (GitError, OSError, ValueError):
            blamed = None
        rows = []
        for d in ledger.load(repo.root):
            try:
                det = detectors.build(index, d.detector)
            except (ValueError, KeyError, TypeError):
                continue
            if det.relevant(file_path) or (blamed and d.commit == blamed):
                rows.append(d)
    finally:
        repo.close()
    if not rows:
        out.print("No recorded decision covers this file.")
        return
    for d in rows:
        out.print(f"[bold]{safe(d.statement)}[/bold] [dim]{safe(d.tier)}, {safe(d.enforcement)}[/dim]")
        out.print(f"  decided in {safe(d.commit[:8])} on {safe(d.date[:10])} by {safe(d.author)}: “{safe(d.subject)}”")
        for b in d.basis:
            out.print(f"  · {safe(b)}")


@decisions_app.command("list")
def decisions_list(
    path: Path = typer.Argument(Path("."), help="Repository."),
    show_all: bool = typer.Option(False, "--all", help="Include findings that are not proven, with the reason."),
):
    """List decisions in the ledger."""
    repo = _repo(path)
    root = repo.root
    repo.close()
    table = Table(show_header=True, header_style="bold")
    for col in ("id", "tier", "level", "decision"):
        table.add_column(col, overflow="fold")
    if show_all:
        cache = ledger.load_cache(root)
        if cache is None:
            fail("No scan results yet. Run `scarwright scan` first.")
        for d in cache["decisions"]:
            note = f"\n[dim]{safe(d.get('reason') or '')}[/dim]" if d.get("tier") != "proven" else ""
            table.add_row(safe(d["id"]), safe(d.get("tier")), safe(d.get("enforcement")), safe(d.get("statement")) + note)
    else:
        for d in ledger.load(root):
            table.add_row(safe(d.id), safe(d.tier), safe(d.enforcement if d.status == "active" else d.status), safe(d.statement))
    out.print(table)


@decisions_app.command("show")
def decisions_show(decision_id: str, path: Path = typer.Option(Path("."), "--repo", help="Repository.")):
    """Show one decision with its evidence and backtest numbers."""
    repo = _repo(path)
    root = repo.root
    repo.close()
    cache = ledger.load_cache(root) or {"decisions": []}
    row = next((d for d in cache["decisions"] if d.get("id") == decision_id), None)
    if row is None:
        fail("Unknown decision id (run `scarwright scan` to refresh).")
    sys.stdout.write(json.dumps(row, indent=1) + "\n")


def _set(decision_id: str, path: Path, key: str, value: str) -> None:
    repo = _repo(path)
    root = repo.root
    repo.close()
    try:
        done = ledger.set_field(root, decision_id, key, value)
    except (LedgerError, OSError) as e:
        fail(f"Not saved: {plain(e)[:300]}")
    if not done:
        fail("No such decision in the ledger.")
    out.print(f"{safe(decision_id)}: {key} = {safe(value)}")


@decisions_app.command("enforce")
def decisions_enforce(decision_id: str, level: str = typer.Argument(..., help="warn, block or off"),
                      path: Path = typer.Option(Path("."), "--repo", help="Repository.")):
    """Set how a decision is enforced: warn, block or off."""
    if level not in ("warn", "block", "off"):
        fail("Level must be warn, block or off.")
    _set(decision_id, path, "enforcement", level)


@decisions_app.command("reject")
def decisions_reject(decision_id: str, path: Path = typer.Option(Path("."), "--repo", help="Repository.")):
    """Mark a decision as wrong so it is never enforced. The file stays as a record."""
    _set(decision_id, path, "status", "rejected")


def run() -> None:
    """Entry point. Whatever a hostile repository manages to trigger, the user gets one plain line, never a traceback."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        app()
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"scarwright: stopped on an unexpected {type(e).__name__}. Nothing was changed by this command "
                         "unless a message above says so. Please report this.\n")
        sys.exit(3)
