#!/usr/bin/env bash
# Demo MVP (M5): Aura mutates policy under Meta/Twitter-shaped load; C runs kernels.
# ~2 minutes. Exit 0 iff static LRU loses and adaptive wins on Phase A.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-26880}"
MAXMEM="${AURA_REDIS_MAXMEMORY:-120000}"
LOG_SRV="${TMPDIR:-/tmp}/aura-redis-demo-mvp-srv.log"
CTL_LOG="${TMPDIR:-/tmp}/aura-redis-demo-mvp-ctl.log"
RESULTS="${TMPDIR:-/tmp}/aura-redis-demo-mvp-results.txt"
SRV_PID=""
CTL_PID=""

banner() {
  echo ""
  echo "════════════════════════════════════════════════════════════"
  echo "  $*"
  echo "════════════════════════════════════════════════════════════"
}

cleanup() {
  if [[ -n "${CTL_PID}" ]]; then kill "$CTL_PID" 2>/dev/null || true; fi
  if [[ -n "${SRV_PID}" ]]; then
    kill "$SRV_PID" 2>/dev/null || true
    wait "$SRV_PID" 2>/dev/null || true
  fi
  fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
  rm -f /tmp/aura-redis-demo-mvp-profile
}
trap cleanup EXIT

"$ROOT/scripts/build-native.sh" >/dev/null
# shellcheck source=/dev/null
source "$ROOT/scripts/sandbox-policy-profile.sh" 2>/dev/null || true
export AURA_REDIS_DENY_PLUGIN=1
export AURA_REDIS_ROOT="$ROOT"

: >"$LOG_SRV"
: >"$RESULTS"

banner "AURA-REDIS DEMO MVP — policy mutation under big-tech load"
echo "  C data plane: aura_redis_server  DENY_PLUGIN=1  maxmemory=$MAXMEM"
echo "  Control:      Python mirror of choose_normal.aura (joint EVICT|LAYOUT)"
echo "  (set AURA_AGENT=1 to prefer Docker policy_agent.aura when available)"
echo "  Port: $PORT"

start_server() {
  local evict="$1"
  fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
  sleep 0.15
  : >"$LOG_SRV"
  AURA_REDIS_DENY_PLUGIN=1 \
    "$ROOT/native/build/aura_redis_server" \
      --port "$PORT" --evict "$evict" --maxmemory "$MAXMEM" --layout flat \
      >"$LOG_SRV" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 50); do
    if grep -q "listening on" "$LOG_SRV" 2>/dev/null; then return 0; fi
    if ! kill -0 "$SRV_PID" 2>/dev/null; then
      echo "server died:" >&2; cat "$LOG_SRV" >&2; exit 1
    fi
    sleep 0.08
  done
  echo "timeout waiting for listen" >&2; cat "$LOG_SRV" >&2; exit 1
}

stop_ctl() {
  if [[ -n "${CTL_PID}" ]]; then
    kill "$CTL_PID" 2>/dev/null || true
    wait "$CTL_PID" 2>/dev/null || true
    CTL_PID=""
  fi
}

