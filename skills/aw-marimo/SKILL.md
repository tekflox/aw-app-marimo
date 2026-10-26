---
name: aw-marimo
description: >-
  Where this workspace's Marimo notebooks live on disk, how to open the app,
  what does and does not survive a container recreate, and the two settings
  that must never be flipped on it. Use when asked to open, find, create or
  reason about a Marimo notebook in this workspace, or before changing
  anything about how the Marimo app is exposed.
---

# Marimo in this workspace

Marimo is a **reactive** Python notebook: change a cell and every cell that
depends on it re-runs automatically, so the notebook can never display a
stale value. It runs here as a Tier-2 container app (`marimo`), in `edit`
mode, reached only through the workspace's own authenticated reverse proxy.

This skill is documentation only — **this app contributes no MCP server and
no agent tools.** You cannot execute a notebook cell from an agent session;
a human drives the UI.

## Opening it

Apps grid → **Marimo**. That window loads the app's own per-app subdomain
(`https://marimo.app.<workspace>.workspace.<apex>/`) at its root, which is
marimo's notebook file browser over the notebooks directory below. The
secondary entry `/api/apps/marimo/` works too (handy for `curl`), but it is
not what the window uses.

Both entries sit behind the workspace's `IdentityGuard`, so both require
workspace authentication.

## Where notebooks live

| | |
|---|---|
| Inside the container | `/data/notebooks` |
| On the workspace host | `/opt/aw-workspace/.aw-workspace/data/marimo/notebooks` |
| Marimo's own user config | `/data/home` → `…/data/marimo/home` |

That host path is `$AW_APP_DATA` for app id `marimo`, resolved by the
framework — it is not configured anywhere and must not be hand-rolled.

Notebooks are **plain `.py` files**, so you can read, grep, diff and
version them with ordinary tools from a workspace shell. One caveat: files
the container creates are owned by the uid podman runs as, which is not
necessarily the uid of a workspace-container shell — reading is fine,
editing a notebook in place from a shell may hit permissions. The UI is the
editor.

## What survives a container recreate

Survives (an app update, a `restart`, a reinstall, a workspace redeploy):

- every notebook under `/data/notebooks`;
- marimo's user config under `/data/home` — theme, keymap, AI settings.

**Does NOT survive: packages installed with `!pip install` inside a notebook
cell.** Those land in the container's own filesystem layer, which is thrown
away on every recreate. This is deliberate scope, not an oversight. The
sanctioned way to make a notebook reproducible is marimo's **inline script
dependencies** (PEP 723) — declare the packages in the notebook's own header
and marimo materialises them with `uv`, which is baked into the image. For
something every notebook needs, add it to the image's `pip install` line in
`container/Dockerfile` and release a new version.

## Two settings that must never be flipped on this app

Marimo runs with `--no-token`, i.e. **its own authentication is off by
design**, because the workspace authenticates every request before the proxy
and the WebSocket bridge cannot carry marimo's auth cookie anyway. The
consequence is that the workspace's authentication is the *only* thing in
front of a surface that executes arbitrary Python.

So, in this app's Settings gear:

- **"Authentication required" must stay ON.** Turning it off takes effect
  immediately, with no restart, and leaves marimo reachable with **no
  credential at all** on a public subdomain. This exact flag, flipped on a
  different app to fix a login bounce, is how unauthenticated OTLP ingest
  was once exposed to the open internet here.
- **"Public" must stay at its default.** Same reasoning.

Likewise, do not add a published host port (`runtime.publish`), a Caddy
host, a tunnel or a share link. If direct exposure is ever genuinely needed,
marimo's token has to come back first — and that needs the workspace's
WebSocket bridge to forward headers, which is a core change, not an app
change.
