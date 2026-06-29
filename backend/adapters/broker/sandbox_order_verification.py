"""Safe parsing and verification helpers for sandbox order/position checks."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_order_data(response: Dict[str, Any]) -> Dict[str, Any]:
    data = response.get("data") or {}
    if isinstance(data.get("order"), dict):
        return data["order"]
    return data if isinstance(data, dict) else {}


def extract_broker_order_id(response: Dict[str, Any]) -> Optional[str]:
    order = extract_order_data(response)
    for key in ("id", "order-id", "order_id"):
        value = order.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def broker_order_id_from_execution(order_id: Optional[str], raw: Optional[Dict[str, Any]]) -> Optional[str]:
    if raw and raw.get("broker_order_id"):
        return str(raw["broker_order_id"])
    if order_id and order_id.upper().startswith("SANDBOX-"):
        suffix = order_id.split("-", 1)[1]
        if suffix:
            return suffix
    return None


def map_broker_status_to_record_status(broker_status: Optional[str]) -> str:
    if not broker_status:
        return "filled"
    normalized = broker_status.strip().lower()
    if normalized in {"filled", "cancelled", "canceled", "rejected", "expired", "live", "contingent"}:
        if normalized in {"canceled"}:
            return "cancelled"
        if normalized == "live":
            return "submitted"
        return normalized
    return "submitted"


def summarize_order_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """Extract safe, loggable order fields from a Tastytrade get_order/submit response."""
    order = extract_order_data(response)
    legs = order.get("legs") or []
    leg = legs[0] if legs else {}
    broker_status = order.get("status") or order.get("order-status")
    return {
        "broker_order_id": extract_broker_order_id(response),
        "status": map_broker_status_to_record_status(
            str(broker_status) if broker_status is not None else None
        ),
        "broker_status": str(broker_status) if broker_status is not None else None,
        "symbol": (leg.get("symbol") or order.get("symbol") or "").upper(),
        "side": _infer_side_from_action(leg.get("action")),
        "quantity": leg.get("quantity") or order.get("quantity"),
        "order_type": order.get("order-type") or order.get("order_type"),
        "limit_price": _coerce_float(order.get("price")),
        "fill_price": _coerce_float(
            order.get("fill-price")
            or order.get("average-fill-price")
            or order.get("average_fill_price")
        ),
        "trading_mode": "sandbox",
    }


def _infer_side_from_action(action: Any) -> str:
    text = str(action or "").lower()
    if "sell" in text:
        return "sell"
    if "buy" in text:
        return "buy"
    return ""


def format_order_status_summary(summary: Dict[str, Any]) -> str:
    lines = ["--- broker order status ---"]
    for key in (
        "broker_order_id",
        "broker_status",
        "status",
        "symbol",
        "side",
        "quantity",
        "order_type",
        "limit_price",
        "fill_price",
        "trading_mode",
    ):
        lines.append(f"{key}: {summary.get(key)}")
    return "\n".join(lines)


def find_equity_position(positions: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
    target = symbol.strip().upper()
    for position in positions:
        pos_symbol = (
            position.get("symbol")
            or position.get("underlying-symbol")
            or position.get("underlying_symbol")
            or ""
        )
        if str(pos_symbol).strip().upper() == target:
            return position
    return None


def position_quantity(position: Dict[str, Any]) -> float:
    for key in ("quantity", "quantity-direction", "quantity_direction"):
        value = position.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def verify_buy_position_present(
    positions: List[Dict[str, Any]],
    symbol: str,
    *,
    expected_qty: int = 1,
) -> tuple[bool, str]:
    position = find_equity_position(positions, symbol)
    if not position:
        return False, (
            f"warning: sandbox position for {symbol.upper()} not visible yet after submit "
            "(positions API may lag in cert sandbox — not treated as failure)"
        )
    qty = position_quantity(position)
    if qty < expected_qty:
        return False, (
            f"warning: sandbox position qty for {symbol.upper()} is {qty}, expected >= {expected_qty} "
            "(positions API may lag in cert sandbox — not treated as failure)"
        )
    return True, f"position_verified: {symbol.upper()} qty={qty}"


def verify_position_closed(positions: List[Dict[str, Any]], symbol: str) -> tuple[bool, str]:
    position = find_equity_position(positions, symbol)
    if not position:
        return True, f"position_closed: no open {symbol.upper()} position"
    qty = position_quantity(position)
    if qty == 0:
        return True, f"position_closed: {symbol.upper()} qty=0"
    return False, (
        f"warning: {symbol.upper()} position still shows qty={qty} after close "
        "(sandbox positions may lag — not treated as failure)"
    )
