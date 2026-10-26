---
name: aw-marimo
description: >-
  The six `aw__marimo__*` MCP tools for listing, reading, creating, updating,
  validating and executing this workspace's Marimo notebooks — with their
  bounds, their two surprising semantics and the threat model behind
  execution — plus where notebooks live on disk, how to open the app, what
  survives a container recreate, and the two settings that must never be
  flipped on it. Use when asked to open, find, create, change or RUN a Marimo
  notebook in this workspace, or before changing anything about how the
  Marimo app is exposed.
---

# Marimo in this workspace

Marimo is a **reactive** Python notebook: change a cell and every cell that
depends on it re-runs automatically, so the notebook can never display a
stale value. It runs here as a Tier-2 container app (`marimo`), in `edit`
mode, reached only through the workspace's own authenticated reverse proxy.

A human drives the UI. An agent drives the same files through the six MCP
tools below — the two are deliberately two doors onto one notebook
directory, so a notebook you write as an agent opens in the human's editor
and vice versa.

## The six tools

Prefix `aw__marimo__` (server key `marimo`, reached by the MCP gateway at
`http://aw-app-marimo:2719/mcp` inside the container network — no host port).

| Tool | Arguments | Returns |
|---|---|---|
| `list_notebooks` | — | `{root, notebooks: [{path, size_bytes, modified_at, sha256}]}` |
| `read_notebook` | `path` | `{path, content, sha256, size_bytes, modified_at}` |
| `create_notebook` | `path`, `content?` | `{path, sha256, created}` |
| `update_notebook` | `path`, `content`, `expected_hash` | `{path, sha256}` |
| `validate_notebook` | `path` | `{valid, errors[], structure{…}}` |
| `execute_notebook` | `path`, `timeout_seconds?` | `{status, exit_code, duration_ms, started_at, timeout_seconds, stdout, stderr, stdout_truncated, stderr_truncated}` |

Paths are **relative to `/data/notebooks`** and must end `.py`. Absolute or
empty paths are refused (`invalid_path`); anything resolving outside the
root, `../` or a symlink pointing out, is refused (`path_outside_root`).
Other codes you will meet: `not_found`, `already_exists`, `not_a_notebook`,
`hash_conflict`, `content_too_large`.

**To change an existing notebook you must pass `expected_hash`** — the
sha256 `read_notebook` just gave you. If it no longer matches, the write is
refused with `hash_conflict` carrying the current hash and the file is left
untouched. Re-read, re-apply, retry. There is no force flag, and
`create_notebook` will not overwrite.

### Three things that catch people out

1. **`print()` or you get nothing.** Executing runs the notebook as a plain
   script, so only `print()` output reaches stdout. A cell's trailing
   display expression — the thing marimo renders beautifully in the UI — is
   evaluated and discarded. If you are running a notebook to get a value
   back, print it.
2. **PEP 723 inline deps are not installed.** Execution is `python <file>`,
   not `uv run --script`. A header-declared package that is not in the image
   comes back as `status: "error"` with the ImportError in stderr. Use the
   edit UI for the PEP 723 path.
3. **A human editing the same notebook can clobber you, and you them.**
   `expected_hash` protects agent against agent only. Marimo's own save and
   an MCP write are two independent writers of one file, with no lock
   between them. If someone has the notebook open, coordinate out of band.

### Bounds on execution, and what they do not cover

`execute_notebook` is batch and headless: it does not attach to, drive or
share state with anyone's open editor session, and there is no cell-level
execution, kernel reuse or streaming output.

Bounded: 120s default timeout / 600s hard cap (larger requests are clamped,
and the applied value comes back in the response); on timeout the run's
whole process group is SIGTERM'd, given 5s, then SIGKILL'd, so nothing is
orphaned; 64 KiB per stream with explicit `*_truncated` flags, never a
silent cut; at most 2 concurrent runs, a third refused immediately with
`status: "refused_busy"` rather than queued; the process group is reaped at
the end of every run, so `execute` leaves no background jobs.

**Not bounded: the Python itself. The container is the boundary, not the
interpreter.** The threat model is "a notebook misbehaves inside its own
container", not "a notebook is prevented from doing anything unusual". A
running notebook can write anywhere in `/data`, use the container's network
egress, and burn its 1 cpu / 1536 MB envelope. What it cannot reach is the
workspace tree: this container mounts exactly one volume and no container
socket. Do not describe these tools to anyone as a sandbox for untrusted
code.

`validate_notebook` is the safe pre-flight: it parses and structurally
checks (marimo import, app assignment, cell count, main guard) and **never
imports or runs the file**, so a notebook with top-level side effects has
none when validated.

## Opening it

Apps grid → **Marimo**. That window loads the app's own per-app subdomain
(`https://marimo.app.<workspace>.workspace.<apex>/`) at its root, which is
marimo's notebook file browser over the notebooks directory below. The
secondary entry `/api/apps/marimo/` works too (handy for `curl`), but it is
not what the window uses.

Both entries sit behind the workspace's `IdentityGuard`, so both require
workspace authentication.

**Known quirk:** marimo's file-browser links (including "Create a new
notebook") are `target="_blank"`. From inside the app window that is a popup
from a cross-origin iframe, so the browser blocks it and the click looks like
it did nothing. Use the window title bar's pop-out button to create or switch
notebooks, then return to the window. A notebook already open in the window
edits and runs normally.

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
