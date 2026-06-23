"""Single public entry point for the new Phase 2 execution stack."""

from __future__ import annotations

import uuid
from typing import Optional

from backend.execution.execution_router import ExecutionRouter
from backend.repositories.decision_repository import DecisionRepository
from backend.repositories.error_repository import ErrorRepository
from backend.repositories.order_repository import OrderRepository
from backend.risk.models import ExecutionResult, OrderIntent, RiskContext
from backend.risk.risk_engine import RiskEngine


class OrderExecutor:
    """
    OrderExecutor → RiskEngine → ExecutionRouter → PaperSimulator

    Optional repositories log decisions/orders without requiring PostgreSQL in unit tests.
    """

    def __init__(
        self,
        risk_engine: RiskEngine | None = None,
        router: ExecutionRouter | None = None,
        order_repository: Optional[OrderRepository] = None,
        decision_repository: Optional[DecisionRepository] = None,
        error_repository: Optional[ErrorRepository] = None,
    ) -> None:
        self._risk = risk_engine or RiskEngine()
        self._router = router or ExecutionRouter()
        self._orders = order_repository
        self._decisions = decision_repository
        self._errors = error_repository

    @property
    def risk_engine(self) -> RiskEngine:
        return self._risk

    @property
    def router(self) -> ExecutionRouter:
        return self._router

    def execute(self, intent: OrderIntent, context: RiskContext) -> ExecutionResult:
        pending_order_id: Optional[str] = None
        try:
            approval = self._risk.approve(intent, context)
            if not approval.approved:
                self._log_risk_rejection(intent, approval.rejection_code, approval.reason, approval.checks)
                rejected_id = f"REJECTED-{uuid.uuid4()}"
                self._log_rejected_order(
                    order_id=rejected_id,
                    intent=intent,
                    rejection_code=approval.rejection_code or "UNKNOWN",
                    message=approval.reason,
                    raw={"checks": approval.checks},
                )
                return ExecutionResult(
                    success=False,
                    status="rejected",
                    symbol=intent.symbol.strip().upper(),
                    side=intent.side.strip().lower(),
                    quantity=intent.quantity,
                    trading_mode=intent.trading_mode.strip().lower(),
                    message=approval.reason,
                    order_id=rejected_id,
                    raw={
                        "rejection_code": approval.rejection_code,
                        "checks": approval.checks,
                    },
                )

            routed_intent = approval.normalized_intent or intent
            self._log_risk_approval(routed_intent, approval.reason, approval.checks)

            pending_order_id = f"PENDING-{uuid.uuid4()}"
            self._create_pending_order(pending_order_id, routed_intent)

            result = self._router.route(routed_intent, context)
            self._log_execution_result(pending_order_id, routed_intent, result)
            return result
        except Exception as exc:
            self._log_error("order_executor.execute", exc, context={"symbol": intent.symbol})
            if pending_order_id and self._orders:
                try:
                    self._orders.mark_order_error(
                        pending_order_id,
                        message="Order execution failed",
                        raw={"error_type": type(exc).__name__},
                    )
                except Exception:
                    pass
            return ExecutionResult(
                success=False,
                status="error",
                symbol=intent.symbol.strip().upper(),
                side=intent.side.strip().lower(),
                quantity=intent.quantity,
                trading_mode=intent.trading_mode.strip().lower(),
                message="Order execution failed",
                order_id=pending_order_id,
            )

    def _log_risk_approval(self, intent: OrderIntent, reason: str, checks: list) -> None:
        if not self._decisions:
            return
        try:
            self._decisions.log_risk_approval(
                symbol=intent.symbol,
                source=intent.source,
                reason=reason,
                payload={"checks": checks},
            )
        except Exception as exc:
            self._log_error("order_executor.risk_approval_log", exc)

    def _log_risk_rejection(
        self,
        intent: OrderIntent,
        rejection_code: Optional[str],
        reason: str,
        checks: list,
    ) -> None:
        if not self._decisions:
            return
        try:
            self._decisions.log_risk_rejection(
                symbol=intent.symbol,
                source=intent.source,
                rejection_code=rejection_code or "UNKNOWN",
                reason=reason,
                payload={"checks": checks},
            )
        except Exception as exc:
            self._log_error("order_executor.risk_rejection_log", exc)

    def _log_rejected_order(
        self,
        *,
        order_id: str,
        intent: OrderIntent,
        rejection_code: str,
        message: str,
        raw: Optional[dict],
    ) -> None:
        if not self._orders:
            return
        try:
            self._orders.mark_order_rejected(
                order_id,
                rejection_code=rejection_code,
                message=message,
                mode=intent.trading_mode,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                raw=raw,
            )
        except Exception as exc:
            self._log_error("order_executor.rejected_order_log", exc)

    def _create_pending_order(self, pending_order_id: str, intent: OrderIntent) -> None:
        if not self._orders:
            return
        try:
            self._orders.create_pending_order(
                order_id=pending_order_id,
                mode=intent.trading_mode,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                order_type=intent.order_type,
                message="pending paper execution",
            )
        except Exception as exc:
            self._log_error("order_executor.pending_order_log", exc)

    def _log_execution_result(
        self,
        pending_order_id: str,
        intent: OrderIntent,
        result: ExecutionResult,
    ) -> None:
        if not self._orders:
            return
        try:
            if result.status == "filled" and result.success and result.order_id:
                self._orders.finalize_paper_fill(
                    pending_order_id,
                    result.order_id,
                    fill_price=float(result.fill_price or 0),
                    raw=result.raw,
                )
            elif result.status == "rejected":
                self._orders.mark_order_rejected(
                    pending_order_id,
                    rejection_code=(result.raw or {}).get("rejection_code", "REJECTED"),
                    message=result.message,
                    mode=result.trading_mode,
                    symbol=result.symbol,
                    side=result.side,
                    quantity=result.quantity,
                    raw=result.raw,
                )
            elif result.status == "error":
                self._orders.mark_order_error(
                    pending_order_id,
                    message=result.message,
                    raw=result.raw,
                )
        except Exception as exc:
            self._log_error("order_executor.order_log", exc)

    def _log_error(self, source: str, exc: BaseException, context: Optional[dict] = None) -> None:
        if not self._errors:
            return
        try:
            self._errors.log_error(
                source=source,
                message=str(exc),
                exc=exc,
                context=context,
            )
        except Exception:
            pass
