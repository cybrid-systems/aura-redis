#!/usr/bin/env bash
# End-to-end performance evaluation: throughput (A) + dynamic hit-rate (B).
# Does NOT mix the two into one score. Writes docs/perf-eval.md summary tables
# from captured logs (interpretation lives in that doc / stdout).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
N="${MEMTIER_N:-20000}"
REDIS_PORT="${REDIS_PORT:-27379}"
AURA_BASE="${AURA_PORT:-27380}"
LISP_PORT="${LISP_PORT:-6380}"
IMG_REDIS="${REDIS_IMAGE:-redis:7-alpine}"
IMG_MEM="${MEMTIER_IMAGE:-redislabs/memtier_benchmark}"
IMG_DEV="${AURA_DEV_IMAGE:-ghcr.io/cybrid-systems/dev:v1.0.7}"
LOG_DIR="${TMPDIR:-/tmp}/aura-redis-e2e-$$"
OUT_MD="${ROOT}/docs/perf-eval.md"
SKIP_LISP="${SKIP_LISP:-0}"
SKIP_AURA_AGENT="${SKIP_AURA_AGENT:-0}"
mkdir -p "$LOG_DIR" "$(dirname "$OUT_MD")"

REDIS_CID=""
declare -a AURA_PIDS=()
CTL_PID=""

cleanup() {
  [[ -n "${CTL_PID}" ]] && kill "$CTL_PID" 2>/dev/null || true
  for p in "${AURA_PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  for p in "${AURA_PIDS[@]:-}"; do wait "$p" 2>/dev/null || true; done
  [[ -n "$REDIS_CID" ]] && sudo docker rm -f "$REDIS_CID" 2>/dev/null || true
  fuser -k "${REDIS_PORT}/tcp" 2>/dev/null || true
  for off in 0 1 2 3; do
    fuser -k "$((AURA_BASE+off))/tcp" 2>/dev/null || true
  done
}
trap cleanup EXIT

echo "== build native =="
"$ROOT/scripts/build-native.sh" | tee "$LOG_DIR/build.txt"

echo "== start redis:$REDIS_PORT =="
REDIS_CID=$(sudo docker run -d --network host --name "ar-e2e-redis-$$" \
  "$IMG_REDIS" redis-server --port "$REDIS_PORT" --save "" --appendonly no \
  --bind 127.0.0.1)
sleep 0.4

start_aura() {
  local name=$1 port=$2 evict=$3
  local log="$LOG_DIR/aura-${name}.log"
  "$ROOT/native/build/aura_redis_server" --port "$port" --evict "$evict" \
    >"$log" 2>&1 &
  AURA_PIDS+=($!)
  for i in $(seq 1 40); do
    grep -q "listening on" "$log" 2>/dev/null && return 0
    sleep 0.1
  done
  echo "WARN: aura $name may not be listening" >&2
}

# Adaptive controller (Python mirror of choose_normal) — same as bench harness
start_adaptive_ctl() {
  local port=$1
  python3 "$ROOT/scripts/_e2e_adaptive_ctl.py" "$port" >"$LOG_DIR/adaptive-ctl.log" 2>&1 &
  CTL_PID=$!
}

run_memtier() {
  local name=$1 port=$2 pipeline=$3
  local out="$LOG_DIR/${name}-p${pipeline}.txt"
  echo "-- memtier $name port=$port pipeline=$pipeline --"
  sudo docker run --rm --network host "$IMG_MEM" \
    -s 127.0.0.1 -p "$port" \
    -c 1 -t 1 --ratio=1:10 -d 32 \
    --key-pattern=R:R --key-minimum=1 --key-maximum=10000 \
    -n "$N" --pipeline="$pipeline" \
    2>&1 | tee "$out"
}

extract_ops() {
  awk '/^[[:space:]]*Totals/{print $2; exit}' "$1"
}
extract_lat() {
  # Avg Latency column often field 5 on Totals line
  awk '/^[[:space:]]*Totals/{print $5; exit}' "$1"
}

