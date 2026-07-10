"""Tests for junos_mcp.pool — ConnectionPool behaviour."""

import logging
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
