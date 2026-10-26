# Marimo for aw-workspace — a reactive Python notebook editor, Tier-2.
#
# The runtime command is baked into ENTRYPOINT rather than passed as
# `docker run` args, because aw-workspace's ContainerSupervisor only ever
# runs `docker run <image>` with no extra args of its own (same reason as
# aw-app-code-server's Dockerfile says it).
#
# Published by .github/workflows/build.yml as
# ghcr.io/tekflox/aw-app-marimo:latest.
#
# One-time manual step after the FIRST image push: GitHub creates the
# container package PRIVATE regardless of this repo's visibility, and there
# is no REST/GraphQL endpoint to flip it (confirmed on aw-app-browser,
# 2026-07-28). Until someone with org admin does it by hand at
#   github.com/orgs/tekflox/packages/container/aw-app-marimo/settings
#   -> Danger Zone -> Change visibility -> Public
# the workspace's tokenless pull fails and the install reads as a broken
# image rather than as a missing click.
FROM python:3.12-slim

# marimo is PINNED. The image tag is `:latest` (fleet convention), so the
# only thing that makes a marimo upgrade a deliberate, reviewable act is
# this pin — bump it in a commit, never let the build float.
ARG MARIMO_VERSION=0.25.2

# The MCP SDK the agent-facing sidecar runs on, pinned for the same reason
# marimo is: `:latest` floats, so a deliberate commit is the only thing that
# makes an SDK upgrade reviewable. Held below 2.0 — that release moved and
# renamed FastMCP (mcp==2.0.0 has no `mcp.server.fastmcp`), the same ceiling
# aw-mcp-gateway pins itself to.
ARG MCP_SDK_VERSION=1.30.0

# uv is what makes marimo's inline script dependencies (PEP 723) work: a
# notebook can declare its own packages in a header comment and marimo asks
# uv to materialise them. That is the sanctioned reproducibility path here,
# because pip installs run inside a notebook cell land in the container
# layer and die on the next recreate (README: "What survives a recreate").
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /usr/local/bin/

# procps, for `ps`/`pgrep`. Not a convenience: the acceptance criterion for
# execute's timeout is that no orphan process survives it, and the only way
# to check that is the container's own process table — `python:3.12-slim`
# ships neither tool, so without this the bound can only be taken on the
# tool response's word, which is exactly what the criterion refuses.
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "marimo==${MARIMO_VERSION}" "mcp==${MCP_SDK_VERSION}"

COPY entrypoint.sh /aw-entrypoint.sh
COPY mcp_server.py /aw-mcp-server.py
RUN chmod +x /aw-entrypoint.sh

# Documentation only. The supervisor never publishes this port to the host:
# the app is reached exclusively through the workspace's authenticated
# reverse proxy, and that is load-bearing — see the README's auth section.
EXPOSE 2718

# The agent-facing MCP sidecar, reached by the workspace's MCP gateway at
# http://aw-app-marimo:2719/mcp over the internal container network. Also
# documentation only — publishing it would put an unauthenticated
# code-execution surface on the host, same objection as 2718.
EXPOSE 2719

# Marimo's own health endpoint, probed in the probe this app was designed
# from: GET /api/status -> 200 {"status":"healthy","mode":"edit",...}.
# urllib rather than curl so the image stays slim (python is already here).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:2718/api/status', timeout=4).status == 200 else 1)"]

# Runs as root deliberately. Under aw-workspace's ROOTLESS podman, container
# uid 0 maps to the host uid podman itself runs as — the same uid that owns
# the $AW_APP_DATA host directory bind-mounted onto /data. A non-zero
# container uid maps to an unrelated subordinate host uid that cannot write
# there, which comes up as a container that silently fails to start (seen
# live on aw-app-code-server with `USER coder`).
ENTRYPOINT ["/aw-entrypoint.sh"]
