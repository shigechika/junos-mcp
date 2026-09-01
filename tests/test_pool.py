"""Tests for junos_mcp.pool — ConnectionPool behaviour."""

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import junos_mcp.pool as pool_module
from junos_mcp.pool import ConnectionPool, PoolConnectionError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_pool_singleton():
    """Reset the module-level pool singleton before and after each test."""
    pool_module._pool = None
    yield
    pool_module._pool = None


def _make_dev(connected=True):
    """Return a mock PyEZ Device."""
    dev = MagicMock()
    dev.connected = connected
    return dev


def _ok(dev):
    return {"ok": True, "dev": dev}


def _fail(msg="auth failed", error=None):
    return {"ok": False, "error_message": msg, "error": error}


# ---------------------------------------------------------------------------
# ConnectionPool unit tests
# ---------------------------------------------------------------------------


class TestConnectionPoolReuse:
    def test_second_acquire_reuses_device(self):
        """Same host+path: second acquire gets the same Device without reconnecting."""
        dev = _make_dev()
        p = ConnectionPool(idle_timeout=60)

        with patch("junos_mcp.pool.common.connect", return_value=_ok(dev)) as mock_connect:
            with p.acquire("rt1", "/cfg") as d1:
                pass
            with p.acquire("rt1", "/cfg") as d2:
                pass

        mock_connect.assert_called_once()
        assert d1 is dev
        assert d2 is dev
        dev.close.assert_not_called()

    def test_different_hosts_get_different_entries(self):
        """Different hostnames use separate pool entries."""
        dev_a = _make_dev()
        dev_b = _make_dev()
        p = ConnectionPool(idle_timeout=60)

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[_ok(dev_a), _ok(dev_b)],
        ) as mock_connect:
            with p.acquire("rt1", "/cfg") as d1:
                pass
            with p.acquire("rt2", "/cfg") as d2:
                pass

        assert mock_connect.call_count == 2
        assert d1 is dev_a
        assert d2 is dev_b


class TestConnectionPoolIdleEviction:
    def test_idle_timeout_triggers_reopen(self):
        """Connection idle past timeout is closed and a fresh one is opened."""
        dev1 = _make_dev()
        dev2 = _make_dev()
        p = ConnectionPool(idle_timeout=0.05)  # 50 ms

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[_ok(dev1), _ok(dev2)],
        ) as mock_connect:
            with p.acquire("rt1", "/cfg"):
                pass
            time.sleep(0.12)  # exceed 50 ms idle timeout
            with p.acquire("rt1", "/cfg") as d2:
                pass

        assert mock_connect.call_count == 2
        dev1.close.assert_called_once()
        assert d2 is dev2

    def test_zero_idle_disables_eviction(self):
        """idle_timeout=0 means never evict on idle."""
        dev = _make_dev()
        p = ConnectionPool(idle_timeout=0)

        with patch("junos_mcp.pool.common.connect", return_value=_ok(dev)) as mock_connect:
            with p.acquire("rt1", "/cfg"):
                pass
            time.sleep(0.05)
            with p.acquire("rt1", "/cfg"):
                pass

        mock_connect.assert_called_once()
        dev.close.assert_not_called()


class TestConnectionPoolStaleSession:
    def test_disconnected_device_is_replaced(self):
        """dev.connected==False triggers evict-and-reopen on next acquire."""
        dev1 = _make_dev(connected=True)
        dev2 = _make_dev(connected=True)
        p = ConnectionPool(idle_timeout=60)

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[_ok(dev1), _ok(dev2)],
        ) as mock_connect:
            with p.acquire("rt1", "/cfg"):
                pass
            dev1.connected = False  # simulate dropped session
            with p.acquire("rt1", "/cfg") as d2:
                pass

        assert mock_connect.call_count == 2
        dev1.close.assert_called_once()
        assert d2 is dev2

    def test_operation_exception_evicts_device(self):
        """Exception raised inside the with-block closes the device."""
        dev = _make_dev()
        p = ConnectionPool(idle_timeout=60)

        with patch("junos_mcp.pool.common.connect", return_value=_ok(dev)):
            with pytest.raises(RuntimeError, match="boom"):
                with p.acquire("rt1", "/cfg"):
                    raise RuntimeError("boom")

        dev.close.assert_called_once()

        # Next acquire should open a fresh connection
        dev2 = _make_dev()
        with patch("junos_mcp.pool.common.connect", return_value=_ok(dev2)) as mock_connect:
            with p.acquire("rt1", "/cfg") as d:
                pass

        mock_connect.assert_called_once()
        assert d is dev2


