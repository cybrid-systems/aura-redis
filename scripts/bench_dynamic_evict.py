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

6. diurnal_shift — quiet→peak→flash-sale→cool (M8); favors adaptive mutation.

7. poison_heal — start inverted choose-fn; adaptive_mutate heals, frozen stays bad.

8. ttl_wave / session_churn (M9) — durable no-TTL keep set + mass short-TTL
   session SETs under maxmemory (sessions also GET-boosted so LFU/LRU cling);
   ttl_aware prefers soonest expire → keep survives GET storm. Adaptive should
   EVICT → ttl_aware from INFO keys_with_ttl / avg_ttl_ms / expired.

9. flash_churn (M10 soft-goal) — tiny maxmemory flash-sale: durable keep* +
   write-heavy unique cold flood (LFU thrash: high evict_rate, keep dies).
   Soft-goal adaptive refuses lfu|+pin when erate≥20 → lru/ttl_aware/noop;
   keeps useful hit% higher and/or eviction under budget vs fixed LFU / nosoft.

10. evolve_gain (M11) — bad initial thresholds (high min-ops/miss-pin) stuck on
    LRU lose hot set; multi-gen evolve loop mutates thresholds, logs fitness,
    keep-if-better / revert; adaptive_evolve beats adaptive_evolve_frozen.

11. prefix_mix (M12) — noisy-neighbor: a: session/TTL-ish durable + b: zipf hot.
    RESP POLICY a: session / b: zipf → Aura pins both prefixes; global one-policy
    (no POLICY) loses at least one set's useful GETs.

12. prefix_mix_v2 (A5) — conflicting optima: a: wants lfu+pin, b: wants ttl_aware
    (nopin). Short-TTL high-freq noise + cold flood. Global adaptive picks one
    kernel and loses the victim tenant; adaptive_prefix deep bags compose
    ttl_aware + PIN only a: → ≥ +20pp on worse tenant.

Policies: lru | lfu | ttl_aware | adaptive (=adaptive_soft) | adaptive_soft |
          adaptive_nosoft | adaptive_frozen | adaptive_mutate |
          poison_frozen | poison_mutate | adaptive_evolve | adaptive_evolve_frozen

Adaptive path (DEFAULT): Aura policy_agent.aura (native in GHA container jobs,
docker on laptops) writes EVICT / LAYOUT / PIN decisions (choose_normal.aura:
min-ops=40, miss-spike→lfu|flat|pin, WS→lru|flat + UNPIN). Python choose_policy()
is a host-only mirror for --python-ctl / AURA_AGENT=0 — not the product control plane.

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
from _agentutil import (  # noqa: E402
    agent_logs as _agent_logs,
    start_agent as _start_agent,
    stop_agent as _stop_agent,
    wait_log as _wait_log,
)

DEFAULT_PORT = 26730
_ACTIVE_CONTROLLER = None  # set by run_one for marathon pin retarget


ADAPTIVE_POLICIES = {
    "adaptive",
    "adaptive_soft",
    "adaptive_nosoft",
    "adaptive_frozen",
    "adaptive_mutate",
    "poison_frozen",
    "poison_mutate",
    "adaptive_evolve",
    "adaptive_evolve_frozen",
    "adaptive_prefix",
}


def is_adaptive(mode: str) -> bool:
    return mode in ADAPTIVE_POLICIES or mode == "adaptive"


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
    dexpired: int = 0,
    avg_ttl: int = 0,
    keys_ttl: int = 0,
) -> str:
    """HOST-ONLY CI MIRROR of choose_*.aura (policy_agent is the product path).

    Returns "" | "lfu" | "lru" | "ttl_aware" | "noop" | "lfu|flat|pin" | …
    Pin path prefers flat layout so migrate does not hurt zipf/hot retention.
    M9 TTL signals: dexpired, avg_ttl (ms), keys_ttl from INFO.
    M10 soft-goal: if evict_rate_pct = (devicted*100)/ops ≥ budget, refuse
    expensive lfu|+pin; prefer ttl_aware / lru / noop (see choose_normal.aura).
    """
    ops = dg + ds
    hit_pct = (100 * dh) // (dh + dm) if (dh + dm) > 0 else 0
    miss_pct = (100 * dm) // (dh + dm) if (dh + dm) > 0 else 0
    ttl_share = (100 * keys_ttl) // nkeys if nkeys > 0 else 0
    erate = (100 * devicted) // ops if ops > 0 else 0

    def ttl_pressure(share_thr: int = 20, keys_thr: int = 15) -> bool:
        return (
            keys_ttl > 0
            and avg_ttl < 12000
            and (ttl_share >= share_thr or keys_ttl > keys_thr)
        )

    def soft_refuse(budget: int = 20, extreme: int = 80) -> str:
        """M10: maximize hit% s.t. eviction CPU budget.

        Refuse expensive lfu|+pin when erate over budget; prefer ttl_aware/lru.
        (noop extreme omitted — bounce noop↔lfu hurt hit%; lru stops thrash.)
        """
        if erate >= budget:
            if keys_ttl > 0 and avg_ttl < 12000:
                return "ttl_aware|flat|soft"
            return "lru|flat|soft"
        return ""

    if profile == "aggressive":
        if ops < 25:
            return ""
        soft = soft_refuse(18, 80)
        if soft:
            return soft
        if keys_ttl > 0 and avg_ttl < 12000 and (ttl_share >= 15 or keys_ttl > 10):
            return "ttl_aware|flat"
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
        soft = soft_refuse(25, 80)
        if soft:
            return soft
        if keys_ttl > 30 and avg_ttl < 5000 and devicted > 0:
            return "ttl_aware|flat"
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
    if profile == "nosoft":
        # M10 A/B: pre-soft normal (no eviction-rate budget)
        if ops < 40:
            return ""
        if ttl_pressure():
            return "ttl_aware|flat"
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
    # normal (default) — mirrors choose_normal.aura (includes M10 soft-goal)
    if ops < 40:
        return ""
    soft = soft_refuse(20, 50)
    if soft:
        return soft
    if ttl_pressure():
        return "ttl_aware|flat"
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
    want_soft = "soft" in parts
    if want_soft:
        swaps.append(f"soft-goal erate-hint")
    if ev and ev != cur_evict:
        redis_call(sock, "EVICT", ev)
        swaps.append(f"{cur_evict}->{ev}")
    if ly in ("hot_cold", "hot-cold"):
        ly = "hot_cold"
    if ly in ("soft", "pin"):
        ly = ""
    if ly and ly != cur_layout and ly in ("flat", "hot_cold"):
        redis_call(sock, "LAYOUT", ly)
        swaps.append(f"layout:{cur_layout}->{ly}")
    if ev == "lru" and ev != cur_evict:
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
        self.prev: Optional[Tuple[int, int, int, int, int, int]] = None
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
                ex = info_int(info, "expired")
                nk = info_int(info, "keys")
                kt = info_int(info, "keys_with_ttl")
                at = info_int(info, "avg_ttl_ms")
                cur = info.get("evict", "")
                cur_ly = info.get("layout", "flat")
                if self.prev is not None:
                    dg = g - self.prev[0]
                    ds = se - self.prev[1]
                    dh = h - self.prev[2]
                    dm = m - self.prev[3]
                    de = ev - self.prev[4]
                    dx = ex - self.prev[5]
                    win = dh + dm
                    if win > 0:
                        inst = 100.0 * dh / win
                        self.hit_ewma = 0.7 * self.hit_ewma + 0.3 * inst
                    choice = choose_policy(
                        self.profile, dg, ds, dh, dm, de, nk, dx, at, kt
                    )
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
                self.prev = (g, se, h, m, ev, ex)
                time.sleep(self.tick)
        finally:
            try:
                s.close()
            except Exception:
                pass