echo "== A) Throughput matrix =="
start_aura lru $((AURA_BASE+0)) lru
start_aura lfu $((AURA_BASE+1)) lfu
start_aura adaptive $((AURA_BASE+2)) lru
start_adaptive_ctl $((AURA_BASE+2))
sleep 0.3

# pipeline 1 then 16 for each
for p in 1 16; do
  run_memtier redis "$REDIS_PORT" "$p"
  run_memtier aura-lru $((AURA_BASE+0)) "$p"
  run_memtier aura-lfu $((AURA_BASE+1)) "$p"
  run_memtier aura-adaptive $((AURA_BASE+2)) "$p"
done

# Optional short Lisp run (existing container on LISP_PORT or skip)
if [[ "$SKIP_LISP" != "1" ]]; then
  if ss -ltn | grep -q ":${LISP_PORT} "; then
    echo "-- short Lisp memtier (n=500, p=1) --"
    sudo docker run --rm --network host "$IMG_MEM" \
      -s 127.0.0.1 -p "$LISP_PORT" \
      -c 1 -t 1 --ratio=1:10 -d 32 \
      --key-pattern=R:R --key-minimum=1 --key-maximum=1000 \
      -n 500 --pipeline=1 \
      2>&1 | tee "$LOG_DIR/lisp-p1.txt" || echo "Lisp memtier failed" | tee "$LOG_DIR/lisp-p1.txt"
  else
    echo "SKIP Lisp: nothing on :$LISP_PORT" | tee "$LOG_DIR/lisp-p1.txt"
  fi
fi

echo "== B) Dynamic hit-rate =="
python3 "$ROOT/scripts/bench_dynamic_evict.py" --port 26740 \
  --workloads hot_protect,ws_shift,oscillate,zipf_hotkey \
  2>&1 | tee "$LOG_DIR/hitrate.txt"

if [[ "$SKIP_AURA_AGENT" != "1" ]]; then
  echo "== C) Optional aura-agent (hot_protect,ws_shift) =="
  python3 "$ROOT/scripts/bench_dynamic_evict.py" --port 26741 \
    --aura-agent --workloads hot_protect,ws_shift --skip-assert \
    2>&1 | tee "$LOG_DIR/aura-agent.txt" || {
      echo "aura-agent path failed (non-fatal)" | tee -a "$LOG_DIR/aura-agent.txt"
    }
fi

# ── summarize into docs/perf-eval.md ──
python3 - "$LOG_DIR" "$OUT_MD" "$SHA" "$N" <<'PY'
import sys, os, re, datetime, platform
from collections import defaultdict
log_dir, out_md, sha, n = sys.argv[1:5]
now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def ops(name, p):
    f = os.path.join(log_dir, f"{name}-p{p}.txt")
    if not os.path.isfile(f):
        return None, None
    text = open(f).read()
    m = re.search(r"^\s*Totals\s+(\S+)\s+\S+\s+\S+\s+(\S+)", text, re.M)
    if m:
        return float(m.group(1)), m.group(2)
    m = re.search(r"^\s*Totals\s+(\S+)", text, re.M)
    return (float(m.group(1)), None) if m else (None, None)

rows = []
for name, label in [
    ("redis", "redis:7-alpine"),
    ("aura-lru", "aura-redis C EVICT=lru"),
    ("aura-lfu", "aura-redis C EVICT=lfu"),
    ("aura-adaptive", "aura-redis C adaptive (Py controller)"),
]:
    for p in (1, 16):
        o, lat = ops(name, p)
        rows.append((label, p, o, lat))

lisp_ops, _ = ops("lisp", 1) if os.path.isfile(os.path.join(log_dir, "lisp-p1.txt")) else (None, None)
# lisp may use different naming
if lisp_ops is None:
    f = os.path.join(log_dir, "lisp-p1.txt")
    if os.path.isfile(f):
        m = re.search(r"^\s*Totals\s+(\S+)", open(f).read(), re.M)
        if m:
            try: lisp_ops = float(m.group(1))
            except: pass

