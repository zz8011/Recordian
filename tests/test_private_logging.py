from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

from recordian.logging_config import setup_logging


@pytest.fixture(autouse=True)
def isolated_handlers(monkeypatch):
    # Use a separate logger: pytest installs capture handlers after fixture setup.
    # Keep those handlers and the application's global logger untouched.
    from recordian import logging_config

    logger = logging.Logger('recordian.private-log-test')
    logging_proxy = SimpleNamespace(**vars(logging))
    logging_proxy.getLogger = lambda name: logger
    monkeypatch.setattr(logging_config, 'logging', logging_proxy)
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


def test_file_log_is_private_even_with_permissive_umask(tmp_path):
    previous = os.umask(0o022)
    try:
        path = tmp_path / 'recordian.log'
        logger = setup_logging(log_file=path, console=False)
        logger.info('synthetic audit event')
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous)


def test_configured_log_symlink_does_not_overwrite_other_file(tmp_path):
    victim = tmp_path / 'victim'
    victim.write_text('preserve')
    path = tmp_path / 'recordian.log'
    path.symlink_to(victim)
    with pytest.raises(OSError):
        setup_logging(log_file=path, console=False)
    assert victim.read_text() == 'preserve'


def test_reconfigure_closes_the_previous_file_descriptor(tmp_path):
    logger = setup_logging(log_file=tmp_path/'first.log', console=False)
    first = logger.handlers[0]
    setup_logging(log_file=tmp_path/'second.log', console=False, force_reconfigure=True)
    assert first.stream is None
