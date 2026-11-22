"""The receipt: every time a proven decision was broken again, by which commit, and how long that lasted."""
from __future__ import annotations

import re
import statistics

from jinja2 import Environment

from scarwright.scan import ScanResult

_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Scarwright receipt</title>
<style>
:root{--bg:#fbfaf7;--fg:#1d1c1a;--mut:#6b6862;--line:#e3e0d8;--acc:#9a3412}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--fg:#ece9e2;--mut:#9c988f;--line:#2e2c28;--acc:#fb923c}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,system-ui,sans-serif;max-width:860px;margin:0 auto;padding:32px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 8px}
.mut{color:var(--mut)}.num{font-variant-numeric:tabular-nums}
.stats{display:flex;flex-wrap:wrap;gap:24px;margin:20px 0;padding:16px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.stats b{display:block;font-size:24px}
.row{padding:12px 0;border-bottom:1px solid var(--line)}
code{font:13px ui-monospace,monospace;overflow-wrap:anywhere}.tag{color:var(--acc);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
</style></head><body>
<h1>Scarwright receipt</h1>
<div class="mut">History up to <code>{{ r.head[:8] }}</code> · {{ r.commits_scanned }} commits read</div>
<div class="stats">
<div><b class="num">{{ r.proven }}</b>proven decisions</div>
<div><b class="num">{{ r.broken }}</b>times one was broken again{% if r.open %} ({{ r.open|length }} still present){% endif %}</div>
<div><b class="num">{{ r.agent_events }}</b>of those in commits carrying an AI agent trailer</div>
<div><b class="num">{{ r.median_days if r.median_days is not none else "–" }}</b>median days before it was fixed</div>
</div>
{% if r.open %}<h2>Broken again and still present</h2>
{% for e in r.open %}<div class="row"><span class="tag">open {{ e.days }} days</span><div>{{ e.statement }}</div>
<div class="mut">Brought back by <code>{{ e.commit[:8] }}</code> on {{ e.date[:10] }} ({{ e.author }}{% if e.agent %}, {{ e.agent }} trailer{% endif %}): {{ e.subject }}</div></div>{% endfor %}{% endif %}
<h2>Broken again, then fixed</h2>
{% for e in r.events %}<div class="row"><div>{{ e.statement }}</div>
<div class="mut">Decided in <code>{{ e.decided[:8] }}</code>. Brought back by <code>{{ e.commit[:8] }}</code> on {{ e.date[:10] }} ({{ e.author }}{% if e.agent %}, {{ e.agent }} trailer{% endif %}): {{ e.subject }}</div>
<div class="mut">Fixed in <code>{{ e.corrected_by[:8] }}</code> after {{ e.days }} days{% if e.how == "file removed" %} (the file was removed){% endif %}.</div></div>
{% else %}<div class="mut">None found in this history.</div>{% endfor %}
<h2>What this report cannot see</h2>
<ul class="mut">{% for line in r.limits %}<li>{{ line }}</li>{% endfor %}</ul>
</body></html>
"""


def build(result: ScanResult) -> dict:
    events, open_events = [], []
    for d in result.tier("proven"):
        for r in d.backtest.reintroductions:
            events.append({
                "decision": d.id, "statement": d.statement, "decided": d.commit, "commit": r.commit, "date": r.date, "author": r.author,
                "agent": r.agent, "subject": r.subject, "corrected_by": r.corrected_by or "", "days": r.survived_days, "how": r.how,
            })
        if d.backtest.open:
            r = d.backtest.open
            open_events.append({
                "decision": d.id, "statement": d.statement, "decided": d.commit, "commit": r.commit, "date": r.date, "author": r.author,
                "agent": r.agent, "subject": r.subject, "days": r.survived_days, "paths": r.paths,
            })
    events.sort(key=lambda e: e["date"])
    days = [e["days"] for e in events if isinstance(e["days"], (int, float))]
    limits = [
        "Only three kinds of decision are derived so far: removed dependencies, replaced imports, and reverted changes.",
        "An AI agent is recognised only by the address it signs commit trailers with (a known list); "
        "a commit without one may still have been written by an agent.",
        "Import analysis covers Python and JavaScript/TypeScript only, and only imports written as plain text: a module name "
        "built at run time, or a path alias from a bundler config, cannot be followed.",
        "Days are counted from when a change reached the main line to when the fix did.",
    ]
    if result.shallow:
        limits.append("This clone is shallow, so older history was not available.")
    if result.unchecked_languages:
        limits.append("Code in these languages was not analysed: " + ", ".join(result.unchecked_languages) + ".")
    if result.unreadable_sources:
        limits.append(f"{len(result.unreadable_sources)} source file(s) were too large or not readable as text; "
                      "each was treated as if it might still use anything.")
    if result.unparsed_manifests:
        limits.append(f"{len(result.unparsed_manifests)} manifest file(s) could not be read at some point in history and were skipped there.")
    if result.skipped_commits:
        limits.append(f"{len(result.skipped_commits)} very large commit(s) were skipped rather than analysed.")
    if result.errors:
        limits.append(f"{len(result.errors)} commit(s) could not be analysed and were skipped.")
    return {
        "head": result.head, "commits_scanned": result.commits_scanned, "proven": len(result.tier("proven")),
        "candidates": len(result.tier("candidate")), "reversed": len(result.tier("reversed")),
        "events": events, "open": open_events, "broken": len(events) + len(open_events),
        "agent_events": sum(1 for e in events + open_events if e["agent"]),
        "median_days": round(statistics.median(days), 1) if days else None, "limits": limits,
    }


def to_html(report: dict) -> str:
    env = Environment(autoescape=True)
    return env.from_string(_HTML).render(r=report)


def to_markdown(report: dict) -> str:
    def esc(text: object) -> str:
        text = " ".join(str(text).split()).replace("`", "'").replace("<", "&lt;").replace(">", "&gt;")
        return re.sub(r"([\\\[\]()!*_#|~])", r"\\\1", text)

    lines = [
        "# Scarwright receipt", "",
        f"History up to `{report['head'][:8]}`, {report['commits_scanned']} commits read.", "",
        f"- Proven decisions: {report['proven']}",
        f"- Times one was broken again: {report['broken']}" + (f" ({len(report['open'])} still present)" if report["open"] else ""),
        f"- Of those, in commits carrying an AI agent trailer: {report['agent_events']}",
        f"- Median days before it was fixed: {report['median_days'] if report['median_days'] is not None else 'n/a'}", "",
    ]
    if report["open"]:
        lines += ["## Broken again and still present", ""]
        for e in report["open"]:
            lines.append(f"- {esc(e['statement'])} Brought back by `{e['commit'][:8]}` on {e['date'][:10]}, open {e['days']} days.")
        lines.append("")
    lines += ["## Broken again, then fixed", ""]
    if not report["events"]:
        lines.append("None found in this history.")
    for e in report["events"]:
        agent = f", {esc(e['agent'])} trailer" if e["agent"] else ""
        lines.append(
            f"- {esc(e['statement'])} Brought back by `{e['commit'][:8]}` on {e['date'][:10]} ({esc(e['author'])}{agent}); "
            f"fixed in `{e['corrected_by'][:8]}` after {e['days']} days."
        )
    lines += ["", "## What this report cannot see", ""] + [f"- {l}" for l in report["limits"]]
    return "\n".join(lines) + "\n"
