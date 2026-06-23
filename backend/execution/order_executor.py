"""Single public entry point for the new Phase 2 execution stack."""

from __future__ import annotations

from backend.execution.execution_router import ExecutionRouter
from backend.risk.models import ExecutionResult, OrderIntent, RiskContext
from backend.risk.risk_engine import RiskEngine


class OrderExecutor:
    """
    OrderExecutor → RiskEngine → ExecutionRouter → PaperSimulator

    No legacy broker placement imports in this checkpoint.
    """

    def __init__(
        self,
        risk_engine: RiskEngine | None = None,
        router: ExecutionRouter | None = None,
    ) -> None:
        self._risk = risk_engine or RiskEngine()
        self._router = router or ExecutionRouter()

    @property
    def risk_engine(self) -> RiskEngine:
        return self._risk

    @property
    def router(self) -> ExecutionRouter:
        return self._router

    def execute(self, intent: OrderIntent, context: RiskContext) -> ExecutionResult:
        approval = self._risk.approve(intent, context)
        if not approval.approved:
            return ExecutionResult(
                success=False,
                status="rejected",
                symbol=intent.symbol.strip().upper(),
                side=intent.side.strip().lower(),
                quantity=intent.quantity,
                trading_mode=intent.trading_mode.strip().lower(),
                message=approval.reason,
                raw={
                    "rejection_code": approval.rejection_code,
                    "checks": approval.checks,
                },
            )

        routed_intent = approval.normalized_intent or intent
        return self._router.route(routed_intent, context)