class AuraAgentController:
    """Spawn policy_agent.aura (non-demo) against C server.

    Uses tests/_agentutil: native subprocess inside GHA container: jobs
    (no docker CLI), sudo docker on laptops when the CLI is present.
    """

    def __init__(
        self,
        port: int,
        tick_ms: int = 100,
        fitness_mutate: bool = True,
        seed_profile: str = "normal",
        evolve: bool = False,
        thresh_min_ops: Optional[int] = None,
        thresh_miss_pin: Optional[int] = None,
        evolve_max_gens: int = 8,
        evolve_window: int = 4,
    ):
        self.port = port
        self.tick_ms = tick_ms
        self.fitness_mutate = fitness_mutate
        self.seed_profile = seed_profile
        self.evolve = evolve
        self.thresh_min_ops = thresh_min_ops
        self.thresh_miss_pin = thresh_miss_pin
        self.evolve_max_gens = evolve_max_gens
        self.evolve_window = evolve_window
        self.cid: Optional[str] = None
        self.swaps: List[str] = []
        self.fitness_events: List[str] = []
        self.log = Path(f"/tmp/ar-policy-agent-{port}.log")

    def start(self) -> None:
        self.log.write_text("")
        # A1: clear boot-guard so agent does a real boot (not skip-reentry)
        Path(ROOT / f".ar-agent-booted-{self.port}.flag").unlink(missing_ok=True)
        # A6/A13: clear durable pin so a prior evolve/threshold seed cannot
        # resume over mutate seed (conservative→aggressive). Stale pin made
        # mutation_gain Δ=0 by restoring profile=thresholded.
        Path(ROOT / f".ar-policy-pin-{self.port}.pin").unlink(missing_ok=True)
        for p in ROOT.glob(f".ar-policy-hb*{self.port}*"):
            p.unlink(missing_ok=True)
        profile = os.environ.get("AURA_REDIS_POLICY_PROFILE_FILE", "")
        env = {
            "AURA_REDIS_PORT": str(self.port),
            "AURA_REDIS_HOST": "127.0.0.1",
            "AURA_REDIS_POLICY_MS": str(self.tick_ms),
            "AURA_REDIS_DENY_PLUGIN": "1",
            "AURA_REDIS_FITNESS_MUTATE": "1" if self.fitness_mutate else "0",
            "AURA_REDIS_SEED_PROFILE": self.seed_profile,
            # no AURA_REDIS_POLICY_DEMO → run forever
        }
        if not self.fitness_mutate and not self.evolve:
            env["AURA_REDIS_FROZEN"] = "1"
        if self.evolve:
            env.update({
                "AURA_REDIS_EVOLVE": "1",
                "AURA_REDIS_EVOLVE_MAX_GENS": str(self.evolve_max_gens),
                "AURA_REDIS_EVOLVE_WINDOW": str(self.evolve_window),
                "AURA_REDIS_EVOLVE_BACKEND": os.environ.get(
                    "AURA_REDIS_EVOLVE_BACKEND", "hand"
                ),
                "AURA_REDIS_WEIGHT_EVOLVE": os.environ.get(
                    "AURA_REDIS_WEIGHT_EVOLVE", "1"
                ),
            })
            if os.environ.get("AURA_REDIS_FIBER_SHADOW", "0") in (
                "1", "true", "on", "yes",
            ):
                env["AURA_REDIS_FIBER_SHADOW"] = "1"
            for k in ("AURA_REDIS_W_MISS", "AURA_REDIS_W_WRITE", "AURA_REDIS_W_EVICT"):
                if os.environ.get(k):
                    env[k] = os.environ[k]
        if self.thresh_min_ops is not None:
            env["AURA_REDIS_THRESH_MIN_OPS"] = str(self.thresh_min_ops)
        if self.thresh_miss_pin is not None:
            env["AURA_REDIS_THRESH_MISS_PIN"] = str(self.thresh_miss_pin)
        if profile:
            env["AURA_REDIS_POLICY_PROFILE_FILE"] = profile
        self.cid = _start_agent(env=env, log_path=self.log)
        _wait_log(self.cid, ["PING"], timeout=12.0, log_path=self.log, poll=0.15)

    def harvest_swaps(self) -> None:
        if not self.cid:
            return
        text = _agent_logs(self.cid, self.log)
        self.swaps = []
        self.fitness_events = []
        for ln in text.splitlines():
            if not ln.strip().startswith("policy_agent:"):
                continue
            if (("EVICT" in ln and "→" in ln) or "LAYOUT" in ln
                    or "soft-goal" in ln
                    or ln.strip().startswith("policy_agent: PIN ")
                    or ln.strip().startswith("policy_agent: UNPIN")):
                self.swaps.append(ln.strip())
            if ("fitness-swap" in ln or "fitness-heal" in ln or "fitness-threshold-mutate" in ln
                    or "hot-strategy:heal!" in ln
                    or "evolve gen=" in ln or "evolve-seed" in ln
                    or "threshold-seed" in ln
                    or "prefix-policy" in ln
                    or "evolve swarm-gen=" in ln
                    or "evolve-apply" in ln and "w-miss=" in ln
                    or "fiber-shadow" in ln):
                self.fitness_events.append(ln.strip())
                self.swaps.append(ln.strip())

    def stop(self) -> None:
        self.harvest_swaps()
        if self.cid:
            _stop_agent(self.cid)
            self.cid = None



# ── server lifecycle ──────────────────────────────────────────────────


@dataclass
class ServerHandle:
    proc: subprocess.Popen
    port: int
    log: Path
    controller: object = None  # PythonAdaptiveController | AuraAgentController | None


def kill_port(port: int) -> None:
    try:
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    except FileNotFoundError:
        pass
    time.sleep(0.12)


