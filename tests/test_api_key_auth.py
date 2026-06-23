"""API admin key middleware tests."""

from pathlib import Path

import pytest
from flask import Flask, jsonify

from backend.api.middleware.api_key_auth import (
    is_api_key_configured,
    require_api_key,
    verify_api_key,
)
from backend.config.settings import Settings, reset_settings_cache


def _settings(**overrides) -> Settings:
    base = {
        "live_trading_enabled": False,
        "trading_mode": "paper",
        "tastytrade_env": "sandbox",
        "emergency_halt": False,
        "database_url": "sqlite:///:memory:",
        "api_admin_key": "test-secret-key-12345",
    }
    base.update(overrides)
    return Settings(**base)


def test_verify_api_key_accepts_correct_key():
    settings = _settings()
    assert verify_api_key(settings, "test-secret-key-12345") is True


def test_verify_api_key_rejects_wrong_key():
    settings = _settings()
    assert verify_api_key(settings, "wrong") is False


def test_default_api_key_is_not_configured():
    settings = _settings(api_admin_key="change-me-long-random-string")
    assert is_api_key_configured(settings) is False


def test_require_api_key_on_flask_request(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    reset_settings_cache()

    app = Flask(__name__)
    settings = _settings()

    @app.route("/mutate", methods=["POST"])
    def mutate():
        err = require_api_key(settings)
        if err:
            return err
        return jsonify({"ok": True})

    client = app.test_client()
    no_key = client.post("/mutate")
    assert no_key.status_code == 401

    ok = client.post("/mutate", headers={"X-API-Key": "test-secret-key-12345"})
    assert ok.status_code == 200
