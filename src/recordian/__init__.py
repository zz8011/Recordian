"""Recordian core package."""

import os

__all__ = [
    "audio",
    "config",
    "engine",
    "policy",
    "realtime",
]

# Initialize Sentry for error tracking
_sentry_dsn = os.environ.get("SENTRY_DSN")
if _sentry_dsn:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_sentry_dsn,
            traces_sample_rate=1.0,
            profiles_sample_rate=1.0,
        )
    except ImportError:
        pass  # Sentry SDK not installed
