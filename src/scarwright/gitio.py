"""Read-only access to a git repository. Nothing here writes to the repo."""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

MAX_BLOB = 2_000_000
_GIT = ["git", "--no-pager", "-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false"]
# One field per line: hashes, dates, author and subject cannot contain a newline, so nothing in them can shift the fields.
_FMT = "%H%n%P%n%aI%n%cI%n%an%n%s%n%b"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")
_PROGRAM_KEY_RE = re.compile(r"^(?:filter\..+\.(?:clean|smudge|process)|diff\..+\.(?:textconv|command))$")
READ_TIMEOUT = 30
_TRAILER_RE = re.compile(r"(?i)^(?:co-authored-by|assisted-by|generated-by)\s*:\s*[^<>]*<([^<>\s]+)>\s*$")
# Identified by the address the agent itself signs with, never by a first name: people are called Claude and Devin too.
_AGENT_ADDRESSES = (
    ("Claude", re.compile(r"(?i)^noreply@anthropic\.com$")),
    ("Cursor", re.compile(r"(?i)^cursoragent@cursor\.com$")),
    ("Copilot", re.compile(r"(?i)^\d+\+copilot@users\.noreply\.github\.com$")),
    ("Devin", re.compile(r"(?i)^\d+\+devin-ai-integration\[bot\]@users\.noreply\.github\.com$")),
    ("Jules", re.compile(r"(?i)^\d+\+google-labs-jules\[bot\]@users\.noreply\.github\.com$")),
    ("Aider", re.compile(r"(?i)^noreply@aider\.chat$")),
    ("Amp", re.compile(r"(?i)^amp@ampcode\.com$")),
    ("OpenHands", re.compile(r"(?i)^openhands@all-hands\.dev$")),
)


def agent_in_trailers(body: str) -> str | None:
    """An AI agent named in the commit's final trailer block. Quoted text earlier in the message does not count."""
    paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    if not paragraphs:
        return None
    for line in paragraphs[-1].splitlines():
        m = _TRAILER_RE.match(line.strip())
        if m:
            for name, rx in _AGENT_ADDRESSES:
                if rx.match(m.group(1)):
                    return name
    return None


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author: str
    date: str
    cdate: str
    subject: str
    body: str

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def agent(self) -> str | None:
        """Agent named in a commit trailer, if any. Absence proves nothing about who wrote the code."""
        return agent_in_trailers(self.body)


@dataclass(frozen=True)
class Change:
    status: str
    path: str
    old_path: str | None = None


def _env() -> dict:
    env = dict(os.environ)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_NO_REPLACE_OBJECTS="1")
    return env