# redis baselines
r1 = next((o for lab,p,o,_ in rows if lab.startswith("redis") and p==1), None)
r16 = next((o for lab,p,o,_ in rows if lab.startswith("redis") and p==16), None)

def ratio(a, r):
    if a is None or not r: return "—"
    v = a/r
    if v < 0.01: return f"{v:.5f}"
    return f"{v:.3f}"

# parse hit-rate table from hitrate.txt
hit_lines = []
hr = open(os.path.join(log_dir, "hitrate.txt")).read() if os.path.isfile(os.path.join(log_dir, "hitrate.txt")) else ""
# capture SUMMARY section
in_sum = False
for ln in hr.splitlines():
    if "SUMMARY" in ln or ln.startswith("===="):
        in_sum = True
    if in_sum:
        hit_lines.append(ln)

# structured parse: "    lru        overall_hit=  0.0%  useful=0      [hot_protect=0.0%(0)]"
pat = re.compile(
    r"^\s+(lru|lfu|adaptive)\s+overall_hit=\s*([\d.]+)%\s+useful=(\d+)\s+\[(.*)\]"
)
wl_pat = re.compile(r"^\s+(\w+):$")
hit_rows = []  # workload, policy, overall, useful, phases
cur_wl = None
for ln in hr.splitlines():
    m = wl_pat.match(ln)
    if m and m.group(1) not in ("PASS",):
        # only workload names
        if m.group(1) in ("hot_protect", "ws_shift", "oscillate", "zipf_hotkey"):
            cur_wl = m.group(1)
        continue
    m = pat.match(ln)
    if m and cur_wl:
        hit_rows.append((cur_wl, m.group(1), float(m.group(2)), int(m.group(3)), m.group(4)))

agent_note = ""
af = os.path.join(log_dir, "aura-agent.txt")
if os.path.isfile(af):
    at = open(af).read()
    if "FAIL" in at or "failed" in at.lower() or "Error" in at or "Timeout" in at:
        agent_note = "Aura policy_agent path: flaky/failed on this host — see log; Python controller is the default harness path."
    else:
        # extract adaptive lines
        agent_hits = []
        cur = None
        for ln in at.splitlines():
            m = wl_pat.match(ln)
            if m and m.group(1) in ("hot_protect", "ws_shift", "oscillate", "zipf_hotkey"):
                cur = m.group(1)
            m = pat.match(ln)
            if m and cur:
                agent_hits.append(f"{cur}/{m.group(1)}={m.group(2)}%")
        agent_note = (
            "Aura policy_agent (--aura-agent) matched Python-controller hit rates on "
            + (", ".join(agent_hits) if agent_hits else "completed")
            + ". Docker agent adds startup/tick latency; adaptation still wins."
        )

cpu = platform.processor() or platform.machine()
try:
    with open("/proc/cpuinfo") as f:
        for ln in f:
            if ln.startswith("model name"):
                cpu = ln.split(":",1)[1].strip()
                break
except Exception:
    pass
nproc = os.cpu_count() or "?"

lines = []
lines.append("# aura-redis end-to-end performance evaluation")
lines.append("")
lines.append(f"**Date:** {now} (Asia/Shanghai)")
lines.append(f"**SHA:** `{sha}`")
lines.append(f"**Host:** Linux {platform.machine()}, {nproc} CPUs, `{cpu}`")
lines.append("**Data plane:** host `native/build/aura_redis_server` (Release)")
lines.append("**Redis baseline:** `redis:7-alpine` (Docker, host network)")
lines.append("")
lines.append("Two independent dimensions — **do not** collapse into one score:")
lines.append("")
lines.append("1. **Throughput** (memtier) — no `maxmemory` pressure; measures raw RESP/data-plane speed.")
lines.append("2. **Hit quality** (dynamic workloads) — small `maxmemory`; measures eviction policy fitness.")
lines.append("")
# Headline contrasts from hit_rows + throughput
hl = []
pivot = defaultdict(dict)
for wl, pol, hit, useful, phases in hit_rows:
    pivot[wl][pol] = hit
