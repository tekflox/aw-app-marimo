#!/usr/bin/env python3
"""Agent-facing MCP server for this workspace's Marimo notebooks.

A second process inside the Marimo container, listening on 2719 (never
published to the host) and reached by the workspace's MCP gateway at
`http://aw-app-marimo:2719/mcp`. `marimo edit` stays the container's
foreground process; this one is supervised by a restart loop in
`entrypoint.sh` so a crash here can never take the notebook UI down.

Six tools, server name `marimo`, so callers see `aw__marimo__*`.

## The boundary

The container is the boundary, not the interpreter. `execute_notebook` runs
arbitrary Python by design — that is the feature. What is bounded is the
filesystem it can see (one volume, no workspace mounts, no container
socket), how long it runs, how much it can say, how many can run at once,
and that its process tree is reaped. The threat model is "a notebook
misbehaves inside its own container", not "a notebook is prevented from
doing anything unusual".

## Two things that trip callers

* Run as a script, a notebook prints only what it `print()`s. A cell's
  trailing display expression is evaluated but NOT written to stdout.
* PEP 723 inline dependencies are not materialised — this is plain
  `python <file>`, not `uv run --script`. A missing import comes back as
  `status: "error"` with the ImportError in stderr. The edit UI remains the
  PEP 723 path.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio.to_thread
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

# The container name the gateway dials us by. It is committed contract: the
# repo-root mcp.json hardcodes it, so renaming the container needs a
# coordinated app release, not just a supervisor change.
CONTAINER_NAME = "aw-app-marimo"
PORT = int(os.environ.get("MARIMO_MCP_PORT", "2719"))

# Overridable only so the test suite can point the whole server at a tmpdir.
# Nothing sets it in the image; the default is the real notebook root.
NOTEBOOKS_DIR_ENV = "MARIMO_NOTEBOOKS_DIR"
DEFAULT_NOTEBOOKS_DIR = "/data/notebooks"

#: The interpreter notebooks run under — deliberately NOT this process's own.
#: This server lives in its own venv (marimo wants mcp>=2.0 for its own MCP
#: client, FastMCP needs mcp<2.0, so they cannot share site-packages), and
#: that venv has no marimo in it. Reaching for `sys.executable` here would
#: make `import marimo` fail in every notebook. The image sets this to the
#: system python; the default keeps the test suite, which runs the server
#: and the notebooks under one interpreter, working unchanged.
NOTEBOOK_PYTHON_ENV = "MARIMO_NOTEBOOK_PYTHON"

MAX_CONTENT_BYTES = 1024 * 1024          # 1 MiB per write
MAX_STREAM_BYTES = 64 * 1024             # 64 KiB per captured stream
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
MAX_CONCURRENT_EXECUTES = 2
KILL_GRACE_SECONDS = 5

SKELETON = """import marimo

