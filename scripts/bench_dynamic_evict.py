#!/usr/bin/env python3
"""Dynamic eviction workloads: static LRU vs LFU vs Aura-adaptive.

Design goal: show cases where fixed LRU loses, and adaptive (lru↔lfu via
RESP EVICT, mirroring policy choose_normal / policy_agent rules) tracks the
better kernel across phases.

Workloads
---------
1. hot_protect (LFU-favoring)
   Write-prelude (adaptive → lfu) → insert+boost small hot set under LFU
   → unique cold flood past maxmemory → GET hot set.
   LRU evicts hot (recency); LFU/adaptive keep them (frequency).

2. ws_shift (LRU-favoring)
   Boost set A heavily → stop using A; drive set B under memory pressure.
   LFU clings to A and thrashes B; LRU follows the new working set.

3. oscillate
   hot_protect → FLUSHDB → read-heavy bridge (adaptive → lru) → ws_shift
   on one server lifetime. Adaptive should be close to best-of {lru,lfu}
   on each phase.

Adaptive path (default): Python controller with the same rules as
src/redis/policy/choose_normal.aura (min-ops=80, write-heavy→lfu,
read-heavy+hit≥60%→lru). Optional --aura-agent uses policy_agent.aura
in Docker against the C data plane (Aura-native story; DENY_PLUGIN=1).

Usage
-----
  ./scripts/build-native.sh
  python3 scripts/bench_dynamic_evict.py
  python3 scripts/bench_dynamic_evict.py --workloads hot_protect,ws_shift
  python3 scripts/bench_dynamic_evict.py --aura-agent   # needs docker+Aura image
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402

DEFAULT_PORT = 26730
IMG = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")


def parse_info(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def info_int(info: Dict[str, str], key: str) -> int:
    return int(info.get(key, "0"))


# ── adaptive controllers ──────────────────────────────────────────────


class PythonAdaptiveController(threading.Thread):
    """Mirrors choose_normal.aura / adaptive_body.aura rules via RESP EVICT."""

    def __init__(self, port: int, tick: float = 0.08, min_ops: int = 80):
        super().__init__(daemon=True)
        self.port = port
        self.tick = tick
        self.min_ops = min_ops
        self._stop = threading.Event()
        self.swaps: List[str] = []
        self.prev: Optional[Tuple[int, int, int, int]] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            s = socket.create_connection(("127.0.0.1", self.port), 3)
        except OSError:
            return
        try:
            while not self._stop.is_set():
                try:
                    info = parse_info(redis_call(s, "INFO"))
                except Exception:
                    time.sleep(self.tick)
                    continue
                g = info_int(info, "gets")
                se = info_int(info, "sets")
                h = info_int(info, "hits")
                m = info_int(info, "misses")
                cur = info.get("evict", "")
                if self.prev is not None:
                    dg = g - self.prev[0]
                    ds = se - self.prev[1]
                    dh = h - self.prev[2]
                    dm = m - self.prev[3]
                    ops = dg + ds
                    choice = ""
                    if ops >= self.min_ops:
                        hit_pct = (100 * dh) // (dh + dm) if (dh + dm) > 0 else 0
                        if ds > dg * 2:
                            choice = "lfu"
                        elif dg > ds * 5 and hit_pct >= 60:
                            choice = "lru"
                    if choice and choice != cur:
                        try:
                            redis_call(s, "EVICT", choice)
                            self.swaps.append(f"{cur}->{choice}")
                        except Exception:
                            pass
                self.prev = (g, se, h, m)
                time.sleep(self.tick)
        finally:
            try:
                s.close()
            except Exception:
                pass


class AuraAgentController:
    """Spawn policy_agent.aura (non-demo) against C server."""

    def __init__(self, port: int, tick_ms: int = 100):
        self.port = port
        self.tick_ms = tick_ms
        self.cid: Optional[str] = None
        self.swaps: List[str] = []
        self.log = Path(f"/tmp/ar-policy-agent-{port}.log")

    def start(self) -> None:
        self.log.write_text("")
        cmd = [
            "sudo", "docker", "run", "-d", "--network", "host", "--entrypoint", "",
            "-v", f"{ROOT}:/work", "-w", "/work",
            "-e", "AURA_SANDBOX=off",
            "-e", "AURA_PIPELINE_STRICT=0",
            "-e", "AURA_PATH=/work/.deps/aura/lib",
            "-e", f"AURA_REDIS_PORT={self.port}",
            "-e", "AURA_REDIS_HOST=127.0.0.1",
            "-e", f"AURA_REDIS_POLICY_MS={self.tick_ms}",
            "-e", "AURA_REDIS_DENY_PLUGIN=1",
            # no AURA_REDIS_POLICY_DEMO → run forever
            IMG,
            "/work/.deps/aura/build/aura",
            "/work/src/redis/policy_agent.aura",
        ]
        self.cid = subprocess.check_output(cmd, text=True).strip()
        # wait for PING
        for _ in range(80):
            subprocess.run(
                ["sudo", "docker", "logs", self.cid],
                stdout=self.log.open("w"), stderr=subprocess.STDOUT, check=False,
            )
            text = self.log.read_text(errors="replace")
            if "PING" in text:
                return
            time.sleep(0.15)
        raise TimeoutError(f"policy_agent did not PING; log:\n{self.log.read_text(errors='replace')}")

    def harvest_swaps(self) -> None:
        if not self.cid:
            return
        subprocess.run(
            ["sudo", "docker", "logs", self.cid],
            stdout=self.log.open("w"), stderr=subprocess.STDOUT, check=False,
        )
        text = self.log.read_text(errors="replace")
        self.swaps = []
        for ln in text.splitlines():
            if "EVICT" in ln and "→" in ln:
                self.swaps.append(ln.strip())

    def stop(self) -> None:
        self.harvest_swaps()
        if self.cid:
            subprocess.run(["sudo", "docker", "rm", "-f", self.cid],
                           capture_output=True)
            self.cid = None


# ── server lifecycle ──────────────────────────────────────────────────


@dataclass
class ServerHandle:
    proc: subprocess.Popen
    port: int
    log: Path
    controller: object = None  # PythonAdaptiveController | AuraAgentController | None


def kill_port(port: int) -> None:
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    time.sleep(0.12)


def start_server(
    port: int,
    evict: str,
    maxmemory: int,
    adaptive: str = "off",  # off | python | aura
) -> ServerHandle:
    kill_port(port)
    bin_path = ROOT / "native/build/aura_redis_server"
    if not bin_path.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    log = Path(f"/tmp/ar-dyn-{port}-{evict}-{adaptive}.log")
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    proc = subprocess.Popen(
        [
            str(bin_path),
            "--port", str(port),
            "--evict", evict,
            "--maxmemory", str(maxmemory),
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    # wait listen
    for _ in range(60):
        if "listening on" in log.read_text(errors="replace"):
            break
        if proc.poll() is not None:
            raise RuntimeError(f"server died:\n{log.read_text(errors='replace')}")
        time.sleep(0.05)
    else:
        raise TimeoutError(f"no listen; log:\n{log.read_text(errors='replace')}")

    h = ServerHandle(proc=proc, port=port, log=log)
    if adaptive == "python":
        ctl = PythonAdaptiveController(port)
        ctl.start()
        time.sleep(0.12)
        h.controller = ctl
    elif adaptive == "aura":
        ctl = AuraAgentController(port)
        ctl.start()
        h.controller = ctl
    return h


def stop_server(h: ServerHandle) -> List[str]:
    swaps: List[str] = []
    ctl = h.controller
    if ctl is not None:
        if isinstance(ctl, PythonAdaptiveController):
            swaps = list(ctl.swaps)
            ctl.stop()
            ctl.join(timeout=2)
        elif isinstance(ctl, AuraAgentController):
            ctl.stop()
            swaps = list(ctl.swaps)
    h.proc.terminate()
    try:
        h.proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        h.proc.kill()
    return swaps


def connect(port: int) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), 5)
    redis_call(s, "PING")
    return s


def wait_evict(s: socket.socket, want: str, mode: str, rounds: int = 25) -> bool:
    """Nudge traffic so adaptive controller can flip to want."""
    for _ in range(rounds):
        if parse_info(redis_call(s, "INFO")).get("evict") == want:
            return True
        if mode != "adaptive":
            return parse_info(redis_call(s, "INFO")).get("evict") == want
        if want == "lfu":
            for j in range(120):
                redis_call(s, "SET", f"__pre{j}", "p" * 64)
        else:
            for j in range(80):
                redis_call(s, "SET", f"__br{j}", "b" * 64)
            for _r in range(6):
                for j in range(80):
                    redis_call(s, "GET", f"__br{j}")
        time.sleep(0.12)
    return parse_info(redis_call(s, "INFO")).get("evict") == want


# ── workloads ─────────────────────────────────────────────────────────


@dataclass
class PhaseResult:
    name: str
    hits: int
    misses: int
    evicted: int = 0
    useful_gets: int = 0  # client-observed hits on target keys

    @property
    def hit_rate(self) -> float:
        t = self.hits + self.misses
        return (self.hits / t) if t else 0.0


@dataclass
class RunResult:
    workload: str
    policy: str  # lru | lfu | adaptive
    phases: List[PhaseResult] = field(default_factory=list)
    swaps: List[str] = field(default_factory=list)
    notes: str = ""

    @property
    def total_useful(self) -> int:
        return sum(p.useful_gets for p in self.phases)

    @property
    def overall_hit_rate(self) -> float:
        h = sum(p.hits for p in self.phases)
        m = sum(p.misses for p in self.phases)
        return (h / (h + m)) if (h + m) else 0.0


def workload_hot_protect(s: socket.socket, mode: str) -> PhaseResult:
    """LFU-favoring: protect early hot keys across a cold flood."""
    nhot = 40
    boost = 30
    ncold = 800
    vlen = 180
    val = "V" * vlen

    if mode == "adaptive":
        if not wait_evict(s, "lfu", mode):
            raise RuntimeError("adaptive failed to flip to lfu before hot_protect")

    for i in range(nhot):
        redis_call(s, "SET", f"hot{i:04d}", val)
    for _ in range(boost):
        for i in range(nhot):
            redis_call(s, "GET", f"hot{i:04d}")

    for i in range(ncold):
        redis_call(s, "SET", f"cold{i:05d}", val)

    hits = misses = 0
    for i in range(nhot):
        if redis_call(s, "GET", f"hot{i:04d}") is None:
            misses += 1
        else:
            hits += 1
    info = parse_info(redis_call(s, "INFO"))
    return PhaseResult(
        name="hot_protect",
        hits=hits,
        misses=misses,
        evicted=info_int(info, "evicted"),
        useful_gets=hits,
    )


def workload_ws_shift(s: socket.socket, mode: str) -> PhaseResult:
    """LRU-favoring: working-set shift from A → B under pressure."""
    if mode == "adaptive":
        # Prefer LRU for shifting locality; nudge if still on lfu.
        wait_evict(s, "lru", mode)

    nA, nB = 200, 150
    boostA = 40
    rounds = 8
    reads_per = 200
    vlen = 400
    val = "V" * vlen

    for i in range(nA):
        redis_call(s, "SET", f"A{i:04d}", val)
    for _ in range(boostA):
        for i in range(nA):
            redis_call(s, "GET", f"A{i:04d}")

    hits = misses = 0
    for _ in range(rounds):
        for i in range(nB):
            redis_call(s, "SET", f"B{i:04d}", val)
        for j in range(reads_per):
            if redis_call(s, "GET", f"B{j % nB:04d}") is None:
                misses += 1
            else:
                hits += 1
    info = parse_info(redis_call(s, "INFO"))
    return PhaseResult(
        name="ws_shift",
        hits=hits,
        misses=misses,
        evicted=info_int(info, "evicted"),
        useful_gets=hits,
    )


def workload_oscillate(s: socket.socket, mode: str) -> List[PhaseResult]:
    """hot_protect then bridge then ws_shift on one server."""
    p1 = workload_hot_protect(s, mode)
    redis_call(s, "FLUSHDB")
    # Bridge: clear pressure artifacts; adaptive should move toward lru.
    if mode == "adaptive":
        wait_evict(s, "lru", mode)
    else:
        # Fixed policies: small settle
        for j in range(40):
            redis_call(s, "SET", f"__br{j}", "b" * 32)
        for _ in range(3):
            for j in range(40):
                redis_call(s, "GET", f"__br{j}")
    redis_call(s, "FLUSHDB")
    p2 = workload_ws_shift(s, mode)
    return [p1, p2]


# ── orchestration ─────────────────────────────────────────────────────


def run_one(
    workload: str,
    policy: str,
    port: int,
    maxmemory: int,
    adaptive_backend: str,
) -> RunResult:
    adaptive = "off"
    start_evict = policy
    if policy == "adaptive":
        adaptive = adaptive_backend  # python | aura
        start_evict = "lru"

    h = start_server(port, start_evict, maxmemory, adaptive=adaptive)
    try:
        s = connect(port)
        try:
            if workload == "hot_protect":
                phases = [workload_hot_protect(s, policy)]
            elif workload == "ws_shift":
                phases = [workload_ws_shift(s, policy)]
            elif workload == "oscillate":
                phases = workload_oscillate(s, policy)
            else:
                raise ValueError(workload)
            try:
                redis_call(s, "QUIT")
            except Exception:
                pass
        finally:
            s.close()
    finally:
        swaps = stop_server(h)
    return RunResult(
        workload=workload,
        policy=policy,
        phases=phases,
        swaps=swaps,
        notes=f"backend={adaptive_backend}" if policy == "adaptive" else "",
    )


def print_table(results: List[RunResult]) -> None:
    print()
    print("=" * 78)
    print("Dynamic eviction comparison (higher hit rate / useful GETs = better)")
    print("=" * 78)
    hdr = f"{'workload':<14} {'policy':<10} {'phase':<12} {'hits':>6} {'miss':>6} {'hit%':>7} {'useful':>7} {'evicted':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        for p in r.phases:
            print(
                f"{r.workload:<14} {r.policy:<10} {p.name:<12} "
                f"{p.hits:>6} {p.misses:>6} {100*p.hit_rate:>6.1f}% "
                f"{p.useful_gets:>7} {p.evicted:>8}"
            )
        if r.swaps:
            shown = r.swaps[:6]
            extra = "" if len(r.swaps) <= 6 else f" (+{len(r.swaps)-6} more)"
            print(f"  └─ swaps[{len(r.swaps)}]: {shown}{extra}")
    print()
    # Summary: per workload, compare policies on useful gets / hit%
    print("Summary (per workload, primary metric = useful target GETs / hit%):")
    print("-" * 78)
    by_wl: Dict[str, List[RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, []).append(r)
    for wl, runs in by_wl.items():
        print(f"  {wl}:")
        for r in runs:
            phases_s = ", ".join(
                f"{p.name}={100*p.hit_rate:.1f}%({p.useful_gets})" for p in r.phases
            )
            print(f"    {r.policy:<10} overall_hit={100*r.overall_hit_rate:5.1f}%  "
                  f"useful={r.total_useful:<5}  [{phases_s}]")
        # highlight gap
        lru = next((x for x in runs if x.policy == "lru"), None)
        best = max(runs, key=lambda x: (x.overall_hit_rate, x.total_useful))
        if lru and best.policy != "lru" and best.overall_hit_rate > lru.overall_hit_rate + 0.05:
            gap = best.overall_hit_rate - lru.overall_hit_rate
            print(f"    → static LRU loses by {100*gap:.1f}pp hit-rate vs {best.policy}")
    print("=" * 78)


def assert_success(results: List[RunResult]) -> None:
    """Require at least one workload where LRU is clearly worse than LFU or adaptive."""
    by_wl: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, {})[r.policy] = r
    ok = False
    reasons = []
    for wl, m in by_wl.items():
        lru = m.get("lru")
        if not lru:
            continue
        for alt_name in ("lfu", "adaptive"):
            alt = m.get(alt_name)
            if not alt:
                continue
            if alt.overall_hit_rate >= lru.overall_hit_rate + 0.15 or (
                alt.total_useful >= lru.total_useful + max(10, int(0.2 * max(1, lru.total_useful)))
            ):
                ok = True
                reasons.append(
                    f"{wl}: {alt_name} hit={100*alt.overall_hit_rate:.1f}% "
                    f"vs lru={100*lru.overall_hit_rate:.1f}%"
                )
    if not ok:
        raise SystemExit(
            "FAIL: expected LRU to lose on ≥1 workload vs LFU/adaptive; got:\n"
            + "\n".join(
                f"  {r.workload}/{r.policy}: {100*r.overall_hit_rate:.1f}%"
                for r in results
            )
        )
    print("PASS criteria: " + "; ".join(reasons))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--workloads",
        default="hot_protect,ws_shift,oscillate",
        help="Comma list: hot_protect,ws_shift,oscillate",
    )
    ap.add_argument(
        "--policies",
        default="lru,lfu,adaptive",
        help="Comma list: lru,lfu,adaptive",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--maxmemory", type=int, default=120_000)
    ap.add_argument(
        "--aura-agent",
        action="store_true",
        help="Use Aura policy_agent.aura (Docker) instead of Python controller",
    )
    ap.add_argument("--seed", type=int, default=42, help="Reserved for future RNG (deterministic phases today)")
    ap.add_argument("--skip-assert", action="store_true")
    args = ap.parse_args()

    workloads = [w.strip() for w in args.workloads.split(",") if w.strip()]
    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    backend = "aura" if args.aura_agent else "python"

    print(f"bench_dynamic_evict: maxmemory={args.maxmemory} port={args.port} "
          f"adaptive_backend={backend} seed={args.seed}")
    print(f"  workloads={workloads}")
    print(f"  policies={policies}")

    results: List[RunResult] = []
    for wl in workloads:
        for pol in policies:
            print(f"\n>> running workload={wl} policy={pol} ...")
            t0 = time.time()
            r = run_one(wl, pol, args.port, args.maxmemory, backend)
            dt = time.time() - t0
            print(f"   done in {dt:.2f}s  hit={100*r.overall_hit_rate:.1f}% "
                  f"useful={r.total_useful} swaps={len(r.swaps)}")
            results.append(r)

    print_table(results)
    if not args.skip_assert:
        assert_success(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
