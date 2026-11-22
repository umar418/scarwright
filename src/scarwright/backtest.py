"""Replay a detector over the repository's own history and judge whether the decision is proven."""
from __future__ import annotations

from datetime import datetime

from scarwright.gitio import Repo
from scarwright.models import Backtest, Decision, Reintroduction, Settings
from scarwright.redact import clean


def _days(a: str, b: str) -> float | None:
    try:
        # git writes UTC as "Z"; Python 3.10 only understands "+00:00"
        a, b = (x[:-1] + "+00:00" if x.endswith("Z") else x for x in (a, b))
        return max(0.0, round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400, 1))
    except (ValueError, TypeError):
        return None


def landing_index(repo: Repo, mainline: list[str], pos: dict[str, int], sha: str) -> int | None:
    """Index of the first main-line commit that contains sha."""
    if sha in pos:
        return pos[sha]
    if not mainline or not repo.is_ancestor(sha, mainline[-1]):
        return None
    lo, hi = 0, len(mainline) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if repo.is_ancestor(sha, mainline[mid]):
            hi = mid
        else:
            lo = mid + 1
    return lo


def _culprit(repo: Repo, detector, merge_sha: str, paths: frozenset[str]) -> str:
    """Inside a merge, the commit that put the pattern into the offending file.

    The branch may have forked before the decision, so its own history never shows a silent-to-firing flip;
    what identifies the culprit is the commit that last made that file fire.
    """
    if not repo.commit(merge_sha).is_merge:
        return merge_sha
    found = merge_sha
    for sha in repo.rev_list(["--no-merges", "--reverse", f"{merge_sha}^1..{merge_sha}"])[:300]:
        touched = [c.path for c in repo.changes(sha) if c.path in paths]
        if not touched:
            continue
        before = detector.update(frozenset(), repo.parent_of(sha), touched)
        after = detector.update(frozenset(), sha, touched)
        if after - before:
            return sha
        if after and found == merge_sha:
            found = sha
    return found


def _clock(repo: Repo, sha: str) -> str:
    """When a main-line commit took effect: its committer date, which is when it was made or merged."""
    return repo.commit(sha).cdate


def run(repo: Repo, detector, decision_sha: str, mainline: list[str], pos: dict[str, int], landing: int | None = None,
        reversal_days: int = 90) -> Backtest:
    bt = Backtest(head=mainline[-1] if mainline else "")
    bt.fires_before = bool(detector.state(repo.parent_of(decision_sha)))
    bt.silent_after = not detector.state(decision_sha)
    if not (bt.fires_before and bt.silent_after):
        return bt
    li = landing if landing is not None else landing_index(repo, mainline, pos, decision_sha)
    if li is None:
        return bt
    bt.landed = mainline[li]
    state = detector.state(mainline[li])
    bt.silent_at_landing = not state
    if state:
        return bt

    changes = repo.mainline_changes()
    open_r: Reintroduction | None = None
    for i in range(li + 1, len(mainline)):
        m = mainline[i]
        changed = [p for p in changes.get(m, ()) if detector.relevant(p)]
        if not changed:
            continue
        new_state = detector.update(state, m, changed)
        if not state and new_state:
            c = repo.commit(_culprit(repo, detector, m, new_state))
            # Dated from when it reached the main line, not when it was first written on a branch.
            open_r = Reintroduction(c.sha, _clock(repo, m), clean(c.author, 60), c.agent, clean(c.subject), [clean(p, 300) for p in sorted(new_state)[:10]])
        elif state and not new_state and open_r is not None:
            fixer = repo.commit(m)
            tree = repo.tree(m)
            open_r.corrected_by = m
            open_r.corrected_date = fixer.cdate
            open_r.survived_days = _days(open_r.date, fixer.cdate)
            open_r.how = "changed" if any(p in tree for p in state) else "file removed"
            if open_r.survived_days is not None and open_r.survived_days > reversal_days:
                bt.reversed_by = open_r  # it came back and stayed: the decision ended there, whatever happened later
                return bt
            bt.reintroductions.append(open_r)
            open_r = None
        if not new_state:
            bt.exposure += 1
            if open_r is None:
                bt.exposure_before_open = bt.exposure
        state = new_state
    if open_r is not None:
        open_r.survived_days = _days(open_r.date, _clock(repo, mainline[-1]))
        bt.open = open_r
    return bt


def judge(d: Decision, s: Settings) -> None:
    """Set tier and reason from the backtest. Proof needs history; without it the decision stays a candidate."""
    bt = d.backtest
    if not (bt.fires_before and bt.silent_after):
        d.tier, d.reason = "rejected", "the check did not separate the code before and after the change"
        return
    if not bt.silent_at_landing:
        d.tier, d.reason = "rejected", "the change did not hold when it reached the main line"
        return
    exposure = bt.exposure
    if bt.reversed_by is not None:
        r = bt.reversed_by
        d.tier, d.reason = "reversed", f"brought back in {r.commit[:8]} and kept for {r.survived_days} days"
        return
    if bt.open is not None:
        age = bt.open.survived_days
        if age is None or age > s.reversal_days:
            d.tier, d.reason = "reversed", f"present again since {bt.open.commit[:8]} and never removed"
            return
        exposure = bt.exposure_before_open
    corrected = [r for r in bt.reintroductions if r.how == "changed"]
    need = s.min_exposure_manifest if d.family == "dependency" else s.min_exposure_code

    proven, why = False, ""
    if d.family == "dependency":
        if corrected:
            proven, why = True, "it was added back and removed again"
        elif d.intent.get("usage_files", 0) >= 1 and exposure >= need:
            proven, why = True, "code was migrated off it, and neither the manifest nor the code has brought it back"
        elif d.intent.get("usage_files", 0) >= 1:
            why = f"too little history since to prove it ({exposure} of {need} later changes to the manifest)"
        else:
            why = "removed from the manifest only; no sign the code was moved off it"
    elif d.family == "boundary":
        if not d.intent.get("replaced_by"):
            why = "imports disappeared but nothing replaced them, so this may be cleanup rather than a decision"
        elif exposure >= need:
            proven, why = True, "the replacement has held through later changes"
        else:
            why = f"too little history since to prove it ({exposure} of {need} later changes in scope)"
    elif d.family == "revert":
        if corrected or exposure >= need:
            proven, why = True, "the reverted change has stayed out through later changes to the same files"
        else:
            why = f"too little history since to prove it ({exposure} of {need} later changes to those files)"
    d.tier = "proven" if proven else "candidate"
    d.reason = why
    if proven and corrected:
        note = f"brought back and removed again {len(corrected)} time(s)"
        if note not in d.basis:
            d.basis.append(note)
