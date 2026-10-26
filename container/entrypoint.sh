#!/usr/bin/env sh
# Prepare /data, then exec marimo in edit mode.
#
# Everything here has to be safe on a /data that is a fresh empty volume AND
# on one carried over from a previous install — so each step creates what is
# missing and leaves what is already there alone.
set -e

DATA_DIR="${MARIMO_DATA_DIR:-/data}"
PORT="${MARIMO_PORT:-2718}"

# HOME must exist and be writable BEFORE marimo starts: it writes its user
# config at startup and warns (repeatedly) on an unwritable home. Keeping it
# inside the volume is also what makes theme/keymap/AI settings survive a
# container recreate — root's default /root would not.
mkdir -p "$DATA_DIR/notebooks" "$DATA_DIR/home"
export HOME="$DATA_DIR/home"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_DATA_HOME="$HOME/.local/share"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"

# --no-token is REQUIRED, not a convenience. Marimo's session auth is
# cookie-based, and the workspace's WebSocket bridge (src/apps/proxy.py)
# dials upstream with a bare connect that forwards no cookies and no
# headers — so with a token enabled, HTTP would authenticate while every
# cell-execution WebSocket failed, i.e. the app would render and then hang
# on Run. Authentication is the workspace's job and happens before the
# proxy (IdentityGuard, src/apps/runtime.py). See the README: if a host
# port is ever published, or the "Authentication required" toggle is ever
# turned off, this app has NO authentication left in front of a
# remote-code-execution surface.
#
# No --base-url: the Apps-grid window loads this container at the ROOT of
# its own per-app subdomain, and the /api/apps/marimo path mount has its
# prefix stripped before the request arrives. Marimo's asset URLs are
# relative, so root is correct on both entries and a base-url would break
# the one the window actually uses.
#
# No --sandbox/--no-sandbox: the default (prompt in the UI, uv available)
# is the scoped behaviour.

# The agent-facing MCP sidecar, on 2719 (never published to the host). It
# runs on its OWN interpreter ($MCP_VENV): marimo wants mcp>=2.0 for its own
# MCP-client feature and the sidecar needs mcp<2.0 for FastMCP, and sharing
# one site-packages flips marimo's feature gate on with the wrong API — see
# the Dockerfile for the measurement.
#
# It runs in a background restart loop rather than sharing marimo's fate: a
# bug in the sidecar must not be able to take the notebook UI down, because
# an observable change to the human UI is a regression, not a feature. The
# failure stays loud anyway — a sidecar that is permanently broken shows up
# as a dead/parked upstream in the MCP gateway and is named by
# `aw-workspace-cli doctor`.
#
# The HEALTHCHECK deliberately stays on marimo's 2718 only. Probing 2719
# there would recycle the whole container on a sidecar failure, which is the
# exact coupling this loop exists to avoid.
(
  while true; do
    "${MCP_VENV:-/opt/aw-mcp-venv}/bin/python" /aw-mcp-server.py || true
    echo "aw-app-marimo: MCP sidecar exited, restarting in 2s" >&2
    sleep 2
  done
) &

exec marimo edit "$DATA_DIR/notebooks" \
    --headless \
    --host 0.0.0.0 \
    -p "$PORT" \
    --no-token \
    --skip-update-check
