"""Guard: all version strings in the repo must agree with ``junos_mcp.__version__``.

``junos_mcp/__init__.py`` is the single source of truth for the
package version. Two other locations carry a copy that must be kept
in sync:

1. ``pyproject.toml`` — already reads ``junos_mcp.__version__``
   dynamically via ``dynamic = ["version"]`` + ``[tool.setuptools.dynamic]
   version = {attr = "junos_mcp.__version__"}``, so no extra assertion
   is needed there.

2. ``server.json`` — the MCP Registry metadata file. It carries the
   version twice (top-level ``version`` and ``packages[0].version``).
   The release workflow (``.github/workflows/release.yml``) patches both
   fields from the git tag at publish time, but the committed copy
   still has to agree with ``__init__.py`` so that:

   - local ``mcp-publisher validate`` works against an accurate file,
   - readers of the repo see the same version everywhere,
   - a forgotten bump is caught by CI instead of silently diverging.

If this test fails after bumping ``__init__.py``, update ``server.json``
to match (both the top-level ``version`` and ``packages[0].version``).
"""

import json
from pathlib import Path

import junos_mcp

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_server_json_version_matches_package():
    """server.json's version fields must match junos_mcp.__version__."""
    with (REPO_ROOT / "server.json").open() as f:
        data = json.load(f)

    expected = junos_mcp.__version__

    assert data["version"] == expected, (
        f"server.json top-level version ({data['version']!r}) "
        f"does not match junos_mcp.__version__ ({expected!r}). "
        f"Bump server.json when you bump junos_mcp/__init__.py."
    )

    packages = data.get("packages", [])
    assert packages, "server.json is missing the 'packages' array"
    pkg_version = packages[0].get("version")
    assert pkg_version == expected, (
        f"server.json packages[0].version ({pkg_version!r}) "
        f"does not match junos_mcp.__version__ ({expected!r}). "
        f"Both version fields in server.json must be kept in sync."
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