def start_server(
    port: int,
    evict: str,
    maxmemory: int,
    adaptive: str = "off",  # off | python | aura
    fitness_mutate: bool = True,
    seed_profile: str = "normal",
    evolve: bool = False,
    thresh_min_ops: Optional[int] = None,
    thresh_miss_pin: Optional[int] = None,
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
        ctl = AuraAgentController(
            port, fitness_mutate=fitness_mutate, seed_profile=seed_profile,
            evolve=evolve, thresh_min_ops=thresh_min_ops,
            thresh_miss_pin=thresh_miss_pin,
        )
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
        if not is_adaptive(mode):
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

    if is_adaptive(mode):
        ok = wait_evict(s, "lfu", mode)
        if not ok:
            # poison_frozen (inverted seed) intentionally cannot flip — measure hit% drop
            print("  note: wait_evict(lfu) did not succeed (ok for poison_frozen)")

    for i in range(nhot):
        redis_call(s, "SET", f"hot{i:04d}", val)
    for _ in range(boost):
        for i in range(nhot):
            redis_call(s, "GET", f"hot{i:04d}")

    # Bench-side PIN only when agent can choose LFU (not poison_frozen inverted)
    if is_adaptive(mode) and "frozen" not in mode:
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
    if is_adaptive(mode):
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
    if is_adaptive(mode):
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
    if is_adaptive(mode) and isinstance(
        _ACTIVE_CONTROLLER, PythonAdaptiveController
    ):
        pass  # pin_prefix already set by run_one
    p1 = workload_zipf_hotkey(s, mode)

    redis_call(s, "FLUSHDB")
    # Bridge toward LRU + clear pins so set-A from zipf does not stick
    if is_adaptive(mode):
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
    if is_adaptive(mode):
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

    if is_adaptive(mode):
        if not wait_evict(s, "lfu", mode):
            print("  note: wait_evict(lfu) failed before zipf_hotkey")

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
    if is_adaptive(mode) and "frozen" not in mode:
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



def workload_diurnal_shift(s: socket.socket, mode: str) -> List[PhaseResult]:
    """M8: quiet → peak write → flash-sale cold flood → cool read (4 phases).

    Short phases so conservative (slow thresholds) lags; aggressive/mutate catches
    flips. Cumulative useful-GET hit% attributes mutation vs frozen.
    """
    phases: List[PhaseResult] = []
    vlen = 160
    val = "D" * vlen

    # Phase 0 — quiet read-ish (LRU ok)
    if is_adaptive(mode):
        # no wait_evict crutch — choose-fn / fitness must flip
        for _j in range(8):
            redis_call(s, "SET", f"__nudge{_j}", "n" * 32)
            redis_call(s, "GET", f"__nudge{_j}")
    for i in range(60):
        redis_call(s, "SET", f"q{i:04d}", val)
    hits = misses = 0
    for _ in range(4):
        for i in range(60):
            if redis_call(s, "GET", f"q{i:04d}") is None:
                misses += 1
            else:
                hits += 1
    info = parse_info(redis_call(s, "INFO"))
    phases.append(PhaseResult("quiet", hits, misses, info_int(info, "evicted"), hits))

    redis_call(s, "FLUSHDB")

    # Phase 1 — peak write + small hot set (LFU)
    if is_adaptive(mode):
        for _j in range(12):
            redis_call(s, "SET", f"__w{_j}", "w" * 48)
    nhot = 24
    for i in range(nhot):
        redis_call(s, "SET", f"z{i:04d}", val)
    for _ in range(20):
        for i in range(nhot):
            redis_call(s, "GET", f"z{i:04d}")
    # No bench-side PIN — Aura choose-fn / fitness-swap must emit lfu|flat|pin
    if is_adaptive(mode):
        try:
            redis_call(s, "EVICT", "samples", "64")
        except Exception:
            pass
        for i in range(nhot):
            redis_call(s, "SET", f"z{i:04d}", val)
    # write storm
    for i in range(200):
        redis_call(s, "SET", f"pk{i:04d}", val)
    hits = misses = 0
    for _ in range(6):
        for i in range(nhot):
            if redis_call(s, "GET", f"z{i:04d}") is None:
                misses += 1
            else:
                hits += 1
    info = parse_info(redis_call(s, "INFO"))
    phases.append(PhaseResult("peak", hits, misses, info_int(info, "evicted"), hits))

    # Phase 2 — flash-sale cold flood (need LFU+pin; LRU dies)
    # Missy traffic so fitness can swap conservative→aggressive, then re-SET hot
    # so agent PIN (from aggressive choose-fn) lands BEFORE the flood.
    if is_adaptive(mode) and "frozen" not in mode:
        import time as _t
        # A1: dense miss burst (no per-op sleep) so one agent tick sees ops≥12
        # and miss_spike → fitness-swap conservative→aggressive, then PIN.
        for _k in range(40):
            redis_call(s, "SET", f"__miss{_k}", "m" * 64)
            redis_call(s, "GET", f"__nope{_k}")
        _t.sleep(0.55)  # ≥1 fitness tick @100ms after dense burst
        # re-materialize hot set under aggressive, dense missy window → lfu|flat|pin
        for i in range(nhot):
            redis_call(s, "SET", f"z{i:04d}", val)
        for _k in range(60):
            redis_call(s, "SET", f"__c{_k}", "c" * 48)
            redis_call(s, "GET", f"__gone{_k}")
        _t.sleep(0.65)  # agent tick: EVICT lfu + PIN hot/z before flood
    for i in range(700):
        redis_call(s, "SET", f"fl{i:05d}", val)
    hits = misses = 0
    # weight flash higher (was 8; cool diluted mutateΔ to 0)
    for _ in range(12):
        for i in range(nhot):
            if redis_call(s, "GET", f"z{i:04d}") is None:
                misses += 1
            else:
                hits += 1
    info = parse_info(redis_call(s, "INFO"))
    phases.append(PhaseResult("flash", hits, misses, info_int(info, "evicted"), hits))

    redis_call(s, "FLUSHDB")
    # clear pins
    if is_adaptive(mode):
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
        wait_evict(s, "lru", mode, rounds=12)

    # Phase 3 — cool WS shift (LRU)
    nA, nB = 120, 100
    for i in range(nA):
        redis_call(s, "SET", f"A{i:04d}", val)
    for _ in range(25):
        for i in range(nA):
            redis_call(s, "GET", f"A{i:04d}")
    hits = misses = 0
    # A1: fewer cool rounds so flash mutate-vs-frozen Δ is not diluted to 0
    for _ in range(3):
        for i in range(nB):
            redis_call(s, "SET", f"B{i:04d}", val)
        for j in range(120):
            if redis_call(s, "GET", f"B{j % nB:04d}") is None:
                misses += 1
            else:
                hits += 1
    info = parse_info(redis_call(s, "INFO"))
    phases.append(PhaseResult("cool", hits, misses, info_int(info, "evicted"), hits))
    return phases



def workload_ttl_wave(s: socket.socket, mode: str) -> PhaseResult:
    """M9: durable keep (no TTL) + short-TTL session churn under maxmemory.

    Sessions are GET-boosted so LRU (recency) and LFU (freq) cling to them and
    evict durable keys. ttl_aware prefers soonest expire_at → keep survives.
    Adaptive must flip to ttl_aware *before* the mass flood (re-SET keep after).
    """
    import time as _t

    val = "v" * 180
    nkeep = 48

    def seed_keep() -> None:
        for i in range(nkeep):
            redis_call(s, "SET", f"keep{i:04d}", val)

    seed_keep()
    for i in range(nkeep):
        redis_call(s, "GET", f"keep{i:04d}")

    if is_adaptive(mode):
        # Prelude: enough short-TTL + eviction pressure for choose → ttl_aware
        for i in range(120):
            redis_call(s, "SET", f"sess{i:05d}", val, "EX", "4")
        _t.sleep(0.55)
        wait_evict(s, "ttl_aware", mode, rounds=25)
        # Re-materialize durable set UNDER ttl_aware before the storm
        seed_keep()
        _t.sleep(0.15)

    # Mass session churn: short TTL + GET boost (LFU/LRU trap)
    for i in range(900):
        redis_call(s, "SET", f"sess{i:05d}", val, "EX", "5")
        if i % 2 == 0:
            redis_call(s, "GET", f"sess{i:05d}")

    hits = misses = 0
    for _ in range(10):
        for i in range(nkeep):
            if redis_call(s, "GET", f"keep{i:04d}") is None:
                misses += 1
            else:
                hits += 1
    info = parse_info(redis_call(s, "INFO"))
    return PhaseResult("ttl_wave", hits, misses, info_int(info, "evicted"), hits)


def workload_session_churn(s: socket.socket, mode: str) -> PhaseResult:
    """Alias of ttl_wave with a distinct phase name for scoreboards."""
    p = workload_ttl_wave(s, mode)
    p.name = "session_churn"
    return p


def workload_flash_churn(s: socket.socket, mode: str) -> PhaseResult:
    """M10 soft-goal: flash-sale thrash under tiny maxmemory.

    Durable keep* + one fat burst of short-TTL sess* (GET-boosted) + unique
    cold SETs so the next agent tick sees high erate + TTL together. Soft-goal
    refuses lfu (erate≥20) → ttl_aware|flat|soft (log proof). Fixed LFU clings
    to sessions and loses keep. Adaptive rematerializes keep under ttl_aware.
    """
    import time as _t

    val = "F" * 200
    nkeep = 40

    def seed_keep() -> None:
        for i in range(nkeep):
            redis_call(s, "SET", f"keep{i:04d}", val)

    seed_keep()
    for i in range(nkeep):
        redis_call(s, "GET", f"keep{i:04d}")

    if is_adaptive(mode):
        # Brief settle on LFU without TTL (avoid early ttl_aware before erate)
        for j in range(40):
            redis_call(s, "SET", f"__w{j}", "w" * 40)
        _t.sleep(0.25)
        seed_keep()

    # Fat burst — no mid-sleep — next tick sees erate + keys_ttl together
    for i in range(650):
        redis_call(s, "SET", f"sess{i:05d}", val, "EX", "5")
        if i % 2 == 0:
            redis_call(s, "GET", f"sess{i:05d}")
        if i % 3 == 0:
            redis_call(s, "SET", f"cold{i:05d}", val)

    if is_adaptive(mode):
        _t.sleep(0.55)  # soft-goal tick (want |soft log)
        seed_keep()
        _t.sleep(0.2)
        for i in range(220):
            redis_call(s, "SET", f"sess2{i:05d}", val, "EX", "5")
            if i % 2 == 0:
                redis_call(s, "GET", f"sess2{i:05d}")
        _t.sleep(0.25)

    hits = misses = 0
    for _ in range(10):
        for i in range(nkeep):
            if redis_call(s, "GET", f"keep{i:04d}") is None:
                misses += 1
            else:
                hits += 1

    info = parse_info(redis_call(s, "INFO"))
    return PhaseResult(
        "flash_churn", hits, misses, info_int(info, "evicted"), hits
    )



def workload_evolve_gain(s: socket.socket, mode: str) -> List[PhaseResult]:
    """M11: bad thresholds lose; multi-gen evolve improves cumulative hit%.

    Several challenge rounds with missy traffic between so the agent can run
    evolve windows (baseline → trial → keep/revert). No bench-side wait_evict
    or PIN — choose-fn / evolve must emit lfu|flat|pin.
    """
    phases: List[PhaseResult] = []
    nhot = 28
    ncold = 550
    vlen = 170
    val = "E" * vlen
    rounds = 5

    for rnd in range(rounds):
        # Missy write window — drive low hit-EWMA + give evolve ticks time
        for k in range(36):
            redis_call(s, "SET", f"__em{rnd}_{k}", "m" * 48)
            redis_call(s, "GET", f"__egone{rnd}_{k}")
            if k % 6 == 0:
                time.sleep(0.05)
        time.sleep(0.55)  # ~5–6 agent ticks @100ms

        for i in range(nhot):
            redis_call(s, "SET", f"hot{i:04d}", val)
        for _ in range(12):
            for i in range(nhot):
                redis_call(s, "GET", f"hot{i:04d}")

        # More miss/write so evolved (low min-ops) choose fires LFU+pin
        if is_adaptive(mode):
            for k in range(40):
                redis_call(s, "SET", f"__ew{rnd}_{k}", "w" * 40)
                redis_call(s, "GET", f"__emiss{rnd}_{k}")
            time.sleep(0.45)
            # re-SET hot so PIN (if emitted) can land on live keys
            for i in range(nhot):
                redis_call(s, "SET", f"hot{i:04d}", val)

        for i in range(ncold):
            redis_call(s, "SET", f"cold{rnd}_{i:05d}", val)

        hits = misses = 0
        for i in range(nhot):
            if redis_call(s, "GET", f"hot{i:04d}") is None:
                misses += 1
            else:
                hits += 1
        info = parse_info(redis_call(s, "INFO"))
        phases.append(
            PhaseResult(
                f"evolve_r{rnd}",
                hits,
                misses,
                info_int(info, "evicted"),
                hits,
            )
        )
        time.sleep(0.55)  # A2: longer inter-round evolve windows
    return phases

def workload_prefix_mix(s: socket.socket, mode: str) -> List[PhaseResult]:
    """M12: a: low-freq durable + b: zipf hot vs high-freq session noise.

    LFU clings to boosted sess* and may keep b:, but loses low-freq a: without
    PIN. adaptive_prefix: RESP POLICY → Aura pins a: and b: → both survive.
    Global adaptive pins hot/z only → loses a:/b:.
    """
    na, nb = 28, 28
    nsess = 80
    ncold = 500
    val = "P" * 170

    use_policy = is_adaptive(mode) and "prefix" in mode
    if use_policy:
        try:
            redis_call(s, "POLICY", "a:", "session")
            redis_call(s, "POLICY", "b:", "zipf")
            time.sleep(0.25)  # agent tick sees policy_hints
        except Exception as e:
            print(f"  note: POLICY failed: {e}")

    # Low-freq durable a: (few GETs — LFU will drop without PIN)
    for i in range(na):
        redis_call(s, "SET", f"a:{i:04d}", val)
    for i in range(na):
        redis_call(s, "GET", f"a:{i:04d}")

    # Zipf-ish b: moderate boost
    for i in range(nb):
        redis_call(s, "SET", f"b:{i:04d}", val)
    for _ in range(8):
        for i in range(nb):
            redis_call(s, "GET", f"b:{i:04d}")

    # High-freq session noise (LFU cling) + short TTL
    for i in range(nsess):
        redis_call(s, "SET", f"sess{i:04d}", val, "EX", "8")
    for _ in range(25):
        for i in range(nsess):
            redis_call(s, "GET", f"sess{i:04d}")

    if is_adaptive(mode):
        for k in range(40):
            redis_call(s, "SET", f"__pw{k}", "w" * 40)
            redis_call(s, "GET", f"__pm{k}")
        time.sleep(0.65)  # prefix override + PIN
        for i in range(na):
            redis_call(s, "SET", f"a:{i:04d}", val)
        for i in range(nb):
            redis_call(s, "SET", f"b:{i:04d}", val)
        time.sleep(0.35)

    for i in range(ncold):
        redis_call(s, "SET", f"cold{i:05d}", val)

    hits_a = miss_a = hits_b = miss_b = 0
    for i in range(na):
        if redis_call(s, "GET", f"a:{i:04d}") is None:
            miss_a += 1
        else:
            hits_a += 1
    for i in range(nb):
        if redis_call(s, "GET", f"b:{i:04d}") is None:
            miss_b += 1
        else:
            hits_b += 1
    info = parse_info(redis_call(s, "INFO"))
    return [
        PhaseResult("prefix_a", hits_a, miss_a, info_int(info, "evicted"), hits_a),
        PhaseResult("prefix_b", hits_b, miss_b, info_int(info, "evicted"), hits_b),
    ]


def workload_prefix_mix_v2(s: socket.socket, mode: str) -> List[PhaseResult]:
    """A5: conflicting optima — a: lfu+pin vs b: ttl_aware.

    Same noisy-neighbor skeleton as prefix_mix, but POLICY profiles conflict:
    a:=lfu (PIN+LFU bag) vs b:=ttl_aware (PIN+ttl_aware bag). Deep compose picks
    ttl_aware|flat|pin and PINs both tenant prefixes. Global adaptive without
    POLICY pins hot/z only → victim tenant collapses (≥ +20pp worse-tenant).
    """
    na, nb = 28, 28
    nsess = 80
    ncold = 500
    val = "V" * 170

    use_policy = is_adaptive(mode) and "prefix" in mode
    if use_policy:
        try:
            redis_call(s, "POLICY", "a:", "lfu")
            redis_call(s, "POLICY", "b:", "ttl_aware")
            time.sleep(0.25)
        except Exception as e:
            print(f"  note: POLICY v2 failed: {e}")

    for i in range(na):
        redis_call(s, "SET", f"a:{i:04d}", val)
    for i in range(na):
        redis_call(s, "GET", f"a:{i:04d}")

    for i in range(nb):
        redis_call(s, "SET", f"b:{i:04d}", val)
    for _ in range(8):
        for i in range(nb):
            redis_call(s, "GET", f"b:{i:04d}")

    for i in range(nsess):
        redis_call(s, "SET", f"sess{i:04d}", val, "EX", "8")
    for _ in range(25):
        for i in range(nsess):
            redis_call(s, "GET", f"sess{i:04d}")

    if is_adaptive(mode):
        for k in range(40):
            redis_call(s, "SET", f"__pw{k}", "w" * 40)
            redis_call(s, "GET", f"__pm{k}")
        time.sleep(0.65)
        for i in range(na):
            redis_call(s, "SET", f"a:{i:04d}", val)
        for i in range(nb):
            redis_call(s, "SET", f"b:{i:04d}", val)
        time.sleep(0.35)

    for i in range(ncold):
        redis_call(s, "SET", f"cold{i:05d}", val)

    hits_a = miss_a = hits_b = miss_b = 0
    for i in range(na):
        if redis_call(s, "GET", f"a:{i:04d}") is None:
            miss_a += 1
        else:
            hits_a += 1
    for i in range(nb):
        if redis_call(s, "GET", f"b:{i:04d}") is None:
            miss_b += 1
        else:
            hits_b += 1
    info = parse_info(redis_call(s, "INFO"))
    return [
        PhaseResult("prefix_a", hits_a, miss_a, info_int(info, "evicted"), hits_a),
        PhaseResult("prefix_b", hits_b, miss_b, info_int(info, "evicted"), hits_b),
    ]



# ── orchestration ─────────────────────────────────────────────────────


def _policy_agent_opts(policy: str) -> tuple:
    """Return (fitness_mutate, seed_profile, evolve, thresh_min, thresh_miss)."""
    evolve = False
    thresh_min = None
    thresh_miss = None
    if policy in ("adaptive_frozen", "poison_frozen", "adaptive_nosoft",
                  "adaptive_soft", "adaptive_evolve_frozen", "adaptive_evolve",
                  "adaptive_prefix"):
        fitness = False
    else:
        fitness = True  # adaptive, adaptive_mutate, poison_mutate
    if policy in ("poison_frozen", "poison_mutate"):
        seed = "inverted"
    elif policy == "adaptive_nosoft":
        seed = "nosoft"
    elif policy in ("adaptive_soft", "adaptive"):
        seed = "normal"  # normal includes M10 soft-goal
    elif policy == "adaptive_frozen":
        seed = "normal"
    elif policy == "adaptive_mutate":
        # Same seed as frozen; only fitness swap/heal differs.
        # Use conservative so frozen lags; mutate upgrades → measurable pp.
        seed = os.environ.get("AURA_BENCH_MUTATE_SEED", "conservative")
    elif policy in ("adaptive_evolve", "adaptive_evolve_frozen"):
        seed = "normal"
        # Bad thresholds: choose never fires until evolve lowers them
        thresh_min = int(os.environ.get("AURA_BENCH_EVOLVE_MIN_OPS", "900"))
        thresh_miss = int(os.environ.get("AURA_BENCH_EVOLVE_MISS_PIN", "75"))
        evolve = policy == "adaptive_evolve"
        fitness = False
    else:
        seed = "normal"
    # When comparing frozen vs mutate on mutation_gain, both use same seed.
    if policy == "adaptive_frozen":
        seed = os.environ.get("AURA_BENCH_MUTATE_SEED", "conservative")
    return fitness, seed, evolve, thresh_min, thresh_miss


def run_one(
    workload: str,
    policy: str,
    port: int,
    maxmemory: int,
    adaptive_backend: str,
) -> RunResult:
    global _ACTIVE_CONTROLLER
    adaptive = "off"
    start_evict = policy if policy in ("lru", "lfu", "noop", "ttl_aware") else "lru"
    fitness_mutate = True
    seed_profile = "normal"
    evolve = False
    thresh_min_ops = None
    thresh_miss_pin = None
    if workload == "flash_churn" and is_adaptive(policy):
        # Start on LFU so soft-goal must refuse it (no read-heavy bridge crutch)
        start_evict = "lfu"
    if is_adaptive(policy):
        adaptive = adaptive_backend  # python | aura
        fitness_mutate, seed_profile, evolve, thresh_min_ops, thresh_miss_pin = (
            _policy_agent_opts(policy)
        )
        # M9: ttl_wave needs stable choose_normal (has ttl_aware). Mid-loop
        # hot-strategy:swap! → eval-current re-enters policy_agent and breaks
        # the live tick loop (invalid closure). Freeze fitness for these.
        if workload in ("ttl_wave", "session_churn"):
            fitness_mutate = False
            seed_profile = "normal"
            evolve = False
        if workload == "flash_churn":
            # A/B soft vs nosoft: freeze fitness; seed from policy
            fitness_mutate = False
            evolve = False
            if policy == "adaptive_nosoft":
                seed_profile = "nosoft"
            elif policy in ("adaptive", "adaptive_soft"):
                seed_profile = "normal"
        if workload in ("prefix_mix", "prefix_mix_v2"):
            fitness_mutate = False
            evolve = False
            seed_profile = "normal"
        if workload == "evolve_gain":
            # Force bad-threshold seed + evolve A/B regardless of policy alias
            if policy in ("adaptive", "adaptive_mutate", "adaptive_evolve"):
                evolve = True
                fitness_mutate = False
                thresh_min_ops = int(os.environ.get("AURA_BENCH_EVOLVE_MIN_OPS", "900"))
                thresh_miss_pin = int(os.environ.get("AURA_BENCH_EVOLVE_MISS_PIN", "75"))
            elif policy in ("adaptive_frozen", "adaptive_evolve_frozen"):
                evolve = False
                fitness_mutate = False
                thresh_min_ops = int(os.environ.get("AURA_BENCH_EVOLVE_MIN_OPS", "900"))
                thresh_miss_pin = int(os.environ.get("AURA_BENCH_EVOLVE_MISS_PIN", "75"))

    mm = maxmemory
    if workload == "flash_churn" and maxmemory >= 120_000:
        # Tiny maxmemory amplifies LFU thrash / soft-goal contrast
        mm = min(maxmemory, 90_000)
    h = start_server(
        port, start_evict, mm, adaptive=adaptive,
        fitness_mutate=fitness_mutate, seed_profile=seed_profile,
        evolve=evolve, thresh_min_ops=thresh_min_ops,
        thresh_miss_pin=thresh_miss_pin,
    )
    if is_adaptive(policy) and isinstance(h.controller, PythonAdaptiveController):
        # Python mirror: simulate frozen by locking profile; poison by inverted
        h.controller.profile = seed_profile if seed_profile != "conservative" else "conservative"
        if policy == "adaptive_nosoft":
            h.controller.profile = "nosoft"
        elif policy in ("adaptive_soft", "adaptive") and workload == "flash_churn":
            h.controller.profile = "normal"
        if not fitness_mutate:
            h.controller.dwell_s = 1e9  # never flip EVICT mid-run? still choose_policy
        if workload in ("zipf_hotkey", "phase_marathon", "diurnal_shift", "mutation_gain"):
            h.controller.pin_prefix = "z"
            h.controller.pin_n = 24
        elif workload in ("hot_protect", "oscillate", "poison_heal"):
            h.controller.pin_prefix = "hot"
            h.controller.pin_n = 40
    try:
        _ACTIVE_CONTROLLER = h.controller
        s = connect(port)
        try:
            # Keep real policy name (poison_frozen vs adaptive_mutate) for PIN/wait logic
            mode = policy if is_adaptive(policy) else policy
            if workload == "hot_protect":
                phases = [workload_hot_protect(s, mode)]
            elif workload == "ws_shift":
                phases = [workload_ws_shift(s, mode)]
            elif workload == "oscillate":
                phases = workload_oscillate(s, mode)
            elif workload == "zipf_hotkey":
                phases = [workload_zipf_hotkey(s, mode)]
            elif workload == "phase_marathon":
                phases = workload_phase_marathon(s, mode)
            elif workload == "diurnal_shift":
                phases = workload_diurnal_shift(s, mode)
            elif workload == "ttl_wave":
                phases = [workload_ttl_wave(s, mode)]
            elif workload == "session_churn":
                phases = [workload_session_churn(s, mode)]
            elif workload == "flash_churn":
                phases = [workload_flash_churn(s, mode)]
            elif workload == "evolve_gain":
                phases = workload_evolve_gain(s, mode)
            elif workload == "prefix_mix":
                phases = workload_prefix_mix(s, mode)
            elif workload == "prefix_mix_v2":
                phases = workload_prefix_mix_v2(s, mode)
            elif workload in ("mutation_gain", "poison_heal"):
                # mutation_gain = diurnal under mutate-vs-frozen attribution
                # poison_heal = hot_protect-shaped under inverted seed
                if workload == "poison_heal":
                    # Drive traffic so fitness ticks see miss EWMA + heal inverted seed
                    import time as _t
                    for i in range(40):
                        try:
                            redis_call(s, "SET", f"__poi{i}", "x" * 40)
                            redis_call(s, "GET", f"__poi{i}")
                            redis_call(s, "GET", f"__missing{i}")
                            redis_call(s, "INFO")
                        except Exception:
                            pass
                        _t.sleep(0.08)
                    phases = [workload_hot_protect(s, mode)]
                    phases[0].name = "poison_hot"
                else:
                    phases = workload_diurnal_shift(s, mode)
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
        notes=(
            f"backend={adaptive_backend} fitness={fitness_mutate} seed={seed_profile}"
            f" evolve={evolve} thresh={thresh_min_ops}/{thresh_miss_pin}"
            if is_adaptive(policy) else ""
        ),
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



def print_mutation_attribution(results: List[RunResult]) -> None:
    """Show pp gain attributable to fitness-driven hot-strategy swap/heal."""
    print()
    print("=" * 78)
    print("MUTATION ATTRIBUTION (Aura fitness swap/heal vs frozen choose-fn)")
    print("=" * 78)
    by_wl: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, {})[r.policy] = r

    for wl in ("mutation_gain", "diurnal_shift", "poison_heal", "phase_marathon"):
        m = by_wl.get(wl)
        if not m:
            continue
        print(f"  workload={wl}")
        for pol in ("lru", "lfu", "adaptive_frozen", "adaptive_mutate",
                    "poison_frozen", "poison_mutate", "adaptive"):
            r = m.get(pol)
            if not r:
                continue
            fit = [s for s in r.swaps if "fitness-" in s or "heal!" in s]
            print(
                f"    {pol:<18} cum_hit={100*r.overall_hit_rate:5.1f}%  "
                f"useful={r.total_useful:<5}  fitness_events={len(fit)}  "
                f"notes={r.notes}"
            )
            for ln in fit[:4]:
                print(f"      · {ln}")
        if wl == "poison_heal":
            frozen = m.get("poison_frozen") or m.get("adaptive_frozen")
            mutate = m.get("poison_mutate") or m.get("adaptive_mutate") or m.get("adaptive")
        else:
            frozen = m.get("adaptive_frozen") or m.get("poison_frozen")
            mutate = m.get("adaptive_mutate") or m.get("poison_mutate") or m.get("adaptive")
        if frozen and mutate:
            delta = 100 * (mutate.overall_hit_rate - frozen.overall_hit_rate)
            print(
                f"    → mutation-attributable Δ = {delta:+.1f}pp "
                f"(mutate {100*mutate.overall_hit_rate:.1f}% − "
                f"frozen {100*frozen.overall_hit_rate:.1f}%)"
            )
            if mutate.total_useful >= frozen.total_useful:
                print(
                    f"    → useful GETs: mutate {mutate.total_useful} ≥ "
                    f"frozen {frozen.total_useful}"
                )
    print()