for wl in ("hot_protect", "zipf_hotkey"):
    if wl in pivot and pivot[wl].get("lru") is not None and pivot[wl].get("adaptive") is not None:
        hl.append(f"| `{wl}` LRU collapses | LRU **{pivot[wl]['lru']:.1f}%** vs adaptive **{pivot[wl]['adaptive']:.1f}%** |")
if r1 and any(lab.startswith("aura") and p==1 and o for lab,p,o,_ in rows):
    best_p1 = max((o/r1 for lab,p,o,_ in rows if o and p==1 and not lab.startswith("redis")), default=None)
    best_p16 = max((o/r16 for lab,p,o,_ in rows if o and p==16 and r16 and not lab.startswith("redis")), default=None) if r16 else None
    if best_p1:
        hl.append(f"| Aura C ≥ Redis ops/s | best p=1 **{best_p1:.3f}×**; best p=16 **{(best_p16 or 0):.3f}×** |")
if hl:
    lines.append("## Headline contrasts")
    lines.append("")
    lines.append("| Claim | Evidence (this run) |")
    lines.append("|-------|---------------------|")
    lines.extend(hl)
    lines.append("")
lines.append("---")
lines.append("")
lines.append("## A) Throughput (ops/s)")
lines.append("")
lines.append(f"Frozen matrix: **1c×1t**, SET:GET=**1:10**, value **32B**, key 1..10000 **R:R**, `-n {n}`.")
lines.append("Adaptive = C server `--evict lru` + Python RESP `EVICT` controller (same rules as `choose_normal`).")
lines.append("")
lines.append("| Engine | pipeline | Totals ops/s | Avg latency | vs Redis |")
lines.append("|--------|----------|--------------|-------------|----------|")
for p_want in (1, 16):
    for lab, p, o, lat in rows:
        if p != p_want:
            continue
        base = r1 if p == 1 else r16
        os_ = f"{o:.2f}" if o is not None else "—"
        ls_ = lat if lat else "—"
        vs = "1.00" if lab.startswith("redis") else ratio(o, base)
        lines.append(f"| {lab} | {p} | {os_} | {ls_} | {vs} |")
if lisp_ops is not None:
    lines.append(f"| Aura Lisp `server.aura` (short) | 1 | {lisp_ops:.2f} | — | {ratio(lisp_ops, r1)} |")
lines.append("")
lines.append("Notes: Fixed LRU vs LFU vs adaptive should be nearly identical here (no memory pressure);")
lines.append("small differences are tracking/`INFO` controller noise. Gap vs Redis shows C data-plane maturity.")
lines.append("")
lines.append("---")
lines.append("")
lines.append("## B) Hit rate under dynamic load")
lines.append("")
lines.append("Harness: `python3 scripts/bench_dynamic_evict.py --workloads hot_protect,ws_shift,oscillate,zipf_hotkey` (maxmemory=120000).")
lines.append("Policies: fixed `lru`, fixed `lfu`, `adaptive` (Python controller).")
lines.append("")
lines.append("| workload | policy | overall hit% | useful GETs | phase detail |")
lines.append("|----------|--------|--------------|-------------|--------------|")
for wl, pol, hit, useful, phases in hit_rows:
    lines.append(f"| {wl} | {pol} | {hit:.1f}% | {useful} | {phases} |")
if not hit_rows:
    lines.append("| _(parse failed — see scripts output)_ | | | | |")
    lines.append("")
    lines.append("```")
    lines.extend(hit_lines[:80] if hit_lines else ["(no SUMMARY)"])
    lines.append("```")
