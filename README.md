# Scarwright

Scarwright reads a git repository's history, finds decisions that were made in it, turns each one into a check, and proves that check against the same history before it is allowed to flag anything.

AI coding agents start every session with no memory of what was already decided. They bring back a dependency you removed, re-import a module you migrated away from, or re-apply a change you reverted. Scarwright catches those, and every flag cites the commit where the decision was made.

**Status: early.** This release covers three kinds of decision and two language families. Read [What it cannot see](#what-it-cannot-see) before relying on it.

## Install

```bash
pip install git+https://github.com/umar418/scarwright
```

Requires Python 3.10 or newer and git. Everything runs on your machine; nothing is sent anywhere and no AI model is used.

## Use

```bash
scarwright scan            # find decisions, prove them, write the ledger
scarwright check           # check staged changes against the proven decisions
scarwright check --worktree
scarwright check --base main
scarwright receipt         # every time a proven decision was broken again, and for how long
scarwright why src/app.py  # which decisions cover this file, with evidence
scarwright decisions list --all
```

`scan` writes one small file per proven decision to `.scarwright/decisions/`. Commit that folder so the whole team shares it. Use `scan --no-write` to look without writing.

`check` warns by default and exits 0. To make a decision fail the check:

```bash
scarwright decisions enforce <id> block
```

If a decision is wrong, `scarwright decisions reject <id>` switches it off and keeps the file as a record.

## What counts as a decision

| Kind | Found when | Proven when |
|---|---|---|
| Removed dependency | A package leaves `package.json`, `pyproject.toml`, `requirements*.txt`, `Cargo.toml` or `go.mod` | Code stopped importing it in the same commit and nothing readable still imports it, and it stayed out through later changes; or it came back briefly and was removed again |
| Replaced import | Three or more files stop importing a module in one commit, leaving none in that folder | The same lines that used the old name were rewritten to use a new one, and it held through later changes |
| Reverted change | A commit explicitly reverts another | Most of the original change is really gone, and it stayed out through later changes to those files |

Each decision is replayed over every later commit on the main line. Anything that does not pass is listed as "not yet provable" with the reason, and is never enforced.

A few rules worth knowing:

- Something brought back and kept for more than 90 days is treated as a change of mind, not a violation.
- A decision about code that no longer exists is retired.
- Time is counted from when a change reached the main line, not from when it was written on a branch.
- A file Scarwright cannot read (too large, not text) is assumed to still use everything, so it can block a decision but never create a false one.

## What it cannot see

- **Only three kinds of decision.** Conventions, architecture rules and anything you only ever told an agent in conversation are not derived yet.
- **Python and JavaScript/TypeScript only** for import analysis (including `.vue`, `.svelte`, `.astro`, `.mdx`). Other languages are reported as not analysed.
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

Before the first release the tool was attacked by independent testers with about 190 hostile cases (false claims, leaks, injection, hangs, writes outside the repository). Every defect found is now a regression test. The test suite's own strength was checked by deliberately breaking the logic and confirming the tests notice.

## License

MIT