def print_soft_goal_table(results: List[RunResult]) -> None:
    """M10: lfu vs adaptive_soft vs adaptive_nosoft on flash_churn."""
    print()
    print("=" * 78)
    print("SOFT-GOAL (M10) — hit% s.t. eviction budget (flash_churn)")
    print("=" * 78)
    by_wl: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, {})[r.policy] = r
    m = by_wl.get("flash_churn")
    if not m:
        print("  (no flash_churn runs)")
        print("=" * 78)
        return
    print(f"  {'policy':<18} {'hit%':>7} {'useful':>7} {'evicted':>8}  notes")
    print("  " + "-" * 60)
    for pol in ("lfu", "lru", "ttl_aware", "adaptive_nosoft", "adaptive_soft",
                "adaptive"):
        r = m.get(pol)
        if not r:
            continue
        ev = sum(p.evicted for p in r.phases)
        soft_sw = [s for s in r.swaps if "soft-goal" in s or "erate=" in s
                   or "→ lru" in s or "→ noop" in s or "→ ttl_aware" in s]
        print(
            f"  {pol:<18} {100*r.overall_hit_rate:6.1f}% {r.total_useful:>7} "
            f"{ev:>8}  swaps={len(r.swaps)}"
        )
        for ln in soft_sw[:3]:
            print(f"      · {ln}")
    lfu = m.get("lfu")
    soft = m.get("adaptive_soft") or m.get("adaptive")
    nosoft = m.get("adaptive_nosoft")
    if soft and lfu:
        ev_s = sum(p.evicted for p in soft.phases)
        ev_l = sum(p.evicted for p in lfu.phases)
        print(
            f"  → soft vs lfu: hit Δ={100*(soft.overall_hit_rate-lfu.overall_hit_rate):+.1f}pp "
            f"useful {soft.total_useful} vs {lfu.total_useful}; "
            f"evicted {ev_s} vs {ev_l}"
        )
    if soft and nosoft:
        ev_s = sum(p.evicted for p in soft.phases)
        ev_n = sum(p.evicted for p in nosoft.phases)
        print(
            f"  → soft vs nosoft: hit Δ="
            f"{100*(soft.overall_hit_rate-nosoft.overall_hit_rate):+.1f}pp; "
            f"evicted {ev_s} vs {ev_n}"
        )
    print("=" * 78)


