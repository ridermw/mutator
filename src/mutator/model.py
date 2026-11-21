"""Mutation sites and the per-function counts uml-viewer reads."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Site:
    """One replacement inside a function crapper would score."""

    file: str
    namespace: str
    form_id: str
    name: str
    line: int
    start: int
    end: int
    original: str
    mutant: str
    category: str
    mutation_id: str
    # The line its statement starts on; coverage on that line counts for the site.
    anchor_line: int = 0
    # Distinguishes same-name overloads in the same file/namespace (even on one line).
    form_line: int = 0
    form_ordinal: int = 0

    @property
    def description(self) -> str:
        if self.mutant == "":
            return f"delete {self.original}"
        return f"{self.original} -> {self.mutant}"


@dataclass
class FormResult:
    """Killed, survived, uncovered, invalid, and error totals for one operation."""

    id: str
    namespace: str
    name: str
    private: bool
    file: str
    line: int
    end_line: int
    digest: str
    killed: int = 0
    survived: int = 0
    uncovered: int = 0
    sites: int = 0
    invalid: int = 0
    errors: int = 0
    incomplete: bool = False
    ordinal: int | None = None

    @property
    def score(self) -> float | None:
        executed = self.killed + self.survived
        if executed == 0:
            return None
        return self.killed / executed


@dataclass
class PriorForm:
    namespace: str
    id: str
    digest: str
    file: str
    line: int = 0
    ordinal: int | None = None


@dataclass
class History:
    """Previous results for one source file, used for differential runs."""

    forms: dict[tuple[str, str], PriorForm] = field(default_factory=dict)
    declaration_forms: dict[tuple[str, str, int], PriorForm] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)
    classifier_versions: dict[str, int] = field(default_factory=dict)


@dataclass
class RunResult:
    path: str
    forms: list[FormResult]
    written: list[str]
    sites: list[Site] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    baseline_failed: bool = False
    baseline_message: str = ""
    incomplete: bool = False
    skipped: str = ""
