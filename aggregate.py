"""Aggregation — per-field verdict from the independent check results
(2026-09-02, verification stack).

Semantics (owner spec, tuned against the golden eval set — nothing final):
  - applicable checks are weighted by `check_weights` (missing checks drop
    out; the weights re-normalize over what actually ran).
  - HARD FAIL: any applicable check in `hard_fail_checks` that did not pass
    forces review_human — arithmetic (a failed identity implicates ALL rule
    fields; never pick a culprit) and disagreement (a second model actively
    disagrees). Grounding/format failures merely pull the score down.
  - verdict: score >= accept -> accept; >= review -> review_secondary
    (Phase 3: triggers the gated recheck/Check 4); else review_human.

Check-result shape: {check: str, passed: bool, score: float, detail: str}.
Verdict shape: {field, score, level, flagged, checks, hard_fail}.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_WEIGHTS = {
    "grounding": 0.25,
    "arithmetic": 0.25,
    "format": 0.15,
    "disagreement": 0.35,
}
DEFAULT_HARD_FAIL = ("arithmetic", "disagreement")
DEFAULT_ACCEPT = 0.8
DEFAULT_REVIEW = 0.5


@dataclass
class FieldVerdict:
    field: str
    score: float
    level: str                    # accept | review_secondary | review_human
    checks: list[dict] = field(default_factory=list)
    hard_fail: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return self.level != "accept"


def aggregate(field: str, results: list[dict], *,
              weights: dict | None = None,
              hard_fail: tuple[str, ...] | None = None,
              accept: float = DEFAULT_ACCEPT,
              review: float = DEFAULT_REVIEW) -> FieldVerdict:
    weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
    hard_fail = DEFAULT_HARD_FAIL if hard_fail is None else hard_fail

    applied = [r for r in results if r.get("check") in weights]
    if not applied:
        # No check ran (e.g. region omitted and no other applicable check):
        # nothing to contradict the primary — pass silently.
        return FieldVerdict(field, 1.0, "accept", list(results))

    total_weight = sum(weights[r["check"]] for r in applied)
    score = sum(r.get("score", 0.0) * weights[r["check"]] for r in applied) / total_weight
    hard = [r["check"] for r in applied
            if r["check"] in hard_fail and not r.get("passed", False)]

    if hard or score < review:
        level = "review_human"
    elif score < accept:
        level = "review_secondary"
    else:
        level = "accept"
    return FieldVerdict(field, round(score, 4), level, list(results), hard)