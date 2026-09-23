#!/usr/bin/env python3
"""Hit-quality side-by-side: aura adaptive/lru/lfu/slru vs Redis allkeys-lru/lfu.

Redis has fixed maxmemory-policy only — no live EVICT/PIN/LAYOUT. Adaptive
path is Aura-only. This script still measures Redis hit% on the *same*
client-observed useful-GET workloads so tables stay comparable.

Emits JSON to stdout (last object) and optional --md / --json paths.

Workloads (simplified marathon-family, client-side hit accounting):
  phase_marathon, zipf, hot_protect, ws_shift, oscillate
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402

DEFAULT_AURA_PORT = 26480
DEFAULT_REDIS_PORT = 26479
IMG_REDIS = os.environ.get("REDIS_IMAGE", "redis:7-alpine")
IMG_DEV = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")


@dataclass
class HitResult:
    engine: str  # aura|redis
    policy: str
    workload: str
    hits: int = 0
    misses: int = 0
    useful_gets: int = 0
    notes: str = ""

    @property
    def hit_pct(self) -> float:
        t = self.hits + self.misses
        return (100.0 * self.hits / t) if t else 0.0


def kill_port(port: int) -> None:
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    time.sleep(0.1)


def wait_listen(port: int, timeout: float = 8.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), 0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"port {port} not listening")


def start_aura(port: int, evict: str, maxmemory: int, adaptive: bool) -> Tuple[subprocess.Popen, Optional[str]]:
    kill_port(port)
    bin_path = ROOT / "native/build/aura_redis_server"
    if not bin_path.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    log = Path(f"/tmp/ar-hit-aura-{port}.log")
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    proc = subprocess.Popen(
        [str(bin_path), "--port", str(port), "--evict", evict, "--maxmemory", str(maxmemory)],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    wait_listen(port)
    cid = None
    if adaptive:
        boot = ROOT / f".ar-agent-booted-{port}.flag"
        boot.unlink(missing_ok=True)
        cmd = [
            "sudo", "docker", "run", "-d", "--network", "host", "--entrypoint", "",
            "-v", f"{ROOT}:/work", "-v", "/tmp:/tmp", "-w", "/work",
            "-e", "AURA_SANDBOX=off",
            "-e", "AURA_PIPELINE_STRICT=0",
            "-e", "AURA_PATH=/work/.deps/aura/lib",
            "-e", f"AURA_REDIS_PORT={port}",
            "-e", "AURA_REDIS_HOST=127.0.0.1",
            "-e", "AURA_REDIS_POLICY_MS=100",
            "-e", "AURA_REDIS_DENY_PLUGIN=1",
            "-e", "AURA_REDIS_FITNESS_MUTATE=1",
            IMG_DEV,
            "/work/.deps/aura/build/aura",
            "/work/src/redis/policy_agent.aura",
        ]
        cid = subprocess.check_output(cmd, text=True).strip()
        for _ in range(80):
            if boot.exists():
                break
            time.sleep(0.1)
        time.sleep(0.4)
    return proc, cid


def start_redis(port: int, policy: str, maxmemory: int) -> str:
    """policy: allkeys-lru | allkeys-lfu

    Redis used_memory at idle is ~1MB; aura maxmemory is a *data* budget.
    We start without a tight cap, measure baseline, then CONFIG SET
    maxmemory = baseline + data_budget so eviction pressure matches intent.
    """
    kill_port(port)
    name = f"ar-hit-redis-{port}-{os.getpid()}"
    cid = subprocess.check_output(
        [
            "sudo", "docker", "run", "-d", "--network", "host", "--name", name,
            IMG_REDIS,
            "redis-server",
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
            "--maxmemory-policy", policy,
        ],
        text=True,
    ).strip()
    wait_listen(port)
    s = socket.create_connection(("127.0.0.1", port), 5)
    try:
        info = redis_call(s, "INFO", "memory")
        base = 0
        for ln in str(info).splitlines():
            if ln.startswith("used_memory:"):
                base = int(ln.split(":")[1])
                break
        # Redis idle used_memory ~1MB. Use a *small* data headroom so eviction
        # fires comparably to aura maxmemory (data-only). Default headroom 100KiB
        # unless AURA_REDIS_REDIS_HEADROOM overrides.
        headroom = int(os.environ.get("AURA_REDIS_REDIS_HEADROOM", "150000"))
        cap = max(base + headroom, base + 1)
        assert redis_call(s, "CONFIG", "SET", "maxmemory", str(cap)) == "OK"
        assert redis_call(s, "CONFIG", "SET", "maxmemory-policy", policy) == "OK"
        print(f"   redis baseline_used={base} maxmemory={cap} policy={policy}")
    finally:
        s.close()
    return cid


def stop_aura(proc: subprocess.Popen, cid: Optional[str]) -> None:
    if cid:
        subprocess.run(["sudo", "docker", "rm", "-f", cid], capture_output=True)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def stop_redis(cid: str) -> None:
    subprocess.run(["sudo", "docker", "rm", "-f", cid], capture_output=True)


def connect(port: int) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), 5)
    redis_call(s, "PING")
    return s


def flush(s: socket.socket, reseat_redis_headroom: int | None = None) -> None:
    try:
        redis_call(s, "FLUSHDB")
    except RuntimeError:
        pass
    # Redis jemalloc often keeps used_memory high after FLUSHDB; re-seat maxmemory
    # so each phase gets a comparable data budget.
    if reseat_redis_headroom is not None:
        try:
            info = str(redis_call(s, "INFO", "memory"))
            base = 0
            for ln in info.splitlines():
                if ln.startswith("used_memory:"):
                    base = int(ln.split(":")[1])
                    break
            cap = base + reseat_redis_headroom
            redis_call(s, "CONFIG", "SET", "maxmemory", str(cap))
        except Exception:
            pass


def get_hit(s: socket.socket, key: str) -> bool:
    return redis_call(s, "GET", key) is not None


def _set_allow_evict(s: socket.socket, key: str, val: str) -> bool:
    """SET; on Redis OOM retry once (eviction may free space). Returns ok."""
    try:
        redis_call(s, "SET", key, val)
        return True
    except RuntimeError as e:
        if "OOM" not in str(e):
            raise
        time.sleep(0.005)
        try:
            redis_call(s, "SET", key, val)
            return True
        except RuntimeError as e2:
            if "OOM" not in str(e2):
                raise
            return False


def workload_zipf(s: socket.socket, seed: int = 7, engine: str = "aura") -> HitResult:
    """Boost hot set, cold flood, measure hot retention via GET."""
    rng = random.Random(seed)
    flush(s, _REDIS_HEADROOM if engine == "redis" else None)
    val = "v" * 400
    nkeys = 220
    hot_n = 20
    for i in range(nkeys):
        _set_allow_evict(s, f"z{i}", val)
    for _ in range(600):
        if rng.random() < 0.8:
            k = rng.randrange(hot_n)
        else:
            k = rng.randrange(nkeys)
        redis_call(s, "GET", f"z{k}")
    # cold flood
    for i in range(nkeys, nkeys + 400):
        _set_allow_evict(s, f"cold{i}", val)
    hits = misses = 0
    for i in range(hot_n):
        if get_hit(s, f"z{i}"):
            hits += 1
        else:
            misses += 1
    return HitResult("", "", "zipf", hits, misses, hits + misses)


def workload_hot_protect(s: socket.socket, engine: str = "aura") -> HitResult:
    flush(s, _REDIS_HEADROOM if engine == "redis" else None)
    val = "v" * 400
    hot = 16
    for i in range(hot):
        _set_allow_evict(s, f"hot{i:04d}", val)
        for _ in range(40):
            redis_call(s, "GET", f"hot{i:04d}")
    for i in range(500):
        _set_allow_evict(s, f"cold{i:05d}", val)
    hits = misses = 0
    for i in range(hot):
        if get_hit(s, f"hot{i:04d}"):
            hits += 1
        else:
            misses += 1
    return HitResult("", "", "hot_protect", hits, misses, hits + misses)


def workload_ws_shift(s: socket.socket, engine: str = "aura") -> HitResult:
    flush(s, _REDIS_HEADROOM if engine == "redis" else None)
    val = "v" * 400
    nA, nB = 40, 40
    for i in range(nA):
        _set_allow_evict(s, f"A{i:04d}", val)
        for _ in range(25):
            redis_call(s, "GET", f"A{i:04d}")
    # shift to B under pressure
    for i in range(nB):
        _set_allow_evict(s, f"B{i:04d}", val)
    for _ in range(4):
        for i in range(nB):
            redis_call(s, "GET", f"B{i:04d}")
    for i in range(200):
        _set_allow_evict(s, f"flood{i}", val)
    hits = misses = 0
    for i in range(nB):
        if get_hit(s, f"B{i:04d}"):
            hits += 1
        else:
            misses += 1
    return HitResult("", "", "ws_shift", hits, misses, hits + misses)


def workload_phase_marathon(s: socket.socket, engine: str) -> HitResult:
    """Sequential zipf → ws_shift → hot_protect; cumulative useful GETs."""
    total_h = total_m = 0
    for wl_fn in (workload_zipf, workload_ws_shift, workload_hot_protect):
        # small bridge settle
        try:
            redis_call(s, "SET", "__br0", "b" * 32)
            redis_call(s, "GET", "__br0")
        except RuntimeError:
            pass
        time.sleep(0.15 if engine == "aura" else 0.05)
        r = wl_fn(s, engine=engine) if wl_fn is not workload_zipf else wl_fn(s, engine=engine)
        total_h += r.hits
        total_m += r.misses
    return HitResult("", "", "phase_marathon", total_h, total_m, total_h + total_m)


def workload_oscillate(s: socket.socket, engine: str = "aura") -> HitResult:
    r1 = workload_hot_protect(s, engine=engine)
    flush(s, _REDIS_HEADROOM if engine == "redis" else None)
    r2 = workload_ws_shift(s, engine=engine)
    return HitResult(
        "", "", "oscillate",
        r1.hits + r2.hits, r1.misses + r2.misses,
        r1.useful_gets + r2.useful_gets,
    )


WORKLOADS: Dict[str, Callable] = {
    "zipf": lambda s, eng: workload_zipf(s, engine=eng),
    "hot_protect": lambda s, eng: workload_hot_protect(s, engine=eng),
    "ws_shift": lambda s, eng: workload_ws_shift(s, engine=eng),
    "oscillate": lambda s, eng: workload_oscillate(s, engine=eng),
    "phase_marathon": lambda s, eng: workload_phase_marathon(s, eng),
}


def run_aura_case(
    port: int, policy: str, workload: str, maxmemory: int
) -> HitResult:
    adaptive = policy == "adaptive"
    evict = "lru" if adaptive else policy
    if policy == "slru":
        evict = "slru"
    proc, cid = start_aura(port, evict, maxmemory, adaptive)
    try:
        s = connect(port)
        fn = WORKLOADS[workload]
        r = fn(s, "aura")
        r.engine = "aura"
        r.policy = policy
        r.workload = workload
        try:
            redis_call(s, "QUIT")
        except Exception:
            pass
        s.close()
        return r
    finally:
        stop_aura(proc, cid)
        kill_port(port)


# Module default for post-FLUSHDB reseat (set by run_redis_case)
_REDIS_HEADROOM = int(os.environ.get("AURA_REDIS_REDIS_HEADROOM", "150000"))


def run_redis_case(
    port: int, policy: str, workload: str, maxmemory: int
) -> HitResult:
    # policy: allkeys-lru | allkeys-lfu  (short labels lru/lfu in tables)
    global _REDIS_HEADROOM
    full = "allkeys-lru" if policy in ("lru", "allkeys-lru") else "allkeys-lfu"
    cid = start_redis(port, full, maxmemory)
    try:
        s = connect(port)
        fn = WORKLOADS[workload]
        r = fn(s, "redis")
        r.engine = "redis"
        r.policy = "lru" if "lru" in full else "lfu"
        r.workload = workload
        r.notes = f"maxmemory-policy={full}"
        try:
            redis_call(s, "QUIT")
        except Exception:
            pass
        s.close()
        return r
    finally:
        stop_redis(cid)
        kill_port(port)


def print_md_table(results: List[HitResult]) -> str:
    lines = [
        "| Workload | Engine | Policy | Hit% | Useful GET hits | Misses | Notes |",
        "|----------|--------|--------|------|-----------------|--------|-------|",
    ]
    for r in sorted(results, key=lambda x: (x.workload, x.engine, x.policy)):
        lines.append(
            f"| {r.workload} | {r.engine} | {r.policy} | {r.hit_pct:.1f} | "
            f"{r.hits} | {r.misses} | {r.notes or '—'} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workloads",
        default="phase_marathon,zipf,hot_protect,ws_shift,oscillate",
    )
    ap.add_argument(
        "--aura-policies",
        default="lru,lfu,slru,adaptive",
        help="aura kernels / adaptive",
    )
    ap.add_argument(
        "--redis-policies",
        default="lru,lfu",
        help="mapped to allkeys-lru / allkeys-lfu",
    )
    ap.add_argument("--aura-port", type=int, default=DEFAULT_AURA_PORT)
    ap.add_argument("--redis-port", type=int, default=DEFAULT_REDIS_PORT)
    ap.add_argument("--maxmemory", type=int, default=120_000)
    ap.add_argument("--json-out", type=str, default="")
    ap.add_argument("--md-out", type=str, default="")
    ap.add_argument("--skip-adaptive", action="store_true")
    args = ap.parse_args()

    workloads = [w.strip() for w in args.workloads.split(",") if w.strip()]
    aura_pols = [p.strip() for p in args.aura_policies.split(",") if p.strip()]
    if args.skip_adaptive:
        aura_pols = [p for p in aura_pols if p != "adaptive"]
    redis_pols = [p.strip() for p in args.redis_policies.split(",") if p.strip()]

    results: List[HitResult] = []
    for wl in workloads:
        for pol in aura_pols:
            print(f">> aura {pol} / {wl} ...", flush=True)
            r = run_aura_case(args.aura_port, pol, wl, args.maxmemory)
            print(f"   hit={r.hit_pct:.1f}% hits={r.hits}/{r.hits+r.misses}")
            results.append(r)
        for pol in redis_pols:
            print(f">> redis allkeys-{pol} / {wl} ...", flush=True)
            r = run_redis_case(args.redis_port, pol, wl, args.maxmemory)
            print(f"   hit={r.hit_pct:.1f}% hits={r.hits}/{r.hits+r.misses}")
            results.append(r)

    md = print_md_table(results)
    print("\n# Hit-quality scoreboard (client useful-GET)\n")
    print(md)
    print(
        "\nNote: Redis baseline is **fixed-policy only** (allkeys-lru / allkeys-lfu). "
        "Aura adaptive uses policy_agent live EVICT/PIN — not available on Redis.\n"
    )

    payload = {
        "kind": "hit_quality",
        "maxmemory": args.maxmemory,
        "results": [asdict(r) | {"hit_pct": r.hit_pct} for r in results],
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"Wrote {args.json_out}")
    if args.md_out:
        Path(args.md_out).write_text(
            "# Hit-quality scoreboard\n\n"
            + md
            + "\n\nRedis = fixed allkeys-lru/lfu only; Aura adaptive is Aura-only.\n"
        )
        print(f"Wrote {args.md_out}")
    print(json.dumps({"summary": "ok", "n": len(results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
