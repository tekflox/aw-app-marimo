# aw-app-marimo — docs

## Why does this exist?

Frederico wanted a reactive Python notebook he could open from this
workspace's Apps grid, with notebooks that are still there tomorrow and
still there after an app update — no token to paste, no port to remember, no
terminal step. It has to be its own app rather than a feature of something
else for one hard reason: a notebook kernel executes arbitrary user Python
in a long-running process, and that is precisely what the Tier-2 container
boundary exists for. Installing marimo into the workspace's own virtualenv
would let notebook dependencies into the venv that runs the workspace.

## What does it do?

- Runs `marimo edit` against a notebooks directory, so the app is an
  **editor**, not a viewer: you create notebooks in it and they save.
- Opens as a window from the Apps grid, behind the workspace's own
  authentication, with no host port published and no public tunnel.
- Keeps every notebook (plain `.py` files) and marimo's own user config on
  the workspace disk at
  `/opt/aw-workspace/.aw-workspace/data/marimo/`, so they survive a
  container recreate, an app update and a reinstall.
- Ships `uv` in the image so a notebook can declare its own dependencies
  inline (PEP 723) — the sanctioned reproducibility path, since cell-level
  `!pip install` does not survive a recreate.

## What it deliberately does not do

No MCP server and no agent tools (a human drives the UI). No access to the
workspace repo tree — the container mounts only its own data directory. No
public exposure, no multi-user or notebook sharing, no `marimo run`
publishing mode, no WASM export, no app database tables.

## Further reading

- [`architecture/aw-app-marimo.md`](architecture/aw-app-marimo.md) — the
  design, the live proxy/WebSocket probe behind it, and the rejected
  alternatives.
- [`../README.md`](../README.md) — install, data paths, the authentication
  model and the release/update mechanics.
- [`../skills/aw-marimo/SKILL.md`](../skills/aw-marimo/SKILL.md) — what an
  agent is told about this app.
