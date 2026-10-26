<!--
This is the architecture record for aw-app-marimo, kept in the repo as the
design it was built from. It is the Architect's document (2026-10-26) moved
here verbatim, plus §9 below: the Product Owner's binding addition on
design acceptance, which the original text predates. §8's probe artifacts
lived in the workspace's scratch dir and are not part of this repo.

Where the shipped code differs from this text, the code is the authority
and the difference is called out in §9.
-->

# aw-app-marimo — Architecture (card 4055bf3b-9510-8122-8147-e22920d011fa)

Architect design, 2026-10-26. PO scope (latest card comment) is binding and
is not re-litigated here. Deliverable for the Coder: a Tier-2 container app,
repo `tekflox/aw-app-marimo`, app id `marimo`, released through the shared
marketplace pipeline and installed via `aw-workspace-cli marketplace install
marimo`.

## 0. The decision in three sentences

Marimo runs in **`edit` mode** against `/data/notebooks` inside a slim Python
image, bound to `0.0.0.0` on container port **2718**, with **`--no-token`**
and **no `--base-url`**, as a standard Tier-2 app mirroring
`aw-app-home-assistant`'s manifest shape (two capabilities, one `$AW_APP_DATA`
volume, a `managed_app` window). No proxy-adaptation shim is needed at all:
I ran marimo 0.25.2 live and probed it the exact way the workspace proxy
drives apps — it is already compatible (evidence in §2). The release pipeline
is the template's `release.yml` plus a push-triggered image `build.yml`
(Home Assistant's pattern), with explicit image-freshness verification steps
so green CI cannot be mistaken for a live image.

## 1. How the proxy actually reaches the app (read this first)

Three facts from the workspace source decide everything else:

- `src/apps/runtime.py:1512-1543` (`_attach_mount`): every Tier-2 app's
  `ContainerReverseProxy` is mounted **twice** behind the same
  `IdentityGuard` — path-mounted at `/api/apps/<id>` *and* host-mounted at
  `Host("<id>.app.{_}")`, i.e. the per-app subdomain
  `marimo.app.<workspace>.<apex>`, where the path passes through **as-is**
  (no prefix).
- The Apps-grid window does **not** use the `/api/apps/marimo` path. A
  `managed_app` window body is translated to an `app_iframe` widget
  (`repos/aw-workspace-ui/src/components/AppWindowBody.jsx:11-32`), and
  `app_iframe` resolves `appSubdomainUrl(app_id)`
  (`AppWindow.jsx:349-357`, `apiBase.js:162-165`) — the browser loads
  **`https://marimo.app.<slug>.workspace.<apex>/`**, root path.
- `src/apps/proxy.py` is a byte-for-byte HTTP passthrough plus a WS bridge.
  Two properties matter: (a) on the path mount, Starlette strips the prefix
  (`_target`, proxy.py:101-113), so the container always sees
  prefix-relative paths on *both* entries; (b) the WS bridge
  (`proxy.py:196-240`) dials upstream with a **bare**
  `websockets.connect(url)` — query string forwarded, but **no cookies, no
  Origin, no other request headers**.

Consequence: marimo serves at the **root** of its own origin inside the
window. `--base-url` must stay at its default (empty). The
`/api/apps/marimo/...` path entry still works for curl/QA because marimo's
asset URLs are relative (probed, §2), but it is the secondary entry, not the
one the window uses.

## 2. The highest-risk unknown, settled empirically (card item 5)

I installed marimo 0.25.2 in `/opt/aw-workspace/.tmp/marimo-probe/` and ran
`marimo edit notebooks --headless --host 0.0.0.0 -p 2718 --no-token
--skip-update-check`, then probed it exactly as the workspace stack would:

| Probe | Result | What it settles |
|---|---|---|
| `GET /` | 200; **no `X-Frame-Options`, no CSP** on the response; `cache-control: private, no-cache` set by marimo itself | The cross-origin iframe (SPA origin → app subdomain) will not be blanked. No HA-style `use_x_frame_options` shim needed. The proxy's no-store override won't even trigger (marimo is explicit about caching). |
| HTML body | every asset reference is **relative** (`./assets/...`, `./favicon.ico`) | Works at subdomain root; also survives the prefix-stripped `/api/apps/marimo` path entry. `--base-url` stays empty. |
| `GET /` with `Host: marimo.app.aw.workspace.aw.tekflox.com`, `X-Forwarded-For: 10.89.0.9`, `X-Forwarded-Proto: https` | 200 | Marimo does not validate Host or forwarded headers. The `aw-app-home-assistant` failure class (400 on unfamiliar `X-Forwarded-*`, `container/ensure_proxy_config.py`) **does not apply** — no config shim is needed. |
| `ws://…/ws?session_id=…&file=probe.py` via bare `websockets.connect` — no Origin, no cookies, mirroring `proxy.py:203` | connects, receives `kernel-ready` with cell ids/codes | Cell execution WS works through the workspace WS bridge. `code-server` (installed, 0.20.0) is the daily-proven precedent for WS through this same bridge; marimo behaves the same. |
| `GET /api/status` | 200 `{"status":"healthy","mode":"edit",...}` | Free health endpoint for the container HEALTHCHECK. |
| `marimo edit --help` | `--host`, `--port`, `--headless`, `--token/--no-token`, `--base-url` (default ""), `--skip-update-check`, `--sandbox/--no-sandbox`, `--watch` all exist in 0.25.2 | The entrypoint flags below are real, not from memory. |

One probe caveat so nobody "fixes" a non-bug: a WS connect with an invalid
`file=` param (`file=new`) is closed abruptly by marimo with a server-side
`RuntimeError` in its log. The real UI never sends that; ignore it.

**Finding for the PO: none needed.** `edit` mode works behind the proxy
as-is. No downgrade, no shim, no base-url gymnastics.

### Why `--no-token` is REQUIRED, not merely acceptable

The PO already ruled token suppression acceptable because `IdentityGuard`
authenticates every request before the proxy (`src/apps/proxy.py` mounted
behind it, `runtime.py:1498-1511`) and no host port is published. The design
adds a harder reason: marimo's session auth is cookie-based, and the WS
bridge **drops cookies** (`proxy.py:203`). With a token enabled, HTTP would
authenticate but every cell-execution WS would 401 — the app would render
and then hang on execution. So `--no-token` is baked into the entrypoint,
not configurable. The inverse dependency must be documented in the README:
**if anyone ever publishes a port or adds `runtime.publish`, the token must
come back and the WS bridge problem must be solved first.**

## 3. The repo and the manifest (card items 1-4)

### Repo

`tekflox/aw-app-marimo`, created by **copying the working tree** of the
latest `repos/aw-app-template` (0.31.1) into a fresh directory, `git init`,
single initial commit — no fork relation, no template history. Must be
**public** at creation (`gh repo create tekflox/aw-app-marimo --public
--source=. --push`); the skill's `--private` example is wrong for
marketplace apps (catalog tarball fetch is tokenless).

Delete the template's Tier-1 machinery wholesale: `template_app/`, `ui/`,
`external-client/`, `examples/`, the `system_clis`/`routes`/`frontend`/
`event_hooks` contributions, `pyproject.toml`'s entrypoint. A Tier-2 app has
no workspace-side Python. What remains: `aw-app.json`, `container/`,
`tests/`, `skills/` (one doc skill), `README.md`, `.github/workflows/`,
`docs/`.

### Manifest (`aw-app.json`) — mirror of `repos/aw-app-home-assistant/aw-app.json`

- `id: "marimo"` — **exactly**; this is what makes `$AW_APP_DATA` resolve to
  `/opt/aw-workspace/.aw-workspace/data/marimo` (`src/apps/runtime.py:2000`,
  `paths.workspace_home()/data/<manifest.id>`). Do not hand-roll a path.
- `tier: "container"`, `manifest_version: 1`, `publisher: "TekFlox"`,
  initial `version: "0.1.0"` (the release bot owns every bump after that —
  never hand-bump).
- `runtime`: `image: "ghcr.io/tekflox/aw-app-marimo:latest"`, `port: 2718`,
  `resources: {cpus: 1, mem_mb: 1536}`, and exactly ONE volume:
  `{source: "$AW_APP_DATA", target: "/data", mode: "rw"}` with a `reason`
  string (HA precedent at `aw-app-home-assistant/aw-app.json` runtime.volumes).
  The `fs:workspace-data` permission is enforced for it at `runtime.py:1992`.
  No `runtime.publish`, no `run_flags_needed`, no `host_power`.
- `permissions`: `["containers:manage", "fs:workspace-data"]` — exactly two,
  mirroring home-assistant. Any third goes back to the PO on the card first.
- `contributes.windows`: one window, `id: "marimo.main"`, body
  `{type: "managed_app", kind: "web", path: "/"}` — identical shape to
  home-assistant and code-server. Opens the subdomain root = marimo's
  notebook file browser over `/data/notebooks`. (Why `managed_app` and not a
  hand-written `app_iframe` declarative spec: `managed_app` is the canonical
  Tier-2 signal — `manifest.py:502-524` keys `is_managed_app` off it, the
  Apps-grid tile and mobile shell derive from it, and the UI translates it
  to `app_iframe` itself. Hand-writing the widget would duplicate what the
  shell already does.)
- `contributes.skills`: one documentation-only skill
  (`skills/aw-marimo/SKILL.md`): where notebooks live on disk, how to open
  the app, the pip-installs-don't-survive-recreate caveat, the no-public-
  exposure invariant. Zero extra permissions; cuttable if the PO objects,
  but every future agent asked "open my notebook" benefits.
- `config_schema`: `auto_start` boolean, default true (runtime honors it at
  `runtime.py:1665`). Nothing else. `config_visible` default (true) is fine.
- NOT in the manifest: `mcp`/`mcp.template.json`, `nav`, `routes`,
  `frontend`, `db`, `system_clis`, `dependencies` — all out of scope.

### Container (`container/`)

- `Dockerfile`: `FROM python:3.12-slim`; `pip install --no-cache-dir
  marimo==<pinned>` (pin the exact version the Coder validates, 0.25.2 as of
  this design); install `uv` (single static binary) so marimo's inline
  script-dependency support (PEP 723 sandbox) works — that is the PO's named
  answer to reproducibility, so the runtime it needs must be in the image;
  `COPY entrypoint.sh`; `HEALTHCHECK` curling `http://127.0.0.1:2718/api/status`
  (use python stdlib urllib if curl isn't installed, to keep the image slim);
  `EXPOSE 2718` (documentation only — the supervisor never publishes it).
  Run as **root** inside the container (HA precedent): under rootless podman
  that maps to the host user, which keeps `/data` files readable at the host
  path; a non-root container uid would land host files under a subuid.
- `entrypoint.sh` (baked as ENTRYPOINT — `ContainerSupervisor` runs the
  image with no args, per the note at the top of
  `apps/code-server/container/Dockerfile`):
  `mkdir -p /data/notebooks /data/home`; `export HOME=/data/home` (and
  XDG_CONFIG/CACHE under it — marimo writes config at startup and warns on
  an unwritable HOME, observed in the probe); then
  `exec marimo edit /data/notebooks --headless --host 0.0.0.0 -p 2718
  --no-token --skip-update-check`. Port overridable via env
  (`${MARIMO_PORT:-2718}`) but the manifest and Dockerfile agree on 2718.
  Do NOT pass `--base-url`. Do not pass `--sandbox`/`--no-sandbox` — the
  default (prompt in the UI, uv available) is the behavior the PO scoped;
  if QA finds the sandbox prompt misbehaves headless, `--no-sandbox` is the
  sanctioned fallback and inline deps then install into the container env
  (still non-durable, still documented).

Durability semantics (card item 3): notebooks under
`/data/notebooks` → host `/opt/aw-workspace/.aw-workspace/data/marimo/notebooks`;
marimo's own user config (theme, keymap, AI settings) under `/data/home` →
survives recreate/update/reinstall, because `$AW_APP_DATA` lives outside the
package dir which updates overwrite. Pip installs inside notebook cells land
in the container layer and **die on recreate — by scope, documented in the
README and the skill, not engineered around**.

### Tests (card item 4)

- `tests/validate_manifest.py` — keep the template's validator (schema lives
  in aw-marketplace, resolved from a sibling checkout or `--schema`).
- `tests/test_manifest_invariants.py` — NEW, cheap, codifies the PO's
  constraints so drift fails CI rather than review: id == "marimo"; tier ==
  "container"; permissions == exactly {containers:manage, fs:workspace-data};
  exactly one volume, source `$AW_APP_DATA`, mode rw; no `runtime.publish`,
  no `host_power`, no `run_flags_needed` containing `--privileged` or `-p`;
  window body is managed_app. Also lint the entrypoint: contains
  `--no-token`, `--headless`, no `--base-url`.
- The template's `test.yml` workflow runs pytest — keep it.
- No live-container test in CI (needs the image; the publish workflow is the
  build). The end-to-end proof is the install-verification section (§4),
  which QA replays.

### README/docs (card item 4)

README: what the app is, where notebooks live on the host, the auth model
(workspace IdentityGuard, marimo token deliberately off, the port-publish
inversion warning from §2), the pip-ephemerality note with marimo's inline
dependencies as the sanctioned alternative, and update/release notes
(release bot owns the version; image is `:latest`, built by `build.yml`).

## 4. Release pipeline + verified install (card items 6-8), trap-proofed

Workflows, both taken from working precedents:

- `.github/workflows/release.yml` — the template's, verbatim (calls
  `tekflox/aw-marketplace/.github/workflows/app-release.yml@master`,
  `secrets: inherit`, `permissions: contents: write`). Repo is under
  `tekflox`, so the catalog-sync PR automerges.