app = marimo.App()


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
"""


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

def _fail(code: str, message: str, **extra: Any) -> ToolError:
    """A structured tool error.

    FastMCP re-wraps whatever we raise as
    ``Error executing tool <name>: <our message>``, so the message itself is
    the JSON body — the code stays readable in the prefix-ed wire text and
    the payload is still parseable from the first ``{``.
    """
    payload = {"error": code, "message": message}
    payload.update(extra)
    return ToolError(json.dumps(payload))


# --------------------------------------------------------------------------
# path confinement — one resolver, used by every path-taking tool
# --------------------------------------------------------------------------

def notebooks_root() -> Path:
    return Path(os.environ.get(NOTEBOOKS_DIR_ENV, DEFAULT_NOTEBOOKS_DIR)).resolve()


def notebook_python() -> str:
    return os.environ.get(NOTEBOOK_PYTHON_ENV) or sys.executable


def resolve_notebook_path(path: str) -> Path:
    """Map a caller-supplied relative path to a real path inside the root.

    `resolve()` follows symlinks in every component and collapses `..`, so a
    symlink planted inside the tree that points outside resolves outside and
    fails containment here rather than later.
    """
    root = notebooks_root()

    if not isinstance(path, str) or not path.strip():
        raise _fail("invalid_path", "path must be a non-empty string")
    if path.startswith("/") or Path(path).is_absolute():
        raise _fail(
            "invalid_path",
            f"path must be relative to {root} — absolute paths are rejected",
            path=path,
        )
    if not path.endswith(".py"):
        raise _fail("not_a_notebook", "path must end with .py", path=path)

    try:
        resolved = (root / path).resolve()
    except (ValueError, OSError) as exc:  # NUL bytes, symlink loops
        raise _fail("invalid_path", f"path could not be resolved: {exc}", path=path)

    if not resolved.is_relative_to(root):
        raise _fail(
            "path_outside_root",
            f"resolved path escapes {root}",
            path=path,
            root=str(root),
        )
    return resolved


def _relative(resolved: Path) -> str:
    return resolved.relative_to(notebooks_root()).as_posix()


# --------------------------------------------------------------------------
# file helpers
# --------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _read_bytes(resolved: Path, path: str) -> bytes:
    if not resolved.exists():
        raise _fail("not_found", "notebook does not exist", path=path)
    if not resolved.is_file():
        raise _fail("not_a_notebook", "path is not a regular file", path=path)
    return resolved.read_bytes()


def _decode(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail("not_a_notebook", f"file is not valid UTF-8: {exc}", path=path)


def _encode_within_cap(content: str, path: str) -> bytes:
    data = content.encode("utf-8")
    if len(data) > MAX_CONTENT_BYTES:
        raise _fail(
            "content_too_large",
            f"content is {len(data)} bytes, cap is {MAX_CONTENT_BYTES}",
            path=path,
            size_bytes=len(data),
            max_bytes=MAX_CONTENT_BYTES,
        )
    return data


#: What a notebook written here ends up as on disk. `mkstemp` creates 0600
#: and `os.replace` carries that mode onto the destination, so without this
#: an agent-written notebook is unreadable to everyone but root — including
#: on the host path under `.aw-workspace/data/marimo/notebooks/`, where a
#: person is explicitly meant to be able to read it. Found live, not in
#: review: inside the container everything runs as root, so the UI and the
#: tools both looked fine while the host copy was 0600.
NOTEBOOK_MODE = 0o644


def _atomic_write(resolved: Path, data: bytes) -> None:
    """Write via a same-directory temp file + os.replace.

    Same directory so the replace is a rename within one filesystem, which
    is what makes it atomic: a reader sees either the whole old file or the
    whole new one, never a half-written one, whatever happens mid-write.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(resolved.parent), prefix=f".{resolved.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Updating an existing notebook keeps whatever mode it already had;
        # a new one gets the same mode an ordinary write would have made.
        try:
            os.chmod(tmp, resolved.stat().st_mode & 0o7777)
        except FileNotFoundError:
            os.chmod(tmp, NOTEBOOK_MODE)
        os.replace(tmp, resolved)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

def allowed_hosts() -> list[str]:
    """Every name the gateway might address us by, with port variants.

    DNS-rebinding protection stays ON. The SDK defaults `allowed_hosts` to
    empty, which passes localhost and nothing else, so an upstream addressed
    as `aw-app-marimo:2719` fails every connect with 421 Misdirected Request
    — a Host-header failure, not a reachability one, which is why binding
    0.0.0.0 is necessary but not sufficient (research-search, 2026-09).
    """
    names = {CONTAINER_NAME, socket.gethostname(), "localhost", "127.0.0.1", "0.0.0.0"}
    hosts: list[str] = []
    for name in sorted(n for n in names if n):
        hosts += [name, f"{name}:{PORT}", f"{name}:*"]
    return hosts


def allowed_origins() -> list[str]:
    return [f"http://{host}" for host in allowed_hosts()]


mcp = FastMCP(
    name="marimo",
    instructions=(
        "Read, write, validate and run this workspace's Marimo notebooks. All "
        "paths are relative to /data/notebooks and must end in .py. Execution "
        "is batch and headless: the notebook runs start to finish as a script "
        "in its own container, it does not attach to anyone's open editor "
        "session."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path="/mcp",
    # A sidecar restart mid-session returns a clean response instead of a
    # session-not-found error, and none of these six tools need server-side
    # session state.
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts(),
        allowed_origins=allowed_origins(),
    ),
)


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

@mcp.tool()
def list_notebooks() -> dict[str, Any]:
    """List every notebook under /data/notebooks, recursively.

    Returns relative paths, sizes, modification times and content hashes.
    The sha256 of a notebook is what `update_notebook` wants as
    `expected_hash`.
    """
    root = notebooks_root()
    notebooks: list[dict[str, Any]] = []
    if root.is_dir():
        for candidate in sorted(root.rglob("*.py")):
            try:
                if not candidate.is_file():
                    continue
                # A symlink pointing out of the tree is not ours to list.
                if not candidate.resolve().is_relative_to(root):
                    continue
                stat = candidate.stat()
                notebooks.append(
                    {
                        "path": candidate.relative_to(root).as_posix(),
                        "size_bytes": stat.st_size,
                        "modified_at": _iso(stat.st_mtime),
                        "sha256": _sha256(candidate.read_bytes()),
                    }
                )
            except OSError:
                continue  # vanished or unreadable mid-walk
    return {"root": str(root), "notebooks": notebooks}


@mcp.tool()
def read_notebook(path: str) -> dict[str, Any]:
    """Read one notebook's full source.

    Args:
        path: Path relative to /data/notebooks, ending in .py.
    """
    resolved = resolve_notebook_path(path)
    data = _read_bytes(resolved, path)
    stat = resolved.stat()
    return {
        "path": _relative(resolved),
        "content": _decode(data, path),
        "sha256": _sha256(data),
        "size_bytes": stat.st_size,
        "modified_at": _iso(stat.st_mtime),
    }


@mcp.tool()
def create_notebook(path: str, content: str | None = None) -> dict[str, Any]:
    """Create a new notebook. Refuses to overwrite an existing one.

    Create is create: there is no overwrite flag. Use `update_notebook`,
    which requires the current hash, to change a notebook that already
    exists.

    Args:
        path: Path relative to /data/notebooks, ending in .py. Intermediate
            directories inside the root are created.
        content: Full file content. Omitted, a minimal valid marimo skeleton
            is written. Cap 1 MiB.
    """
    resolved = resolve_notebook_path(path)
    if resolved.exists():
        raise _fail("already_exists", "notebook already exists", path=path)

    data = _encode_within_cap(SKELETON if content is None else content, path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(resolved, data)
    return {"path": _relative(resolved), "sha256": _sha256(data), "created": True}


@mcp.tool()
def update_notebook(path: str, content: str, expected_hash: str) -> dict[str, Any]:
    """Replace a notebook's content, refusing if it changed under you.

    `expected_hash` is required and is the sha256 of the bytes you last read
    (`read_notebook`/`list_notebooks` return it). On mismatch nothing is
    written and the current hash comes back in the error.

    Note the limit this does NOT cover: it protects one agent against
    another, not against a human with the same notebook open in the Marimo
    editor. Those are two independent writers of one file and either can
    clobber the other.

    Args:
        path: Path relative to /data/notebooks, ending in .py.
        content: The full new file content. Cap 1 MiB.
        expected_hash: sha256 hex of the file's current bytes.
    """
    resolved = resolve_notebook_path(path)
    current = _read_bytes(resolved, path)
    actual = _sha256(current)
    if actual != (expected_hash or "").strip().lower():
        raise _fail(
            "hash_conflict",
            "notebook changed since it was read; re-read it and retry",
            path=path,
            expected_hash=expected_hash,
            actual_hash=actual,
        )

    data = _encode_within_cap(content, path)
    _atomic_write(resolved, data)
    return {"path": _relative(resolved), "sha256": _sha256(data)}


def _structure(tree: ast.Module) -> dict[str, Any]:
    has_marimo_import = False
    has_app_assignment = False
    cell_count = 0
    has_main_guard = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            has_marimo_import |= any(a.name.split(".")[0] == "marimo" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            has_marimo_import |= (node.module or "").split(".")[0] == "marimo"
        elif isinstance(node, ast.Assign):
            value = node.value
            func = value.func if isinstance(value, ast.Call) else None
            if isinstance(func, ast.Attribute) and func.attr == "App":
                has_app_assignment = True
            elif isinstance(func, ast.Name) and func.id == "App":
                has_app_assignment = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr == "cell":
                    cell_count += 1
                    break
        elif isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and any(
                    isinstance(c, ast.Constant) and c.value == "__main__"
                    for c in test.comparators
                )
            ):
                has_main_guard = True

    return {
        "has_marimo_import": has_marimo_import,
        "has_app_assignment": has_app_assignment,
        "cell_count": cell_count,
        "has_main_guard": has_main_guard,
    }


@mcp.tool()
def validate_notebook(path: str) -> dict[str, Any]:
    """Check that a notebook parses and looks like a marimo app.

    Parsing and structure only — this never imports, execs or otherwise runs
    the file. A notebook whose top-level code has side effects produces none
    when validated. That is the point: a "validate" that ran the module
    would be a second execution path with none of execute's bounds on it.

    Args:
        path: Path relative to /data/notebooks, ending in .py.
    """
    resolved = resolve_notebook_path(path)
    source = _decode(_read_bytes(resolved, path), path)

    try:
        tree = ast.parse(source, filename=str(resolved))
    except SyntaxError as exc:
        return {
            "path": _relative(resolved),
            "valid": False,
            "errors": [
                {
                    "line": exc.lineno or 0,
                    "col": exc.offset or 0,
                    "message": exc.msg or "syntax error",
                }
            ],
            "structure": {
                "has_marimo_import": False,
                "has_app_assignment": False,
                "cell_count": 0,
                "has_main_guard": False,
            },
        }

    return {
        "path": _relative(resolved),
        "valid": True,
        "errors": [],
        "structure": _structure(tree),
    }


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------

_execute_slots = threading.BoundedSemaphore(MAX_CONCURRENT_EXECUTES)
_running_lock = threading.Lock()
_running = 0


def _reap_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _capture(handle: Any) -> tuple[str, bool]:
    """Read back at most MAX_STREAM_BYTES, reporting whether more existed."""
    handle.seek(0)
    data = handle.read(MAX_STREAM_BYTES + 1)
    truncated = len(data) > MAX_STREAM_BYTES
    if truncated:
        data = data[:MAX_STREAM_BYTES]
    return data.decode("utf-8", errors="replace"), truncated


def _run_notebook(resolved: Path, timeout: int, root: Path) -> dict[str, Any]:
    """Blocking half of execute_notebook. Runs on a worker thread."""
    started = time.monotonic()
    started_at = _iso(time.time())

    # Output goes to unlinked temp files rather than pipes: a grandchild
    # holding the write end can't then wedge us, and the cap is applied on
    # read-back instead of by buffering an unbounded stream in memory.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            [notebook_python(), str(resolved)],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            # Its own session, so everything the notebook spawns shares one
            # process group we can signal as a unit.
            start_new_session=True,
        )
        pgid = proc.pid
        status = "ok"
        try:
            exit_code = proc.wait(timeout=timeout)
            if exit_code != 0:
                status = "error"
        except subprocess.TimeoutExpired:
            status = "timeout"
            exit_code = None
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _reap_group(pgid)
                proc.wait()
        finally:
            # Also on the success path: a run that exits leaving background
            # children behind would be exactly the orphan this is meant to
            # prevent, and "no background jobs" is in scope for this tool.
            _reap_group(pgid)

        stdout, stdout_truncated = _capture(out)
        stderr, stderr_truncated = _capture(err)

    return {
        "path": _relative(resolved),
        "status": status,
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "started_at": started_at,
        "timeout_seconds": timeout,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def _clamp_timeout(timeout_seconds: int | None) -> int:
    if timeout_seconds is None:
        return DEFAULT_TIMEOUT_SECONDS
    return max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))


