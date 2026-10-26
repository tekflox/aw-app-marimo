"""The scope constraints of this app, as assertions.

`validate_manifest.py` checks that the manifest is *structurally* valid. This
file checks that it still says what this app was deliberately scoped to say —
so widening it fails CI instead of passing review.

Every assertion here traces to a decision, not a preference:

* **id / the single `$AW_APP_DATA` volume** — `id: "marimo"` is what makes the
  volume resolve to `/opt/aw-workspace/.aw-workspace/data/marimo`
  (`src/apps/runtime.py`, `paths.workspace_home()/data/<manifest.id>`). Rename
  the app and every existing notebook silently stops being found.
* **exactly two permissions** — minimal-capability ruling on the card. A third
  one is a decision for a human, so it has to break the build.
* **no published port, no host power, no privileged run flags** — the app runs
  `marimo --no-token`, so the workspace's own authentication is the *only*
  thing in front of an arbitrary-Python-execution surface. Publishing a port
  routes around it.
* **no `auth_required` / `public` in our own `config_schema`** — the framework
  injects both for a managed app with safe defaults
  (`src/apps/manifest.py` `effective_config_schema`), and app-declared
  properties WIN that merge. Shipping either key here would let this app
  override the framework's safe default from its own manifest.
* **the entrypoint keeps `--no-token`/`--headless` and no `--base-url`** — the
  WebSocket bridge forwards no cookies, so token auth breaks cell execution,
  and a base-url breaks the subdomain-root entry the window actually loads.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((ROOT / "aw-app.json").read_text())


@pytest.fixture(scope="module")
def entrypoint() -> str:
    return (ROOT / "container" / "entrypoint.sh").read_text()


@pytest.fixture(scope="module")
def entrypoint_code(entrypoint) -> str:
    """The entrypoint with comment lines dropped.

    The comments explain at length why there is no `--base-url` and why the
    token is off, so an assertion about a flag's *absence* has to read the
    code, not the prose that names it.
    """
    return "\n".join(
        line for line in entrypoint.splitlines()
        if not line.lstrip().startswith("#")
    )


def test_app_id_is_marimo(manifest):
    assert manifest["id"] == "marimo"


def test_is_a_container_app(manifest):
    assert manifest["tier"] == "container"


def test_permissions_are_exactly_the_two_granted(manifest):
    assert set(manifest["permissions"]) == {"containers:manage", "fs:workspace-data"}
    assert len(manifest["permissions"]) == 2


def test_exactly_one_volume_and_it_is_the_app_data_dir(manifest):
    volumes = manifest["runtime"]["volumes"]
    assert len(volumes) == 1
    (volume,) = volumes
    assert volume["source"] == "$AW_APP_DATA"
    assert volume["mode"] == "rw"
    assert volume["reason"].strip()


def test_no_workspace_tree_is_mounted(manifest):
    sources = {v["source"] for v in manifest["runtime"]["volumes"]}
    for forbidden in (
        "$AW_WORKSPACE_ROOT",
        "$AW_WORKSPACE_REPOS",
        "$AW_WORKSPACE_FOLDERS",
        "$AW_MCP_JSON",
    ):
        assert not any(s == forbidden or s.startswith(forbidden + "/") for s in sources)


def test_nothing_is_published_to_the_host(manifest):
    assert "publish" not in manifest["runtime"]


def test_no_elevated_host_access(manifest):
    runtime = manifest["runtime"]
    assert "host_power" not in runtime
    assert "host_power_optional" not in runtime
    flags = " ".join(runtime.get("run_flags_needed", []))
    assert "--privileged" not in flags
    assert "-p" not in flags.split()
    assert "--publish" not in flags


def test_the_window_is_a_managed_app_at_the_subdomain_root(manifest):
    (window,) = manifest["contributes"]["windows"]
    assert window["body"] == {"type": "managed_app", "kind": "web", "path": "/"}


def test_config_schema_does_not_override_the_framework_auth_toggles(manifest):
    """The app must not ship its own default for either framework key.

    `effective_config_schema` merges as ``{**framework_props, **app_props}``,
    so an app-declared `auth_required: {default: false}` would beat the
    framework's safe `True` with nobody reviewing it.
    """
    properties = manifest["config_schema"]["properties"]
    assert "auth_required" not in properties
    assert "public" not in properties


def test_the_port_agrees_across_manifest_and_container(manifest):
    port = manifest["runtime"]["port"]
    assert port == 2718
    assert f"EXPOSE {port}" in (ROOT / "container" / "Dockerfile").read_text()


def test_entrypoint_runs_marimo_headless_without_a_token(entrypoint_code):
    assert "marimo edit" in entrypoint_code
    assert "--no-token" in entrypoint_code
    assert "--headless" in entrypoint_code
    assert "--host 0.0.0.0" in entrypoint_code


def test_entrypoint_does_not_set_a_base_url(entrypoint_code):
    assert "--base-url" not in entrypoint_code


def test_entrypoint_does_not_force_the_sandbox_either_way(entrypoint_code):
    """`--no-sandbox` is a sanctioned fallback, not the shipped default."""
    assert "--sandbox" not in entrypoint_code
    assert "--no-sandbox" not in entrypoint_code


def test_entrypoint_makes_home_writable_inside_the_volume(entrypoint_code):
    assert 'export HOME="$DATA_DIR/home"' in entrypoint_code


def test_marimo_version_is_pinned(manifest):
    dockerfile = (ROOT / "container" / "Dockerfile").read_text()
    assert "ARG MARIMO_VERSION=" in dockerfile
    assert 'pip install --no-cache-dir "marimo==${MARIMO_VERSION}"' in dockerfile


# --------------------------------------------------------------------------
# The MCP contribution
#
# `contributes.mcp` is DOCUMENTATION — the gateway discovers upstreams only
# by scanning `apps/<slug>/mcp.json` on disk. Declaring tools in the
# manifest and shipping no mcp.json is what left aw-app-remote-host-cli with
# 16 advertised tools and zero served, so these two have to be checked
# against each other, not just individually.
# --------------------------------------------------------------------------

TOOLS = {
    "list_notebooks",
    "read_notebook",
    "create_notebook",
    "update_notebook",
    "validate_notebook",
    "execute_notebook",
}


@pytest.fixture(scope="module")
def mcp_json() -> dict:
    return json.loads((ROOT / "mcp.json").read_text())


@pytest.fixture(scope="module")
def sidecar() -> str:
    return (ROOT / "container" / "mcp_server.py").read_text()


def test_mcp_json_registers_exactly_one_server_named_marimo(mcp_json):
    """The key is the caller-facing prefix: `marimo` -> `aw__marimo__*`."""
    assert list(mcp_json["mcpServers"]) == ["marimo"]


def test_mcp_json_points_at_the_container_over_the_internal_network(mcp_json):
    server = mcp_json["mcpServers"]["marimo"]
    assert server["enabled"] is True
    assert server["type"] == "http"
    # The STABLE container name, not an ephemeral container-id hostname: a
    # cached dead id-hostname is the documented zombie-upstream failure.
    assert server["url"] == "http://aw-app-marimo:2719/mcp"


def test_mcp_json_carries_no_credential(mcp_json):
    """This repo is public, so anything committed here is public."""
    server = mcp_json["mcpServers"]["marimo"]
    assert set(server) == {"enabled", "type", "url"}


def test_manifest_declares_the_gateway_as_a_required_dependency(manifest):
    (dependency,) = manifest["dependencies"]["apps"]
    assert dependency["id"] == "mcp-gateway"
    assert dependency["required"] is True
    assert dependency["reason"].strip()


def test_manifest_advertises_exactly_the_tools_the_sidecar_defines(manifest, sidecar):
    declared = manifest["contributes"]["mcp"]["provides"]
    assert set(declared) == TOOLS
    assert len(declared) == len(TOOLS)
    for tool in TOOLS:
        assert f"def {tool}(" in sidecar


def test_the_sidecar_port_agrees_across_mcp_json_dockerfile_and_source(mcp_json, sidecar):
    assert "2719" in mcp_json["mcpServers"]["marimo"]["url"]
    assert "EXPOSE 2719" in (ROOT / "container" / "Dockerfile").read_text()
    assert 'os.environ.get("MARIMO_MCP_PORT", "2719")' in sidecar


def test_the_sidecar_is_baked_into_the_image(sidecar):
    """A Tier-2 update RECREATES the container, so anything pip-installed
    into a running container is gone. The sidecar has to be in the image."""
    dockerfile = (ROOT / "container" / "Dockerfile").read_text()
    assert "COPY mcp_server.py /aw-mcp-server.py" in dockerfile
    assert "ARG MCP_SDK_VERSION=" in dockerfile
    assert '"mcp==${MCP_SDK_VERSION}"' in dockerfile


def test_the_sidecar_cannot_take_the_notebook_ui_down(entrypoint_code):
    """marimo stays the exec'd foreground process; the sidecar is a
    background restart loop. Coupled fate would turn a sidecar bug into a UI
    outage, which the card calls a regression."""
    assert "python /aw-mcp-server.py || true" in entrypoint_code
    assert entrypoint_code.index("/aw-mcp-server.py") < entrypoint_code.index("exec marimo edit")
    assert "exec marimo edit" in entrypoint_code
    assert "exec python /aw-mcp-server.py" not in entrypoint_code


def test_the_healthcheck_still_probes_only_the_notebook_ui(manifest):
    """Probing 2719 would recycle the container on a sidecar failure,
    inverting the decoupling above."""
    dockerfile = (ROOT / "container" / "Dockerfile").read_text()
    healthcheck = [line for line in dockerfile.splitlines() if "127.0.0.1:" in line]
    assert healthcheck and all("127.0.0.1:2718" in line for line in healthcheck)
    assert "2719" not in "".join(healthcheck)


def test_the_mcp_port_is_still_not_published_to_the_host(manifest):
    """Internal network only — §6 forbids a new host port outright."""
    assert "publish" not in manifest["runtime"]
    assert manifest["runtime"]["port"] == 2718
