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

## Single user

`marimo edit` assumes one trusted editor. There is no multi-user mode, no
notebook sharing and no `marimo run` publishing mode in this app. Those are
a different architecture, not a setting.

## Repository layout

```
aw-app.json                     the manifest — app id, volume, window, capabilities
container/Dockerfile            python:3.12-slim + pinned marimo + uv + HEALTHCHECK
container/entrypoint.sh         prepares /data, exports HOME, execs marimo edit
skills/aw-marimo/SKILL.md       what an agent needs to know about this app
tests/validate_manifest.py      manifest schema + dangling-reference checks
tests/test_manifest_invariants.py   this app's scope, as assertions
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
