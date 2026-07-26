"""Probe specs for this server's tools — the JUNOS-specific half of the smoke test.

Every registered tool needs an entry here (the harness fails on a tool with no
spec), so adding a tool forces a decision: how would we know it works?

Three constraints shape everything below.

**Read-only.** These tools drive production routers. Everything that can change
one — pushing configuration, copying or installing a package, rolling one back,
scheduling a reboot — is skipped by name and must stay skipped; the test suite
enforces it. Collecting RSI is skipped for a different reason: it changes
nothing, but it is minutes of RE CPU and a file on disk per device, for an
answer no assertion here would read.

**No inventory-specific values in this file.** This repository is public, so a
probe may not name a device or a hardware model. The tools that take a hostname
get one from an ``args_factory`` that reads the configured inventory at run
time, and skip when it is empty.

**Bounded.** The fan-out tools run against every configured device by default;
each probe pins the parallelism and targets a single discovered device instead.

Assertions are shape-first: these tools answer with formatted text and render
their failures as ordinary text rather than raising, so every probe both pins
the shape a working answer has and refuses the ``Error: ...`` /
``Connection error: ...`` lines the tool produces instead. A device that
answers "no differences from rollback 1" is fine; a device that could not be
reached is not.
"""

import re
from typing import Any

from smoke_harness import Caller, Probe, SkipProbe

#: A command every JUNOS device answers and no device is changed by.
SHOW_COMMAND = "show system uptime"

#: One worker: each probe below targets a single discovered device, and pinning
#: the value keeps a scheduled run from inheriting a fan-out sized for an
#: operator sweeping the estate.
MAX_WORKERS = 1


async def _first_host(call: Caller) -> dict[str, Any]:
    """Discover a configured device at run time for the per-host tools."""
    payload = await call("get_router_list", {})
    text = payload if isinstance(payload, str) else str(payload)
    # "Available routers (N):" then one "- <hostname>" per device.
    match = re.search(r"^- (\S.*)$", text, re.MULTILINE)
    if not match:
        raise SkipProbe("no device configured to probe with")
    return {"hostname": match.group(1).strip()}


async def _first_host_as_target(call: Caller) -> dict[str, Any]:
    """The same device, shaped for the tools that take a list of targets."""
    args = await _first_host(call)
    return {"hostnames": [args["hostname"]]}



async def _host_and_model(call: Caller) -> dict[str, Any]:
    """Discover a device and the model string its own facts report."""
    args = await _first_host(call)
    facts = await call("get_device_facts", args)
    match = re.search(r"'model':\s*'([^']+)'", str(facts))
    if not match:
        raise SkipProbe("device facts carried no model to look a package up by")
    return {**args, "model": match.group(1)}

#: Every tool renders a failed connection or an unknown host as text rather
#: than raising, so each probe has to refuse those lines explicitly — otherwise
#: an unreachable device reads as a successful call.
NO_ERROR = (r"^(Error|Config error|Connection error)[ :]",)


