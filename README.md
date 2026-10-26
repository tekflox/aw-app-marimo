# aw-app-marimo

[Marimo](https://marimo.io) as an aw-workspace app. Tier-2 (container): a
reactive Python notebook editor on port **2718**, reverse-proxied by the
workspace, opening as a window from the Apps grid.

| | |
|---|---|
| App id | `marimo` |
| Image | `ghcr.io/tekflox/aw-app-marimo:latest` |
| Container | `aw-app-marimo` |
| Port | 2718 (never published to the host) |
| Data volume | `$AW_APP_DATA` → `/data` |
| Capabilities | `containers:manage`, `fs:workspace-data` |
| Marimo | pinned in `container/Dockerfile` (`ARG MARIMO_VERSION`) |

## What it is

A notebook where the cells form a dependency graph instead of a running
order: edit one cell and everything downstream re-runs on its own, so the
notebook can never show you a value that no longer follows from its inputs.
Notebooks are stored as plain `.py` files, which means they diff, grep and
version like code.

Open it from the Apps grid. The window loads this container at the root of
its own per-app subdomain (`https://marimo.app.<workspace>.workspace.<apex>/`),
which is marimo's file browser over the notebooks directory. The path entry
`/api/apps/marimo/` is mounted as well and works the same way — useful for
`curl` and for health checks — but the window does not use it.

### One quirk of the file browser inside the window

Every link in marimo's own file browser — "Create a new notebook" and each
existing notebook — opens with `target="_blank"`, i.e. in a **new browser
tab**. Inside the app window that is a popup from a cross-origin iframe, so
the browser blocks it and clicking appears to do nothing. The notebook
editor itself works perfectly in the window; it is only the file browser's
links that cannot drive it.

So from the window, use the title bar's **pop-out** button to get marimo in
its own tab when you want to create or switch notebooks, then come back. A
notebook already open in the window keeps working normally. This is marimo's
own markup, not something this app sets, and fixing it would mean patching
marimo's bundle at build time — not done, deliberately, since it would break
silently on every marimo upgrade.

## Where your work lives

| | |
|---|---|
| Notebooks, in the container | `/data/notebooks` |
| Notebooks, on the host | `/opt/aw-workspace/.aw-workspace/data/marimo/notebooks` |
| Marimo's user config | `/data/home` → `…/data/marimo/home` |

The host path is what the framework resolves `$AW_APP_DATA` to for app id
`marimo` (`paths.workspace_home()/data/<id>`, enforced together with the
`fs:workspace-data` permission in aw-workspace's `src/apps/runtime.py`). It
is deliberately not configurable: renaming the app would orphan every
notebook.

`$AW_APP_DATA` lives **outside** the installed package directory, which an
update overwrites wholesale. That is what makes notebooks and settings
survive a container recreate, an app update, an uninstall/reinstall and a
workspace redeploy.

### What does NOT survive: `!pip install` in a cell

Packages installed from inside a notebook cell land in the container's own
filesystem layer and are gone on the next recreate. **This is scope, not a
bug** — persisting a mutable site-packages tree across updates is how an
environment becomes unreproducible.

Two sanctioned alternatives, in order of preference:

1. **Marimo's inline script dependencies (PEP 723).** Declare a notebook's
   packages in its own header and marimo materialises them with
   [`uv`](https://github.com/astral-sh/uv), which is baked into this image
   exactly so this path works. The dependency list then travels with the
   notebook file.
2. **Add it to the image.** For something every notebook needs, add it to
   `pip install` in `container/Dockerfile` and cut a release.

### File ownership on the host

Files the container writes are owned by the uid podman runs as (container
root maps to it under rootless podman — see the Dockerfile's note on why
this app runs as root). A workspace-container shell may therefore be able to
read a notebook but not overwrite it in place. The UI is the editor; this is
tolerated rather than worked around with broad `chmod`.

## Authentication — read this before changing anything about exposure

Marimo runs with **`--no-token`**: its own authentication is off.

That is not a convenience. Marimo's session auth is cookie-based, and the
workspace's WebSocket bridge (`src/apps/proxy.py`) dials upstream with a
bare connect that forwards **no cookies and no headers**. With a token
enabled, HTTP requests would authenticate and every cell-execution
WebSocket would fail — the app would render and then hang on Run. So the
flag is baked into the entrypoint, not exposed as a setting.

What makes that safe is that **the workspace authenticates every request
before it reaches this container**: both the `/api/apps/marimo` path mount
and the `marimo.app.*` host mount are wrapped in the same `IdentityGuard`
(`src/apps/runtime.py`), and no host port is published.

The consequence is that workspace authentication is the **only** line of
defence in front of a surface that executes arbitrary Python. Two ways to
remove it, both of which must not happen:

**1. The Settings gear (the likelier one).** Because this is a managed app,
the framework automatically adds **"Authentication required"** and
**"Public"** to this app's settings panel
(`src/apps/manifest.py`, `effective_config_schema`), and the guard reads
`auth_required` through a live lambda (`src/apps/runtime.py`) — so turning
it off takes effect **immediately, with no restart**. Two clicks, no code
change, no release, and marimo becomes an **unauthenticated
arbitrary-Python-execution endpoint on a public subdomain**, with nothing
behind it because its own token is deliberately off.

This is not hypothetical. The same flag, flipped on another app in this
workspace to fix a login bounce, silently exposed unauthenticated OTLP
ingest to the open internet; it was caught by a credential-free `curl`
against `https://<app>.app.<workspace>.workspace.<apex>` returning 200.
**Leave both toggles at their defaults on this app.**

**2. Publishing a port.** No `runtime.publish`, no Caddy host, no tunnel, no
share link. If direct exposure is ever genuinely needed, marimo's token must
come back **first**, and that requires the workspace's WebSocket bridge to
forward headers — a core change, not an app change.

Other containers on the same podman network can reach this container
without authentication. That is the accepted posture for this workspace's
Tier-2 apps (`code-server` has the same one and is strictly more powerful,
with the whole workspace tree mounted read-write). "Private" here means no
public port and no tunnel.

## Agent tools (MCP)

Alongside the human UI the container runs a second process: a small MCP
server on port **2719**, also never published to the host. The workspace's
MCP gateway reaches it over the internal container network at
`http://aw-app-marimo:2719/mcp` (registered by the committed, credential-free
`mcp.json` at this repo's root) and agents see six tools prefixed
`aw__marimo__`:

| Tool | What it does |
|---|---|
| `list_notebooks` | Every `.py` under `/data/notebooks`, recursively, with size, mtime and sha256 |
| `read_notebook` | One notebook's full source plus its sha256 |
| `create_notebook` | Create a new one. Refuses to overwrite; omitted content writes a minimal marimo skeleton |
| `update_notebook` | Replace content, requiring the sha256 you last read as `expected_hash` |
| `validate_notebook` | Parse and structurally check. **Never imports or runs the file** |
| `execute_notebook` | Run the notebook headless, start to finish, and return stdout/stderr/exit/timing |

All paths are relative to `/data/notebooks` and must end in `.py`. One
resolver guards every one of them, `execute` included: absolute and empty
paths are refused as `invalid_path`, and anything that resolves outside the
root — `../`, or a symlink planted inside the tree pointing out — as
`path_outside_root`. Other codes: `not_found`, `already_exists`,
`not_a_notebook`, `hash_conflict`, `content_too_large`. They arrive as MCP
tool errors whose text is the JSON body, behind FastMCP's own
`Error executing tool <name>: ` prefix.

### Two semantics that will surprise you

- **A cell's trailing display expression is not stdout.** Run as a script, a
  notebook prints only what it actually `print()`s — the rich display value
  marimo renders in the UI is not written anywhere. A notebook meant to hand
  a result back to an agent has to `print()` it.
- **PEP 723 inline dependencies are not materialised.** `execute_notebook`
  runs plain `python <file>`, not `uv run --script`, so a header-declared
  package that is not already in the image comes back as `status: "error"`
  with the ImportError in stderr. The edit UI remains the PEP 723 path; the
  alternative was unbounded network installs inside the timeout budget.

### Concurrent writers: a real limitation, not a bug being hidden

`expected_hash` protects one agent against another. It does **not** protect
against a human with the same notebook open in the Marimo editor. Those are
two independent writers of one file, and marimo's own save can clobber an
MCP write exactly as an MCP write can clobber a human's unsaved buffer.
There is deliberately no locking scheme here. If an agent and a person are
both working a notebook, one side can lose a change.

### The execution threat model

`execute_notebook` runs arbitrary Python by design — that is the feature.
Nobody should claim the *code* is sandboxed. **The container is the
boundary, not the interpreter.** The threat model is "a notebook misbehaves
inside its own container", not "a notebook is prevented from doing anything
unusual".

What *is* bounded:

- **Filesystem** — the container mounts exactly one volume (`$AW_APP_DATA` →
  `/data`), no workspace tree, no container socket, and every tool path
  argument is confined to `/data/notebooks` by the resolver above.
- **Wall time** — default 120s, hard cap 600s (a larger request is clamped
  and the applied value returned). On timeout the run's whole process group
  gets SIGTERM, five seconds, then SIGKILL, so a notebook cannot outlive its
  own timeout and neither can anything it spawned.
- **Output** — stdout and stderr are capped at 64 KiB each, with explicit
  `stdout_truncated` / `stderr_truncated` flags. Nothing is truncated
  silently.
- **Concurrency** — at most two notebooks run at once. A third call is
  refused immediately with `status: "refused_busy"` and the running count,
  rather than queued.
- **Process tree** — each run gets its own session, and its process group is
  reaped when the run ends, on success as well as on timeout. `execute` does
  not leave background jobs behind.

What is **not** bounded: the Python itself. A running notebook can write
anywhere in `/data` (including `/data/home`), use whatever network egress
the container has, and burn its CPU/memory envelope (1 cpu / 1536 MB).

Port 2719 is unauthenticated to peers on the internal network. That adds no
new exposure class: marimo's own 2718 already serves an unauthenticated
(`--no-token`, load-bearing — see the auth section above) code-execution UI
to exactly the same peers. Anything that could abuse 2719 could already
abuse 2718. The boundary in front of both is "no host port published +
workspace network membership", the same posture as `aw-app-kb`.

### How the sidecar is supervised

The sidecar runs on **its own interpreter**, a venv at `/opt/aw-mcp-venv`,
never marimo's site-packages. The two need incompatible majors of the same
package: marimo declares `mcp>=2.0,<3.0` for its own MCP-*client* feature
and switches that feature on the moment `mcp` is importable, while FastMCP
needs `mcp<2.0`. Installing the SDK next to marimo flips that gate on with
the wrong API — measured on the v0.4.0 image: `has()` true, a "Mismatched
version of mcp: expected >=2.0.0, got 1.30.0" warning, and marimo
announcing MCP servers at boot it cannot drive. Notebooks are executed by
the **system** python (`MARIMO_NOTEBOOK_PYTHON`), not the sidecar's, because
that venv has no marimo in it and `import marimo` would otherwise fail.

`entrypoint.sh` starts it in a background restart loop and keeps
`exec marimo edit` as the container's foreground process, so a crash in the
sidecar can never take the notebook UI down. The `HEALTHCHECK` deliberately
stays on marimo's 2718 only — probing 2719 there would recycle the whole
container on a sidecar failure, inverting that decoupling. A permanently
broken sidecar still degrades loudly: the gateway parks the upstream and
`aw-workspace-cli doctor` names it.

## Single user

`marimo edit` assumes one trusted editor. There is no multi-user mode, no
notebook sharing and no `marimo run` publishing mode in this app. Those are
a different architecture, not a setting.

## Repository layout

```
aw-app.json                     the manifest — app id, volume, window, capabilities
mcp.json                        gateway registration for the `marimo` MCP server
container/Dockerfile            python:3.12-slim + pinned marimo + pinned mcp SDK + uv + HEALTHCHECK
container/entrypoint.sh         prepares /data, starts the MCP sidecar, execs marimo edit
container/mcp_server.py         the six agent-facing tools, FastMCP over streamable HTTP
skills/aw-marimo/SKILL.md       what an agent needs to know about this app
tests/validate_manifest.py      manifest schema + dangling-reference checks
tests/test_manifest_invariants.py   this app's scope, as assertions
tests/test_mcp_server.py        the sidecar's bounds, as assertions
docs/architecture/aw-app-marimo.md  the design this was built from
```

## Development

```bash
pip install -r requirements-dev.txt pytest jsonschema
python3 tests/validate_manifest.py aw-app.json   # schema lives in aw-marketplace
pytest tests/ -q
```

To exercise the real thing without the workspace, build and run the image —
note the entrypoint needs `/data` to be writable:

```bash
docker build -t aw-app-marimo:dev container
docker run --rm -p 2718:2718 -v "$PWD/.tmp/data:/data" aw-app-marimo:dev
```

## Releases and updates

- **Never hand-edit `version` in `aw-app.json`.** The shared release
  workflow (`tekflox/aw-marketplace/.github/workflows/app-release.yml`, via
  `.github/workflows/release.yml`) owns every bump, tags it, and opens the
  catalog-sync PR against `tekflox/aw-marketplace`. Re-running a failed
  release job double-bumps.
- **`.github/workflows/build.yml` builds and pushes the image** on every
  push to `master`, including the release bot's own bump commit. A green
  `release.yml` says nothing about the image — they are separate runs, and a
  container app shipping "green" while running old code is the documented
  failure mode in this exact path. Check the ghcr package digest, not the
  release run.
- **The marketplace catalog lags the merged sync PR** by up to ~5 minutes on
  GitHub's raw CDN. Poll `GET /api/apps/-/catalog` (the workspace's own
  view) rather than debugging the installer.
- Install only through the marketplace:
  `aw-workspace-cli marketplace install marimo [--update]`. A sideload gets
  reverted by the reconciler.
- The image tag is `:latest` (fleet convention), so a rebuild can change the
  running app without a version-visible diff. The marimo **version pin** in
  the Dockerfile is what keeps an upgrade deliberate.

## Design

`docs/architecture/aw-app-marimo.md` — including the live probe that settled
whether marimo works behind this proxy (it does, unmodified: no
`X-Frame-Options`, no CSP, relative asset URLs, tolerant of foreign
`Host`/`X-Forwarded-*`, and a WebSocket that reaches `kernel-ready` through
a bare cookie-less connect) and the alternatives that were rejected.
