"""Tastytrade URL constant tests."""

from pathlib import Path

import pytest

from backend.config.tastytrade_urls import (
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
    BrokerUrlBlockedError,
    assert_sandbox_base_url,
    is_production_url,
    resolve_broker_base_url,
)


def test_sandbox_base_url_is_cert_tastyworks():
    assert SANDBOX_BASE_URL == "https://api.cert.tastyworks.com"


def test_production_url_blocked_for_order_paths():
    with pytest.raises(BrokerUrlBlockedError):
        resolve_broker_base_url("production")
    with pytest.raises(BrokerUrlBlockedError):
        resolve_broker_base_url("live")


def test_typo_sandbox_env_cannot_route_to_production():
    with pytest.raises(BrokerUrlBlockedError):
        resolve_broker_base_url("prod")


def test_backend_source_has_no_cert_tastytrade_host():
    backend_root = Path("backend")
    allowed_blocked_list = Path("backend/config/tastytrade_urls.py")
    violations = []
    for path in backend_root.rglob("*.py"):
        if path.parts[-1] == "__pycache__":
            continue
        if path.resolve() == allowed_blocked_list.resolve():
            continue
        text = path.read_text(encoding="utf-8")
        if "api.cert.tastytrade.com" in text:
            violations.append(str(path))
    assert violations == [], f"Deprecated host found: {violations}"


def test_assert_sandbox_rejects_production():
    with pytest.raises(BrokerUrlBlockedError):
        assert_sandbox_base_url(PRODUCTION_BASE_URL)


def test_is_production_url():
    assert is_production_url("https://api.tastyworks.com") is True
    assert is_production_url(SANDBOX_BASE_URL) is False
