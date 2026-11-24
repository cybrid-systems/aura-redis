#!/usr/bin/env python3
"""ZRANGEBYSCORE p99 benchmark (aura-redis native data plane).

The benchmark is deterministic: it fills one sorted set with 2000 members,
warms the command path, then measures a fixed number of ZRANGEBYSCORE calls
that return 200 members. Latency is CPU wall time as observed by this Python
client; compare two runs on the same machine, never across hosts.

Baseline example (before a change):
  python3 scripts/bench_zset_p99.py --json /tmp/p99-baseline.json

Candidate example (after a change):
  python3 scripts/bench_zset_p99.py --baseline-json /tmp/p99-baseline.json

Exit status is non-zero when p99 regresses past --max-regression.
No network and no secrets are used.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(bin_path: Path, lib_dir: Path | None, port: int):
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    if lib_dir is not None:
        env["LD_LIBRARY_PATH"] = str(lib_dir)
    log_path = Path(tempfile.gettempdir()) / f"aura-redis-bench-zset-{port}.log"
    log = log_path.open("w")
    proc = subprocess.Popen(
        [str(bin_path), "--port", str(port), "--evict", "noop", "--maxmemory", "0"],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError(log_path.read_text(errors="replace"))
        if "listening on" in log_path.read_text(errors="replace"):
            return proc
        time.sleep(0.05)
    proc.terminate()
    raise TimeoutError(log_path.read_text(errors="replace"))


def run(bin_path: Path, lib_dir: Path | None, iterations: int, members: int,
        rounds: int):
    port = free_port()
    proc = start_server(bin_path, lib_dir, port)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        batch = 30  # the native ZADD parser caps one call at 32 pairs
        for start in range(0, members, batch):
            args = ["ZADD", "bench:z"]
            for i in range(start, min(start + batch, members)):
                args.extend([str(i), f"m{i}"])
            redis_call(sock, *args)

        # Warm the parse/dispatch path and the client loop.
        for _ in range(1000):
            redis_call(sock, "ZRANGEBYSCORE", "bench:z", "500", "700")

        round_p99 = []
        round_p50 = []
        for _ in range(rounds):
            lat = []
            for _ in range(iterations):
                t0 = time.perf_counter_ns()
                redis_call(sock, "ZRANGEBYSCORE", "bench:z", "500", "700")
                lat.append(time.perf_counter_ns() - t0)
            lat.sort()
            round_p50.append(lat[len(lat) // 2] / 1000.0)
            round_p99.append(lat[int(len(lat) * 0.99)] / 1000.0)
        sock.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # The VM is shared, so a single round's tail is dominated by scheduling.
    # Report the median of per-round p50/p99; keep every round for auditing.
    round_p50.sort()
    round_p99.sort()
    mid = rounds // 2
    return {"iterations": iterations, "members": members, "rounds": rounds,
            "p50_us": round(round_p50[mid], 2),
            "p99_us": round(round_p99[mid], 2),
            "p99_rounds_us": [round(v, 2) for v in round_p99]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=str(ROOT / "native/build/aura_redis_server"))
    ap.add_argument("--lib-dir", default=None,
                    help="directory with libaura_redis_core.so (for a baseline copy)")
    ap.add_argument("--iterations", type=int, default=5000)
    ap.add_argument("--rounds", type=int, default=5,
                    help="independent measurement rounds; the median p99 is reported")
    ap.add_argument("--members", type=int, default=2000)
    ap.add_argument("--json", default=None, help="write the result JSON here")
    ap.add_argument("--baseline-json", default=None,
                    help="compare p99 against this earlier result")
    ap.add_argument("--max-regression", type=float, default=1.25)
    args = ap.parse_args()

    result = run(Path(args.bin), Path(args.lib_dir) if args.lib_dir else None,
                 args.iterations, args.members, args.rounds)
    result["bin"] = str(args.bin)
    print(json.dumps(result, sort_keys=True))
    if args.json:
        Path(args.json).write_text(json.dumps(result, sort_keys=True) + "\n")

    if args.baseline_json:
        base = json.loads(Path(args.baseline_json).read_text())
        ratio = result["p99_us"] / base["p99_us"] if base["p99_us"] else 0.0
        print(f"baseline_p99_us={base['p99_us']} ratio={ratio:.3f} "
              f"limit={args.max_regression:.2f}")
        if ratio > args.max_regression:
            print("p99 REGRESSED", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
