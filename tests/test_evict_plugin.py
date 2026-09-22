#!/usr/bin/env python3
"""Iteration 7: load random eviction plugin .so under maxmemory."""
from __future__ import annotations
import socket, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402


def main() -> int:
    port = 26510
    plugin = ROOT / "native/build/plugins/libar_evict_random.so"
    bin_path = ROOT / "native/build/aura_redis_server"
    if not plugin.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    log = Path("/tmp/ar-plugin-test.log")
    proc = subprocess.Popen(
        [str(bin_path), "--port", str(port), "--plugin", str(plugin),
         "--maxmemory", "200000"],
        stdout=log.open("w"), stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(0.3)
        text = log.read_text(errors="replace")
        if "loaded eviction plugin" not in text and "name=random" not in text:
            # banner may be on stderr merged
            print(text)
            if "dlopen" in text.lower() or "failed" in text.lower():
                print("FAIL: plugin load")
                return 1
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        val = "v" * 1000
        for i in range(500):
            redis_call(s, "SET", f"k{i}", val)
        survivors = sum(1 for i in range(500) if redis_call(s, "EXISTS", f"k{i}") == 1)
        print(f"plugin=random set=500 survivors={survivors}")
        if survivors >= 500:
            print("FAIL: expected eviction under maxmemory")
            return 1
        redis_call(s, "QUIT")
        s.close()
        print("evict plugin test OK")
        print("--- log ---")
        print(log.read_text(errors="replace")[-500:])
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