class TestConnectionPoolFailure:
    def test_connection_failure_raises_pool_connection_error(self):
        """connect() failure raises PoolConnectionError with the error message."""
        p = ConnectionPool()

        with patch("junos_mcp.pool.common.connect", return_value=_fail("auth failed")):
            with pytest.raises(PoolConnectionError, match="auth failed"):
                with p.acquire("rt1", "/cfg"):
                    pass

    def test_failed_connect_not_cached(self):
        """A failed connect attempt is not cached; next acquire retries."""
        dev = _make_dev()
        p = ConnectionPool()

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[_fail("timeout"), _ok(dev)],
        ) as mock_connect:
            with pytest.raises(PoolConnectionError):
                with p.acquire("rt1", "/cfg"):
                    pass
            with p.acquire("rt1", "/cfg") as d:
                pass

        assert mock_connect.call_count == 2
        assert d is dev


class TestConnectionPoolConnectRetry:
    def test_transient_error_retried_then_succeeds(self):
        """A transient ConnectError is retried and the retry's success is used."""
        dev = _make_dev()
        p = ConnectionPool(connect_retry_delay=0)

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[
                _fail(
                    "Cannot connect to device: Error reading SSH protocol banner",
                    error="ConnectError",
                ),
                _ok(dev),
            ],
        ) as mock_connect:
            with p.acquire("rt1", "/cfg") as d:
                pass

        assert mock_connect.call_count == 2
        assert d is dev

    def test_connect_timeout_error_is_retryable(self):
        """ConnectTimeoutError is treated as transient and retried."""
        dev = _make_dev()
        p = ConnectionPool(connect_retry_delay=0)

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[
                _fail("Connection timeout", error="ConnectTimeoutError"),
                _ok(dev),
            ],
        ) as mock_connect:
            with p.acquire("rt1", "/cfg") as d:
                pass

        assert mock_connect.call_count == 2
        assert d is dev

    def test_auth_error_not_retried(self):
        """ConnectAuthError is permanent: fail immediately without a retry."""
        p = ConnectionPool(connect_retry_delay=0)

        with patch(
            "junos_mcp.pool.common.connect",
            return_value=_fail(
                "Authentication credentials fail to login", error="ConnectAuthError"
            ),
        ) as mock_connect:
            with pytest.raises(PoolConnectionError, match="Authentication"):
                with p.acquire("rt1", "/cfg"):
                    pass

        mock_connect.assert_called_once()

    def test_refused_error_not_retried(self):
        """ConnectRefusedError (NETCONF disabled) is permanent: no retry."""
        p = ConnectionPool(connect_retry_delay=0)

        with patch(
            "junos_mcp.pool.common.connect",
            return_value=_fail("NETCONF Connection refused", error="ConnectRefusedError"),
        ) as mock_connect:
            with pytest.raises(PoolConnectionError):
                with p.acquire("rt1", "/cfg"):
                    pass

        mock_connect.assert_called_once()

    def test_retries_exhausted_raises(self):
        """When every attempt fails transiently, PoolConnectionError is raised."""
        p = ConnectionPool(connect_attempts=2, connect_retry_delay=0)

        with patch(
            "junos_mcp.pool.common.connect",
            return_value=_fail(
                "Error reading SSH protocol banner", error="ConnectError"
            ),
        ) as mock_connect:
            with pytest.raises(PoolConnectionError, match="banner"):
                with p.acquire("rt1", "/cfg"):
                    pass

        assert mock_connect.call_count == 2

    def test_attempts_one_disables_retry(self):
        """connect_attempts=1 makes a transient failure fail on the first try."""
        p = ConnectionPool(connect_attempts=1, connect_retry_delay=0)

        with patch(
            "junos_mcp.pool.common.connect",
            return_value=_fail(
                "Error reading SSH protocol banner", error="ConnectError"
            ),
        ) as mock_connect:
            with pytest.raises(PoolConnectionError):
                with p.acquire("rt1", "/cfg"):
                    pass

        mock_connect.assert_called_once()

    def test_attempts_floor_of_one(self):
        """connect_attempts below 1 is clamped to a single attempt."""
        p = ConnectionPool(connect_attempts=0)
        assert p._connect_attempts == 1

    def test_retry_sleeps_between_attempts(self):
        """A retry waits connect_retry_delay seconds before the next attempt."""
        dev = _make_dev()
        p = ConnectionPool(connect_attempts=2, connect_retry_delay=0.5)

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[_fail("banner", error="ConnectError"), _ok(dev)],
        ):
            with patch("junos_mcp.pool.time.sleep") as mock_sleep:
                with p.acquire("rt1", "/cfg") as d:
                    pass

        # Slept exactly once (before the retry), never after the final attempt.
        mock_sleep.assert_called_once_with(0.5)
        assert d is dev

    def test_permanent_error_after_transient_stops_retrying(self):
        """A permanent error on a later attempt stops the loop immediately."""
        p = ConnectionPool(connect_attempts=3, connect_retry_delay=0)

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[
                _fail("Error reading SSH protocol banner", error="ConnectError"),
                _fail(
                    "Authentication credentials fail to login",
                    error="ConnectAuthError",
                ),
                _ok(_make_dev()),  # must never be consumed
            ],
        ) as mock_connect:
            with pytest.raises(PoolConnectionError, match="Authentication"):
                with p.acquire("rt1", "/cfg"):
                    pass

        assert mock_connect.call_count == 2

    def test_retry_logs_warning_then_recovery_info(self, caplog):
        """A retried-then-recovered connect logs a WARNING and a recovery INFO."""
        dev = _make_dev()
        p = ConnectionPool(connect_retry_delay=0)

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[
                _fail("Error reading SSH protocol banner", error="ConnectError"),
                _ok(dev),
            ],
        ):
            with caplog.at_level(logging.INFO, logger="junos_mcp.pool"):
                with p.acquire("rt1", "/cfg"):
                    pass

        messages = [r.getMessage() for r in caplog.records]
        assert any("transient connect failure for rt1" in m for m in messages)
        assert any("recovered" in m for m in messages)

    def test_first_try_success_logs_nothing_and_no_sleep(self, caplog):
        """A clean first-attempt connect emits no retry log and never sleeps."""
        dev = _make_dev()
        p = ConnectionPool()  # default 1.0s delay — must not be hit

        with patch("junos_mcp.pool.common.connect", return_value=_ok(dev)):
            with patch("junos_mcp.pool.time.sleep") as mock_sleep:
                with caplog.at_level(logging.INFO, logger="junos_mcp.pool"):
                    with p.acquire("rt1", "/cfg"):
                        pass

        mock_sleep.assert_not_called()
        assert caplog.records == []


