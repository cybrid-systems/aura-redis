#!/usr/bin/env python3
"""P0.5 — graceful SIGTERM/SIGINT shutdown."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26905"))


def start_server() -> tuple[subprocess.Popen, Path]:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.05)
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    bin_path = ROOT / "native/build/aura_redis_server"
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-shutdown-{PORT}.log")
    proc = subprocess.Popen(
        [str(bin_path), "--port", str(PORT), "--evict", "lru"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(80):
        if "listening on" in log.read_text(errors="replace"):
            return proc, log
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def test_sigterm_clean_exit() -> None:
    proc, log = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "SET", "a", "1") == "OK"
            assert redis_call(sock, "PING") == "PONG"

        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("SIGTERM did not exit within 5s")
        assert rc == 0, f"exit code {rc}, log:\n{log.read_text()}"
        txt = log.read_text(errors="replace")
        assert "graceful shutdown complete" in txt, txt

        # Port should be free / connection refused
        time.sleep(0.1)
        refused = False
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                pass
        except OSError:
            refused = True
        assert refused, "server still accepting after shutdown"
        print("SIGTERM clean exit OK")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_sigint_clean_exit() -> None:
    proc, log = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "PING") == "PONG"
        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=5)
        assert rc == 0, f"exit code {rc}"
        assert "graceful shutdown complete" in log.read_text(errors="replace")
        print("SIGINT clean exit OK")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def main() -> int:
    test_sigterm_clean_exit()
    test_sigint_clean_exit()
    print("test_prod_shutdown: ALL PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
