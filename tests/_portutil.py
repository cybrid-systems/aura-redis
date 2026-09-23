"""Port cleanup helpers for prod tests (tolerate missing fuser in CI images)."""
from __future__ import annotations

import subprocess


def kill_tcp_port(port: int | str) -> None:
    """Best-effort free of TCP listeners on *port*. No-op if fuser is absent."""
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        pass