class TestConnectionPoolCloseAll:
    def test_close_all_closes_every_device(self):
        """close_all() closes all pooled connections and empties entries."""
        dev1 = _make_dev()
        dev2 = _make_dev()
        p = ConnectionPool(idle_timeout=60)

        with patch(
            "junos_mcp.pool.common.connect",
            side_effect=[_ok(dev1), _ok(dev2)],
        ):
            with p.acquire("rt1", "/cfg"):
                pass
            with p.acquire("rt2", "/cfg"):
                pass

        p.close_all()

        dev1.close.assert_called_once()
        dev2.close.assert_called_once()
        assert len(p._entries) == 0

    def test_close_all_tolerates_close_error(self):
        """close_all() continues even if dev.close() raises."""
        dev = _make_dev()
        dev.close.side_effect = Exception("already closed")
        p = ConnectionPool(idle_timeout=60)

        with patch("junos_mcp.pool.common.connect", return_value=_ok(dev)):
            with p.acquire("rt1", "/cfg"):
                pass

        p.close_all()  # should not raise
        assert len(p._entries) == 0

    def test_close_all_closes_devices_concurrently(self):
        """close_all() overlaps device closes instead of running them one at a time.

        Each mock ``close()`` blocks for ``sleep_s``. ``_close_dev`` swallows any
        exception a close() raises, so a serial-vs-parallel regression can't be
        caught by making close() fail on contention — it has to be caught by
        wall-clock time instead: serial closing of ``n`` devices takes roughly
        ``n * sleep_s``, while overlapping them keeps the total close to one
        ``sleep_s`` plus scheduling overhead.
        """
        n = 8
        sleep_s = 0.2
        devs = [_make_dev() for _ in range(n)]
        for dev in devs:
            dev.close.side_effect = lambda: time.sleep(sleep_s)
        p = ConnectionPool(idle_timeout=60)

        with patch("junos_mcp.pool.common.connect", side_effect=[_ok(d) for d in devs]):
            for i, dev in enumerate(devs):
                with p.acquire(f"rt{i}", "/cfg"):
                    pass

        start = time.monotonic()
        p.close_all()
        elapsed = time.monotonic() - start

        for dev in devs:
            dev.close.assert_called_once()
        assert len(p._entries) == 0
        # Serial would take ~n * sleep_s (1.6s here); overlapping stays well under half that.
        assert elapsed < sleep_s * (n / 2)

    def test_close_all_on_empty_pool_is_a_no_op(self):
        """close_all() with nothing pooled doesn't spin up a thread pool."""
        p = ConnectionPool(idle_timeout=60)
        p.close_all()  # should not raise
        assert len(p._entries) == 0

    def test_concurrent_acquire_during_close_all_does_not_survive_it(self):
        """A fresh acquire() started while close_all() is still closing devices
        must not leave an entry behind once close_all() returns.

        close_all() holds ``self._lock`` for its whole call (not just around
        clearing ``_entries``), so a concurrent ``acquire()`` for a different
        host has to wait for the in-flight (slow) close to finish before it
        can even create its own entry. If that guarantee regressed, the
        second acquire() would return almost immediately (racing the close)
        and its entry would still be in ``_entries`` afterwards.
        """
        sleep_s = 0.2
        dev1 = _make_dev()
        dev1.close.side_effect = lambda: time.sleep(sleep_s)
        dev2 = _make_dev()
        p = ConnectionPool(idle_timeout=60)

        with patch("junos_mcp.pool.common.connect", side_effect=[_ok(dev1), _ok(dev2)]):
            with p.acquire("rt1", "/cfg"):
                pass

            close_all_started = threading.Event()

            def _run_close_all():
                close_all_started.set()
                p.close_all()

            closer = threading.Thread(target=_run_close_all)
            closer.start()
            close_all_started.wait()
            time.sleep(sleep_s / 4)  # let close_all() acquire self._lock and start closing rt1

            start = time.monotonic()
            with p.acquire("rt2", "/cfg"):
                pass
            elapsed = time.monotonic() - start

        closer.join()

        # The second acquire() had to wait out most of rt1's close before it
        # could even look up self._entries, because close_all() held the lock
        # the whole time -- not just released it once rt1's close began.
        assert elapsed > sleep_s / 2
        assert list(p._entries.keys()) == [("rt2", "/cfg")]


