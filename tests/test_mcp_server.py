"""The MCP sidecar's bounds, as assertions.

Every test here traces to a line in the card's acceptance criteria (§3c,
"safety, each proven by a call that is *refused*, not by reading the code").
The point of the file is that widening a bound breaks the build.

Two deliberate shapes:

* **The refusal tests assert the error CODE**, not a message substring, so
  rewording an error can't silently turn a refusal into a pass.
* **The timeout test asserts on the process table**, not on the tool's
  answer. A tool that returns `status: "timeout"` while the notebook (or
  something it spawned) keeps running is exactly the failure the bound
  exists to prevent, and the response alone cannot tell the two apart.
"""
import asyncio
import inspect
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import mcp_server

REPO_ROOT = Path(__file__).resolve().parent.parent
SIDECAR = REPO_ROOT / "container" / "mcp_server.py"

#: Every tool that takes a path, execute included. The card is explicit that
#: execute is not allowed its own path-handling code path.
PATH_TOOLS = (
    "read_notebook",
    "create_notebook",
    "update_notebook",
    "validate_notebook",
    "execute_notebook",
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Point the whole server at a tmpdir instead of /data/notebooks."""
    notebooks = tmp_path / "notebooks"
    notebooks.mkdir()
    monkeypatch.setenv(mcp_server.NOTEBOOKS_DIR_ENV, str(notebooks))
    return notebooks.resolve()


def invoke(name: str, **kwargs):
    """Call a tool function, awaiting it if it is async."""
    fn = getattr(mcp_server, name)
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn(**kwargs))
    return fn(**kwargs)


def refusal(name: str, **kwargs) -> dict:
    """Invoke a tool expecting a structured refusal, and return its body."""
    with pytest.raises(ToolError) as caught:
        invoke(name, **kwargs)
    return json.loads(str(caught.value))


def args_for(tool: str, path: str) -> dict:
    extra = {
        "create_notebook": {"content": "x = 1\n"},
        "update_notebook": {"content": "x = 1\n", "expected_hash": "0" * 64},
    }
    return {"path": path, **extra.get(tool, {})}


# ---------------------------------------------------------------------------
# path confinement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", PATH_TOOLS)
@pytest.mark.parametrize("path", ["../escaped.py", "../../etc/passwd.py", "a/../../out.py"])
def test_traversal_is_refused_on_every_path_tool(root, tool, path):
    assert refusal(tool, **args_for(tool, path))["error"] == "path_outside_root"


@pytest.mark.parametrize("tool", PATH_TOOLS)
@pytest.mark.parametrize("path", ["/etc/passwd.py", "/data/notebooks/ok.py"])
def test_absolute_paths_are_refused_on_every_path_tool(root, tool, path):
    assert refusal(tool, **args_for(tool, path))["error"] == "invalid_path"


@pytest.mark.parametrize("tool", PATH_TOOLS)
def test_symlink_escape_is_refused_on_every_path_tool(root, tmp_path, tool):
    """A symlink INSIDE the tree pointing out still resolves out."""
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    (root / "link.py").symlink_to(outside)

    assert refusal(tool, **args_for(tool, "link.py"))["error"] == "path_outside_root"
    # The escape attempt must not have touched the target either way.
    assert outside.read_text() == "secret = 1\n"


@pytest.mark.parametrize("tool", PATH_TOOLS)
def test_symlinked_directory_escape_is_refused(root, tmp_path, tool):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "target.py").write_text("secret = 1\n")
    (root / "jump").symlink_to(outside_dir)

    assert refusal(tool, **args_for(tool, "jump/target.py"))["error"] == "path_outside_root"


@pytest.mark.parametrize("tool", PATH_TOOLS)
def test_non_python_paths_are_refused(root, tool):
    assert refusal(tool, **args_for(tool, "notes.txt"))["error"] == "not_a_notebook"


def test_list_does_not_leak_a_symlink_pointing_outside(root, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    (root / "link.py").symlink_to(outside)
    (root / "real.py").write_text("x = 1\n")

    listed = {n["path"] for n in invoke("list_notebooks")["notebooks"]}
    assert listed == {"real.py"}


# ---------------------------------------------------------------------------
# create / read / update
# ---------------------------------------------------------------------------

def test_create_writes_a_valid_marimo_skeleton_by_default(root):
    created = invoke("create_notebook", path="fresh.py")
    assert created["created"] is True

    checked = invoke("validate_notebook", path="fresh.py")
    assert checked["valid"] is True
    assert checked["structure"]["has_marimo_import"] is True
    assert checked["structure"]["has_app_assignment"] is True
    assert checked["structure"]["cell_count"] == 1
    assert checked["structure"]["has_main_guard"] is True


def test_create_refuses_to_overwrite(root):
    (root / "taken.py").write_text("original = 1\n")
    assert refusal("create_notebook", path="taken.py", content="new = 1\n")["error"] == "already_exists"
    assert (root / "taken.py").read_text() == "original = 1\n"


def test_create_makes_intermediate_directories_inside_the_root(root):
    invoke("create_notebook", path="deep/nested/nb.py", content="x = 1\n")
    assert (root / "deep" / "nested" / "nb.py").read_text() == "x = 1\n"


def test_read_round_trips_content_and_hash(root):
    invoke("create_notebook", path="rt.py", content="value = 42\n")
    read = invoke("read_notebook", path="rt.py")
    assert read["content"] == "value = 42\n"
    assert read["sha256"] == mcp_server._sha256(b"value = 42\n")


def test_oversized_content_is_refused(root):
    too_big = "#" * (mcp_server.MAX_CONTENT_BYTES + 1)
    body = refusal("create_notebook", path="big.py", content=too_big)
    assert body["error"] == "content_too_large"
    assert not (root / "big.py").exists()


def test_update_with_a_stale_hash_is_refused_and_leaves_the_file_untouched(root):
    original = "original = 1\n"
    invoke("create_notebook", path="raced.py", content=original)
    before = (root / "raced.py").read_bytes()

    body = refusal(
        "update_notebook",
        path="raced.py",
        content="clobbered = 1\n",
        expected_hash="0" * 64,
    )
    assert body["error"] == "hash_conflict"
    assert body["actual_hash"] == mcp_server._sha256(before)
    assert (root / "raced.py").read_bytes() == before


def test_update_with_the_current_hash_succeeds(root):
    invoke("create_notebook", path="ok.py", content="a = 1\n")
    current = invoke("read_notebook", path="ok.py")["sha256"]
    invoke("update_notebook", path="ok.py", content="a = 2\n", expected_hash=current)
    assert (root / "ok.py").read_text() == "a = 2\n"


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------

def test_a_created_notebook_is_readable_outside_the_container(root):
    """0600 would make it root-only on the HOST path too.

    Everything in this container runs as root, so a mode regression here is
    invisible from inside it and only shows up when a person opens the file
    under .aw-workspace/data/marimo/notebooks/ — which the card requires
    they can. Caught live on the first end-to-end run, not in review.
    """
    invoke("create_notebook", path="perm.py", content="x = 1\n")
    assert (root / "perm.py").stat().st_mode & 0o777 == mcp_server.NOTEBOOK_MODE


def test_an_update_preserves_the_mode_the_notebook_already_had(root):
    target = root / "owned.py"
    invoke("create_notebook", path="owned.py", content="x = 1\n")
    os.chmod(target, 0o640)

    current = invoke("read_notebook", path="owned.py")["sha256"]
    invoke("update_notebook", path="owned.py", content="x = 2\n", expected_hash=current)

    assert target.stat().st_mode & 0o777 == 0o640


def test_a_write_that_fails_mid_way_leaves_the_old_file_whole(root, monkeypatch):
    """Crash between temp-write and replace: old file, never a truncated one."""
    original = "original = 1\n" * 500
    invoke("create_notebook", path="atomic.py", content=original)

    def boom(src, dst):
        raise OSError("simulated crash between write and replace")

    monkeypatch.setattr(mcp_server.os, "replace", boom)
    current = invoke("read_notebook", path="atomic.py")["sha256"]
    with pytest.raises(OSError):
        invoke("update_notebook", path="atomic.py", content="new\n", expected_hash=current)

    assert (root / "atomic.py").read_text() == original
    assert [p.name for p in root.iterdir()] == ["atomic.py"], "temp file left behind"


def test_a_concurrent_reader_never_sees_a_half_written_file(root):
    """Readers see either the whole old file or the whole new one."""
    old = "old = 1\n" * 20000
    new = "new = 2\n" * 20000
    invoke("create_notebook", path="torn.py", content=old)
    target = root / "torn.py"

    seen: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                seen.append(target.read_text())
            except FileNotFoundError:
                seen.append("<missing>")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for _ in range(20):
            current = invoke("read_notebook", path="torn.py")["sha256"]
            nxt = new if current == mcp_server._sha256(old.encode()) else old
            invoke("update_notebook", path="torn.py", content=nxt, expected_hash=current)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert seen, "reader never ran"
    assert set(seen) <= {old, new}


# ---------------------------------------------------------------------------
# validate never executes
# ---------------------------------------------------------------------------

def test_validate_does_not_execute_top_level_code(root, tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    (root / "sideeffect.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n"
    )

    result = invoke("validate_notebook", path="sideeffect.py")

    assert result["valid"] is True
    assert not sentinel.exists(), "validate_notebook executed the module"


def test_validate_reports_syntax_errors_with_a_position(root):
    (root / "broken.py").write_text("def oops(:\n    pass\n")
    result = invoke("validate_notebook", path="broken.py")
    assert result["valid"] is False
    assert result["errors"][0]["line"] == 1
    assert result["errors"][0]["message"]


def test_validate_counts_cells(root):
    (root / "cells.py").write_text(
        "import marimo\n"
        "app = marimo.App()\n"
        "@app.cell\ndef _():\n    return\n"
        "@app.cell\ndef __():\n    return\n"
    )
    structure = invoke("validate_notebook", path="cells.py")["structure"]
    assert structure["cell_count"] == 2
    assert structure["has_main_guard"] is False


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def test_execute_returns_stdout_and_structured_metadata(root):
    (root / "arith.py").write_text("print(6 * 7)\n")
    result = invoke("execute_notebook", path="arith.py", timeout_seconds=30)

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "42"
    assert result["stdout_truncated"] is False
    assert result["timeout_seconds"] == 30
    assert isinstance(result["duration_ms"], int)
    assert result["started_at"].endswith("Z")


def test_execute_reports_a_failing_notebook_as_error_not_a_crash(root):
    (root / "boom.py").write_text("raise SystemExit(3)\n")
    result = invoke("execute_notebook", path="boom.py", timeout_seconds=30)
    assert result["status"] == "error"
    assert result["exit_code"] == 3


def test_execute_runs_with_the_notebook_root_as_cwd(root):
    (root / "cwd.py").write_text("import os\nprint(os.getcwd())\n")
    result = invoke("execute_notebook", path="cwd.py", timeout_seconds=30)
    assert result["stdout"].strip() == str(root)


def test_execute_uses_the_configured_notebook_interpreter(root, monkeypatch, tmp_path):
    """Not this process's own interpreter — the sidecar's venv has no marimo."""
    marker = tmp_path / "which_python"
    fake = tmp_path / "fake_python"
    fake.write_text(f'#!/bin/sh\necho "$0" > {marker}\nexec {sys.executable} "$@"\n')
    fake.chmod(0o755)
    monkeypatch.setenv(mcp_server.NOTEBOOK_PYTHON_ENV, str(fake))

    (root / "who.py").write_text("print('ran')\n")
    result = invoke("execute_notebook", path="who.py", timeout_seconds=30)

    assert result["status"] == "ok"
    assert result["stdout"].strip() == "ran"
    assert marker.read_text().strip() == str(fake)


def test_execute_falls_back_to_this_interpreter_when_unset(root, monkeypatch):
    monkeypatch.delenv(mcp_server.NOTEBOOK_PYTHON_ENV, raising=False)
    assert mcp_server.notebook_python() == sys.executable


def test_execute_refuses_a_missing_notebook(root):
    assert refusal("execute_notebook", path="nope.py")["error"] == "not_found"


def test_output_over_the_cap_is_truncated_with_an_explicit_flag(root):
    (root / "loud.py").write_text(
        "import sys\n"
        "sys.stdout.write('o' * 200000)\n"
        "sys.stderr.write('e' * 200000)\n"
    )
    result = invoke("execute_notebook", path="loud.py", timeout_seconds=60)

    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert len(result["stdout"]) == mcp_server.MAX_STREAM_BYTES
    assert len(result["stderr"]) == mcp_server.MAX_STREAM_BYTES


def test_timeout_is_clamped_to_the_hard_cap(root):
    (root / "quick.py").write_text("print('hi')\n")
    result = invoke("execute_notebook", path="quick.py", timeout_seconds=99999)
    assert result["timeout_seconds"] == mcp_server.MAX_TIMEOUT_SECONDS


def test_default_timeout_is_applied_when_none_is_given(root):
    (root / "quick.py").write_text("print('hi')\n")
    result = invoke("execute_notebook", path="quick.py")
    assert result["timeout_seconds"] == mcp_server.DEFAULT_TIMEOUT_SECONDS


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_timeout_kills_the_whole_process_group_including_a_grandchild(root, tmp_path):
    """The response saying "timeout" is not the assertion. The process table is.

    A notebook that outlives its own timeout, or leaves a spawned child
    behind, is the orphan this bound exists to prevent — and the tool's
    answer looks identical in both cases.
    """
    pidfile = tmp_path / "grandchild.pid"
    (root / "spawner.py").write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(9999)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(9999)\n"
    )

    result = invoke("execute_notebook", path="spawner.py", timeout_seconds=3)

    assert result["status"] == "timeout"
    assert result["exit_code"] is None

    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild), f"orphan grandchild {grandchild} survived the timeout"


def _defunct_children() -> list[int]:
    """Our own zombie children, read from /proc — a killed grandchild that
    nobody reaps shows up here and holds a PID slot forever."""
    me = os.getpid()
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "stat").read_text()
        except OSError:
            continue
        fields = status.rsplit(")", 1)[-1].split()
        if len(fields) >= 2 and fields[0] == "Z" and fields[1] == str(me):
            out.append(int(entry.name))
    return out


def test_a_killed_grandchild_is_reaped_not_left_defunct(root, tmp_path):
    """A `<defunct>` entry is dead, but it still holds a PID slot.

    Seen live through the gateway before this was fixed: a 3s timeout on a
    notebook that had spawned a child left `[python] <defunct>` in the
    container's process table, because the grandchild re-parented to PID 1
    (`marimo edit`), which never calls wait(). Subreaper + a group-scoped
    waitpid is what clears it.
    """
    if not mcp_server.become_subreaper():
        pytest.skip("kernel refused PR_SET_CHILD_SUBREAPER")

    before = set(_defunct_children())
    (root / "spawns.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(9999)'])\n"
        "time.sleep(9999)\n"
    )

    assert invoke("execute_notebook", path="spawns.py", timeout_seconds=3)["status"] == "timeout"

    deadline = time.monotonic() + 10
    leaked = set(_defunct_children()) - before
    while leaked and time.monotonic() < deadline:
        time.sleep(0.2)
        leaked = set(_defunct_children()) - before
    assert not leaked, f"killed grandchild left defunct: {sorted(leaked)}"


def test_a_normal_run_also_leaves_no_background_child_behind(root, tmp_path):
    pidfile = tmp_path / "bg.pid"
    (root / "daemonish.py").write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(9999)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "print('done')\n"
    )

    result = invoke("execute_notebook", path="daemonish.py", timeout_seconds=30)
    assert result["status"] == "ok"

    leftover = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _alive(leftover) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(leftover), f"background child {leftover} survived the run"


def test_three_concurrent_executes_run_two_and_refuse_one(root):
    (root / "slow.py").write_text("import time\ntime.sleep(4)\nprint('done')\n")

    async def three():
        return await asyncio.gather(
            *(mcp_server.execute_notebook(path="slow.py", timeout_seconds=30) for _ in range(3))
        )

    results = asyncio.run(three())
    statuses = sorted(r["status"] for r in results)

    assert statuses == ["ok", "ok", "refused_busy"]
    refused = next(r for r in results if r["status"] == "refused_busy")
    assert refused["max"] == mcp_server.MAX_CONCURRENT_EXECUTES
    assert "slot" in refused["stderr"]


def test_the_execute_slot_is_released_after_a_refusal(root):
    """A refusal must not leak the slot it did not take."""
    (root / "quick.py").write_text("print('hi')\n")
    for _ in range(mcp_server.MAX_CONCURRENT_EXECUTES + 2):
        assert invoke("execute_notebook", path="quick.py", timeout_seconds=30)["status"] == "ok"


# ---------------------------------------------------------------------------
# transport: the 421 regression
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post(port: int, host: str, payload: dict):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Host": host,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


@pytest.fixture
def live_sidecar(tmp_path):
    """The real server in its real transport, on a real socket.

    A loopback-only fixture is known to be insufficient here: the 421 is a
    Host-header failure, so a test that only ever speaks to 127.0.0.1
    structurally cannot see it (research-search, 2026-09).
    """
    port = _free_port()
    env = {
        **os.environ,
        mcp_server.NOTEBOOKS_DIR_ENV: str(tmp_path / "notebooks"),
        "MARIMO_MCP_PORT": str(port),
    }
    env.pop("PYTHONPATH", None)
    proc = subprocess.Popen(
        [sys.executable, str(SIDECAR)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError("sidecar exited during startup")
                time.sleep(0.2)
        else:
            raise RuntimeError("sidecar never listened")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_tools_list_succeeds_when_addressed_by_container_host(live_sidecar):
    """The spelling the gateway actually uses: `aw-app-marimo:2719`."""
    status, body = _post(
        live_sidecar,
        f"{mcp_server.CONTAINER_NAME}:{mcp_server.PORT}",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )

    assert status == 200, body
    names = {t["name"] for t in json.loads(body.split("data: ", 1)[1])["result"]["tools"]}
    assert names == {
        "list_notebooks",
        "read_notebook",
        "create_notebook",
        "update_notebook",
        "validate_notebook",
        "execute_notebook",
    }


def test_dns_rebinding_protection_is_still_on(live_sidecar):
    """The 421 fix widens which of OUR names are accepted, nothing more."""
    status, _ = _post(
        live_sidecar,
        "evil.example.com",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert status == 421


def test_allowed_hosts_covers_the_container_name_with_and_without_a_port():
    hosts = mcp_server.allowed_hosts()
    assert mcp_server.CONTAINER_NAME in hosts
    assert f"{mcp_server.CONTAINER_NAME}:{mcp_server.PORT}" in hosts
    assert f"{mcp_server.CONTAINER_NAME}:*" in hosts
    assert socket.gethostname() in hosts