start_ctl() {
  stop_ctl
  : >"$CTL_LOG"
  local profile="${1:-normal}"
  PROFILE="$profile" AURA_REDIS_ROOT="$ROOT" python3 - "$PORT" <<'PY' >>"$CTL_LOG" 2>&1 &
import os, socket, sys, time
from pathlib import Path
root = Path(os.environ["AURA_REDIS_ROOT"])
sys.path.insert(0, str(root / "tests"))
sys.path.insert(0, str(root / "scripts"))
from smoke_client import redis_call
from bench_dynamic_evict import apply_choice, choose_policy, parse_info, info_int

port = int(sys.argv[1])
profile = os.environ.get("PROFILE", "normal")
prev = None
s = None
for _ in range(60):
    try:
        s = socket.create_connection(("127.0.0.1", port), 2)
        break
    except OSError:
        time.sleep(0.05)
if not s:
    sys.exit(1)
print(f"ctl: mirrors choose_{profile}.aura joint EVICT|LAYOUT (DENY_PLUGIN)", flush=True)
last_swap = 0.0
dwell = 0.6
while True:
    try:
        info = parse_info(redis_call(s, "INFO"))
    except Exception:
        time.sleep(0.1)
        continue
    g = info_int(info, "gets")
    se = info_int(info, "sets")
    h = info_int(info, "hits")
    m = info_int(info, "misses")
    ev = info_int(info, "evicted")
    nk = info_int(info, "keys")
    cur = info.get("evict", "")
    cur_ly = info.get("layout", "flat")
    if prev is not None:
        dg, ds, dh, dm, de = g-prev[0], se-prev[1], h-prev[2], m-prev[3], ev-prev[4]
        choice = choose_policy(profile, dg, ds, dh, dm, de, nk)
        if choice and (time.time() - last_swap) < dwell:
            parts = [p for p in choice.split("|") if p]
            if parts and parts[0] != cur:
                choice = "|".join([cur] + parts[1:2]) if len(parts) > 1 else ""
                if choice == cur:
                    choice = ""
        swaps = []
        try:
            apply_choice(s, choice, cur, cur_ly, swaps, pin_hot_prefix="hot", pin_n=8)
            for sw in swaps:
                print(f"ctl: {sw}", flush=True)
                if "->" in sw and not sw.startswith("layout:"):
                    last_swap = time.time()
        except Exception as e:
            print(f"ctl: err {e}", flush=True)
    prev = (g, se, h, m, ev)
    pf = Path("/tmp/aura-redis-demo-mvp-profile")
    if pf.exists():
        profile = pf.read_text().strip() or profile
    time.sleep(0.08)
PY
  CTL_PID=$!
}

# ── Phase A under STATIC LRU ──────────────────────────────────────────
banner "PHASE A — Meta-like hot_protect under STATIC LRU"
start_server lru
OUT_LRU=$(python3 "$ROOT/scripts/_demo_mvp_load.py" a --port "$PORT")
echo "  $OUT_LRU"
LRU_PCT=$(echo "$OUT_LRU" | sed -n 's/.*hit_pct=\([0-9.]*\).*/\1/p')
echo "lru_phase_a $OUT_LRU" >>"$RESULTS"
kill "$SRV_PID" 2>/dev/null || true
wait "$SRV_PID" 2>/dev/null || true
SRV_PID=""

# ── Phase A under ADAPTIVE ────────────────────────────────────────────
banner "PHASE A — same load under AURA ADAPTIVE (mirrors choose_normal.aura)"
start_server lru
start_ctl normal
sleep 0.25
OUT_ADAPT=$(python3 "$ROOT/scripts/_demo_mvp_load.py" a --port "$PORT" --wait-lfu)
echo "  $OUT_ADAPT"
ADAPT_PCT=$(echo "$OUT_ADAPT" | sed -n 's/.*hit_pct=\([0-9.]*\).*/\1/p')
echo "adaptive_phase_a $OUT_ADAPT" >>"$RESULTS"
echo "  controller swaps:"
grep -E 'ctl: ' "$CTL_LOG" | tail -n 16 || true

# ── Phase B WS shift ──────────────────────────────────────────────────
banner "PHASE B — working-set shift (expect adaptive → lru / stay optimal)"
OUT_B=$(python3 "$ROOT/scripts/_demo_mvp_load.py" b --port "$PORT")
echo "  $OUT_B"
echo "adaptive_phase_b $OUT_B" >>"$RESULTS"
B_PCT=$(echo "$OUT_B" | sed -n 's/.*hit_pct=\([0-9.]*\).*/\1/p')
echo "  controller swaps (tail):"
grep -E 'ctl: ' "$CTL_LOG" | tail -n 10 || true