# ---------------------------------------------------------------------------
# get_pool() module-level helper
# ---------------------------------------------------------------------------


class TestGetPool:
    def test_returns_pool_by_default(self, monkeypatch):
        """get_pool() returns a ConnectionPool when JUNOS_MCP_POOL is unset."""
        monkeypatch.delenv("JUNOS_MCP_POOL", raising=False)
        result = pool_module.get_pool()
        assert isinstance(result, ConnectionPool)

    def test_disabled_by_env_var(self, monkeypatch):
        """JUNOS_MCP_POOL=0 makes get_pool() return None."""
        monkeypatch.setenv("JUNOS_MCP_POOL", "0")
        result = pool_module.get_pool()
        assert result is None

    def test_singleton_returned_on_repeated_calls(self, monkeypatch):
        """Multiple get_pool() calls return the same object."""
        monkeypatch.delenv("JUNOS_MCP_POOL", raising=False)
        p1 = pool_module.get_pool()
        p2 = pool_module.get_pool()
        assert p1 is p2

    def test_idle_timeout_from_env(self, monkeypatch):
        """JUNOS_MCP_POOL_IDLE configures the idle timeout."""
        monkeypatch.delenv("JUNOS_MCP_POOL", raising=False)
        monkeypatch.setenv("JUNOS_MCP_POOL_IDLE", "120")
        pool = pool_module.get_pool()
        assert pool._idle_timeout == 120.0

    def test_connect_retry_config_from_env(self, monkeypatch):
        """JUNOS_MCP_POOL_CONNECT_ATTEMPTS / _DELAY configure retry behaviour."""
        monkeypatch.delenv("JUNOS_MCP_POOL", raising=False)
        monkeypatch.setenv("JUNOS_MCP_POOL_CONNECT_ATTEMPTS", "3")
        monkeypatch.setenv("JUNOS_MCP_POOL_CONNECT_DELAY", "0.5")
        pool = pool_module.get_pool()
        assert pool._connect_attempts == 3
        assert pool._connect_retry_delay == 0.5

    def test_connect_retry_defaults(self, monkeypatch):
        """Retry defaults to two attempts (one retry) when env is unset."""
        monkeypatch.delenv("JUNOS_MCP_POOL", raising=False)
        monkeypatch.delenv("JUNOS_MCP_POOL_CONNECT_ATTEMPTS", raising=False)
        monkeypatch.delenv("JUNOS_MCP_POOL_CONNECT_DELAY", raising=False)
        pool = pool_module.get_pool()
        assert pool._connect_attempts == 2
        assert pool._connect_retry_delay == 1.0
