"""Harvest candidate decisions from history, backtest each, and judge it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from scarwright import backtest, detectors
from scarwright import imports as imp
from scarwright.gitio import GitError, Repo
from scarwright.harvest import Ctx, boundary, dependency, revert
from scarwright.models import Decision, Settings
from scarwright.redact import clean

_TIER_ORDER = {"proven": 0, "candidate": 1, "covered": 2, "moot": 3, "reversed": 4, "rejected": 5}


@dataclass
class ScanResult:
    head: str
    decisions: list[Decision] = field(default_factory=list)
    commits_scanned: int = 0
    shallow: bool = False
    unparsed_manifests: list[str] = field(default_factory=list)
    unreadable_sources: list[str] = field(default_factory=list)
    skipped_commits: list[str] = field(default_factory=list)
    unchecked_languages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def tier(self, name: str) -> list[Decision]:
        return [d for d in self.decisions if d.tier == name]


def _identity(d: Decision) -> tuple:
    det = d.detector
    if det["kind"] == "dependency":
        return ("dependency", det["ecosystem"], det["name"], det["manifest"])
    if det["kind"] == "import_ban":
        return ("import_ban", det["lang"], det["scope"], det["module"])
    return ("revert", det["reverted"])


def _moot(ctx: Ctx, d: Decision, head: str) -> str | None:
    """A proven decision about something that no longer exists has nothing left to guard."""
    det, tree = d.detector, ctx.repo.tree(head)
    if det["kind"] == "dependency":
        return None if det["manifest"] in tree else "its manifest file no longer exists"
    if det["kind"] == "revert":
        return None if any(p in tree for p in det["signature"]) else "the files it touched no longer exist"
    if d.intent.get("internal") and not all(boundary.target_status(ctx, head, det["lang"], det["module"])):
        return "the module or name it concerns no longer exists in the repository"
    return None


def _unchecked_languages(repo: Repo, head: str) -> list[str]:
    known = {"Go": ".go", "Rust": ".rs", "Java": ".java", "Ruby": ".rb", "PHP": ".php", "C#": ".cs", "Kotlin": ".kt", "Swift": ".swift",
             "C/C++": (".c", ".cc", ".cpp", ".h", ".hpp"), "Dart": ".dart", "Scala": ".scala", "Elixir": (".ex", ".exs"), "notebooks": ".ipynb"}
    paths = list(repo.tree(head))
    return sorted(name for name, ext in known.items() if any(p.endswith(ext) for p in paths))


def scan(repo: Repo, settings: Settings | None = None, progress: Callable[[int, int], None] | None = None) -> ScanResult:
    settings = settings or Settings()
    repo.forget_history()  # the repository may have moved on since this object last looked
    head = repo.head()
    index = detectors.Index(repo)
    mainline = repo.mainline()
    ctx = Ctx(repo, index, settings, mainline, {sha: i for i, sha in enumerate(mainline)})
    result = ScanResult(head=head, shallow=repo.is_shallow())
    commits = repo.history()
    best: dict[tuple, Decision] = {}
    undone: dict[str, str] = {}  # commit -> the later commit that explicitly reverted it

    for n, commit in enumerate(commits, 1):
        if progress:
            progress(n, len(commits))
        if commit.is_merge or not commit.parents:
            continue
        result.commits_scanned += 1
        try:
            reverts = revert._REVERTS_RE.search(commit.body)
            target = repo.resolve(reverts.group(1)) if reverts else None
            if target:
                undone[target] = commit.sha
            changes = repo.changes(commit.sha)
            if len(changes) > settings.max_files_per_commit:
                result.skipped_commits.append(commit.sha[:8])  # too large to analyse in bounded time; said so in the output
                continue
            found = dependency.harvest(ctx, commit, changes)
            skip = {("python" if d.detector["ecosystem"] == "pip" else "js", m) for d in found for m in d.detector["import_names"]}
            found += boundary.harvest(ctx, commit, changes, skip)
            found += revert.harvest(ctx, commit)
            for d in found:
                det = detectors.build(index, d.detector, settings)
                d.backtest = backtest.run(repo, det, commit.sha, mainline, ctx.pos, reversal_days=settings.reversal_days)
                backtest.judge(d, settings)
                if d.tier == "proven":
                    gone = _moot(ctx, d, head)
                    if gone:
                        d.tier, d.reason = "moot", gone
                key = _identity(d)
                cur = best.get(key)
                if cur is None or _TIER_ORDER[d.tier] < _TIER_ORDER[cur.tier]:
                    best[key] = d
        except Exception as e:  # one unreadable commit must never take the whole scan down
            result.errors.append(f"{commit.sha[:8]}: {type(e).__name__}")

    for d in best.values():
        if d.family == "revert" and d.commit in undone and d.tier in ("proven", "candidate"):
            d.tier, d.reason = "reversed", f"the revert was itself reverted in {undone[d.commit][:8]}, putting the change back"
    bans = [d for d in best.values() if d.tier == "proven" and d.detector["kind"] == "import_ban"]
    for d in bans:
        det = d.detector
        wider = next((o for o in bans if o is not d and o.detector["lang"] == det["lang"] and o.detector["module"] == det["module"]
                      and o.detector["scope"] != det["scope"] and detectors.in_scope(det["scope"], o.detector["scope"])), None)
        if wider is not None:
            d.tier, d.reason = "covered", f"covered by the wider decision {wider.id}"
    result.decisions = sorted(best.values(), key=lambda d: (_TIER_ORDER[d.tier], d.date, d.id))
    result.unparsed_manifests = sorted(clean(p, 300) for p in index.unparsed_manifests)
    result.unreadable_sources = sorted(clean(p, 300) for p in index.unreadable_sources)
    result.unchecked_languages = _unchecked_languages(repo, head)
    return result
