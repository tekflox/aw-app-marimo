"""Make `container/mcp_server.py` importable as `mcp_server`.

The sidecar is not a package — it is a single file copied to
`/aw-mcp-server.py` in the image and run directly. Tests import it from its
source location rather than a shipped module.
"""
import sys
from pathlib import Path

CONTAINER_DIR = Path(__file__).resolve().parent.parent / "container"
if str(CONTAINER_DIR) not in sys.path:
    sys.path.insert(0, str(CONTAINER_DIR))
