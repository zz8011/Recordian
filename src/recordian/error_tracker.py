"""Error tracking and reporting module."""

import logging
import os
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)


class ErrorTracker:
    """Centralized error tracking with Sentry integration."""

    def __init__(self, dsn: str | None = None, environment: str = "production", release: str | None = None) -> None:
        """Initialize error tracker.

        Args:
            dsn: Sentry DSN (defaults to SENTRY_DSN env var)
            environment: Environment name (production, development, etc.)
            release: Release version
        """
        self.dsn = dsn or os.environ.get("SENTRY_DSN")
        self.environment = environment
        self.release = release
        self._initialized = False
        self._sentry_sdk = None

        if self.dsn:
            self._init_sentry()

    def _init_sentry(self) -> None:
        """Initialize Sentry SDK."""
        try:
            import sentry_sdk
            from sentry_sdk.integrations.threading import ThreadingIntegration

            self._sentry_sdk = sentry_sdk

            sentry_sdk.init(
                dsn=self.dsn,
                environment=self.environment,
                release=self.release,
                traces_sample_rate=0.1,  # 降低采样率以减少开销
                profiles_sample_rate=0.1,
                integrations=[
                    ThreadingIntegration(propagate_hub=True),
                ],
                before_send=self._before_send,
            )
            self._initialized = True
            logger.info("Sentry error tracking initialized")
        except ImportError:
            logger.warning("Sentry SDK not installed, error tracking disabled")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Failed to initialize Sentry: {exc}")

    def _before_send(self, event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
        """Filter and modify events before sending to Sentry.

        Args:
            event: Sentry event dict
            hint: Additional context

        Returns:
            Modified event or None to drop the event
        """
        # 过滤掉一些不需要上报的异常
        if "exc_info" in hint:
            exc_type, exc_value, _tb = hint["exc_info"]
            # 忽略键盘中断
            if exc_type is KeyboardInterrupt:
                return None
            # 忽略系统退出
            if exc_type is SystemExit:
                return None

        # 添加线程信息
        event.setdefault("tags", {})
        event["tags"]["thread_name"] = threading.current_thread().name
        event["tags"]["thread_id"] = threading.current_thread().ident

        return event

    def capture_exception(self, exc: Exception | None = None, **kwargs: Any) -> None:
        """Capture an exception.

        Args:
            exc: Exception to capture (defaults to current exception)
            **kwargs: Additional context
        """
        if not self._initialized or not self._sentry_sdk:
            logger.error(f"Exception occurred: {exc or sys.exc_info()[1]}", exc_info=True)
            return

        try:
            if kwargs:
                with self._sentry_sdk.push_scope() as scope:
                    for key, value in kwargs.items():
                        scope.set_context(key, value)
                    self._sentry_sdk.capture_exception(exc)
            else:
                self._sentry_sdk.capture_exception(exc)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to capture exception: {e}")

    def capture_message(self, message: str, level: str = "info", **kwargs: Any) -> None:
        """Capture a message.

        Args:
            message: Message to capture
            level: Severity level (debug, info, warning, error, fatal)
            **kwargs: Additional context
        """
        if not self._initialized or not self._sentry_sdk:
            logger.log(getattr(logging, level.upper(), logging.INFO), message)
            return

        try:
            if kwargs:
                with self._sentry_sdk.push_scope() as scope:
                    for key, value in kwargs.items():
                        scope.set_context(key, value)
                    self._sentry_sdk.capture_message(message, level=level)
            else:
                self._sentry_sdk.capture_message(message, level=level)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to capture message: {e}")

    def set_user(self, user_id: str | None = None, **kwargs: Any) -> None:
        """Set user context.

        Args:
            user_id: User identifier
            **kwargs: Additional user attributes (email, username, etc.)
        """
        if not self._initialized or not self._sentry_sdk:
            return

        try:
            user_data = {"id": user_id} if user_id else {}
            user_data.update(kwargs)
            self._sentry_sdk.set_user(user_data)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to set user context: {e}")

    def set_tag(self, key: str, value: str) -> None:
        """Set a tag for all future events.

        Args:
            key: Tag key
            value: Tag value
        """
        if not self._initialized or not self._sentry_sdk:
            return

        try:
            self._sentry_sdk.set_tag(key, value)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to set tag: {e}")

    def set_context(self, key: str, value: dict[str, Any]) -> None:
        """Set context for all future events.

        Args:
            key: Context key
            value: Context data
        """
        if not self._initialized or not self._sentry_sdk:
            return

        try:
            self._sentry_sdk.set_context(key, value)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to set context: {e}")


# Global error tracker instance
_global_tracker: ErrorTracker | None = None


def init_error_tracker(
    dsn: str | None = None,
    environment: str = "production",
    release: str | None = None,
) -> ErrorTracker:
    """Initialize global error tracker.

    Args:
        dsn: Sentry DSN (defaults to SENTRY_DSN env var)
        environment: Environment name
        release: Release version

    Returns:
        ErrorTracker instance
    """
    global _global_tracker  # noqa: PLW0603
    _global_tracker = ErrorTracker(dsn=dsn, environment=environment, release=release)
    return _global_tracker


def get_error_tracker() -> ErrorTracker | None:
    """Get global error tracker instance.

    Returns:
        ErrorTracker instance or None if not initialized
    """
    return _global_tracker