class Repo:
    def __init__(self, path: str | Path = "."):
        self.root = Path(path)
        # Only the trailing newline is git's; a folder name may itself end in a space.
        top = self._raw(["rev-parse", "--show-toplevel"]).decode("utf-8", "replace").rstrip("\r\n")
        self.root = Path(top)
        self._cat: subprocess.Popen | None = None
        self._chk: subprocess.Popen | None = None
        self._oids: dict[tuple[str, str], str | None] = {}
        self._trees: dict[str, dict[str, str]] = {}
        self._commits: dict[str, Commit] = {}
        self._mainline_changes: dict[str, frozenset[str]] | None = None
        self._empty_tree: str | None = None
        self._git = list(_GIT)
        # A repository's own config can name programs for git to run (filters, diff drivers). A repository that
        # arrived as an archive controls that config, so every such entry is switched off for our commands.
        listed = self._raw(["config", "--local", "--name-only", "--list"], ok=(0, 1, 128)).decode("utf-8", "replace")
        for key in sorted(set(listed.split())):
            if _PROGRAM_KEY_RE.match(key) and "\n" not in key:
                self._git += ["-c", f"{key}="]

    # ------------------------------------------------------------ plumbing

    def _raw(self, args: list[str], ok: tuple[int, ...] = (0,), stdin: bytes | None = None) -> bytes:
        if not self.root.is_dir():
            raise GitError(f"folder does not exist: {self.root}")
        try:
            p = subprocess.run(getattr(self, '_git', _GIT) + args, cwd=self.root, capture_output=True, input=stdin, timeout=900, env=_env())
        except FileNotFoundError as e:
            raise GitError("git is not installed or not on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {args[0]} timed out") from e
        if p.returncode not in ok:
            raise GitError(p.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
        return p.stdout

    def _text(self, args: list[str], ok: tuple[int, ...] = (0,)) -> str:
        return self._raw(args, ok).decode("utf-8", "replace")

    def close(self) -> None:
        for proc in (self._cat, self._chk):
            if proc is not None:
                try:
                    proc.stdin.close()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        self._cat = self._chk = None

    # ------------------------------------------------------------ commits

    def forget_history(self) -> None:
        """Drop anything cached about where HEAD is, so a reused Repo sees new commits."""
        self._mainline_changes = None

    def head(self) -> str:
        return self._text(["rev-parse", "--verify", "HEAD^{commit}"]).strip()

    def is_shallow(self) -> bool:
        return self._text(["rev-parse", "--is-shallow-repository"]).strip() == "true"

    def resolve(self, rev: str) -> str | None:
        out = self._raw(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], ok=(0, 1)).decode().strip()
        return out or None

    def _log(self, args: list[str]) -> list[Commit]:
        out = self._raw(["log", "-z", f"--format={_FMT}"] + args)
        commits = []
        for rec in out.split(b"\0"):
            fields = rec.decode("utf-8", "replace").lstrip("\n").split("\n", 6)
            if len(fields) < 6 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
                continue
            fields += [""] * (7 - len(fields))
            adate, cdate = (d if _DATE_RE.match(d) else "" for d in fields[2:4])
            c = Commit(fields[0], tuple(fields[1].split()), fields[4], adate, cdate, fields[5], fields[6])
            self._commits[c.sha] = c
            commits.append(c)
        return commits

    def history(self, rev: str = "HEAD") -> list[Commit]:
        """Every commit reachable from rev, oldest first."""
        return self._log(["--topo-order", "--reverse", rev])

    def commit(self, sha: str) -> Commit:
        if sha not in self._commits:
            found = self._log(["-1", sha])
            if not found:
                raise GitError(f"unknown commit {sha}")
            self._commits[sha] = found[0]
        return self._commits[sha]

    def mainline(self) -> list[str]:
        """First-parent chain of HEAD, oldest first."""
        return self._text(["rev-list", "--first-parent", "--reverse", "HEAD"]).split()

    def rev_list(self, args: list[str]) -> list[str]:
        return self._text(["rev-list"] + args).split()

    def is_ancestor(self, a: str, b: str) -> bool:
        p = subprocess.run(getattr(self, '_git', _GIT) + ["merge-base", "--is-ancestor", a, b], cwd=self.root, capture_output=True, env=_env())
        return p.returncode == 0

    def empty_tree(self) -> str:
        if self._empty_tree is None:
            self._empty_tree = self._raw(["hash-object", "-t", "tree", "--stdin"], stdin=b"").decode().strip()
        return self._empty_tree

    def parent_of(self, sha: str) -> str:
        """First parent, or the empty tree for a root commit."""
        parents = self.commit(sha).parents
        return parents[0] if parents else self.empty_tree()

    # ------------------------------------------------------------ changes

    def changes(self, sha: str) -> list[Change]:
        """Files changed by a commit relative to its first parent, with renames detected."""
        base = self.parent_of(sha)
        out = self._raw(["diff", "--no-ext-diff", "--no-textconv", "-M", "--name-status", "-z", base, sha])
        parts = out.decode("utf-8", "replace").split("\0")
        changes, i = [], 0
        while i < len(parts) and parts[i]:
            status = parts[i]
            if status[0] in "RC" and i + 2 < len(parts):
                changes.append(Change(status[0], parts[i + 2], parts[i + 1]))
                i += 3
            elif i + 1 < len(parts):
                changes.append(Change(status[0], parts[i + 1]))
                i += 2
            else:
                break
        return changes

    def mainline_changes(self) -> dict[str, frozenset[str]]:
        """For each first-parent commit, the paths it changed relative to its first parent."""
        if self._mainline_changes is None:
            out = self._raw(["log", "--first-parent", "-m", "--no-renames", "--name-only", "-z", "--format=%x01%H%x02", "HEAD"])
            result: dict[str, frozenset[str]] = {}
            for chunk in out.decode("utf-8", "replace").split("\x01"):
                if "\x02" not in chunk:
                    continue
                sha, rest = chunk.split("\x02", 1)
                result[sha.strip()] = frozenset(p.strip("\n") for p in rest.split("\0") if p.strip("\n"))
            self._mainline_changes = result
        return self._mainline_changes

    def diff_lines(self, base: str, sha: str, path: str) -> tuple[list[str], list[str]]:
        """(added, removed) lines for one path between two trees."""
        out = self._raw(["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "-U0", base, sha, "--", path])
        added, removed = [], []
        for line in out.decode("utf-8", "replace").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                removed.append(line[1:])
        return added, removed

    def diff_hunks(self, old_oid: str, new_oid: str) -> list[tuple[list[str], list[str]]]:
        """(removed, added) lines per hunk between two blobs, with nearby edits grouped into one hunk."""
        out = self._raw(["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-U3", old_oid, new_oid], ok=(0, 1))
        hunks: list[tuple[list[str], list[str]]] = []
        for line in out.decode("utf-8", "replace").splitlines():
            if line.startswith("@@"):
                hunks.append(([], []))
            elif hunks and line.startswith("-") and not line.startswith("---"):
                hunks[-1][0].append(line[1:])
            elif hunks and line.startswith("+") and not line.startswith("+++"):
                hunks[-1][1].append(line[1:])
        return hunks

    def _renamed(self, args: list[str]) -> list[tuple[str, str]]:
        """(path, path it was before) for added, changed and renamed files."""
        parts = self._raw(["diff", "-M", "--name-status", "-z", "--diff-filter=ACMR"] + args).decode("utf-8", "replace").split("\0")
        out, i = [], 0
        while i < len(parts) and parts[i]:
            if parts[i][0] in "RC" and i + 2 < len(parts):
                out.append((parts[i + 2], parts[i + 1] if parts[i][0] == "R" else parts[i + 2]))
                i += 3
            elif i + 1 < len(parts):
                out.append((parts[i + 1], parts[i + 1]))
                i += 2
            else:
                break
        return out

    # ------------------------------------------------------------ content

    def tree(self, sha: str) -> dict[str, str]:
        """path -> blob id for every file in a commit's tree."""
        if sha not in self._trees:
            if len(self._trees) > 48:
                self._trees.pop(next(iter(self._trees)))
            out = self._raw(["ls-tree", "-r", "-z", sha])
            files = {}
            for rec in out.split(b"\0"):
                meta, _, path = rec.partition(b"\t")
                bits = meta.split()
                if len(bits) == 3 and bits[1] == b"blob":
                    files[path.decode("utf-8", "replace")] = bits[2].decode()
            self._trees[sha] = files
        return self._trees[sha]

    def _guarded(self, proc: subprocess.Popen, read):
        """Run one read against a helper process, killing it if git stalls (a corrupt object can do that)."""
        timer = threading.Timer(READ_TIMEOUT, proc.kill)
        timer.start()
        try:
            return read()
        finally:
            timer.cancel()

    def _check(self, spec: str) -> tuple[str, bytes, int] | None:
        """(object id, type, size) for a revision spec, without reading the object."""
        if "\n" in spec or "\0" in spec:
            return None
        if self._chk is None or self._chk.poll() is not None:
            self._chk = subprocess.Popen(
                self._git + ["cat-file", "--batch-check"], cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=_env(),
            )
        chk = self._chk
        try:
            chk.stdin.write(spec.encode("utf-8", "surrogateescape") + b"\n")
            chk.stdin.flush()
            header = self._guarded(chk, chk.stdout.readline).split()
        except (OSError, ValueError):
            return None
        if len(header) != 3 or not header[2].isdigit():
            return None
        return header[0].decode("ascii", "replace"), header[1], int(header[2])

    def blob(self, oid: str) -> bytes | None:
        """Blob content by id. None if missing, not a blob, larger than MAX_BLOB, or unreadable."""
        if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
            return None
        info = self._check(oid)
        if info is None or info[1] != b"blob" or info[2] > MAX_BLOB:
            return None  # the size is checked before a single byte of content is requested
        if self._cat is None or self._cat.poll() is not None:
            self._cat = subprocess.Popen(
                self._git + ["cat-file", "--batch"], cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=_env(),
            )
        cat = self._cat

        def read():
            header = cat.stdout.readline().split()
            if len(header) != 3 or not header[2].isdigit() or int(header[2]) > MAX_BLOB:
                cat.kill()  # out of step with what was asked for: start a fresh helper next time
                return None
            data = cat.stdout.read(int(header[2]))
            cat.stdout.read(1)
            return data if header[1] == b"blob" and len(data) == int(header[2]) else None

        try:
            cat.stdin.write(oid.encode() + b"\n")
            cat.stdin.flush()
            return self._guarded(cat, read)
        except (OSError, ValueError):
            return None

    def oid_at(self, sha: str, path: str) -> str | None:
        """Blob id of one path in one commit, without listing the whole tree."""
        if sha in self._trees:
            return self._trees[sha].get(path)
        key = (sha, path)
        if key in self._oids:
            return self._oids[key]
        info = self._check(f"{sha}:{path}")
        oid = info[0] if info and info[1] == b"blob" else None
        if len(self._oids) > 400_000:
            self._oids.clear()
        self._oids[key] = oid
        return oid

    def read(self, sha: str, path: str) -> bytes | None:
        oid = self.oid_at(sha, path)
        return self.blob(oid) if oid else None

    def read_index(self, path: str) -> bytes | None:
        p = subprocess.run(getattr(self, '_git', _GIT) + ["show", f":{path}"], cwd=self.root, capture_output=True, env=_env())
        return p.stdout if p.returncode == 0 and len(p.stdout) <= MAX_BLOB else None

    def staged_changes(self) -> list[tuple[str, str]]:
        return self._renamed(["--cached"])

    def worktree_changes(self) -> list[tuple[str, str]]:
        tracked = self._renamed(["HEAD"])
        untracked = self._raw(["ls-files", "--others", "--exclude-standard", "-z"]).decode("utf-8", "replace").split("\0")
        seen = {p for p, _ in tracked}
        return tracked + [(p, p) for p in sorted(set(untracked)) if p and p not in seen]

    def range_changes(self, base: str, tip: str) -> list[tuple[str, str]]:
        return self._renamed([base, tip])

    def blame_commit(self, path: str, line: int) -> str | None:
        out = self._raw(["blame", "--no-textconv", "-L", f"{line},{line}", "--porcelain", "--", path], ok=(0, 128)).decode("utf-8", "replace")
        first = out.split("\n", 1)[0].split()
        return first[0] if first and re.fullmatch(r"[0-9a-f]{40,64}", first[0]) and set(first[0]) != {"0"} else None
