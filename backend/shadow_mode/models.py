"""Shadow mode models (Checkpoint 2.12). Observation / logging only — never orders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.signals.models import MarketSnapshot, SignalDecision, SignalDirection

TRACKED_SYMBOLS = ("TNA", "TZA", "IWM", "SPY", "QQQ")
FOLLOWUP_MAX_QUOTE_AGE_SECONDS = 5.0


class ShadowSafetyError(RuntimeError):
    """Raised if anything tries to mark a shadow record as submitted or unblocked."""


def pct_move(before: Optional[float], after: Optional[float]) -> Optional[float]:
    if before is None or after is None or before <= 0:
        return None
    return round((after - before) / before * 100.0, 6)


def _mid(snapshot: Optional[MarketSnapshot], symbol: str) -> Optional[float]:
    quote = snapshot.get(symbol) if snapshot else None
    return quote.mid if quote else None


def _age(snapshot: Optional[MarketSnapshot], symbol: str) -> Optional[float]:
    quote = snapshot.get(symbol) if snapshot else None
    return quote.quote_age_seconds if quote else None


@dataclass
class FollowupOutcome:
    followup_seconds: float
    mids_after: Dict[str, Optional[float]]
    selected_symbol_move_pct: Optional[float]
    iwm_move_pct: Optional[float]
    direction_was_correct: Optional[bool]
    outcome_note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "followup_seconds": self.followup_seconds,
            **{f"{s.lower()}_mid_after": self.mids_after.get(s) for s in TRACKED_SYMBOLS},
            "selected_symbol_move_pct": self.selected_symbol_move_pct,
            "iwm_move_pct": self.iwm_move_pct,
            "direction_was_correct": self.direction_was_correct,
            "outcome_note": self.outcome_note,
        }


def compute_followup_outcome(
    *,
    decision: str,
    selected_symbol: Optional[str],
    mids_before: Dict[str, Optional[float]],
    after: Optional[MarketSnapshot],
    followup_seconds: float,
    max_after_age_seconds: float = FOLLOWUP_MAX_QUOTE_AGE_SECONDS,
) -> FollowupOutcome:
    """
    Diagnostic direction check (not P&L):
      bullish/TNA correct if TNA rose or IWM rose;
      bearish/TZA correct if TZA rose or IWM fell;
      skip is never marked correct/incorrect (movement recorded only).
    Follow-up quotes older than max_after_age_seconds are not used.
    """
    notes: List[str] = []
    mids_after: Dict[str, Optional[float]] = {}
    for symbol in TRACKED_SYMBOLS:
        mid = _mid(after, symbol)
        age = _age(after, symbol)
        if mid is not None and (age is None or age > max_after_age_seconds):
            notes.append(f"{symbol} follow-up quote stale")
            mid = None
        mids_after[symbol] = mid

    iwm_move = pct_move(mids_before.get("IWM"), mids_after.get("IWM"))
    selected_move = (
        pct_move(mids_before.get(selected_symbol), mids_after.get(selected_symbol))
        if selected_symbol
        else None
    )

    correct: Optional[bool] = None
    if decision == SignalDirection.SKIP.value:
        notes.insert(0, "skip: movement recorded only, not scored")
    else:
        if decision == SignalDirection.BULLISH.value:
            checks = [selected_move is not None and selected_move > 0, iwm_move is not None and iwm_move > 0]
            label = "TNA up or IWM up"
        else:
            checks = [selected_move is not None and selected_move > 0, iwm_move is not None and iwm_move < 0]
            label = "TZA up or IWM down"
        if any(checks):
            correct = True
            notes.insert(0, f"correct: {label}")
        elif selected_move is not None and iwm_move is not None:
            correct = False
            notes.insert(0, f"incorrect: expected {label}")
        else:
            notes.insert(0, "insufficient follow-up data to score direction")

    return FollowupOutcome(
        followup_seconds=followup_seconds,
        mids_after=mids_after,
        selected_symbol_move_pct=selected_move,
        iwm_move_pct=iwm_move,
        direction_was_correct=correct,
        outcome_note="; ".join(notes),
    )


@dataclass
class ShadowCycleRecord:
    """One shadow-mode cycle. submitted is always False; production execution always blocked."""

    run_id: str
    cycle_number: int
    created_at: datetime
    decision: str
    selected_symbol: Optional[str]
    confidence_score: float
    bullish_score: float
    bearish_score: float
    skip_reason: Optional[str]
    market_regime: str
    explanation: str
    quote_ages: Dict[str, Optional[float]]
    mids: Dict[str, Optional[float]]
    vix_last: Optional[float]
    freshness_gate_passed: bool
    raw_snapshot_json: str
    raw_score_json: str
    warnings: List[str] = field(default_factory=list)
    followup: Optional[FollowupOutcome] = None
    db_id: Optional[int] = None
    submitted: bool = False
    production_execution_blocked: bool = True

    def __post_init__(self) -> None:
        self.assert_safe()

    def assert_safe(self) -> None:
        if self.submitted is not False:
            raise ShadowSafetyError("shadow mode records can never be submitted")
        if self.production_execution_blocked is not True:
            raise ShadowSafetyError("shadow mode requires production execution to stay blocked")

    @classmethod
    def from_decision(
        cls,
        *,
        run_id: str,
        cycle_number: int,
        decision: SignalDecision,
        snapshot: MarketSnapshot,
    ) -> "ShadowCycleRecord":
        vix = snapshot.get("VIX")
        breakdown = decision.score_breakdown.to_dict() if decision.score_breakdown else None
        return cls(
            run_id=run_id,
            cycle_number=cycle_number,
            created_at=decision.created_at,
            decision=decision.decision.value,
            selected_symbol=decision.selected_symbol,
            confidence_score=float(decision.confidence_score),
            bullish_score=float(decision.bullish_score),
            bearish_score=float(decision.bearish_score),
            skip_reason=decision.skip_reason.value if decision.skip_reason else None,
            market_regime=decision.market_regime,
            explanation=decision.explanation,
            quote_ages={s: _age(snapshot, s) for s in TRACKED_SYMBOLS},
            mids={s: _mid(snapshot, s) for s in TRACKED_SYMBOLS},
            vix_last=vix.price if vix else None,
            freshness_gate_passed=decision.freshness_gate_passed,
            raw_snapshot_json=json.dumps(snapshot.to_dict(), sort_keys=True, default=str),
            raw_score_json=json.dumps(
                {"score_breakdown": breakdown, "thresholds": dict(decision.thresholds)},
                sort_keys=True,
                default=str,
            ),
            warnings=list(decision.warnings),
        )

    def apply_followup(self, outcome: FollowupOutcome) -> None:
        self.followup = outcome

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.db_id,
            "run_id": self.run_id,
            "cycle_number": self.cycle_number,
            "created_at": self.created_at.isoformat(),
            "decision": self.decision,
            "selected_symbol": self.selected_symbol,
            "confidence_score": round(self.confidence_score, 2),
            "bullish_score": round(self.bullish_score, 2),
            "bearish_score": round(self.bearish_score, 2),
            "skip_reason": self.skip_reason,
            "market_regime": self.market_regime,
            "explanation": self.explanation,
            **{f"quote_age_{s.lower()}": self.quote_ages.get(s) for s in TRACKED_SYMBOLS},
            **{f"{s.lower()}_mid": self.mids.get(s) for s in TRACKED_SYMBOLS},
            "vix_last": self.vix_last,
            "freshness_gate_passed": self.freshness_gate_passed,
            "submitted": self.submitted,
            "production_execution_blocked": self.production_execution_blocked,
            "warnings": list(self.warnings),
        }
        if self.followup is not None:
            data.update(self.followup.to_dict())
        else:
            data.update(
                {
                    "followup_seconds": None,
                    **{f"{s.lower()}_mid_after": None for s in TRACKED_SYMBOLS},
                    "selected_symbol_move_pct": None,
                    "iwm_move_pct": None,
                    "direction_was_correct": None,
                    "outcome_note": None,
                }
            )
        return data


@dataclass
class ShadowCycleError:
    cycle_number: int
    step: str
    reason: str
    fatal: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"cycle_number": self.cycle_number, "step": self.step, "reason": self.reason, "fatal": self.fatal}


@dataclass
class ShadowRunSummary:
    run_id: str
    cycles_requested: int
    records: List[ShadowCycleRecord] = field(default_factory=list)
    errors: List[ShadowCycleError] = field(default_factory=list)
    aborted: bool = False
    abort_reason: Optional[str] = None
    orders_submitted: int = 0

    @property
    def cycles_completed(self) -> int:
        return len(self.records)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "cycles_requested": self.cycles_requested,
            "cycles_completed": self.cycles_completed,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "orders_submitted": self.orders_submitted,
            "errors": [e.to_dict() for e in self.errors],
        }