- `.github/workflows/build.yml` — from `aw-app-home-assistant` (push-to-
  master + workflow_dispatch, buildx, pushes
  `ghcr.io/tekflox/aw-app-marimo:latest`), renamed. Being push-triggered is
  what structurally closes the "green release, stale image" trap for future
  code changes — but the Coder must still verify freshness (below) because
  release.yml green says nothing about build.yml.
- `.github/workflows/test.yml` + `security-scan.yml` — template's.

Ordered delivery steps for the Coder, with the traps inlined:

1. Create the repo (public, fresh history, §3). Immediately add it to **both**
   GitHub allowlists — this is two independent lists and missing the second
   one hangs CI as `queued` forever rather than failing:
   (a) org secret `MARKETPLACE_SYNC_TOKEN` repo allowlist;
   (b) **runner group 3 `aw-private`** repo allowlist
   (`gh api -X PUT /orgs/tekflox/actions/runner-groups/3/repositories/$REPO_ID`).
   `gh auth token` in the workspace carries `admin:org` and works for both
   PUTs (`GH_RUNNERS_ADMIN_TOKEN` named by the skill does not exist in this
   container). Verify membership **with `--paginate`** — the default page
   lies on a 57+ repo list. If visibility is ever flipped later, GitHub may
   silently drop the repo from the secret allowlist — re-check both.