PROBES: dict[str, Probe] = {
    # -- server / inventory --------------------------------------------------
    "health_check": Probe(
        require_keys=("status", "service", "config", "router_count"),
        must_match=(r'"config": "ok"', r'"status": "(healthy|degraded)"'),
        # router_count is the one number worth asserting: an inventory that
        # resolved to zero devices would let every probe below skip politely
        # while the server audits nothing.
        min_values={"router_count": 1},
        allow_empty=True,
    ),
    # The inventory every other probe discovers from, so an empty one fails
    # here even though it is only a skip further down.
    "get_router_list": Probe(
        must_match=(r"^Available routers \(\d+\):",),
        must_not_match=(r"^No routers defined in config", *NO_ERROR),
    ),
    # -- pure computation ----------------------------------------------------
    # No device involved: it parses two JUNOS version strings. Cheap, and the
    # ordering rules are subtle enough to be worth exercising with a known
    # answer rather than a discovered one.
    "compare_version": Probe(
        args={"left": "22.4R3-S6.5", "right": "22.4R3-S7"},
        must_match=(r"\S",),
        must_not_match=(r"^Error: invalid version",),
        min_chars=3,
    ),
    # -- per-device reads ----------------------------------------------------
    "get_version": Probe(
        args_factory=_first_host,
        must_match=(r"^# \S",),
        must_not_match=NO_ERROR,
        min_chars=20,
    ),
    "get_device_facts": Probe(
        args_factory=_first_host,
        must_match=(r"^# \S", r"'model'"),
        must_not_match=NO_ERROR,
    ),
    "get_config": Probe(
        args_factory=_first_host,
        args={"output_format": "text"},
        must_match=(r"^# \S",),
        # A running configuration is never a handful of characters; a truncated
        # or empty read would otherwise pass the header check above.
        min_chars=500,
        must_not_match=(*NO_ERROR, r"^# \S+\nError getting config"),
    ),
    "get_config_diff": Probe(
        args_factory=_first_host,
        args={"rollback_id": 1},
        # "No differences" is the expected answer on a device nobody is
        # mid-change on.
        must_match=(r"^# \S",),
        must_not_match=(*NO_ERROR, r"Error getting config diff"),
    ),
    "list_remote_files": Probe(
        args_factory=_first_host,
        must_match=(r"^# \S",),
        must_not_match=NO_ERROR,
    ),
    "check_upgrade_readiness": Probe(
        args_factory=_first_host,
        must_match=(r"\S",),
        must_not_match=NO_ERROR,
        min_chars=20,
    ),
    # -- command execution ---------------------------------------------------
    # Exercised with a show command: these tools accept operational commands in
    # general, and the smoke test must not be the thing that types one that
    # matters.
    "run_show_command": Probe(
        args_factory=_first_host,
        args={"command": SHOW_COMMAND, "output_format": "text"},
        must_match=(r"\S",),
        min_chars=20,
        must_not_match=NO_ERROR,
    ),
    "run_show_commands": Probe(
        args_factory=_first_host,
        args={"commands": [SHOW_COMMAND], "output_format": "text"},
        min_chars=20,
        must_not_match=NO_ERROR,
    ),
    "run_show_command_batch": Probe(
        args_factory=_first_host_as_target,
        args={"command": SHOW_COMMAND, "max_workers": MAX_WORKERS},
        min_chars=20,
        must_not_match=NO_ERROR,
    ),
    # -- fleet checks --------------------------------------------------------
    "check_reachability": Probe(
        args_factory=_first_host_as_target,
        args={"max_workers": MAX_WORKERS},
        must_match=(r"\S",),
        min_chars=20,
        must_not_match=NO_ERROR,
    ),
    "check_remote_packages": Probe(
        args_factory=_first_host_as_target,
        args={"max_workers": MAX_WORKERS},
        min_chars=10,
        must_not_match=NO_ERROR,
    ),
    # Reads the local package directory and the config, not a device: an estate
    # with no <model>.file entries answers with a sentence, which is a real
    # deployment rather than a failure.
    "check_local_inventory": Probe(
        must_match=(r"\S",),
        min_chars=10,
        must_not_match=NO_ERROR,
    ),
    # get_package_info needs a model name as well as a host, and a model is a
    # piece of inventory this public repository must not name. It is derived
    # from the discovered device's own facts instead.
    # Two legitimate answers, and NO_ERROR cannot separate them: a model with no
    # image staged in config.ini is reported with the same "Error:" prefix as a
    # device that could not be reached. So the accepted shapes are named
    # explicitly instead — anything else (an unknown host, a connection
    # failure) matches neither and fails.
    "get_package_info": Probe(
        args_factory=_host_and_model,
        must_match=(r"^# \S.*\nPackage file: |^Error: No option '[^']+\.file'",),
    ),
    # -- morning patrol ------------------------------------------------------
    "daily_brief": Probe(
        args_factory=_first_host_as_target,
        args={"max_workers": MAX_WORKERS, "since_hours": 18},
        must_match=(r"^## daily_brief — ", r"^## \d+ hosts: "),
        must_not_match=NO_ERROR,
        timeout=900,
    ),
    # -- tools that change a device: never exercised -------------------------
    "push_config": Probe(skip="writes configuration to a production router"),
    "copy_package": Probe(skip="copies a multi-gigabyte image to the device"),
    "install_package": Probe(skip="installs a JUNOS image"),
    "rollback_package": Probe(skip="rolls the device back to its pending version"),
    "schedule_reboot": Probe(skip="schedules a reboot of a production router"),
    # -- read-only but far too expensive to repeat daily ---------------------
    "collect_rsi": Probe(skip="minutes of RE CPU and a file per device, for an answer nothing here reads"),
    "collect_rsi_batch": Probe(skip="the same, once per device in the estate"),
}