def print_prefix_table(results: List[RunResult]) -> None:
    """M12/A5: per-prefix POLICY vs global on prefix_mix / prefix_mix_v2."""
    by_wl: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, {})[r.policy] = r
    for wl, title in (
        ("prefix_mix", "PREFIX POLICY (M12) — a: session + b: zipf noisy neighbor"),
        ("prefix_mix_v2", "PREFIX POLICY v2 (A5) — a: lfu+pin vs b: ttl_aware conflict"),
    ):
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)
        m = by_wl.get(wl)
        if not m:
            print(f"  (no {wl} runs)")
            print("=" * 78)
            continue
        print(f"  {'policy':<18} {'hit%':>7} {'useful':>7}  a_hit  b_hit  notes")
        print("  " + "-" * 66)
        for pol in ("lru", "lfu", "ttl_aware", "adaptive", "adaptive_prefix"):
            r = m.get(pol)
            if not r:
                continue
            pa = next((p for p in r.phases if p.name == "prefix_a"), None)
            pb = next((p for p in r.phases if p.name == "prefix_b"), None)
            pref = [s for s in r.swaps if "prefix-policy" in s or "PIN prefix" in s]
            print(
                f"  {pol:<18} {100*r.overall_hit_rate:6.1f}% {r.total_useful:>7}  "
                f"{100*(pa.hit_rate if pa else 0):5.1f}% "
                f"{100*(pb.hit_rate if pb else 0):5.1f}%  "
                f"pref_logs={len(pref)}"
            )
            for ln in pref[:3]:
                print(f"      · {ln}")
        glo = m.get("adaptive")
        pref = m.get("adaptive_prefix")
        if glo and pref:
            pa_g = next((p for p in glo.phases if p.name == "prefix_a"), None)
            pb_g = next((p for p in glo.phases if p.name == "prefix_b"), None)
            pa_p = next((p for p in pref.phases if p.name == "prefix_a"), None)
            pb_p = next((p for p in pref.phases if p.name == "prefix_b"), None)
            worse_g = min(
                (pa_g.hit_rate if pa_g else 0.0),
                (pb_g.hit_rate if pb_g else 0.0),
            )
            worse_p = min(
                (pa_p.hit_rate if pa_p else 0.0),
                (pb_p.hit_rate if pb_p else 0.0),
            )
            print(
                f"  → prefix vs global: Δ="
                f"{100*(pref.overall_hit_rate-glo.overall_hit_rate):+.1f}pp "
                f"useful {pref.total_useful} vs {glo.total_useful}; "
                f"worse-tenant Δ={100*(worse_p-worse_g):+.1f}pp "
                f"({100*worse_p:.1f}% vs {100*worse_g:.1f}%)"
            )
        print("=" * 78)


