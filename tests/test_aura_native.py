#!/usr/bin/env python3
"""Aura-native control plane test: RESP EVICT/INFO + optional full demo.

Modes:
  --evict-smoke   C server only: EVICT/INFO/DENY_PLUGIN (default, fast)
  --unit-aura     run tests/test_hot_strategy_policy.aura in docker
  --demo          run scripts/demo-aura-native.sh
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
from smoke_client import redis_call  # noqa: E402


def start_server(port: int, deny_plugin: bool = True) -> subprocess.Popen:
    env = os.environ.copy()
    if deny_plugin:
        env["AURA_REDIS_DENY_PLUGIN"] = "1"
    cmd = [
        str(ROOT / "native/build/aura_redis_server"),
        "--port", str(port),
        "--evict", "noop",
        "--maxmemory", "500000",
    ]
    log = Path(os.environ.get("TMPDIR", "/tmp")) / f"aura-redis-test-{port}.log"
    f = log.open("w")
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
    t0 = time.time()
    while time.time() - t0 < 5:
        if "listening on" in log.read_text(errors="replace"):
            return p
        if p.poll() is not None:
            raise RuntimeError(f"server exited: {log.read_text()}")
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def evict_smoke(port: int) -> None:
    so = ROOT / "native/build/libaura_redis_core.so"
    if not so.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    p = start_server(port, deny_plugin=True)
    try:
        s = socket.create_connection(("127.0.0.1", port), 2)
        assert redis_call(s, "EVICT") == "noop"
        assert redis_call(s, "EVICT", "lru") == "OK"
        assert redis_call(s, "EVICT") == "lru"
        assert redis_call(s, "EVICT", "lfu") == "OK"
        info = redis_call(s, "INFO")
        assert "evict:lfu" in info
        assert "gets:" in info
        plug = ROOT / "native/build/plugins/libar_evict_random.so"
        try:
            redis_call(s, "PLUGIN", str(plug))
            raise AssertionError("PLUGIN should be denied")
        except RuntimeError as e:
            assert "denied" in str(e).lower() or "PLUGIN" in str(e)
        # LAYOUT still works
        assert "flat" in redis_call(s, "LAYOUT") or redis_call(s, "LAYOUT", "flat") == "OK"
        s.close()
        print("PASS test_aura_native --evict-smoke")
    finally:
        p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()


def unit_aura() -> None:
    img = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")
    cmd = [
        "sudo", "docker", "run", "--rm", "--network", "host", "--entrypoint", "",
        "-v", f"{ROOT}:/work", "-w", "/work",
        "-e", "AURA_SANDBOX=off",
        "-e", "AURA_PIPELINE_STRICT=0",
        "-e", "AURA_PATH=/work/.deps/aura/lib",
        img,
        "/work/.deps/aura/build/aura",
        "/work/tests/test_hot_strategy_policy.aura",
    ]
    print("+", " ".join(cmd))
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    print(out)
    if "PASS test_hot_strategy_policy" not in out:
        raise SystemExit("unit-aura failed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=26621)
    ap.add_argument("--evict-smoke", action="store_true")
    ap.add_argument("--unit-aura", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    if args.demo:
        subprocess.check_call([str(ROOT / "scripts/demo-aura-native.sh"), str(args.port)])
        return
    if args.unit_aura:
        unit_aura()
        return
    # default
    evict_smoke(args.port)
    if args.unit_aura is False and not args.evict_smoke:
        # also run unit when no flag? keep fast default = evict only
        pass


if __name__ == "__main__":
    main()
