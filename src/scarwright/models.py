from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Settings:
    """Thresholds for what counts as proof. Stricter is quieter."""
    min_migrated_files: int = 3      # files that must drop an import together for it to be a migration
    min_exposure_code: int = 5       # later main-line commits touching the scope with the rule still holding
    min_exposure_manifest: int = 3   # same, for changes to one manifest file
    reversal_days: int = 90          # something brought back for longer than this was a change of mind, not drift
    min_signature_lines: int = 3     # distinctive lines needed to fingerprint a reverted change
    signature_match: float = 0.6     # share of those lines that must reappear
    max_files_per_commit: int = 3000  # a commit touching more files than this is skipped and reported
    max_candidates_per_commit: int = 60  # import changes considered per commit; beyond this the commit is a mass rewrite


@dataclass
class Reintroduction:
    commit: str
    date: str
    author: str
    agent: str | None
    subject: str
    paths: list[str]
    corrected_by: str | None = None
    corrected_date: str | None = None
    survived_days: float | None = None
    how: str | None = None  # "changed" | "file removed"


@dataclass
class Backtest:
    fires_before: bool = False
    silent_after: bool = False
    landed: str | None = None
    silent_at_landing: bool = False
    exposure: int = 0
    exposure_before_open: int = 0
    reintroductions: list[Reintroduction] = field(default_factory=list)
    open: Reintroduction | None = None
    reversed_by: Reintroduction | None = None
    head: str = ""


@dataclass
class Decision:
    id: str
    family: str
    statement: str
    commit: str
    date: str
    author: str
    subject: str
    detector: dict
    basis: list[str] = field(default_factory=list)
    intent: dict = field(default_factory=dict)
    tier: str = "candidate"          # proven | candidate | reversed | rejected
    reason: str = ""
    status: str = "active"           # active | rejected | retired | superseded  (user-controlled)
    enforcement: str = "warn"        # warn | block | off                       (user-controlled)
    backtest: Backtest = field(default_factory=Backtest)

    def to_ledger(self) -> dict:
        return {
            "id": self.id,
            "family": self.family,
            "tier": self.tier,
            "status": self.status,
            "enforcement": self.enforcement,
            "statement": self.statement,
            "decided": {"commit": self.commit, "date": self.date[:10], "author": self.author, "subject": self.subject},
            "basis": self.basis,
            "detector": self.detector,
        }

    @classmethod
    def from_ledger(cls, d: dict) -> "Decision":
        decided = d.get("decided") or {}
        return cls(
            id=str(d["id"]), family=str(d.get("family", "")), statement=str(d.get("statement", "")),
            commit=str(decided.get("commit", "")), date=str(decided.get("date", "")), author=str(decided.get("author", "")),
            subject=str(decided.get("subject", "")), detector=dict(d.get("detector") or {}), basis=list(d.get("basis") or []),
            tier=str(d.get("tier", "candidate")), status=str(d.get("status", "active")), enforcement=str(d.get("enforcement", "warn")),
        )

    @property
    def enforced(self) -> bool:
        return self.tier == "proven" and self.status == "active" and self.enforcement in ("warn", "block")
