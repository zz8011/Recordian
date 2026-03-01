"""Recordian core package."""

import os

from recordian.error_tracker import init_error_tracker

__all__ = [
    "audio",
    "config",
    "engine",
    "policy",
    "realtime",
    "error_tracker",
]

# Initialize error tracking
_sentry_dsn = os.environ.get("SENTRY_DSN")
_environment = os.environ.get("RECORDIAN_ENV", "production")
_release = os.environ.get("RECORDIAN_VERSION")

if _sentry_dsn:
    init_error_tracker(dsn=_sentry_dsn, environment=_environment, release=_release)
