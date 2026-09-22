#!/usr/bin/env python3
"""Iteration 8: layout migrate flat ↔ hot_cold; GET/SET correct under load."""
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


def worker(port: int, start: int, n: int, errors: list, stop: threading.Event):
    """Load traffic on w* keys (separate from seeded k* keys)."""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        i = 0
        while not stop.is_set() and i < n:
            k = f"w{(start + i) % 2000}"
            v = f"wv{start + i}"
            redis_call(s, "SET", k, v)
            got = redis_call(s, "GET", k)
            if got != v:
                errors.append(f"mismatch {k}: {got!r} != {v!r}")
                break
            i += 1
        try:
            redis_call(s, "QUIT")
        except Exception:
            pass
        s.close()
    except Exception as e:
        errors.append(str(e))


def main() -> int:
    port = 26620
    bin_path = ROOT / "native/build/aura_redis_server"
    if not bin_path.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    log = Path("/tmp/ar-layout-test.log")
    proc = subprocess.Popen(
        [str(bin_path), "--port", str(port), "--layout", "flat"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
    )
    try:
        wait_port(port)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)

        st = redis_call(s, "LAYOUT")
        print(f"initial LAYOUT: {st}")
        if not str(st).startswith("flat"):
            print("FAIL: expected flat layout")
            return 1

        N = 1500
        for i in range(N):
            redis_call(s, "SET", f"k{i}", f"val-{i}")

        ok = redis_call(s, "LAYOUT", "hot_cold")
        if ok != "OK":
            print(f"FAIL: migrate hot_cold got {ok!r}")
            return 1
        st = redis_call(s, "LAYOUT")
        print(f"after migrate: {st}")
        if not str(st).startswith("hot_cold"):
            print("FAIL: expected hot_cold")
            return 1

        bad = 0
        for i in range(0, N, 7):
            got = redis_call(s, "GET", f"k{i}")
            if got != f"val-{i}":
                bad += 1
                if bad <= 3:
                    print(f"  bad k{i}={got!r}")
        if bad:
            print(f"FAIL: {bad} wrong GETs after migrate")
            return 1
        print(f"spot-check OK ({(N + 6) // 7} keys)")

        # Concurrent load on w* while migrating layouts
        stop = threading.Event()
        errors: list = []
        threads = [
            threading.Thread(target=worker, args=(port, t * 100, 500, errors, stop))
            for t in range(4)
        ]
        for t in threads:
            t.start()
        time.sleep(0.15)
        for name in ("flat", "hot_cold", "flat", "hot_cold"):
            r = redis_call(s, "LAYOUT", name)
            if r != "OK":
                print(f"FAIL: migrate {name} under load got {r!r}")
                stop.set()
                for t in threads:
                    t.join(timeout=5)
                return 1
            time.sleep(0.05)
        time.sleep(0.25)
        stop.set()
        for t in threads:
            t.join(timeout=10)
        if errors:
            print("FAIL under load:", errors[:5])
            return 1
        print("concurrent migrate OK")

        # Seeded keys must still be intact after migrates + alien traffic
        bad = 0
        for i in range(0, N, 11):
            got = redis_call(s, "GET", f"k{i}")
            if got != f"val-{i}":
                bad += 1
        if bad:
            print(f"FAIL: {bad} seeded keys corrupted")
            return 1

        st = redis_call(s, "LAYOUT")
        print(f"LAYOUT before demote pressure: {st}")

        # Overflow hot soft-cap → demotions; then GET cold keys → promotions
        for i in range(N, N + 1200):
            redis_call(s, "SET", f"extra{i}", f"x-{i}")
        st_d = redis_call(s, "LAYOUT")
        print(f"after overflow SETs: {st_d}")
        # GET older seeded keys (likely demoted)
        for i in range(0, min(400, N)):
            got = redis_call(s, "GET", f"k{i}")
            if got != f"val-{i}":
                print(f"FAIL promote-path get k{i}={got!r}")
                return 1
        st_p = redis_call(s, "LAYOUT")
        print(f"after promote GETs: {st_p}")
        # Expect some demotions and ideally promotions in status string
        if "demo=" in str(st_d):
            demo = int(str(st_d).split("demo=")[1].split()[0])
            if demo < 1:
                print("WARN: expected demotions after overflow (soft-cap)")

        redis_call(s, "QUIT")
        s.close()

        logtxt = log.read_text(errors="replace")
        migrates = logtxt.count("layout migrate")
        print(f"server migrate log lines: {migrates}")
        print("--- server log (tail) ---")
        print(logtxt[-500:])
        if migrates < 1:
            print("FAIL: expected migrate log lines")
            return 1
        print("layout migrate test OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
