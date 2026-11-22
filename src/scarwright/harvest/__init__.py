from __future__ import annotations

import re
from dataclasses import dataclass

from scarwright.detectors import Index
from scarwright.gitio import Repo
from scarwright.models import Settings


@dataclass
class Ctx:
    repo: Repo
    index: Index
    settings: Settings
    mainline: list[str]
    pos: dict[str, int]


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "x"
