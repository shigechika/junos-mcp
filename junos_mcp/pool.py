"""Per-host NETCONF connection pool for junos-mcp.

A single PyEZ Device is not thread-safe, so the pool serializes all
operations on a given host by holding a per-host lock from connection
checkout (acquire) to checkin (exit of the ``with`` block).

Environment variables
---------------------
JUNOS_MCP_POOL
    Set to ``0`` to disable the pool (each call opens a fresh connection,
    same behaviour as junos-mcp < 0.12).  Any other value enables it.
JUNOS_MCP_POOL_IDLE
    Idle timeout in seconds (float, default ``60``).  A pooled connection
    unused for longer than this is closed and reopened on the next acquire.
    Set to ``0`` to disable idle eviction.
JUNOS_MCP_POOL_CONNECT_ATTEMPTS
    Number of connect attempts on a transient failure (int, default ``2``
    = one retry).  Set to ``1`` to disable retrying.
JUNOS_MCP_POOL_CONNECT_DELAY
    Delay in seconds between connect attempts (float, default ``1.0``).
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from junos_ops import common

logger = logging.getLogger(__name__)

_DEFAULT_IDLE: float = 60.0
_DEFAULT_CONNECT_ATTEMPTS: int = 2  # one retry
_DEFAULT_CONNECT_RETRY_DELAY: float = 1.0

# Connect failures worth a retry: a device that is reachable but momentarily
# slow.  ``ConnectError`` is the class PyEZ raises for a bare SSH-layer failure
# such as an "Error reading SSH protocol banner" banner-read timeout;
# ``ConnectTimeoutError`` covers a slow NETCONF handshake.  Permanent failures
# — ``ConnectAuthError`` (bad credentials; retrying risks account lockout),
# ``ConnectRefusedError`` (NETCONF disabled) and ``ConnectUnknownHostError``
# (name resolution) — are deliberately excluded so they fail fast.
_RETRYABLE_CONNECT_ERRORS = frozenset({"ConnectError", "ConnectTimeoutError"})

# Module-level singleton.  Set to None to force re-creation (e.g. in tests).
_pool: ConnectionPool | None = None
_pool_init_lock = threading.Lock()


class PoolConnectionError(Exception):
    """Raised by ConnectionPool.acquire when a device connection cannot be opened."""


class _Entry:
    """State bucket for one pooled host connection."""

    __slots__ = ("lock", "dev", "last_used")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.dev = None  # jnpr.junos.Device or None
        self.last_used: float = 0.0


class ConnectionPool:
    """Per-host NETCONF connection pool.

    Operations on a single host are serialized via a per-entry lock held
    for the full duration of the operation (checkout to checkin).
    """

    def __init__(
        self,
        idle_timeout: float = _DEFAULT_IDLE,
        connect_attempts: int = _DEFAULT_CONNECT_ATTEMPTS,
        connect_retry_delay: float = _DEFAULT_CONNECT_RETRY_DELAY,
    ) -> None:
        self._idle_timeout = idle_timeout
        self._connect_attempts = max(1, connect_attempts)
        self._connect_retry_delay = connect_retry_delay
        self._lock = threading.Lock()  # guards _entries dict
        self._entries: dict[tuple[str, str], _Entry] = {}

    @contextmanager
    def acquire(self, hostname: str, config_path: str) -> Iterator:
        """Yield a connected Device for *hostname*, then return it to the pool.

        The per-host lock is held for the entire duration of the ``with``
        block; concurrent callers for the same host queue behind it.

        :raises PoolConnectionError: if the device cannot be connected.
        """
        entry = self._get_or_create(hostname, config_path)
        entry.lock.acquire()
        try:
            dev = self._get_or_open(entry, hostname)
            yield dev
            entry.last_used = time.monotonic()
        except Exception:
            # Connection failed or operation raised — evict so the next
            # caller gets a fresh attempt instead of a half-open session.
            self._close_dev(entry)
            raise
        finally:
            entry.lock.release()

    def close_all(self) -> None:
        """Close all pooled connections.  Called at process exit via atexit."""
        with self._lock:
            for entry in self._entries.values():
                with entry.lock:
                    self._close_dev(entry)
            self._entries.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create(self, hostname: str, config_path: str) -> _Entry:
        key = (hostname, config_path)
        with self._lock:
            if key not in self._entries:
                self._entries[key] = _Entry()
            return self._entries[key]

    def _get_or_open(self, entry: _Entry, hostname: str):
        """Return the cached Device if still healthy; otherwise evict and reopen."""
        now = time.monotonic()
        if entry.dev is not None:
            if self._idle_timeout > 0 and now - entry.last_used > self._idle_timeout:
                self._close_dev(entry)
            elif not entry.dev.connected:
                self._close_dev(entry)

        if entry.dev is None:
            conn = self._connect(hostname)
            if not conn["ok"]:
                msg = conn.get("error_message") or conn.get("error") or "Connection failed"
                raise PoolConnectionError(msg)
            entry.dev = conn["dev"]
            entry.last_used = time.monotonic()

        return entry.dev

    def _connect(self, hostname: str) -> dict:
        """Open a connection via junos-ops, retrying transient failures.

        A transient connect failure — an SSH banner-read timeout (surfaced by
        PyEZ as ``ConnectError``) or a slow NETCONF handshake
        (``ConnectTimeoutError``) — means the device is reachable but
        momentarily slow, so it is worth up to ``connect_attempts`` tries with
        ``connect_retry_delay`` seconds between them.  Errors outside
        :data:`_RETRYABLE_CONNECT_ERRORS` (auth, NETCONF-refused, unknown host)
        are permanent and return immediately.

        Called with the per-host ``entry.lock`` held, so any retry delay only
        blocks concurrent callers for this same host, not other hosts.

        :param hostname: config section name (host identifier).
        :return: the junos-ops ``connect()`` result dict (last attempt if all
            attempts failed).
        """
        for attempt in range(1, self._connect_attempts + 1):
            conn = common.connect(hostname)
            if conn["ok"]:
                if attempt > 1:
                    logger.info(
                        "connect to %s recovered on attempt %d/%d",
                        hostname,
                        attempt,
                        self._connect_attempts,
                    )
                return conn
            if conn.get("error") not in _RETRYABLE_CONNECT_ERRORS:
                return conn  # permanent failure — fail fast, no retry
            if attempt < self._connect_attempts:
                logger.warning(
                    "transient connect failure for %s (%s); retry %d/%d",
                    hostname,
                    conn.get("error"),
                    attempt,
                    self._connect_attempts - 1,
                )
                if self._connect_retry_delay > 0:
                    time.sleep(self._connect_retry_delay)
        return conn

    @staticmethod
    def _close_dev(entry: _Entry) -> None:
        if entry.dev is not None:
            try:
                entry.dev.close()
            except Exception:
                pass
            entry.dev = None


def get_pool() -> ConnectionPool | None:
    """Return the module-level pool, or None if pooling is disabled.

    Uses double-checked locking so the pool is created at most once.
    """
    if os.environ.get("JUNOS_MCP_POOL", "1") == "0":
        return None
    global _pool
    if _pool is None:
        with _pool_init_lock:
            if _pool is None:
                idle = float(os.environ.get("JUNOS_MCP_POOL_IDLE", str(_DEFAULT_IDLE)))
                attempts = int(
                    os.environ.get(
                        "JUNOS_MCP_POOL_CONNECT_ATTEMPTS", str(_DEFAULT_CONNECT_ATTEMPTS)
                    )
                )
                delay = float(
                    os.environ.get(
                        "JUNOS_MCP_POOL_CONNECT_DELAY", str(_DEFAULT_CONNECT_RETRY_DELAY)
                    )
                )
                _pool = ConnectionPool(
                    idle_timeout=idle,
                    connect_attempts=attempts,
                    connect_retry_delay=delay,
                )
                atexit.register(_pool.close_all)
    return _pool
