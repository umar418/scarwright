# Scarwright

**Stop AI coding agents from undoing decisions your team already made.**

Scarwright is an open-source command-line tool that reads a git repository's history, finds the decisions that were made in it, turns each one into a check, and proves that check against the same history before it is allowed to flag anything.

[![CI](https://github.com/umar418/scarwright/actions/workflows/ci.yml/badge.svg)](https://github.com/umar418/scarwright/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

AI coding agents such as Claude Code, Cursor, GitHub Copilot, Codex CLI, Gemini CLI, Aider and Devin start every session with no memory of what was already decided. They bring back a dependency you removed, re-import a module you migrated away from, or re-apply a change you reverted. Scarwright catches those, and every flag cites the commit where the decision was made.

- **No configuration and no rules to write.** The rules come from your own git history.
- **No AI model.** Every check is deterministic and gives the same answer each time.
- **Runs on your machine.** Nothing is uploaded and there is no account.
- **Prove it or stay silent.** A rule that cannot pass its own backtest is never enforced.

**Status: early (version 0.0.1).** This release covers three kinds of decision and two language families. Read [What it cannot see](#what-it-cannot-see) before relying on it.

## Contents

- [The problem](#the-problem)
- [Install](#install)
- [Quick start](#quick-start)
- [What it works with](#what-it-works-with)
- [What counts as a decision](#what-counts-as-a-decision)
- [How a decision is proven](#how-a-decision-is-proven)
- [Commands](#commands)
- [Use it with AI coding agents](#use-it-with-ai-coding-agents)
- [Use it as a pre-commit hook](#use-it-as-a-pre-commit-hook)
- [Use it in CI](#use-it-in-ci)
- [The decision ledger](#the-decision-ledger)
- [What it cannot see](#what-it-cannot-see)
- [Safety](#safety)
- [How it was tested](#how-it-was-tested)
- [How it differs from other tools](#how-it-differs-from-other-tools)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [License](#license)

## The problem

A codebase carries hundreds of decisions that are written down nowhere: "we dropped that library", "nothing in this folder imports that module any more", "we tried that change and backed it out". People on the team remember them. An AI coding agent does not. It sees the code as it is today, not the reasons behind it.

This is sometimes called architecture drift, context loss or agent amnesia. The usual answers are a rules file (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules`) or hand-written architecture decision records (ADRs). Both depend on someone writing the rule down and keeping it current, and an agent can still ignore a text file.

Scarwright takes a different route. The decisions are already recorded in git, in the commits where they were made. Scarwright finds them there, checks that the codebase really kept to each one afterwards, and only then uses it to flag a new change.

## Install

```bash
pip install git+https://github.com/umar418/scarwright
```

Or, to keep it separate from your project's packages:

```bash
pipx install git+https://github.com/umar418/scarwright
```

Requires Python 3.10 or newer and git. Scarwright is not on PyPI yet.

## Quick start

Run it inside any git repository:

```bash
scarwright scan
```

This is real output from scanning the public [httpx](https://github.com/encode/httpx) repository:

```text
Read 1485 commits. 6 proven, 41 not yet provable, 5 later reversed, 5 about code that no longer exists.
  ✓ `charset-normalizer` was removed from requirements.txt and 1 file(s) stopped importing it. (1c33a285, 2022-05-23)
  ✓ `rfc3986` was removed from pyproject.toml and 1 file(s) stopped importing it. (57daabf6, 2023-01-10)
  ✓ The change “Raise `TypeError` on invalid query params. (#2523)” (4cbf13ec) was reverted. (bddd774c, 2023-01-04)
  ...
Ledger: 6 new, 0 updated in .scarwright/decisions/
```

Now suppose a later change, written by a person or by an agent, brings `rfc3986` back:

```bash
scarwright check --worktree
```

```text
WARN httpx/_urlparse.py:529 imports `rfc3986`
      `rfc3986` was removed from pyproject.toml and 1 file(s) stopped importing it.
      evidence: 57daabf6 2023-01-10 “Drop `rfc3986` requirement. (#2252)” · dep-pip-rfc3986-57daabf6
WARN pyproject.toml:31 adds dependency `rfc3986`
      `rfc3986` was removed from pyproject.toml and 1 file(s) stopped importing it.
      evidence: 57daabf6 2023-01-10 “Drop `rfc3986` requirement. (#2252)” · dep-pip-rfc3986-57daabf6
```

Each warning names the file and line, states the decision, and cites the commit that made it.

## What it works with

### AI coding agents

Scarwright checks the change in git, not the tool that wrote it. So it works alongside any AI coding agent or assistant that edits files in a git repository, including:

- Claude Code
- Cursor
- GitHub Copilot (editor and coding agent)
- OpenAI Codex CLI
- Gemini CLI
- Windsurf
- Aider
- Devin
- Jules
- Amp
- OpenHands
- Cline, Roo Code, Continue and other editor agents

It works just as well on changes written by people.

There are no agent-specific plugins yet. Today you connect Scarwright to an agent through a git hook, a CI step, or a line in the agent's instructions file. See [Use it with AI coding agents](#use-it-with-ai-coding-agents).

The `receipt` report also labels which broken decisions came from commits signed by an agent. It recognises the commit trailers of Claude, Cursor, Copilot, Devin, Jules, Aider, Amp and OpenHands.

### Languages

| Language | Import analysis | File types |
|---|---|---|
| Python | Yes | `.py`, `.pyi`, `.pyx` |
| JavaScript and TypeScript | Yes | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`, `.mts`, `.cts` |
| Vue, Svelte, Astro, MDX, HTML | Yes, for the script parts | `.vue`, `.svelte`, `.astro`, `.mdx`, `.html`, `.htm` |
| Everything else | No | Reported as not analysed |

Reverted changes are tracked in any text file, whatever the language.

### Package managers

| Ecosystem | Files read |
|---|---|
| Python (pip, Poetry, PDM, uv, Hatch) | `pyproject.toml`, `requirements*.txt`, `requirements*.in`, files in a `requirements/` folder |
| JavaScript (npm, pnpm, Yarn, Bun) | `package.json` |
| Rust (Cargo) | `Cargo.toml` |
| Go | `go.mod` |

Removals from `Cargo.toml` and `go.mod` are found but are rarely proven, because proof needs import evidence and Rust and Go imports are not analysed yet.

### Systems

- **Operating systems:** Linux, macOS and Windows. The test suite runs on all three on every push.
- **Python:** 3.10 and newer. Tested on 3.10 and 3.13.
- **Git hosting:** any. Scarwright reads the local repository only, so GitHub, GitLab, Bitbucket and self-hosted git all work the same.
- **Repository size:** a repository with about 9,900 commits (Vite) scanned in about four and a half minutes on a desktop machine.

## What counts as a decision

| Kind | Found when | Proven when |
|---|---|---|
| Removed dependency | A package leaves a manifest file | Code stopped importing it in the same commit, nothing readable still imports it, and it stayed out through later changes; or it came back briefly and was removed again |
| Replaced import | Three or more files stop importing a module in one commit, leaving none in that folder | The same lines that used the old name were rewritten to use a new one, and it held through later changes |
| Reverted change | A commit explicitly reverts another | Most of the original change is really gone, and it stayed out through later changes to those files |

## How a decision is proven

Every candidate goes through the same backtest:

1. The check must fire on the code as it was before the decision.
2. The check must be silent on the code right after the decision.
3. The check is replayed over every later commit on the main line. Each time the old pattern returned, Scarwright records whether it was corrected or kept.
4. Only a decision that held is marked **proven**. Everything else is listed as "not yet provable" with the reason, and is never enforced.

A few rules that follow from this:

- Something brought back and kept for more than 90 days is treated as a change of mind, not a violation.
- A decision about code that no longer exists is retired.
- Time is counted from when a change reached the main line, not from when it was written on a branch.
- A file Scarwright cannot read (too large, or not text) is assumed to still use everything. It can block a decision but can never create a false one.

## Commands

| Command | What it does |
|---|---|
| `scarwright scan` | Find decisions in history, backtest each one and write the proven ones to the ledger |
| `scarwright scan --no-write` | The same, without writing anything |
| `scarwright scan --json` | Print the results as JSON |
| `scarwright check` | Check staged changes against the proven decisions |
| `scarwright check --worktree` | Check the files on disk instead of what is staged |
| `scarwright check --base main` | Check everything committed since a branch, tag or commit |
| `scarwright check --json` | Print the findings as JSON |
| `scarwright why src/app.py` | Show the decisions that cover a file, with their evidence. Also accepts `path:line` |
| `scarwright receipt` | Show every time a proven decision was broken again, and for how long |
| `scarwright receipt --format html -o receipt.html` | Write the receipt as `text`, `md`, `html` or `json` |
| `scarwright decisions list` | List the decisions in the ledger |
| `scarwright decisions list --all` | Also list the findings that are not proven, with the reason for each |
| `scarwright decisions show <id>` | Show one decision with its evidence and backtest numbers |
| `scarwright decisions enforce <id> block` | Set a decision to `warn`, `block` or `off` |
| `scarwright decisions reject <id>` | Mark a decision as wrong so it is never enforced. The file stays as a record |

`check` warns by default and exits 0. It exits 1 only when a decision you have set to `block` is broken.

## Use it with AI coding agents

There are three ways to put Scarwright in an agent's path. They can be combined.

**1. Tell the agent to run it.** Add a line to the instructions file your agent reads (`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex and others, `.cursor/rules` for Cursor, `.github/copilot-instructions.md` for Copilot):

```markdown
Before you commit, run `scarwright check --worktree` and fix anything it reports.
Before you change a file you have not touched before, run `scarwright why <path>`.
```

This is advice to the agent, not enforcement. An agent can skip it.

**2. Enforce it with a git hook.** A pre-commit hook runs whichever tool makes the commit. See the next section.

**3. Enforce it in CI.** This covers agents that open pull requests on their own, such as the Copilot coding agent, Devin and Jules. See [Use it in CI](#use-it-in-ci).

## Use it as a pre-commit hook

Create `.git/hooks/pre-commit` with this content and make it executable:

```bash
#!/bin/sh
exec scarwright check
```

```bash
chmod +x .git/hooks/pre-commit
```

Warnings are printed and the commit goes through. A commit is stopped only when it breaks a decision you have set to `block`.

## Use it in CI

Commit the `.scarwright/decisions/` folder, then add a step that checks the pull request against its base branch. For GitHub Actions:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- uses: actions/setup-python@v5
  with:
    python-version: "3.13"
- run: pip install git+https://github.com/umar418/scarwright
- run: scarwright check --base origin/${{ github.base_ref }}
```

`fetch-depth: 0` matters: the check needs the base branch's history to compare against. The same `scarwright check --base <branch>` command works in GitLab CI and any other CI system.

## The decision ledger

`scan` writes one small file per proven decision to `.scarwright/decisions/`. Commit that folder so the whole team, and every agent working in the repository, shares the same decisions.

A ledger file looks like this (shortened):

```yaml
id: dep-pip-rfc3986-57daabf6
family: dependency
tier: proven
status: active
enforcement: warn
statement: '`rfc3986` was removed from pyproject.toml and 1 file(s) stopped importing it.'
decided:
  commit: 57daabf673705954afa94686c0002801c93d31f3
  date: '2023-01-10'
  author: Tom Christie
  subject: Drop `rfc3986` requirement. (#2252)
```

The files are plain text and meant to be reviewed like code. If a decision is wrong for your project, reject it with `scarwright decisions reject <id>`. Scarwright also writes a cache to `.scarwright/cache/`, which does not need to be committed.

## What it cannot see

- **Only three kinds of decision.** Conventions, architecture rules and anything you only ever told an agent in conversation are not derived yet.
- **Python and JavaScript/TypeScript only** for import analysis. Other languages are reported as not analysed.
- **Young repositories have little to find.** Proof needs history after the decision.
- **Imports built at run time** (a module name assembled from strings) cannot be followed. Bundler path aliases are matched by path suffix, which is a guess.
- **Some returns are missed:** a dependency re-added through a git URL fork or in a different workspace's manifest, and a reverted change re-applied with different spacing.
- **A removal is skipped** if the package name still appears anywhere else in the manifest.
- **AI agent attribution** in the receipt recognises a known list of agent commit trailers. A commit without one may still have been written by an agent.
- **Squash merges** hide the individual commits a decision was made in.

## Safety

The repository being scanned is treated as untrusted, including anything already committed under `.scarwright/`:

- Scarwright never writes through a symbolic link and never outside the repository.
- Programs named in the repository's own git config (filters, diff drivers, signature checks) are switched off for Scarwright's git commands.
- Commit messages, author names, file names and package names are stripped of terminal control characters before they are printed or stored.
- Likely credentials are masked in all output. Reverted changes are stored as line fingerprints, not as code. Masking is best-effort: very short or truncated tokens can get through.

If you received a repository as an archive rather than cloning it, its `.git` folder is also untrusted. Scarwright hardens against the cases it knows, but cloning from a remote you trust is safer.

To report a security problem, open a private security advisory on the GitHub repository.

## How it was tested

Before the first release the tool was attacked by independent testers with about 190 hostile cases: false claims, leaks, injection, hangs and writes outside the repository. Every defect found is now a regression test. The test suite's own strength was checked by deliberately breaking the logic and confirming the tests notice.

It was also run on public repositories. These are the counts of proven decisions from those runs; the results were reviewed by the author, not by an independent party:

| Repository | Proven decisions |
|---|---|
| [encode/httpx](https://github.com/encode/httpx) | 6 |
| [sindresorhus/got](https://github.com/sindresorhus/got) | 52 |
| [pallets/flask](https://github.com/pallets/flask) | 3 |
| [vitejs/vite](https://github.com/vitejs/vite) | about 138 |

## How it differs from other tools

| Approach | What it does | What Scarwright adds |
|---|---|---|
| Rules files (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules`) | Text guidance you write for the agent | Rules derived from history, checked by a program rather than left to the agent to follow |
| Architecture decision records (ADRs) | Decisions written up by hand | Decisions found automatically, each with a working check |
| Linters and import-boundary rules | Enforce rules you configure | No configuration: the rules come from what the repository actually did |
| Drift and pattern detectors | Learn the code's current patterns and flag deviations | Knows what was decided, not only what is common, and proves each rule on past commits |
| Agent memory tools | Carry notes from one session to the next | Deterministic checks that cite commits, with no model involved |

Scarwright is not a replacement for any of these. It covers the decisions that are provable from git history and nothing else.

## FAQ

**Does Scarwright use an AI model or send my code anywhere?**
No. It runs git commands and parses files on your machine. There are no network calls and no model.

**Does it work with Claude Code, Cursor or Copilot?**
Yes, in the sense that it checks whatever they change in git. There is no dedicated plugin for any agent yet. Use the instructions-file line, the pre-commit hook or the CI step described above.

**Will it block my commits?**
Not unless you ask it to. Every decision starts at `warn`. You choose which ones to set to `block`.

**It found nothing in my repository. Is something wrong?**
Probably not. A young repository, or one that has never removed a dependency, migrated an import across several files or reverted a commit, has nothing for this version to prove. After a scan, run `scarwright decisions list --all` to see what was considered and why each candidate was not provable.

**A decision it found is wrong. What do I do?**
Run `scarwright decisions reject <id>`. It is switched off and stays in the ledger as a record. Please also open an issue: a false finding is treated as a more serious bug than a missed one.

**Does it change my repository?**
`scan` writes to `.scarwright/` only. `scan --no-write`, `check`, `why` and `receipt` write nothing unless you pass `-o`.

**How is this different from a linter?**
A linter enforces rules that someone configured. Scarwright has no rules of its own. Each check exists because your history shows the decision was made and kept.

## Roadmap

Planned, and not built yet:

- More kinds of decision: code pattern migrations, guards added by bug fixes, weakened regression tests.
- Decisions from agent session logs and pull request review comments, which never reach git.
- Rules files (`CLAUDE.md`, `AGENTS.md`, ADRs) compiled into checks.
- A Claude Code plugin that checks an edit before it is written, and integrations for other agents.
- A GitHub Action with inline annotations.
- More languages.

## Contributing

Bug reports are welcome, especially false findings: include the repository (if public), the decision id and why it is wrong. To run the tests:

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

## License

MIT
