"""Application configuration: strategy JSON + runtime settings."""

from backend.config.strategy import CONFIG_ENV_KEY, CONFIG_FILENAME, DEFAULT_CONFIG, load_config

__all__ = [
    "CONFIG_ENV_KEY",
    "CONFIG_FILENAME",
    "DEFAULT_CONFIG",
    "load_config",
]