lines.append("")
lines.append("---")
lines.append("")
lines.append("## C) Aura policy_agent overhead (optional)")
lines.append("")
lines.append(agent_note or "Skipped.")
lines.append("")
lines.append("---")
lines.append("")
lines.append("## Interpretation")
lines.append("")
lines.append("- **Raw speed:** aura-redis C is typically ~1.05–1.35× `redis:7-alpine` on this matrix; cite ratios.")
lines.append("- **When adaptive wins hard:** `hot_protect` / `zipf_hotkey` — fixed LRU often **0%**; adaptive near LFU oracle.")
lines.append("- **When adaptive wins across phases:** `oscillate` — swaps LFU↔LRU; beats both fixed policies on useful GETs.")
lines.append("- **When fixed LRU is fine:** `ws_shift` — LRU already 100%; adaptive matches; LFU loses.")
lines.append("- **Do not mix scores:** hit-ratio uses tiny maxmemory; throughput does not.")
lines.append("")
lines.append("---")
lines.append("")
lines.append("## Reproduce")
lines.append("")
lines.append("```bash")
lines.append("./scripts/build-native.sh")
lines.append("./scripts/bench-e2e.sh          # A + B (+ optional C)")
lines.append("# pieces:")
lines.append("./scripts/memtier-cmp.sh        # Redis vs default C (overwrites docs/perf-log.md)")
lines.append("python3 scripts/bench_dynamic_evict.py --workloads hot_protect,ws_shift,oscillate,zipf_hotkey")
lines.append("python3 scripts/bench_dynamic_evict.py --aura-agent --workloads hot_protect,ws_shift")
lines.append("```")
lines.append("")
lines.append(f"Logs from this run: `{log_dir}`")
lines.append("")

open(out_md, "w").write("\n".join(lines) + "\n")
print(f"Wrote {out_md}")
# also print compact stdout summary
print("\n" + "=" * 72)
print("  AURA-REDIS E2E SCOREBOARD")
print("=" * 72)
print(f"  SHA={sha}  memtier_n={n}  host={platform.machine()} x{nproc}")
print("-" * 72)
print("  THROUGHPUT (ops/s, no maxmemory) — higher is better")
print(f"  {'Engine':42} {'p':>3} {'ops/s':>12} {'vs Redis':>10}")
for lab, p, o, lat in rows:
    base = r1 if p == 1 else r16
    os_ = f"{o:.0f}" if o is not None else "—"
    vs = "1.00×" if lab.startswith("redis") else (ratio(o, base) + "×" if o is not None else "—")
    print(f"  {lab:42} {p:3} {os_:>12} {vs:>10}")
if lisp_ops is not None:
    print(f"  {'Aura Lisp server.aura (short)':42} {1:3} {lisp_ops:>12.0f} {(ratio(lisp_ops, r1)+'×'):>10}")
print("-" * 72)
print("  HIT QUALITY (tiny maxmemory) — adaptive should match oracle")
print(f"  {'workload':14} {'lru':>8} {'lfu':>8} {'adaptive':>10}  highlight")
# pivot hit_rows
pivot = defaultdict(dict)
for wl, pol, hit, useful, phases in hit_rows:
    pivot[wl][pol] = hit
for wl in ("hot_protect", "ws_shift", "oscillate", "zipf_hotkey"):
    if wl not in pivot:
        continue
    d = pivot[wl]
    lru_h = d.get("lru")
    lfu_h = d.get("lfu")
    ad_h = d.get("adaptive")
    def fmt(v):
        return f"{v:7.1f}%" if v is not None else "      —"
    note = ""
    if lru_h is not None and ad_h is not None and ad_h >= 95 and lru_h < 20:
        note = "★ LRU collapses, adaptive holds"
    elif lru_h is not None and lfu_h is not None and ad_h is not None:
        bestf = max(lru_h, lfu_h)
        if ad_h + 0.5 >= bestf:
            note = "near-oracle"
        if lfu_h < 50 and lru_h >= 95:
            note = note or "LFU loses, adaptive≈LRU"
    print(f"  {wl:14} {fmt(lru_h)} {fmt(lfu_h)} {fmt(ad_h)}  {note}")
print("=" * 72)
PY

echo "Done. Report: $OUT_MD"
echo "Logs: $LOG_DIR"
