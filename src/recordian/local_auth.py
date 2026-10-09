"""Read an explicitly provisioned private token without following symlinks."""
from __future__ import annotations

import os
import ssl
import stat
from pathlib import Path


def load_private_token(filename: str) -> str:
    fd = os.open(Path(filename).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('token file must be an owner-only regular file (0600)')
        raw = handle.read(4097)
        token = raw.strip()
        if len(raw) > 4096 or not token or not token.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise ValueError('token file must contain a nonempty token of at most 4096 characters')
        return token


def require_private_bind(host: str, token: str, *, encrypted: bool = False) -> None:
    if host not in {'127.0.0.1', 'localhost', '::1'} and not token:
        raise ValueError('a private token file is required for a non-loopback bind')
    if host not in {'127.0.0.1', 'localhost', '::1'} and not encrypted:
        raise ValueError('TLS is required for a non-loopback bind; alternatively use an SSH tunnel to loopback')


def server_tls_context(certificate: str, key: str) -> ssl.SSLContext | None:
    if not certificate and not key:
        return None
    if not certificate or not key:
        raise ValueError('both TLS certificate and key files are required')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(Path(certificate).expanduser()), str(Path(key).expanduser()))
    return context


def http_request_allowed(*, host: str, origin: str | None, base_url: str, authenticated: bool) -> bool:
    """Protect unauthenticated loopback services from browser DNS rebinding/CSRF."""
    if origin and origin != base_url.rstrip('/'):
        return False
    return authenticated or host in {'127.0.0.1', 'localhost', '::1'}