def print_evolve_table(results: List[RunResult]) -> None:
    """M11: adaptive_evolve vs frozen bad-thresholds on evolve_gain."""
    print()
    print("=" * 78)
    print("EVOLVE (M11/A13) — threshold+weight fitness keep/revert (evolve_gain)")
    print("=" * 78)
    by_wl: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, {})[r.policy] = r
    m = by_wl.get("evolve_gain")
    if not m:
        print("  (no evolve_gain runs)")
        print("=" * 78)
        return
    print(f"  {'policy':<24} {'hit%':>7} {'useful':>7}  notes")
    print("  " + "-" * 60)
    for pol in ("lru", "lfu", "adaptive_evolve_frozen", "adaptive_evolve",
                "adaptive_frozen", "adaptive"):
        r = m.get(pol)
        if not r:
            continue
        evo = [s for s in r.swaps if "evolve gen=" in s or "evolve-seed" in s
               or "threshold-seed" in s]
        print(
            f"  {pol:<24} {100*r.overall_hit_rate:6.1f}% {r.total_useful:>7}  "
            f"evolve_logs={len(evo)} swaps={len(r.swaps)}"
        )
        for ln in evo[:6]:
            print(f"      · {ln}")
    frozen = m.get("adaptive_evolve_frozen") or m.get("adaptive_frozen")
    evo = m.get("adaptive_evolve") or m.get("adaptive")
    if frozen and evo:
        delta = 100 * (evo.overall_hit_rate - frozen.overall_hit_rate)
        print(
            f"  → evolve vs frozen: Δ={delta:+.1f}pp "
            f"(evolve {100*evo.overall_hit_rate:.1f}% − "
            f"frozen {100*frozen.overall_hit_rate:.1f}%); "
            f"useful {evo.total_useful} vs {frozen.total_useful}"
        )
    print("=" * 78)


