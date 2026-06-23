"""Bot state initialization on app startup."""

from __future__ import annotations

import logging

from backend.config.settings import Settings

logger = logging.getLogger(__name__)


def initialize_bot_state_on_startup(settings: Settings) -> None:
    """
    Ensure bot_state row exists and reflects stopped/safe defaults.

    Does not start the bot loop. Failures are logged and swallowed so imports/tests stay safe.
    """
    try:
        from backend.db.session import get_db_session
        from backend.repositories.bot_state_repository import BotStateRepository

        session = get_db_session()
        try:
            repo = BotStateRepository(session)
            state = repo.ensure_initialized(settings)
            logger.info(
                "Bot state initialized: running=%s trading_mode=%s emergency_halt=%s",
                state.running,
                state.trading_mode,
                state.emergency_halt,
            )
        finally:
            session.close()
    except Exception as exc:
        logger.warning("Bot state initialization skipped: %s", type(exc).__name__)
