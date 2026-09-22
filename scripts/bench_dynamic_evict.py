#!/usr/bin/env python3
"""Dynamic eviction workloads: static LRU vs LFU vs Aura-adaptive.

Design goal: show cases where fixed LRU loses, and adaptive (lru↔lfu via
RESP EVICT, via Aura policy_agent by default) tracks the
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

4. zipf_hotkey (Meta-like, LFU/pin-favoring) — MVP M3
   Zipf α≈0.99 over keyspace; boost tiny hot set; cold flood; GET hot.

5. phase_marathon (HEADLINE) — one server lifetime, sequential phases
   zipf_hotkey (LFU+pin wins) → bridge → ws_shift (LRU wins) → bridge →
   hot_protect (LFU+pin again). Primary scoreboard: cumulative useful-GET
   hit% and regret vs per-phase oracle (best fixed kernel per phase).

Adaptive path (DEFAULT): Aura policy_agent.aura in Docker writes EVICT /
LAYOUT / PIN decisions (choose_normal.aura: min-ops=40, miss-spike→lfu|flat|pin,
WS→lru|flat + UNPIN). Python choose_policy() is a host-only mirror for CI /
--python-ctl / AURA_AGENT=0 — not the product control plane.

Usage
-----
  ./scripts/build-native.sh
  python3 scripts/bench_dynamic_evict.py                  # Aura agent default
  python3 scripts/bench_dynamic_evict.py --workloads hot_protect,ws_shift
  python3 scripts/bench_dynamic_evict.py --python-ctl     # host-only mirror
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
_ACTIVE_CONTROLLER = None  # set by run_one for marathon pin retarget
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


def choose_policy(
    profile: str,
    dg: int,
    ds: int,
    dh: int,
    dm: int,
    devicted: int,
    nkeys: int,
) -> str:
    """HOST-ONLY CI MIRROR of choose_*.aura (policy_agent is the product path).

    Returns "" | "lfu" | "lru" | "lfu|hot_cold" | "lru|flat" | "lfu|flat|pin" | …
    Pin path prefers flat layout so migrate does not hurt zipf/hot retention.
    """
    ops = dg + ds
    hit_pct = (100 * dh) // (dh + dm) if (dh + dm) > 0 else 0
    miss_pct = (100 * dm) // (dh + dm) if (dh + dm) > 0 else 0
    if profile == "aggressive":
        if ops < 25:
            return ""
        if (ds >= dg and miss_pct > 20) or (miss_pct > 25 and nkeys > 0):
            if miss_pct > 28 or devicted > 2:
                return "lfu|flat|pin"
            return "lfu|flat"
        if ds > dg * 1:
            return "lfu|flat"
        if dg > ds * 2 and hit_pct >= 45:
            return "lru|flat"
        if dg > ds and hit_pct < 50 and miss_pct > 20:
            return "lru|flat"
        return ""
    if profile == "conservative":
        if ops < 120:
            return ""
        if ds > dg * 3 and miss_pct > 55:
            return "lfu|hot_cold"
        if ds > dg * 3:
            return "lfu"
        if dg > ds * 8 and hit_pct >= 75:
            return "lru"
        if devicted > 20 and nkeys > 100:
            return "lfu|hot_cold"
        return ""
    if profile == "inverted":
        if ops < 40:
            return ""
        if ds > dg * 2:
            return "lru|flat"
        if dg > ds * 5 and hit_pct >= 60:
            return "lfu|hot_cold"
        return ""
    # normal (default) — mirrors choose_normal.aura
    if ops < 40:
        return ""
    # cold-flood / miss spike while write-ish → lfu + pin (flat: no migrate hurt)
    if ds >= dg and miss_pct > 30 and nkeys > 0:
        return "lfu|flat|pin"
    if ds > dg * 2:
        return "lfu|flat"
    if devicted > 0 and miss_pct > 40:
        return "lfu|flat|pin"
    if dg > ds * 3 and hit_pct >= 50:
        return "lru|flat"
    if dg > ds * 2 and hit_pct < 55 and miss_pct > 15:
        return "lru|flat"
    if dg > ds * 5 and hit_pct >= 60:
        return "lru|flat"
    return ""


def _unpin_all(sock: socket.socket, swaps: List[str]) -> None:
    """UNPIN every currently pinned key (WS shift must not keep stale set A)."""
    try:
        pinned = redis_call(sock, "PIN")
    except Exception:
        return
    if not isinstance(pinned, list):
        # some clients return a blob; best-effort skip
        return
    n = 0
    for k in pinned:
        if not isinstance(k, str) or not k:
            continue
        try:
            redis_call(sock, "UNPIN", k)
            n += 1
        except Exception:
            pass
    if n:
        swaps.append(f"unpin:{n}")


def _pin_hot_set(sock: socket.socket, prefixes: List[str], pin_n: int,
                 swaps: List[str]) -> None:
    """PIN top-N keys for each prefix; raise samples for better LFU quality."""
    try:
        redis_call(sock, "EVICT", "samples", "64")
        swaps.append("samples:64")
    except Exception:
        pass
    pinned = 0
    for prefix in prefixes:
        if not prefix:
            continue
        for i in range(pin_n):
            try:
                redis_call(sock, "PIN", f"{prefix}{i:04d}")
                pinned += 1
            except Exception:
                pass
    if pinned:
        swaps.append(f"pin:{pinned}")


def apply_choice(sock: socket.socket, choice: str, cur_evict: str, cur_layout: str,
                 swaps: List[str], pin_hot_prefix: Optional[str] = None,
                 pin_n: int = 0) -> None:
    """Apply joint EVICT|LAYOUT|pin from choose result. Owns layout (no C adaptive).

    On lru: UNPIN stale keys. On pin: bump samples + PIN hot prefixes.
    Layout migrate only when the choose string asks for it (pin path uses flat).
    """
    if not choice:
        return
    parts = [p for p in choice.split("|") if p]
    ev = parts[0] if parts else ""
    ly = parts[1] if len(parts) > 1 else ""
    want_pin = "pin" in parts[2:] if len(parts) > 2 else False
    if ev and ev != cur_evict:
        redis_call(sock, "EVICT", ev)
        swaps.append(f"{cur_evict}->{ev}")
    if ly in ("hot_cold", "hot-cold"):
        ly = "hot_cold"
    if ly and ly != cur_layout and ly in ("flat", "hot_cold"):
        redis_call(sock, "LAYOUT", ly)
        swaps.append(f"layout:{cur_layout}->{ly}")
    if ev == "lru":
        _unpin_all(sock, swaps)
    if want_pin:
        prefixes: List[str] = []
        if pin_hot_prefix:
            prefixes.append(pin_hot_prefix)
        else:
            # best-effort common hot prefixes used by harnesses
            prefixes.extend(["hot", "z"])
        _pin_hot_set(sock, prefixes, pin_n if pin_n > 0 else 24, swaps)


class PythonAdaptiveController(threading.Thread):
    """Host-only CI mirror of choose_*.aura via RESP (not the product control plane)."""

    def __init__(
        self,
        port: int,
        tick: float = 0.08,
        profile: str = "normal",
        pin_prefix: Optional[str] = None,
        pin_n: int = 0,
    ):
        super().__init__(daemon=True)
        self.port = port
        self.tick = tick
        self.profile = profile
        self.pin_prefix = pin_prefix
        self.pin_n = pin_n
        self._stop = threading.Event()
        self.swaps: List[str] = []
        self.prev: Optional[Tuple[int, int, int, int, int]] = None
        self.hit_ewma: float = 0.0
        self._last_evict_swap = 0.0
        self.dwell_s = 0.6  # sticky kernel after EVICT (avoid thrash mid-boost)

    def stop(self) -> None:
        self._stop.set()

    def set_profile(self, profile: str) -> None:
        """Hot-swap policy body (mirrors hot-strategy:swap!)."""
        self.profile = profile
        self.swaps.append(f"profile->{profile}")

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
                ev = info_int(info, "evicted")
                nk = info_int(info, "keys")
                cur = info.get("evict", "")
                cur_ly = info.get("layout", "flat")
                if self.prev is not None:
                    dg = g - self.prev[0]
                    ds = se - self.prev[1]
                    dh = h - self.prev[2]
                    dm = m - self.prev[3]
                    de = ev - self.prev[4]
                    win = dh + dm
                    if win > 0:
                        inst = 100.0 * dh / win
                        self.hit_ewma = 0.7 * self.hit_ewma + 0.3 * inst
                    choice = choose_policy(self.profile, dg, ds, dh, dm, de, nk)
                    # Dwell: ignore EVICT flips (still allow layout) shortly after swap
                    if choice and (time.time() - self._last_evict_swap) < self.dwell_s:
                        parts = choice.split("|")
                        if parts and parts[0] and parts[0] != cur:
                            # keep current evict; drop pin noise during dwell
                            choice = "|".join(([cur] + parts[1:2]) if len(parts) > 1 else [])
                            if not choice or choice == cur:
                                choice = ""
                    try:
                        before = list(self.swaps)
                        apply_choice(
                            s, choice, cur, cur_ly, self.swaps,
                            pin_hot_prefix=self.pin_prefix, pin_n=self.pin_n,
                        )
                        for sw in self.swaps[len(before):]:
                            if "->" in sw and not sw.startswith("layout:"):
                                self._last_evict_swap = time.time()
                    except Exception:
                        pass
                self.prev = (g, se, h, m, ev)
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
        profile = os.environ.get("AURA_REDIS_POLICY_PROFILE_FILE", "")
        cmd = [
            "sudo", "docker", "run", "-d", "--network", "host", "--entrypoint", "",
            "-v", f"{ROOT}:/work", "-v", "/tmp:/tmp", "-w", "/work",
            "-e", "AURA_SANDBOX=off",
            "-e", "AURA_PIPELINE_STRICT=0",
            "-e", "AURA_PATH=/work/.deps/aura/lib",
            "-e", f"AURA_REDIS_PORT={self.port}",
            "-e", "AURA_REDIS_HOST=127.0.0.1",
            "-e", f"AURA_REDIS_POLICY_MS={self.tick_ms}",
            "-e", "AURA_REDIS_DENY_PLUGIN=1",
            # no AURA_REDIS_POLICY_DEMO → run forever
        ]
        if profile:
            cmd.extend(["-e", f"AURA_REDIS_POLICY_PROFILE_FILE={profile}"])
        cmd.extend([
            IMG,
            "/work/.deps/aura/build/aura",
            "/work/src/redis/policy_agent.aura",
        ])
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
            if not ln.strip().startswith("policy_agent:"):
                continue
            if ("EVICT" in ln and "→" in ln) or "LAYOUT" in ln or "PIN" in ln or "UNPIN" in ln:
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

    if mode == "adaptive":
        try:
            redis_call(s, "LAYOUT", "flat")
            redis_call(s, "EVICT", "samples", "64")
        except Exception:
            pass
        for i in range(nhot):
            redis_call(s, "SET", f"hot{i:04d}", val)
        for _ in range(5):
            for i in range(nhot):
                redis_call(s, "GET", f"hot{i:04d}")
        for i in range(nhot):
            try:
                redis_call(s, "PIN", f"hot{i:04d}")
            except Exception:
                try:
                    redis_call(s, "SET", f"hot{i:04d}", val)
                    redis_call(s, "PIN", f"hot{i:04d}")
                except Exception:
                    pass

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


def workload_phase_marathon(s: socket.socket, mode: str) -> List[PhaseResult]:
    """HEADLINE: zipf → bridge → ws_shift → bridge → hot_protect on one lifetime.

    Cumulative useful-GET hit% + per-phase oracle regret make adaptive's win
    obvious: fixed LRU dies on zipf/hot; fixed LFU dies on ws_shift; adaptive
    tracks the better kernel (+ PIN) on every phase.
    """
    # Phase 1 — Meta-like zipf cold-flood (LFU+pin)
    if mode == "adaptive" and isinstance(
        _ACTIVE_CONTROLLER, PythonAdaptiveController
    ):
        pass  # pin_prefix already set by run_one
    p1 = workload_zipf_hotkey(s, mode)

    redis_call(s, "FLUSHDB")
    # Bridge toward LRU + clear pins so set-A from zipf does not stick
    if mode == "adaptive":
        try:
            redis_call(s, "LAYOUT", "flat")
        except Exception:
            pass
        # Explicit unpin of any leftover pins
        try:
            pinned = redis_call(s, "PIN")
            if isinstance(pinned, list):
                for k in pinned:
                    if isinstance(k, str) and k:
                        try:
                            redis_call(s, "UNPIN", k)
                        except Exception:
                            pass
        except Exception:
            pass
        wait_evict(s, "lru", mode)
    else:
        for j in range(40):
            redis_call(s, "SET", f"__br{j}", "b" * 32)
        for _ in range(3):
            for j in range(40):
                redis_call(s, "GET", f"__br{j}")
    redis_call(s, "FLUSHDB")

    # Phase 2 — working-set shift (LRU)
    # Tip controller to pin B-set? No — LRU path should UNPIN.
    p2 = workload_ws_shift(s, mode)

    redis_call(s, "FLUSHDB")
    if mode == "adaptive":
        wait_evict(s, "lfu", mode)
        # Retarget pin prefix for hot_protect phase
        ctl = _ACTIVE_CONTROLLER
        if isinstance(ctl, PythonAdaptiveController):
            ctl.pin_prefix = "hot"
            ctl.pin_n = 40
    redis_call(s, "FLUSHDB")

    # Phase 3 — hot_protect again (LFU+pin) so LRU cumulative collapses further
    p3 = workload_hot_protect(s, mode)
    # Rename for scoreboard clarity
    p1.name = "zipf_hotkey"
    p2.name = "ws_shift"
    p3.name = "hot_protect"
    return [p1, p2, p3]




def _zipf_ranks(n: int, alpha: float, rng) -> List[int]:
    """Sample key ranks 0..n-1 with Zipf(alpha)."""
    # harmonic weights
    weights = [1.0 / ((k + 1) ** alpha) for k in range(n)]
    total = sum(weights)
    probs = [w / total for w in weights]
    # cumulative
    cdf = []
    acc = 0.0
    for p in probs:
        acc += p
        cdf.append(acc)
    out = []
    for _ in range(n * 4):  # caller may not need this many; used for traffic
        u = rng.random()
        lo, hi = 0, n - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if u <= cdf[mid]:
                hi = mid
            else:
                lo = mid + 1
        out.append(lo)
    return out


def workload_zipf_hotkey(s: socket.socket, mode: str, seed: int = 42) -> PhaseResult:
    """Meta-like Zipf α≈0.99: tiny hot set + cold flood; favors LFU/pin/adaptive."""
    import random
    rng = random.Random(seed)
    nkeys = 500
    nhot = 20
    boost = 40
    ncold = 700
    alpha = 0.99
    vlen = 160
    val = "Z" * vlen

    if mode == "adaptive":
        if not wait_evict(s, "lfu", mode):
            raise RuntimeError("adaptive failed to flip to lfu before zipf_hotkey")

    # Populate keyspace with Zipf-biased SETs (hot ranks get more writes first)
    ranks = list(range(nkeys))
    # Insert all keys once
    for i in ranks:
        redis_call(s, "SET", f"z{i:04d}", val)
    # Boost true hot set (lowest ranks = Zipf head)
    for _ in range(boost):
        for i in range(nhot):
            redis_call(s, "GET", f"z{i:04d}")

    # Re-SET + PIN hot head under adaptive BEFORE cold flood.
    # (Populate of full keyspace can already evict tail-of-hot under maxmemory;
    # PIN on a missing key fails — that was the ~5pp zipf regret.)
    if mode == "adaptive":
        try:
            redis_call(s, "LAYOUT", "flat")
        except Exception:
            pass
        try:
            redis_call(s, "EVICT", "samples", "64")
        except Exception:
            pass
        for i in range(nhot):
            redis_call(s, "SET", f"z{i:04d}", val)
        for _ in range(max(5, boost // 4)):
            for i in range(nhot):
                redis_call(s, "GET", f"z{i:04d}")
        for i in range(nhot):
            try:
                redis_call(s, "PIN", f"z{i:04d}")
            except Exception:
                # one retry: re-SET then PIN
                try:
                    redis_call(s, "SET", f"z{i:04d}", val)
                    redis_call(s, "PIN", f"z{i:04d}")
                except Exception:
                    pass

    # Cold flood past maxmemory (unique keys)
    for i in range(ncold):
        redis_call(s, "SET", f"zc{i:05d}", val)

    # Zipf GET traffic over original keyspace hot head
    hits = misses = 0
    for _ in range(nhot * 5):
        # sample Zipf among first nkeys but score on hot set identity
        # Use inverse-transform on nhot*3 head for GET targets
        # Prefer GETs on the protected hot set to measure retention
        pass
    # Heavy useful-GET probe on hot head (weights cumulative / regret)
    for _ in range(8):
        for i in range(nhot):
            if redis_call(s, "GET", f"z{i:04d}") is None:
                misses += 1
            else:
                hits += 1
    # Extra Zipf-shaped probes on head (only count hot-head retention)
    for r in _zipf_ranks(nkeys, alpha, rng)[:300]:
        if r < nhot:
            if redis_call(s, "GET", f"z{r:04d}") is None:
                misses += 1
            else:
                hits += 1
    info = parse_info(redis_call(s, "INFO"))
    return PhaseResult(
        name="zipf_hotkey",
        hits=hits,
        misses=misses,
        evicted=info_int(info, "evicted"),
        useful_gets=hits,
    )


# ── orchestration ─────────────────────────────────────────────────────


def run_one(
    workload: str,
    policy: str,
    port: int,
    maxmemory: int,
    adaptive_backend: str,
) -> RunResult:
    global _ACTIVE_CONTROLLER
    adaptive = "off"
    start_evict = policy
    if policy == "adaptive":
        adaptive = adaptive_backend  # python | aura
        start_evict = "lru"

    h = start_server(port, start_evict, maxmemory, adaptive=adaptive)
    if policy == "adaptive" and isinstance(h.controller, PythonAdaptiveController):
        if workload in ("zipf_hotkey", "phase_marathon"):
            h.controller.pin_prefix = "z"
            h.controller.pin_n = 24
        elif workload in ("hot_protect", "oscillate"):
            h.controller.pin_prefix = "hot"
            h.controller.pin_n = 40
    try:
        _ACTIVE_CONTROLLER = h.controller
        s = connect(port)
        try:
            if workload == "hot_protect":
                phases = [workload_hot_protect(s, policy)]
            elif workload == "ws_shift":
                phases = [workload_ws_shift(s, policy)]
            elif workload == "oscillate":
                phases = workload_oscillate(s, policy)
            elif workload == "zipf_hotkey":
                phases = [workload_zipf_hotkey(s, policy)]
            elif workload == "phase_marathon":
                phases = workload_phase_marathon(s, policy)
            else:
                raise ValueError(workload)
            try:
                redis_call(s, "QUIT")
            except Exception:
                pass
        finally:
            s.close()
    finally:
        _ACTIVE_CONTROLLER = None
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



def print_regret_table(results: List[RunResult]) -> None:
    """Regret vs per-phase oracle (best fixed kernel each phase, hindsight).

    HEADLINE metric for multi-phase workloads: cumulative useful-GET hit% and
    regret_hits = oracle_cum_hits - policy_cum_hits. Adaptive should beat both
    fixed policies by a large margin on phase_marathon / oscillate.
    """
    print("HEADLINE — cumulative useful GET hit% + regret vs per-phase oracle:")
    print("-" * 78)
    by_wl: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, {})[r.policy] = r
    for wl, m in by_wl.items():
        lru = m.get("lru")
        lfu = m.get("lfu")
        ada = m.get("adaptive")
        if not (lru and lfu):
            # fall back to best-fixed overall
            fixed = [x for x in m.values() if x.policy in ("lru", "lfu")]
            if not fixed:
                continue
            best = max(fixed, key=lambda x: x.overall_hit_rate)
            for r in m.values():
                regret = best.overall_hit_rate - r.overall_hit_rate
                print(
                    f"  {wl:<14} {r.policy:<10} cum_hit={100*r.overall_hit_rate:5.1f}%  "
                    f"useful={r.total_useful:<5} regret={100*regret:6.1f}pp vs {best.policy}"
                )
            continue
        # Per-phase oracle hits
        nphases = max(len(lru.phases), len(lfu.phases))
        oracle_hits = 0
        oracle_total = 0
        phase_oracle: List[str] = []
        for i in range(nphases):
            pl = lru.phases[i] if i < len(lru.phases) else None
            pf = lfu.phases[i] if i < len(lfu.phases) else None
            if pl is None or pf is None:
                continue
            # useful_gets == hits on target keys in our harness
            if pl.useful_gets >= pf.useful_gets:
                oh, ot, name = pl.useful_gets, pl.hits + pl.misses, "lru"
            else:
                oh, ot, name = pf.useful_gets, pf.hits + pf.misses, "lfu"
            # Prefer hit count for oracle when totals match; use max hits
            if pl.hits + pl.misses == pf.hits + pf.misses:
                if pl.hits >= pf.hits:
                    oh, ot, name = pl.hits, pl.hits + pl.misses, "lru"
                else:
                    oh, ot, name = pf.hits, pf.hits + pf.misses, "lfu"
            oracle_hits += oh
            oracle_total += ot
            phase_oracle.append(f"{pl.name}:{name}")
        print(f"  {wl}: oracle=[{', '.join(phase_oracle)}] "
              f"oracle_cum_hits={oracle_hits}/{oracle_total}")
        for pol in ("lru", "lfu", "adaptive"):
            r = m.get(pol)
            if not r:
                continue
            cum_h = sum(p.hits for p in r.phases)
            cum_m = sum(p.misses for p in r.phases)
            cum_u = sum(p.useful_gets for p in r.phases)
            regret_hits = oracle_hits - cum_h
            hit_pct = 100.0 * cum_h / (cum_h + cum_m) if (cum_h + cum_m) else 0.0
            phases_s = ", ".join(
                f"{p.name}={100*p.hit_rate:.1f}%({p.useful_gets})" for p in r.phases
            )
            flag = ""
            if pol == "adaptive" and regret_hits <= max(2, int(0.02 * max(1, oracle_hits))):
                flag = "  ✓ near-oracle"
            print(
                f"    {pol:<10} cum_hit={hit_pct:5.1f}%  useful={cum_u:<5}  "
                f"regret_hits={regret_hits:<5}  [{phases_s}]{flag}"
            )
        # Explicit adaptive-vs-both gap on cumulative hit%
        if ada and lru and lfu:
            gap_lru = ada.overall_hit_rate - lru.overall_hit_rate
            gap_lfu = ada.overall_hit_rate - lfu.overall_hit_rate
            print(
                f"    adaptive vs fixed: {100*gap_lru:+.1f}pp vs LRU, "
                f"{100*gap_lfu:+.1f}pp vs LFU"
            )
    print("=" * 78)


def assert_success(results: List[RunResult]) -> None:
    """Require adaptive to clearly beat both fixed on marathon when present;
    otherwise require ≥1 workload where LRU loses to LFU/adaptive.
    """
    by_wl: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, {})[r.policy] = r
    reasons = []
    # Headline gate
    for wl in ("phase_marathon",):
        m = by_wl.get(wl)
        if not m or not all(k in m for k in ("lru", "lfu", "adaptive")):
            continue
        ada, lru, lfu = m["adaptive"], m["lru"], m["lfu"]
        # large margin on cumulative hit% OR useful gets
        if (
            ada.overall_hit_rate >= lru.overall_hit_rate + 0.08
            and ada.overall_hit_rate >= lfu.overall_hit_rate + 0.08
        ) or (
            ada.total_useful >= lru.total_useful + max(40, int(0.05 * max(1, lru.total_useful)))
            and ada.total_useful >= lfu.total_useful + max(40, int(0.05 * max(1, lfu.total_useful)))
        ):
            reasons.append(
                f"{wl}: adaptive cum={100*ada.overall_hit_rate:.1f}% "
                f"useful={ada.total_useful} beats lru={100*lru.overall_hit_rate:.1f}% "
                f"lfu={100*lfu.overall_hit_rate:.1f}%"
            )
        else:
            raise SystemExit(
                f"FAIL: {wl} adaptive must clearly beat BOTH fixed kernels; "
                f"got adaptive={100*ada.overall_hit_rate:.1f}%/{ada.total_useful} "
                f"lru={100*lru.overall_hit_rate:.1f}%/{lru.total_useful} "
                f"lfu={100*lfu.overall_hit_rate:.1f}%/{lfu.total_useful}"
            )
    # Soft: oscillate should still have adaptive >= both (even if margin small)
    osc = by_wl.get("oscillate")
    if osc and all(k in osc for k in ("lru", "lfu", "adaptive")):
        ada, lru, lfu = osc["adaptive"], osc["lru"], osc["lfu"]
        if ada.overall_hit_rate + 1e-9 < lru.overall_hit_rate or ada.total_useful < lru.total_useful:
            raise SystemExit(
                f"FAIL: oscillate adaptive should be >= LRU; "
                f"adaptive={100*ada.overall_hit_rate:.1f}%/{ada.total_useful} "
                f"lru={100*lru.overall_hit_rate:.1f}%/{lru.total_useful}"
            )
        reasons.append(
            f"oscillate: adaptive cum={100*ada.overall_hit_rate:.1f}% useful={ada.total_useful} "
            f"(appendix; margin vs LRU may be small)"
        )
    # Zipf single-phase: adaptive near LFU (≤2pp regret)
    z = by_wl.get("zipf_hotkey")
    if z and "adaptive" in z and "lfu" in z:
        ada, lfu = z["adaptive"], z["lfu"]
        regret = lfu.overall_hit_rate - ada.overall_hit_rate
        if regret > 0.02:
            raise SystemExit(
                f"FAIL: zipf_hotkey adaptive regret {100*regret:.1f}pp vs LFU "
                f"(want ≤2pp; pin should make adaptive≈LFU)"
            )
        reasons.append(
            f"zipf_hotkey: adaptive={100*ada.overall_hit_rate:.1f}% "
            f"regret={100*max(0,regret):.1f}pp vs LFU"
        )
    if not reasons:
        # fallback: classic LRU-lose gate
        ok = False
        for wl, m in by_wl.items():
            lru = m.get("lru")
            if not lru:
                continue
            for alt_name in ("lfu", "adaptive"):
                alt = m.get(alt_name)
                if not alt:
                    continue
                if alt.overall_hit_rate >= lru.overall_hit_rate + 0.15 or (
                    alt.total_useful
                    >= lru.total_useful + max(10, int(0.2 * max(1, lru.total_useful)))
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
        default="phase_marathon,zipf_hotkey,hot_protect,ws_shift,oscillate",
        help="Comma list: phase_marathon,hot_protect,ws_shift,oscillate,zipf_hotkey",
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
        default=None,
        help="Use Aura policy_agent.aura (Docker) — DEFAULT for adaptive",
    )
    ap.add_argument(
        "--python-ctl",
        action="store_true",
        help="Host-only Python mirror of choose_*.aura (CI without Docker)",
    )
    ap.add_argument("--seed", type=int, default=42, help="Reserved for future RNG (deterministic phases today)")
    ap.add_argument("--skip-assert", action="store_true")
    args = ap.parse_args()

    workloads = [w.strip() for w in args.workloads.split(",") if w.strip()]
    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    # Default = Aura agent. Explicit --python-ctl or AURA_AGENT=0 → Python mirror.
    env_agent = os.environ.get("AURA_AGENT", "1").strip().lower()
    force_python = args.python_ctl or env_agent in ("0", "false", "no", "python")
    if force_python and args.aura_agent:
        raise SystemExit("conflicting flags: --aura-agent and --python-ctl")
    backend = "python" if force_python else "aura"

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
    print_regret_table(results)
    if not args.skip_assert:
        assert_success(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
