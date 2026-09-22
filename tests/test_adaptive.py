#!/usr/bin/env python3
"""Load patterns for Iteration 6 adaptive supervisor demo/test.

Phases:
  write — many SETs (write-heavy) → supervisor should prefer lfu
  read  — warm keys then GET storm (read-heavy, high hit) → prefer lru
  both  — run write then read; optionally assert log file contains swaps

Can also drive a full self-contained test with --spawn (Aura via docker).
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call, encode_array  # noqa: E402


def blast_write(sock: socket.socket, n: int, vlen: int = 64) -> None:
    val = "W" * vlen
    for i in range(n):
        redis_call(sock, "SET", f"w{i % 2000}", val)


_read_warmed = False

def blast_read(sock: socket.socket, n: int, keyspace: int = 200) -> None:
    global _read_warmed
    # Warm once so the window is GET-dominated (read-heavy → lru)
    if not _read_warmed:
        for i in range(keyspace):
            redis_call(sock, "SET", f"r{i}", f"v{i}")
        _read_warmed = True
    for i in range(n):
        redis_call(sock, "GET", f"r{i % keyspace}")


def run_phase(port: int, phase: str, seconds: float) -> None:
    deadline = time.time() + seconds
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        redis_call(s, "PING")
        while time.time() < deadline:
            if phase == "write":
                blast_write(s, 400)
            else:
                blast_read(s, 800)
    finally:
        try:
            redis_call(s, "QUIT")
        except Exception:
            pass
        s.close()


def spawn_adaptive(port: int, log: Path) -> subprocess.Popen:
    img = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")
    so = ROOT / "native/build/libaura_redis_core.so"
    if not so.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    cmd = [
        "sudo", "docker", "run", "--rm", "--network", "host",
        "--entrypoint", "",
        "-v", f"{ROOT}:/work", "-w", "/work",
        "-e", "AURA_SANDBOX=off",
        "-e", "AURA_PIPELINE_STRICT=0",
        "-e", "AURA_PATH=/work/.deps/aura/lib",
        "-e", f"AURA_REDIS_PORT={port}",
        "-e", "AURA_REDIS_ENGINE=ffi",
        "-e", "AURA_REDIS_CORE_SO=/work/native/build/libaura_redis_core.so",
        "-e", "AURA_REDIS_MAXMEMORY=500000",
        "-e", "AURA_REDIS_EVICT=lru",
        "-e", "AURA_REDIS_ADAPTIVE=1",
        img,
        "/work/.deps/aura/build/aura",
        "/work/src/redis/server_ffi.aura",
    ]
    log.parent.mkdir(parents=True, exist_ok=True)
    f = log.open("w")
    return subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)


def wait_listen(log: Path, timeout: float = 30.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if log.exists():
            text = log.read_text(errors="replace")
            if "listening on" in text or "adaptive supervisor ON" in text:
                # C listen banner is "listening on"
                if "listening on" in text:
                    return
        time.sleep(0.2)
    raise TimeoutError(f"server did not listen; log:\n{log.read_text(errors='replace')}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=26421)
    p.add_argument("--phase", choices=("write", "read", "both"), default="both")
    p.add_argument("--seconds", type=float, default=2.5)
    p.add_argument("--spawn", action="store_true",
                   help="Start adaptive Aura server via docker, run both phases, assert swaps")
    p.add_argument("--log", type=Path, default=Path("/tmp/ar-adaptive-test.log"))
    args = p.parse_args()

    proc = None
    if args.spawn:
        args.log.write_text("")
        proc = spawn_adaptive(args.port, args.log)
        try:
            wait_listen(args.log)
            print("test_adaptive: phase write-heavy")
            run_phase(args.port, "write", args.seconds)
            print("test_adaptive: phase read-heavy")
            run_phase(args.port, "read", args.seconds)
            time.sleep(0.6)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        text = args.log.read_text(errors="replace")
        print("--- server log (tail) ---")
        print("\n".join(text.strip().splitlines()[-40:]))
        swaps = [ln for ln in text.splitlines() if "adaptive: swap" in ln]
        print(f"test_adaptive: swaps={len(swaps)}")
        for ln in swaps:
            print(f"  {ln}")
        if len(swaps) < 1:
            print("FAIL: no adaptive: swap lines")
            return 1
        # Prefer seeing both directions when both phases ran
        joined = "\n".join(swaps)
        if "lfu" not in joined:
            print("WARN: expected an lfu swap under write-heavy load")
        if "lru" not in joined and len(swaps) >= 2:
            print("WARN: expected an lru swap under read-heavy load")
        print("test_adaptive: OK")
        return 0

    if args.phase == "both":
        run_phase(args.port, "write", args.seconds)
        run_phase(args.port, "read", args.seconds)
    else:
        run_phase(args.port, args.phase, args.seconds)
    print(f"test_adaptive: phase={args.phase} done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
