"""Tests for junos_mcp.pool — ConnectionPool behaviour."""

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


def _fail(msg="auth failed"):
    return {"ok": False, "error_message": msg}


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