@mcp.tool()
async def execute_notebook(path: str, timeout_seconds: int | None = None) -> dict[str, Any]:
    """Run a notebook headless, start to finish, and return what it produced.

    Batch execution as a plain script inside the Marimo container: it does
    not attach to, drive or share state with a human's open editor session.

    Two things worth knowing before reading the output:

    * stdout holds only what the notebook `print()`s. A cell's trailing
      display expression is evaluated but not printed, so a notebook written
      to prove a result must `print()` it.
    * PEP 723 inline dependencies are not installed — this is `python
      <file>`, not `uv run --script`. A missing package comes back as
      `status: "error"` with the ImportError in stderr.

    Bounds: default 120s timeout, hard cap 600s (a larger request is clamped
    and the applied value is returned); on timeout the whole process group
    gets SIGTERM, 5s, then SIGKILL, so nothing is orphaned; stdout and
    stderr are capped at 64 KiB each with explicit truncation flags; at most
    2 notebooks run at once and a third call is refused immediately with
    `status: "refused_busy"` rather than queued.

    Args:
        path: Path relative to /data/notebooks, ending in .py.
        timeout_seconds: Wall-clock limit. Default 120, capped at 600.
    """
    global _running

    resolved = resolve_notebook_path(path)
    if not resolved.exists():
        raise _fail("not_found", "notebook does not exist", path=path)
    if not resolved.is_file():
        raise _fail("not_a_notebook", "path is not a regular file", path=path)

    timeout = _clamp_timeout(timeout_seconds)

    if not _execute_slots.acquire(blocking=False):
        with _running_lock:
            running = _running
        return {
            "path": _relative(resolved),
            "status": "refused_busy",
            "exit_code": None,
            "duration_ms": 0,
            "started_at": _iso(time.time()),
            "timeout_seconds": timeout,
            "stdout": "",
            "stderr": (
                f"refused: {running} of {MAX_CONCURRENT_EXECUTES} execution slots "
                f"are in use; retry shortly"
            ),
            "stdout_truncated": False,
            "stderr_truncated": False,
            "running": running,
            "max": MAX_CONCURRENT_EXECUTES,
        }

    with _running_lock:
        _running += 1
    try:
        root = notebooks_root()
        return await anyio.to_thread.run_sync(_run_notebook, resolved, timeout, root)
    finally:
        with _running_lock:
            _running -= 1
        _execute_slots.release()


def main() -> None:
    root = notebooks_root()
    root.mkdir(parents=True, exist_ok=True)
    print(
        f"aw-app-marimo MCP sidecar: 0.0.0.0:{PORT}/mcp, root={root}, "
        f"allowed_hosts={allowed_hosts()}",
        file=sys.stderr,
        flush=True,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