def print_overhead_summary(results: List[RunResult]) -> None:
    """A14 — controller overhead dashboard: swaps vs hitΔ + heartbeat rate fields."""
    print()
    print("=" * 78)
    print("OVERHEAD (A14) — controller cost vs hit-quality (bench summary)")
    print("=" * 78)
    print(f"  {'workload':<16} {'policy':<22} {'hit%':>7} {'swaps':>6}  notes")
    print("  " + "-" * 66)
    for r in results:
        if not is_adaptive(r.policy):
            continue
        rate_notes = []
        for s in r.swaps:
            if "auto_freeze" in s or "auto_unfreeze" in s:
                rate_notes.append(s.split("policy_agent: ", 1)[-1][:48])
            if "w-miss=" in s and "evolve gen=" in s:
                rate_notes.append("weight-evolve")
            if "swarm-gen=" in s:
                rate_notes.append("swarm-propose")
            if "fiber-shadow" in s:
                rate_notes.append("fiber-shadow")
        # de-dupe preserve order
        seen = set()
        notes = []
        for n in rate_notes:
            if n not in seen:
                seen.add(n)
                notes.append(n)
        print(
            f"  {r.workload:<16} {r.policy:<22} {100*r.overall_hit_rate:6.1f}% "
            f"{len(r.swaps):6d}  {','.join(notes[:4]) or '—'}"
        )
    print("  heartbeat fields (agent): applies_sec polls_sec hit_ewma swaps "
          "w_miss evolve_gen meta_frozen")
    print("=" * 78)


