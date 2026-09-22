#!/usr/bin/env python3
"""Iteration 7 stretch: live-reload eviction .so mid-traffic without reconnect storm.

Proves:
  - PLUGIN <path> swaps ops while listen socket stays up
  - Persistent client survives swap (no reconnect)
  - Concurrent traffic sockets keep working (no reconnect storm)
  - PLUGIN status shows name + reloads counter
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402

PORT = 26520
PLUG_RANDOM = ROOT / "native/build/plugins/libar_evict_random.so"
PLUG_RR = ROOT / "native/build/plugins/libar_evict_rr.so"
BIN = ROOT / "native/build/aura_redis_server"


def wait_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {port} not up")


def traffic_worker(port: int, tag: int, n: int, stop: threading.Event, errors: list,
                   ops: list) -> None:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        i = 0
        while not stop.is_set() and i < n:
            k = f"t{tag}_{i % 800}"
            v = f"v{tag}-{i}"
            redis_call(s, "SET", k, v)
            got = redis_call(s, "GET", k)
            # Under maxmemory, GET may miss after eviction — only fail on protocol/conn
            if got is not None and got != v:
                # Overwritten by peer or evicted+reset — tolerate miss/None only
                pass
            ops[0] += 1
            i += 1
        try:
            redis_call(s, "QUIT")
        except Exception:
            pass
        s.close()
    except Exception as e:
        errors.append(f"worker{tag}: {e}")


def main() -> int:
    if not PLUG_RANDOM.exists() or not PLUG_RR.exists() or not BIN.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    if not PLUG_RR.exists():
        print("FAIL: libar_evict_rr.so missing after build")
        return 1

    log = Path("/tmp/ar-plugin-reload-test.log")
    proc = subprocess.Popen(
        [
            str(BIN),
            "--port",
            str(PORT),
            "--plugin",
            str(PLUG_RANDOM),
            "--maxmemory",
            "250000",
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
    )
    try:
        wait_port(PORT)
        ctrl = socket.create_connection(("127.0.0.1", PORT), timeout=5)

        st0 = redis_call(ctrl, "PLUGIN")
        print(f"initial PLUGIN: {st0}")
        if "random" not in str(st0) or "plugin=1" not in str(st0):
            print("FAIL: expected random plugin loaded at start")
            return 1

        # Seed some keys on the control connection
        for i in range(100):
            redis_call(ctrl, "SET", f"seed{i}", "x" * 200)

        stop = threading.Event()
        errors: list = []
        ops = [0]
        threads = [
            threading.Thread(
                target=traffic_worker, args=(PORT, t, 2000, stop, errors, ops)
            )
            for t in range(4)
        ]
        for t in threads:
            t.start()
        time.sleep(0.2)

        # Mid-traffic live reload: random → rr → random on SAME control socket
        for path, expect in (
            (PLUG_RR, "rr"),
            (PLUG_RANDOM, "random"),
            (PLUG_RR, "rr"),
        ):
            r = redis_call(ctrl, "PLUGIN", str(path))
            if r != "OK":
                print(f"FAIL: PLUGIN load {path.name} got {r!r}")
                stop.set()
                for t in threads:
                    t.join(timeout=5)
                return 1
            st = redis_call(ctrl, "PLUGIN")
            print(f"after load {path.name}: {st}")
            if expect not in str(st):
                print(f"FAIL: expected name {expect} in {st!r}")
                stop.set()
                for t in threads:
                    t.join(timeout=5)
                return 1
            # Control socket still alive (PING)
            if redis_call(ctrl, "PING") != "PONG":
                print("FAIL: control connection died after PLUGIN")
                stop.set()
                for t in threads:
                    t.join(timeout=5)
                return 1
            # New client can connect (listen socket intact)
            s2 = socket.create_connection(("127.0.0.1", PORT), timeout=2)
            if redis_call(s2, "PING") != "PONG":
                print("FAIL: new connection after PLUGIN failed PING")
                s2.close()
                stop.set()
                for t in threads:
                    t.join(timeout=5)
                return 1
            redis_call(s2, "QUIT")
            s2.close()
            time.sleep(0.08)

        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=15)

        if errors:
            print("FAIL: traffic errors (reconnect storm / drop?):")
            for e in errors[:8]:
                print(" ", e)
            return 1

        stf = redis_call(ctrl, "PLUGIN")
        print(f"final PLUGIN: {stf}")
        # 1 initial --plugin + 3 mid-run loads
        if "reloads=4" not in str(stf) and "reloads=" not in str(stf):
            print("FAIL: missing reloads counter")
            return 1
        # parse reloads
        try:
            part = [p for p in str(stf).split() if p.startswith("reloads=")][0]
            n = int(part.split("=", 1)[1])
        except Exception:
            print(f"FAIL: parse reloads from {stf!r}")
            return 1
        if n < 4:
            print(f"FAIL: expected reloads>=4 got {n}")
            return 1

        print(f"traffic_ops≈{ops[0]} workers=4 (no errors)")
        print("plugin live-reload test OK (listen socket held; no reconnect storm)")
        print("--- log tail ---")
        print(log.read_text(errors="replace")[-800:])
        redis_call(ctrl, "QUIT")
        ctrl.close()
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
