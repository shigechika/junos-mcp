"""Guard: version strings in the repo must agree with ``junos_mcp.__version__``.

``junos_mcp/__init__.py`` is the single source of truth for the
package version.

1. ``pyproject.toml`` — already reads ``junos_mcp.__version__``
   dynamically via ``dynamic = ["version"]`` + ``[tool.setuptools.dynamic]
   version = {attr = "junos_mcp.__version__"}``, so no extra assertion
   is needed there.

2. ``server.json`` — the MCP Registry metadata file. It carries the
   version twice (top-level ``version`` and ``packages[0].version``),
   but those fields are **placeholders** intentionally pinned to the
   sentinel ``"0.0.0"``. The release workflow
   (``.github/workflows/release.yml``) patches both fields from the git
   tag at publish time, so the committed file never needs to be bumped
   by hand. This test only enforces that the sentinel is present and
   the two fields stay in lockstep.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SENTINEL_VERSION = "0.0.0"


def test_server_json_version_is_sentinel_placeholder():
    """server.json version fields must be the sentinel '0.0.0'.

    The committed server.json never carries a real version — the release
    workflow patches both ``version`` and ``packages[0].version`` from the
    git tag at publish time. Pinning the committed copy to the sentinel
    keeps ``__init__.py`` as the single source of truth and removes the
    hand-sync step from the release checklist.
    """
    with (REPO_ROOT / "server.json").open() as f:
        data = json.load(f)

    assert data["version"] == SENTINEL_VERSION, (
        f"server.json top-level version must be the sentinel "
        f"{SENTINEL_VERSION!r} (got {data['version']!r}). The release "
        f"workflow rewrites it from the git tag at publish time."
    )

    packages = data.get("packages", [])
    assert packages, "server.json is missing the 'packages' array"
    pkg_version = packages[0].get("version")
    assert pkg_version == SENTINEL_VERSION, (
        f"server.json packages[0].version must be the sentinel "
        f"{SENTINEL_VERSION!r} (got {pkg_version!r})."
    )


def test_pyproject_reads_version_from_init():
    """pyproject.toml must use dynamic version sourced from __init__.py.

    This guards against someone accidentally hardcoding a version in
    pyproject.toml, which would reintroduce a third place to update.
    """
    # Prefer tomllib on Python 3.11+; tomli as a fallback.
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]

    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    assert "version" not in project, (
        "pyproject.toml [project] must not hardcode a 'version' key; "
        "it should be listed under 'dynamic' and sourced from "
        "junos_mcp.__version__ via [tool.setuptools.dynamic]."
    )
    assert "version" in project.get("dynamic", []), (
        "pyproject.toml [project].dynamic must include 'version'."
    )

    dynamic_cfg = (
        data.get("tool", {})
        .get("setuptools", {})
        .get("dynamic", {})
        .get("version", {})
    )
    assert dynamic_cfg.get("attr") == "junos_mcp.__version__", (
        "[tool.setuptools.dynamic] version.attr must be "
        "'junos_mcp.__version__' so there is exactly one source of "
        "truth for the package version."
    )
