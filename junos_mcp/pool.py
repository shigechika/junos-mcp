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
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from junos_ops import common

_DEFAULT_IDLE: float = 60.0

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

    def __init__(self, idle_timeout: float = _DEFAULT_IDLE) -> None:
        self._idle_timeout = idle_timeout
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
            conn = common.connect(hostname)
            if not conn["ok"]:
                msg = conn.get("error_message") or conn.get("error") or "Connection failed"
                raise PoolConnectionError(msg)
            entry.dev = conn["dev"]
            entry.last_used = time.monotonic()

        return entry.dev

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
                _pool = ConnectionPool(idle_timeout=idle)
                atexit.register(_pool.close_all)
    return _pool