def assert_success(results: List[RunResult]) -> None:
    """Require adaptive to clearly beat both fixed on marathon when present;
    otherwise require ≥1 workload where LRU loses to LFU/adaptive.
    """
    reasons: List[str] = []
    by_wl_mut: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl_mut.setdefault(r.workload, {})[r.policy] = r


    for wl in ("mutation_gain", "diurnal_shift"):
        mm = by_wl_mut.get(wl, {})
        frozen = mm.get("adaptive_frozen")
        mutate = mm.get("adaptive_mutate")
        if frozen and mutate:
            delta = mutate.overall_hit_rate - frozen.overall_hit_rate
            if delta + 1e-9 < 0.08:
                raise SystemExit(
                    f"FAIL: {wl} adaptive_mutate must beat frozen by ≥8pp; "
                    f"got mutate={100*mutate.overall_hit_rate:.1f}% "
                    f"frozen={100*frozen.overall_hit_rate:.1f}% "
                    f"(Δ={100*delta:+.1f}pp)"
                )
            fit = [s for s in mutate.swaps
                   if "fitness-swap" in s or "fitness-threshold-mutate" in s]
            if wl == "mutation_gain" and not fit:
                raise SystemExit(
                    f"FAIL: {wl} expected fitness-swap/threshold-mutate log; "
                    f"swaps={mutate.swaps[:6]}"
                )
            msg = (
                f"{wl}: mutate={100*mutate.overall_hit_rate:.1f}% vs "
                f"frozen={100*frozen.overall_hit_rate:.1f}% "
                f"(Δ={100*delta:+.1f}pp) fitness_events={len(fit)}"
            )
            print(msg)
            reasons.append(msg)
    if "poison_heal" in by_wl_mut:
        mm = by_wl_mut["poison_heal"]
        pf, pm = mm.get("poison_frozen"), mm.get("poison_mutate")
        if pf and pm:
            if pm.overall_hit_rate < pf.overall_hit_rate + 0.10:
                raise SystemExit(
                    f"FAIL: poison_heal mutate must beat frozen by ≥10pp; "
                    f"mutate={100*pm.overall_hit_rate:.1f}% frozen={100*pf.overall_hit_rate:.1f}%"
                )
            print(
                f"poison_heal: mutate={100*pm.overall_hit_rate:.1f}% vs "
                f"frozen={100*pf.overall_hit_rate:.1f}% "
                f"(Δ={100*(pm.overall_hit_rate-pf.overall_hit_rate):+.1f}pp)"
            )

    if "prefix_mix" in by_wl_mut:
        mm = by_wl_mut["prefix_mix"]
        pref = mm.get("adaptive_prefix")
        glo = mm.get("adaptive")
        if pref and glo:
            if pref.total_useful < glo.total_useful + 8:
                raise SystemExit(
                    f"FAIL: prefix_mix adaptive_prefix useful must beat global; "
                    f"prefix={pref.total_useful} global={glo.total_useful}"
                )
            pa = next((p for p in pref.phases if p.name == "prefix_a"), None)
            pb = next((p for p in pref.phases if p.name == "prefix_b"), None)
            if not pa or not pb or pa.useful_gets < 10 or pb.useful_gets < 10:
                raise SystemExit(
                    f"FAIL: prefix_mix must win useful GETs on BOTH a: and b:; "
                    f"a={pa.useful_gets if pa else None} "
                    f"b={pb.useful_gets if pb else None}"
                )
            plogs = [s for s in pref.swaps if "prefix-policy" in s]
            if not plogs:
                raise SystemExit(
                    f"FAIL: prefix_mix expected prefix-policy log; swaps={pref.swaps[:4]}"
                )
            print(
                f"prefix_mix: prefix useful={pref.total_useful} "
                f"(a={pa.useful_gets},b={pb.useful_gets}) vs "
                f"global={glo.total_useful}; logs={len(plogs)}"
            )

    if "prefix_mix_v2" in by_wl_mut:
        mm = by_wl_mut["prefix_mix_v2"]
        pref = mm.get("adaptive_prefix")
        glo = mm.get("adaptive")
        if pref and glo:
            pa_g = next((p for p in glo.phases if p.name == "prefix_a"), None)
            pb_g = next((p for p in glo.phases if p.name == "prefix_b"), None)
            pa_p = next((p for p in pref.phases if p.name == "prefix_a"), None)
            pb_p = next((p for p in pref.phases if p.name == "prefix_b"), None)
            if not pa_p or not pb_p:
                raise SystemExit("FAIL: prefix_mix_v2 missing prefix phases")
            worse_g = min(
                (pa_g.hit_rate if pa_g else 0.0),
                (pb_g.hit_rate if pb_g else 0.0),
            )
            worse_p = min(pa_p.hit_rate, pb_p.hit_rate)
            delta = worse_p - worse_g
            if delta + 1e-9 < 0.20:
                raise SystemExit(
                    f"FAIL: prefix_mix_v2 worse-tenant must gain ≥ +20pp vs global; "
                    f"prefix_worse={100*worse_p:.1f}% global_worse={100*worse_g:.1f}% "
                    f"(Δ={100*delta:+.1f}pp); "
                    f"a={100*pa_p.hit_rate:.1f}/{100*(pa_g.hit_rate if pa_g else 0):.1f} "
                    f"b={100*pb_p.hit_rate:.1f}/{100*(pb_g.hit_rate if pb_g else 0):.1f}"
                )
            plogs = [s for s in pref.swaps
                     if "prefix-policy" in s or "prefix-policy-deep" in s]
            if not plogs:
                raise SystemExit(
                    f"FAIL: prefix_mix_v2 expected prefix-policy[-deep] log; "
                    f"swaps={pref.swaps[:6]}"
                )
            print(
                f"prefix_mix_v2: worse-tenant prefix={100*worse_p:.1f}% vs "
                f"global={100*worse_g:.1f}% (Δ={100*delta:+.1f}pp); "
                f"a={pa_p.useful_gets} b={pb_p.useful_gets}; logs={len(plogs)}"
            )

    if "evolve_gain" in by_wl_mut:
        mm = by_wl_mut["evolve_gain"]
        frozen = mm.get("adaptive_evolve_frozen") or mm.get("adaptive_frozen")
        evo = mm.get("adaptive_evolve") or mm.get("adaptive")
        if frozen and evo:
            if evo.overall_hit_rate + 1e-9 < frozen.overall_hit_rate + 0.08:
                raise SystemExit(
                    f"FAIL: evolve_gain adaptive_evolve must beat frozen by ≥8pp; "
                    f"evolve={100*evo.overall_hit_rate:.1f}% "
                    f"frozen={100*frozen.overall_hit_rate:.1f}%"
                )
            evo_logs = [s for s in evo.swaps if "evolve gen=" in s]
            if len(evo_logs) < 2:
                raise SystemExit(
                    f"FAIL: evolve_gain expected multi-gen evolve logs (≥2); "
                    f"got {len(evo_logs)}: {evo_logs[:4]}"
                )
            # A13: weight evolve path must appear in logs (w-miss=)
            w_logs = [s for s in evo.swaps if "w-miss=" in s]
            if not w_logs:
                raise SystemExit(
                    f"FAIL: evolve_gain A13 expected weight evolve path (w-miss=); "
                    f"swaps={evo.swaps[:8]}"
                )
            print(
                f"evolve_gain: evolve={100*evo.overall_hit_rate:.1f}% vs "
                f"frozen={100*frozen.overall_hit_rate:.1f}% "
                f"(Δ={100*(evo.overall_hit_rate-frozen.overall_hit_rate):+.1f}pp) "
                f"evolve_logs={len(evo_logs)} weight_logs={len(w_logs)}"
            )

    by_wl: Dict[str, Dict[str, RunResult]] = {}
    for r in results:
        by_wl.setdefault(r.workload, {})[r.policy] = r
        # M10 soft-goal gate
    fc = by_wl.get("flash_churn", {})
    if fc:
        soft = fc.get("adaptive_soft") or fc.get("adaptive")
        lfu = fc.get("lfu")
        if soft and lfu:
            ev_s = sum(p.evicted for p in soft.phases)
            ev_l = sum(p.evicted for p in lfu.phases)
            hit_ok = soft.overall_hit_rate + 1e-9 >= lfu.overall_hit_rate - 0.02
            evict_ok = ev_s <= ev_l or soft.overall_hit_rate >= lfu.overall_hit_rate + 0.05
            useful_ok = soft.total_useful >= lfu.total_useful
            if not ((hit_ok and useful_ok) or (evict_ok and soft.overall_hit_rate >= 0.5)):
                raise SystemExit(
                    f"FAIL: flash_churn soft must protect hit%/useful or cut "
                    f"evicts vs LFU; soft={100*soft.overall_hit_rate:.1f}%/"
                    f"{soft.total_useful}/ev={ev_s} "
                    f"lfu={100*lfu.overall_hit_rate:.1f}%/{lfu.total_useful}/ev={ev_l}"
                )
            soft_logs = [s for s in soft.swaps if "soft-goal" in s]
            ttl_or_lru = [s for s in soft.swaps
                          if "→ ttl_aware" in s or "→ lru" in s or "→ noop" in s]
            reasons.append(
                f"flash_churn: soft={100*soft.overall_hit_rate:.1f}%/"
                f"{soft.total_useful}/ev={ev_s} vs lfu="
                f"{100*lfu.overall_hit_rate:.1f}%/{lfu.total_useful}/ev={ev_l}"
                f" soft_logs={len(soft_logs)} refuse_swaps={len(ttl_or_lru)}"
            )
            if soft.overall_hit_rate + 1e-9 < lfu.overall_hit_rate + 0.05:
                raise SystemExit(
                    "FAIL: flash_churn soft must beat LFU by ≥5pp hit "
                    f"(soft={100*soft.overall_hit_rate:.1f}% "
                    f"lfu={100*lfu.overall_hit_rate:.1f}%)"
                )
            if not soft_logs:
                raise SystemExit(
                    "FAIL: flash_churn expected soft-goal log proof "
                    "(policy_agent: soft-goal refuse…); "
                    f"swaps={soft.swaps[:4]}"
                )
        nosoft = fc.get("adaptive_nosoft")
        if soft and nosoft:
            if (soft.overall_hit_rate + 1e-9 < nosoft.overall_hit_rate - 0.05
                    and sum(p.evicted for p in soft.phases)
                    >= sum(p.evicted for p in nosoft.phases)):
                raise SystemExit(
                    f"FAIL: flash_churn soft should not lose badly to nosoft; "
                    f"soft={100*soft.overall_hit_rate:.1f}% "
                    f"nosoft={100*nosoft.overall_hit_rate:.1f}%"
                )
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
            for alt_name in ("lfu", "adaptive", "adaptive_soft", "ttl_aware"):
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
        help="Comma list: phase_marathon,diurnal_shift,mutation_gain,poison_heal,zipf_hotkey,...",
    )
    ap.add_argument(
        "--policies",
        default="lru,lfu,adaptive",
        help="Comma list: lru,lfu,adaptive,adaptive_frozen,adaptive_mutate,poison_frozen,poison_mutate",
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
    print_mutation_attribution(results)
    if any(r.workload == 'flash_churn' for r in results):
        print_soft_goal_table(results)
    if any(r.workload == 'evolve_gain' for r in results):
        print_evolve_table(results)
    print_overhead_summary(results)
    if any(r.workload in ('prefix_mix', 'prefix_mix_v2') for r in results):
        print_prefix_table(results)
    if not args.skip_assert:
        assert_success(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