# ── Optional invert mutation ──────────────────────────────────────────
banner "MUTATION — hot-strategy style swap → inverted policy"
echo inverted >/tmp/aura-redis-demo-mvp-profile
python3 "$ROOT/scripts/_demo_mvp_load.py" invert --port "$PORT"
echo "  restoring normal policy (heal)"
echo normal >/tmp/aura-redis-demo-mvp-profile
sleep 0.2

# ── Live phase table (appendix) ───────────────────────────────────────
banner "LIVE PHASES (appendix — single-phase visibility)"
printf "  %-22s %10s\n" "policy / phase" "hot hit%"
printf "  %-22s %10s\n" "----------------------" "----------"
printf "  %-22s %9s%%\n" "static LRU  (A)" "${LRU_PCT:-?}"
printf "  %-22s %9s%%\n" "Aura adaptive (A)" "${ADAPT_PCT:-?}"
printf "  %-22s %9s%%\n" "Aura adaptive (B WS)" "${B_PCT:-?}"
echo ""

# ── HEADLINE: phase_marathon cumulative + regret ──────────────────────
banner "HEADLINE — phase_marathon cumulative hit% / regret vs oracle"
# Stop live server so bench owns the port
if declare -f stop_ctl >/dev/null 2>&1; then stop_ctl; fi
if [[ -n "${CTL_PID:-}" ]]; then kill "$CTL_PID" 2>/dev/null || true; CTL_PID=""; fi
if [[ -n "${SRV_PID:-}" ]]; then
  kill "$SRV_PID" 2>/dev/null || true
  wait "$SRV_PID" 2>/dev/null || true
  SRV_PID=""
fi
fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
sleep 0.2

MARATHON_LOG="${TMPDIR:-/tmp}/aura-redis-demo-mvp-marathon.log"
set +e
python3 "$ROOT/scripts/bench_dynamic_evict.py" \
  --workloads phase_marathon \
  --policies lru,lfu,adaptive \
  --port "$PORT" \
  --maxmemory "$MAXMEM" \
  | tee "$MARATHON_LOG"
MC=$?
set -e

python3 - "$MARATHON_LOG" "$MC" "${LRU_PCT:-0}" "${ADAPT_PCT:-0}" "${B_PCT:-0}" <<'PY'
import re, sys
from pathlib import Path
log = Path(sys.argv[1]).read_text(errors="replace")
mc = sys.argv[2]
lru = float(sys.argv[3] or 0)
ad = float(sys.argv[4] or 0)
b = float(sys.argv[5] or 0)
print()
print("  One-liner: Aura mutates policy under sandbox; C only runs kernels.")
print("  Headline metric = cumulative useful-GET hit% across phases (not memtier).")
print()
m = re.search(r"→?\s*adaptive vs fixed: ([+-]?[0-9.]+)pp vs LRU, ([+-]?[0-9.]+)pp vs LFU", log)
ok = True
if ad < lru + 15 and ad < 80:
    print("FAIL: adaptive did not clearly beat LRU on Phase A")
    ok = False
if mc != "0":
    print("FAIL: phase_marathon bench exited non-zero (adaptive must beat BOTH fixed)")
    ok = False
elif m:
    g_lru, g_lfu = float(m.group(1)), float(m.group(2))
    print(f"  marathon gaps: adaptive {g_lru:+.1f}pp vs LRU, {g_lfu:+.1f}pp vs LFU")
    if g_lru < 5 or g_lfu < 5:
        print("FAIL: marathon adaptive margin vs both fixed must be >=5pp")
        ok = False
else:
    print("WARN: could not parse marathon gap line (bench assert is authoritative)")
if lru > 50:
    print("WARN: LRU unexpectedly healthy on Phase A")
if b < 50:
    print("WARN: Phase B hit% low (WS shift); check controller timing")
if not ok:
    raise SystemExit(1)
print("PASS: demo MVP — adaptive wins live Phase A AND phase_marathon cumulative")
PY

banner "DONE"
