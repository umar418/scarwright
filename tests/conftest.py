from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_GIT = ["git", "-c", "user.name=Test Author", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
        "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main"]
_BASE = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


class Sandbox:
    """A throwaway git repository with deterministic, strictly increasing commit dates."""

    def __init__(self, root: Path):
        self.root = root
        self.day = 0
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        when = (_BASE + timedelta(days=self.day)).isoformat()
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        p = subprocess.run(_GIT + list(args), cwd=self.root, capture_output=True, env=env)
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        return p.stdout.decode("utf-8", "replace").strip()

    def write(self, path: str, text: str | bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))

    def remove(self, path: str) -> None:
        (self.root / path).unlink()

    def commit(self, message: str, days: int = 1) -> str:
        self.day += days
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def revert(self, sha: str, days: int = 1) -> str:
        self.day += days
        self.git("revert", "--no-edit", sha)
        return self.git("rev-parse", "HEAD")

    def touch_commits(self, path: str, count: int, label: str = "work") -> None:
        """Unrelated later changes to one file, so a decision has history to be tested against."""
        for i in range(count):
            self.write(path, f"// {label} {i}\n" if not path.endswith(".py") else f"# {label} {i}\n")
            self.commit(f"{label} {i}")


def package_json(deps: dict[str, str]) -> str:
    return json.dumps({"name": "demo", "dependencies": deps}, indent=2) + "\n"


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path / "repo")
