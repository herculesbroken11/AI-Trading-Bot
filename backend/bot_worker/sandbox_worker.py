"""Run one controlled sandbox bot cycle — no continuous loop."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.sandbox_order_verification import (
    broker_order_id_from_execution,
    partition_orders_for_cancel_list,
    position_quantity,
    summarize_live_orders_response,
    summarize_order_response,
)
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError, Settings, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import SANDBOX_BASE_URL, assert_sandbox_base_url
from backend.execution.execution_router import ExecutionRouter
from backend.execution.order_executor import OrderExecutor
from backend.repositories.account_snapshot_repository import AccountSnapshotRepository
from backend.repositories.bot_state_repository import BotStateRepository
from backend.repositories.decision_repository import DecisionRepository
from backend.repositories.error_repository import ErrorRepository
from backend.repositories.order_repository import OrderRepository
from backend.risk.models import OrderIntent, RiskContext
from backend.risk.risk_engine import RiskEngine

logger = logging.getLogger(__name__)

ALLOWED_SIGNALS = frozenset({"bullish", "bearish", "none"})
SIGNAL_SYMBOL_MAP = {"bullish": "TNA", "bearish": "TZA"}
WORKER_SOURCE = "sandbox_bot_worker"


@dataclass
class SandboxWorkerRepositories:
    order_repository: Optional[OrderRepository] = None
    decision_repository: Optional[DecisionRepository] = None
    error_repository: Optional[ErrorRepository] = None
    account_snapshot_repository: Optional[AccountSnapshotRepository] = None
    bot_state_repository: Optional[BotStateRepository] = None


@dataclass
class SandboxBotCycleResult:
    success: bool
    decision_status: str
    signal: str
    message: str = ""
    symbol: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    dry_run_passed: bool = False
    submitted: bool = False
    order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    broker_status: Optional[str] = None
    account_number: Optional[str] = None
    active_live_orders_count: int = 0
    positions_count: int = 0
    risk_approved: Optional[bool] = None
    limit_price: Optional[float] = None
    order_type: str = "Limit"

    def to_safe_summary(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "decision_status": self.decision_status,
            "signal": self.signal,
            "symbol": self.symbol,
            "message": self.message,
            "dry_run_passed": self.dry_run_passed,
            "submitted": self.submitted,
            "order_id": self.order_id,
            "broker_order_id": self.broker_order_id,
            "broker_status": self.broker_status,
            "account_number": self.account_number,
            "active_live_orders_count": self.active_live_orders_count,
            "positions_count": self.positions_count,
            "risk_approved": self.risk_approved,
            "limit_price": self.limit_price,
            "order_type": self.order_type,
            "warnings": list(self.warnings),
        }


def validate_sandbox_worker_settings(settings: Settings) -> None:
    if settings.trading_mode.strip().lower() != "sandbox":
        raise ConfigurationError("SandboxBotWorker requires TRADING_MODE=sandbox")
    if settings.tastytrade_env.strip().lower() != "sandbox":
        raise ConfigurationError("SandboxBotWorker requires TASTYTRADE_ENV=sandbox")
    if settings.live_trading_enabled:
        raise ConfigurationError("LIVE_TRADING_ENABLED must be false")
    if settings.emergency_halt:
        raise ConfigurationError("EMERGENCY_HALT must be false")
    assert_sandbox_base_url(SANDBOX_BASE_URL)


def map_signal_to_symbol(signal: str) -> Optional[str]:
    normalized = signal.strip().lower()
    if normalized == "none":
        return None
    return SIGNAL_SYMBOL_MAP.get(normalized)


def count_open_positions(positions: List[Dict[str, Any]]) -> int:
    return sum(1 for position in positions if position_quantity(position) > 0)


def _coerce_balance_amount(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class SandboxBotWorker:
    """Execute exactly one sandbox cycle. No loop, no public routes."""

    def __init__(
        self,
        settings: Settings,
        adapter: TastytradeSandboxAdapter,
        *,
        risk_engine: Optional[RiskEngine] = None,
        executor: Optional[OrderExecutor] = None,
        repositories: Optional[SandboxWorkerRepositories] = None,
    ) -> None:
        validate_sandbox_worker_settings(settings)
        self._settings = settings
        self._adapter = adapter
        self._risk = risk_engine or RiskEngine()
        self._executor = executor or OrderExecutor(
            risk_engine=self._risk,
            router=ExecutionRouter(sandbox_adapter=adapter),
        )
        self._repos = repositories or SandboxWorkerRepositories()

    @classmethod
    def from_settings(
        cls,
        settings: Optional[Settings] = None,
        *,
        with_db: bool = False,
    ) -> "SandboxBotWorker":
        reset_settings_cache()
        resolved = settings or load_settings()
        validate_sandbox_worker_settings(resolved)
        adapter = TastytradeSandboxAdapter(resolved)
        repos = SandboxWorkerRepositories()
        executor = OrderExecutor(
            risk_engine=RiskEngine(),
            router=ExecutionRouter(sandbox_adapter=adapter),
        )
        if with_db:
            from backend.db.session import configure_engine, get_db_session

            configure_engine(resolved.database_url, sql_echo=resolved.sql_echo)
            session = get_db_session()
            repos = SandboxWorkerRepositories(
                order_repository=OrderRepository(session),
                decision_repository=DecisionRepository(session),
                error_repository=ErrorRepository(session),
                account_snapshot_repository=AccountSnapshotRepository(session),
                bot_state_repository=BotStateRepository(session),
            )
            executor = OrderExecutor(
                risk_engine=RiskEngine(),
                router=ExecutionRouter(sandbox_adapter=adapter),
                order_repository=repos.order_repository,
                decision_repository=repos.decision_repository,
                error_repository=repos.error_repository,
            )
        return cls(resolved, adapter, executor=executor, repositories=repos)

    def run_cycle(
        self,
        *,
        signal: str = "none",
        order_type: str = "Limit",
        limit_price: Optional[float] = 2.0,
        reference_price: float = 50.0,
        confirm_submit: bool = False,
    ) -> SandboxBotCycleResult:
        normalized_signal = signal.strip().lower()
        if normalized_signal not in ALLOWED_SIGNALS:
            return SandboxBotCycleResult(
                success=False,
                decision_status="invalid_signal",
                signal=normalized_signal,
                message=f"signal must be one of {sorted(ALLOWED_SIGNALS)}",
            )

        normalized_order_type = order_type.strip().capitalize()
        if normalized_order_type == "Limit" and limit_price is None:
            return SandboxBotCycleResult(
                success=False,
                decision_status="invalid_order",
                signal=normalized_signal,
                message="limit_price is required for Limit orders",
            )

        warnings: List[str] = []

        try:
            self._adapter._auth.ensure_authenticated()
            self._adapter.get_customers_me()
            accounts = self._adapter.get_accounts()
            if not accounts:
                raise SandboxApiError(404, "No sandbox accounts returned")
            balance = self._adapter.get_balance()
            account_number = balance.get("account_number")
            positions = self._adapter.get_positions(account_number)
            positions_count = count_open_positions(positions)

            live_items = self._adapter.list_live_orders(account_number)
            live_summaries = summarize_live_orders_response({"data": {"items": live_items}})
            active_live, _history = partition_orders_for_cancel_list(live_summaries)
            active_live_count = len(active_live)

            self._log_account_snapshot(balance, positions, positions_count, warnings)

            skip = self._check_duplicate_protection(
                active_live_count=active_live_count,
                positions_count=positions_count,
                warnings=warnings,
            )
            if skip:
                skip.active_live_orders_count = active_live_count
                skip.positions_count = positions_count
                skip.account_number = account_number
                skip.signal = normalized_signal
                skip.order_type = normalized_order_type
                skip.limit_price = limit_price
                self._log_skipped(skip.decision_status, skip.message, normalized_signal, warnings)
                return skip

            self._log_strategy_decision(normalized_signal, warnings)

            if normalized_signal == "none":
                result = SandboxBotCycleResult(
                    success=True,
                    decision_status="skipped_no_signal",
                    signal=normalized_signal,
                    message="No trade signal; cycle complete without dry-run",
                    account_number=account_number,
                    active_live_orders_count=active_live_count,
                    positions_count=positions_count,
                    order_type=normalized_order_type,
                    limit_price=limit_price,
                )
                self._log_skipped(result.decision_status, result.message, normalized_signal, warnings)
                result.warnings = warnings
                return result

            symbol = map_signal_to_symbol(normalized_signal)
            assert symbol is not None

            risk_price = float(limit_price if normalized_order_type == "Limit" else reference_price)
            context = self._build_context(balance, positions_count, risk_price)
            intent = OrderIntent(
                symbol=symbol,
                side="buy",
                quantity=1,
                trading_mode="sandbox",
                order_type=normalized_order_type,
                limit_price=limit_price if normalized_order_type == "Limit" else None,
                source=WORKER_SOURCE,
                reason=f"sandbox worker signal={normalized_signal}",
                current_price=risk_price,
            )

            approval = self._risk.approve(intent, context)
            if not approval.approved:
                self._log_risk_rejection(intent, approval.rejection_code, approval.reason, warnings)
                result = SandboxBotCycleResult(
                    success=False,
                    decision_status="risk_rejected",
                    signal=normalized_signal,
                    symbol=symbol,
                    message=approval.reason,
                    account_number=account_number,
                    active_live_orders_count=active_live_count,
                    positions_count=positions_count,
                    risk_approved=False,
                    order_type=normalized_order_type,
                    limit_price=limit_price,
                    warnings=warnings,
                )
                return result

            self._log_risk_approval(symbol, approval.reason, warnings)

            try:
                self._adapter.dry_run_equity_order(
                    account_number,
                    symbol,
                    "buy",
                    1,
                    order_type=normalized_order_type,
                    limit_price=limit_price if normalized_order_type == "Limit" else None,
                )
            except (SandboxApiError, SandboxAuthError) as exc:
                self._log_dry_run_result(False, symbol, str(exc), warnings)
                return SandboxBotCycleResult(
                    success=False,
                    decision_status="dry_run_failed",
                    signal=normalized_signal,
                    symbol=symbol,
                    message=str(exc),
                    account_number=account_number,
                    active_live_orders_count=active_live_count,
                    positions_count=positions_count,
                    risk_approved=True,
                    order_type=normalized_order_type,
                    limit_price=limit_price,
                    warnings=warnings,
                )

            self._log_dry_run_result(True, symbol, "dry-run passed", warnings)

            if not confirm_submit:
                result = SandboxBotCycleResult(
                    success=True,
                    decision_status="dry_run_passed",
                    signal=normalized_signal,
                    symbol=symbol,
                    message="Dry-run passed; re-run with confirm_submit to place order",
                    account_number=account_number,
                    active_live_orders_count=active_live_count,
                    positions_count=positions_count,
                    dry_run_passed=True,
                    risk_approved=True,
                    order_type=normalized_order_type,
                    limit_price=limit_price,
                    warnings=warnings,
                )
                return result

            execution = self._executor.execute(intent, context)
            broker_order_id = broker_order_id_from_execution(execution.order_id, execution.raw)
            broker_status = (execution.raw or {}).get("broker_status")
            if broker_order_id:
                try:
                    status_summary = self._adapter.fetch_order_status_summary(
                        account_number,
                        broker_order_id,
                    )
                    broker_status = status_summary.get("broker_status") or broker_status
                except (SandboxApiError, SandboxAuthError) as exc:
                    warnings.append(f"broker status fetch failed: {exc}")

            if not execution.success:
                return SandboxBotCycleResult(
                    success=False,
                    decision_status="submit_failed",
                    signal=normalized_signal,
                    symbol=symbol,
                    message=execution.message,
                    account_number=account_number,
                    active_live_orders_count=active_live_count,
                    positions_count=positions_count,
                    dry_run_passed=True,
                    submitted=False,
                    risk_approved=True,
                    order_type=normalized_order_type,
                    limit_price=limit_price,
                    warnings=warnings,
                )

            return SandboxBotCycleResult(
                success=True,
                decision_status="submitted",
                signal=normalized_signal,
                symbol=symbol,
                message=execution.message,
                account_number=account_number,
                active_live_orders_count=active_live_count,
                positions_count=positions_count,
                dry_run_passed=True,
                submitted=True,
                order_id=execution.order_id,
                broker_order_id=broker_order_id,
                broker_status=broker_status,
                risk_approved=True,
                order_type=normalized_order_type,
                limit_price=limit_price,
                warnings=warnings,
            )
        except ConfigurationError as exc:
            return SandboxBotCycleResult(
                success=False,
                decision_status="configuration_error",
                signal=normalized_signal,
                message=str(exc),
                warnings=warnings,
            )
        except (SandboxApiError, SandboxAuthError) as exc:
            message = str(exc)
            self._log_error("sandbox_worker.cycle", exc, warnings)
            return SandboxBotCycleResult(
                success=False,
                decision_status="sandbox_error",
                signal=normalized_signal,
                message=message,
                warnings=warnings,
            )
        except Exception as exc:
            self._log_error("sandbox_worker.cycle", exc, warnings)
            return SandboxBotCycleResult(
                success=False,
                decision_status="error",
                signal=normalized_signal,
                message="Sandbox bot cycle failed",
                warnings=warnings,
            )

    def _build_context(self, balance: dict, positions_count: int, price: float) -> RiskContext:
        buying_power = _coerce_balance_amount(balance.get("buying_power"))
        if buying_power is None or buying_power <= 0:
            buying_power = _coerce_balance_amount(balance.get("cash_balance")) or 10_000.0
        return RiskContext(
            trading_mode="sandbox",
            live_trading_enabled=False,
            tastytrade_env=self._settings.tastytrade_env,
            emergency_halt=self._settings.emergency_halt,
            buying_power=float(buying_power),
            current_price=price,
            open_positions_count=positions_count,
            pending_orders_count=0,
            trades_today_count=0,
            daily_pnl=0.0,
            market_data_healthy=True,
            max_trades_per_day=3,
            max_daily_loss_usd=self._settings.max_daily_loss_usd,
            buying_power_reserve_pct=0.08,
            max_position_pct_of_buying_power=0.25,
            single_position_mode=True,
            ai_decision_valid=True,
            strategy_signal_valid=True,
        )

    def _check_duplicate_protection(
        self,
        *,
        active_live_count: int,
        positions_count: int,
        warnings: List[str],
    ) -> Optional[SandboxBotCycleResult]:
        if self._settings.emergency_halt:
            return SandboxBotCycleResult(
                success=False,
                decision_status="skipped_emergency_halt",
                signal="",
                message="EMERGENCY_HALT is enabled",
            )

        bot_state = self._repos.bot_state_repository
        if bot_state:
            state = bot_state.get_state()
            if state.emergency_halt:
                return SandboxBotCycleResult(
                    success=False,
                    decision_status="skipped_emergency_halt",
                    signal="",
                    message="bot_state emergency_halt is enabled",
                )
            if state.running:
                return SandboxBotCycleResult(
                    success=False,
                    decision_status="skipped_bot_running",
                    signal="",
                    message="bot_state.running is true in another process",
                )
            if state.active_trade_id is not None:
                return SandboxBotCycleResult(
                    success=False,
                    decision_status="skipped_active_trade_exists",
                    signal="",
                    message=f"unresolved active_trade_id={state.active_trade_id}",
                )

        if active_live_count > 0:
            return SandboxBotCycleResult(
                success=True,
                decision_status="skipped_live_order_exists",
                signal="",
                message="Active live sandbox order exists; skipping new trade",
            )

        if positions_count > 0:
            return SandboxBotCycleResult(
                success=True,
                decision_status="skipped_position_exists",
                signal="",
                message="Open sandbox position exists; skipping new trade",
            )
        return None

    def _log_account_snapshot(
        self,
        balance: dict,
        positions: List[Dict[str, Any]],
        positions_count: int,
        warnings: List[str],
    ) -> None:
        repo = self._repos.account_snapshot_repository
        if not repo:
            return
        try:
            repo.record_snapshot(
                mode="sandbox",
                buying_power=_coerce_balance_amount(balance.get("buying_power")),
                cash_available=_coerce_balance_amount(balance.get("cash_balance")),
                open_positions_count=positions_count,
                positions={"items": positions},
            )
        except Exception as exc:
            warnings.append(f"account snapshot logging failed: {type(exc).__name__}")

    def _log_strategy_decision(self, signal: str, warnings: List[str]) -> None:
        repo = self._repos.decision_repository
        if not repo:
            return
        try:
            repo.log_decision(
                decision_type="strategy_signal",
                source=WORKER_SOURCE,
                symbol=map_signal_to_symbol(signal),
                approved=signal != "none",
                reason=f"manual signal={signal}",
                payload={"signal": signal},
            )
        except Exception as exc:
            warnings.append(f"strategy decision logging failed: {type(exc).__name__}")

    def _log_skipped(self, status: str, reason: str, signal: str, warnings: List[str]) -> None:
        repo = self._repos.decision_repository
        if not repo:
            return
        try:
            repo.log_decision(
                decision_type="cycle_skipped",
                source=WORKER_SOURCE,
                symbol=map_signal_to_symbol(signal),
                approved=False,
                reason=reason,
                payload={"decision_status": status},
            )
        except Exception as exc:
            warnings.append(f"skip logging failed: {type(exc).__name__}")

    def _log_risk_approval(self, symbol: str, reason: str, warnings: List[str]) -> None:
        if not self._repos.decision_repository:
            return
        try:
            self._repos.decision_repository.log_risk_approval(
                symbol=symbol,
                source=WORKER_SOURCE,
                reason=reason,
            )
        except Exception as exc:
            warnings.append(f"risk approval logging failed: {type(exc).__name__}")

    def _log_risk_rejection(
        self,
        intent: OrderIntent,
        rejection_code: Optional[str],
        reason: str,
        warnings: List[str],
    ) -> None:
        if not self._repos.decision_repository:
            return
        try:
            self._repos.decision_repository.log_risk_rejection(
                symbol=intent.symbol,
                source=WORKER_SOURCE,
                rejection_code=rejection_code or "UNKNOWN",
                reason=reason,
            )
        except Exception as exc:
            warnings.append(f"risk rejection logging failed: {type(exc).__name__}")

    def _log_dry_run_result(
        self,
        passed: bool,
        symbol: str,
        message: str,
        warnings: List[str],
    ) -> None:
        if not self._repos.decision_repository:
            return
        try:
            self._repos.decision_repository.log_decision(
                decision_type="dry_run_passed" if passed else "dry_run_failed",
                source=WORKER_SOURCE,
                symbol=symbol,
                approved=passed,
                reason=message,
            )
        except Exception as exc:
            warnings.append(f"dry-run logging failed: {type(exc).__name__}")

    def _log_error(self, source: str, exc: BaseException, warnings: List[str]) -> None:
        if not self._repos.error_repository:
            warnings.append(f"{source}: {type(exc).__name__}")
            return
        try:
            self._repos.error_repository.log_error(
                source=source,
                message=str(exc),
                exc=exc,
            )
        except Exception as log_exc:
            warnings.append(f"error logging failed: {type(log_exc).__name__}")