2. Push; watch `release.yml` AND `build.yml` to green. If release.yml goes
   red after the bump commit landed (push-race `missing necessary objects`),
   do NOT re-run the job (it would double-bump) — repair per the documented
   hand-steps (push the release/<tag> branch, run aw-marketplace's own
   `scripts/sync_catalog_entry.py`).
3. First-image-push manual gate: GitHub creates
   `ghcr.io/tekflox/aw-app-marimo` **private** regardless of repo visibility
   and there is **no API to flip it** (confirmed on aw-app-browser,
   2026-07-28; documented in HA's build.yml header). Until an org admin
   flips it Public in the GitHub UI, the workspace's tokenless pull fails
   and the install errors look like a broken image. Verify with an
   unauthenticated manifest pull; if no admin is reachable, this is the
   card's named `need_human` blocker.
4. Prove the image is real before installing: after build.yml green, check
   the ghcr package version's updated timestamp/digest (`gh api
   /orgs/tekflox/packages/container/aw-app-marimo/versions`) — a green
   *release* run alone proves nothing about the image.
5. Poll the **workspace's own catalog** (`GET /api/apps/-/catalog`, or
   `aw-workspace-cli marketplace` with refresh) until it serves the new
   version — GitHub's raw CDN lags the sync-PR merge by up to ~5 minutes;
   poll, don't debug the installer. Then
   `aw-workspace-cli marketplace install marimo`. Never sideload — the
   reconciler reverts it.
6. On any later `--update`: verify the **container image ID changed** —
   `marketplace install --update` has reported success without recreating
   the container (card 2026-09-03), so "installed" output is not evidence.
7. Verification evidence for the card (mirrors the PO's three load-bearing
   checks): repo public; release tag URL; catalog version; installed
   version; container running + healthy (podman healthcheck green);
   `podman port` on the container shows **nothing** published;
   `/api/apps/marimo/api/status` 200 **with** workspace auth and 401/403
   **without**; UI click-through in the window (create a notebook, run a
   cell — this exercises the WS path end to end); notebook file present at
   `/opt/aw-workspace/.aw-workspace/data/marimo/notebooks/...` on the host
   path; `aw-workspace-cli restart marimo` then the notebook still opens;
   `marketplace install marimo --update` then still opens;
   `aw-workspace-cli doctor` clean.

## 5. Rejected alternatives (and why)

1. **`--base-url=/api/apps/marimo`** — wrong twice. The window opens the
   subdomain root (`AppWindow.jsx:349`, `runtime.py:1525`) where a base-url
   would 404 everything; and on the path mount Starlette strips the prefix
   (`proxy.py:101-113`) so marimo would never see the path it was told to
   expect. The probe shows relative asset URLs make base-url unnecessary on
   both entries. Runner-up: none — this is settled by code + probe.
2. **An HA-style `ensure_proxy_config.py` shim** — HA needed it because HA
   400s on unfamiliar `X-Forwarded-*`; probed marimo returns 200 with
   foreign Host and forwarded headers. Shipping a do-nothing shim would
   teach the next reader a false dependency.
3. **Tier-1 (pip into the workspace venv)** — marimo executes arbitrary user
   Python in long-running kernels; that is exactly what Tier-2 isolation is
   for, and the workspace venv must not inherit notebook deps.
4. **`marimo run` serving mode** — PO-excluded, and unnecessary: edit mode
   is proven behind the proxy.
5. **Marimo token kept on (via config/secret)** — not just friction: broken,
   because the WS bridge drops cookies (§2). Revisit only if a port is ever
   published, which is itself out of scope.
6. **A second volume / baked site-packages persistence for cell pip
   installs** — PO-excluded; documented instead. Marimo's inline-deps +
   `uv` in the image is the sanctioned reproducibility path.
7. **Custom host path for notebooks** — `$AW_APP_DATA` already resolves to
   the card's exact required path (`runtime.py:2000`); anything else is a
   regression and loses the permission enforcement at `runtime.py:1992`.
8. **MCP server / agent tools, `$AW_WORKSPACE_*` mounts, published port,
   Caddy host, share link** — all PO-excluded; the design depends on their
   absence (the `--no-token` trade collapses if any port is published).
9. **Non-root container user** — code-server matches uid deliberately for
   its workspace-tree mount; marimo mounts only its own data dir, and root
   under rootless podman maps to the host user (HA precedent), keeping host
   files readable without subuid noise.

## 6. What this makes harder later

- **No repo/folder access**: notebooks cannot read workspace code. The named
  follow-up (PO) would add an `$AW_WORKSPACE_REPOS`-class volume + the
  matching permission — a manifest + capability-review change, not a
  redesign.
- **`--no-token` bakes in "never publish a port"**: any future direct
  exposure needs marimo auth back AND a solution for the header-dropping WS
  bridge (`proxy.py:203`) — that is a workspace-core change, not an app
  change. The README must carry this so it isn't rediscovered as an
  incident.
- **Pinned marimo + `:latest` image tag** (HA/code-server convention):
  upgrading marimo requires an image rebuild; conversely a rebuild changes
  the running app without a version-visible diff. Accepted to stay
  idiomatic; revisit if the marketplace ever moves to digest-pinned images.
- **Single-user by design**: marimo `edit` is one trusted user; multi-user
  or sharing later is a different architecture (separate sessions or `run`
  mode), not an extension of this app.
- **Same-network exposure**: other containers on the podman app network can
  reach marimo unauthenticated (same posture as code-server, which is
  strictly more powerful with its rw workspace mount). If per-app network
  segmentation ever lands, marimo should adopt it early.

## 7. Risks for the Coder (the non-obvious breakage)

1. **The WS bridge forwards no headers** (`proxy.py:203`). Don't add any
   marimo setting that authenticates WS by cookie/header; query params are
   the only thing that survives (`_target`, proxy.py:109).
2. **A green release run is not a built image**, and "installed" is not a
   recreated container — verify digest and image ID at steps 4/6 of §4.
   These are the two documented false-greens in this exact path.
3. **The first release run goes red or hangs for allowlist reasons** that
   look unrelated (red: MARKETPLACE_SYNC_TOKEN; hung-queued: runner group).
   Fix both allowlists before the first push, verify with `--paginate`.
4. **ghcr package visibility** is the one genuinely manual step — plan for
   it early (step 3 of §4), it is the only expected `need_human`.
5. **HOME must be writable before marimo starts** — it writes config at
   startup; an unwritable HOME produced warnings in the probe. The
   entrypoint's `mkdir -p` + `export HOME=/data/home` is load-bearing.
6. **Don't chase the WS `file=new` error** in marimo's log (§2 caveat) — the
   real UI never sends it.
7. **The sandbox prompt**: with `uv` in the image, marimo may prompt in the
   UI to run inline-dep notebooks sandboxed. If that flow misbehaves in the
   iframe, fall back to `--no-sandbox` (documented in §3) rather than
   removing `uv`.
8. **Version field**: set `0.1.0` once at repo creation and never touch it
   again — the release bot bumps it, and re-running a failed release job
   double-bumps.
9. **File ownership on the host**: files created by the container land as
   the podman host user. If a workspace-container shell (uid 1001) needs to
   edit notebooks directly, permissions may bite; that is tolerable for this
   card (the UI is the editor) — note it in the README rather than chmod-ing
   broadly.

## 8. Probe artifacts

`/opt/aw-workspace/.tmp/marimo-probe/` holds the venv, a `notebooks/probe.py`,
and `marimo.log` from the live probe (marimo 0.25.2, stopped). Reproduce with
the commands in §2. Safe to delete after the Coder has read it.

## 9. Product Owner addendum (binding, accepted 2026-10-26)

The PO accepted the design in full, ruled on the three open questions, and
added one requirement the design did not name. Recorded here because the
addition is a safety property of this app, not a preference.

### The ruled-on questions

- **The one documentation-only contributed skill: kept.**
- **`--no-sandbox` is a sanctioned FALLBACK only.** Not used in the shipped
  entrypoint; if it is ever taken, the reason must be stated on the card.
- **Same-podman-network reachability (§6) is ACCEPTED**, not a scope
  violation. "Private" on this card means no public port and no tunnel.
  `code-server` has the same posture and is strictly more powerful.

### The addition: the `--no-token` trade has a second inversion

§2 and §6 tie "the token must come back" to *publishing a port*. That is
not the only way in, and it is the less likely one.

Because this app is a `managed_app`, `src/apps/manifest.py`
(`effective_config_schema`) **injects `auth_required` and `public` into this
app's config schema automatically**, as user-facing gear toggles marked
`x-framework: True` with `auth_required` defaulting to `True`. And
`src/apps/runtime.py` reads that flag through a **live lambda** when it
builds the `IdentityGuard`, so flipping it takes effect on an
already-mounted app **with no restart**.

So the real exposure is two clicks in the Settings gear — no code change, no
release — after which marimo is an **unauthenticated
arbitrary-Python-execution endpoint on a public subdomain**, with no second
line of defence precisely because its own token is deliberately off.

Not hypothetical: it is the documented shape of the SigNoz incident in this
workspace (`aw-autoskill-app-auth-required-routing`), where the same flag,
flipped to fix a login bounce, silently exposed unauthenticated OTLP ingest
to the open internet — found by a credential-free `curl` against
`https://<app>.app.<workspace>.workspace.<apex>` returning 200.

The toggles are framework-owned and are deliberately **not** fought in code.
Three cheap requirements instead, all implemented:

1. **The README and the contributed skill carry this inversion**, worded
   against the *toggles*, not only against publishing a port. The
   port-publish warning from §2 stays as well.
2. **`tests/test_manifest_invariants.py` asserts this app's own
   `config_schema` contains neither `auth_required` nor `public`** — the
   merge in `effective_config_schema` is `{**framework_props, **app_props}`,
   so an app-declared default would beat the framework's safe one with
   nobody reviewing it.
3. **The unauthenticated check is run against the SUBDOMAIN host**, not only
   the `/api/apps/marimo` path mount in §4 step 7. The window loads the
   subdomain, and the subdomain is where the SigNoz exposure was found.
