# Aura policy bodies (hot-strategy)

These modules export **choose-fn code strings** for `std/hot-strategy`.
They are not loaded into `server_ffi.aura` (FFI+closure quirk on this Aura rev).

The **policy agent** (`../policy_agent.aura`) seeds a workspace define
`choose-fn`, then `hot-strategy:register!` / `swap!` / `heal!` mutates it.
Chosen names are applied to the C data plane via RESP **`EVICT`** / **`LAYOUT`**
(and optional **`PIN`**) — not PLUGIN/.so.

## choose-fn contract (MVP M1+)

```text
(lambda (dgets dsets dhits dmisses [devicted nkeys]) …)
  → "" | "lfu" | "lru" | "noop" | "ttl_aware" | "slru" | "tinylfu"
  → "lfu|hot_cold" | "lru|flat"     # joint EVICT+LAYOUT
  → "lfu|flat|pin"              # pin on miss spike (flat: no migrate hurt)
  → "lfu|hot_cold|pin"              # optional pin hint
```

| File | Body |
|------|------|
| `choose_normal.aura` | write-heavy→`lfu\|hot_cold`; read-heavy+hit≥60%→`lru\|flat` |
| `choose_aggressive.aura` | lower min-ops; pin sooner on miss spikes |
| `choose_conservative.aura` | higher thresholds; layout often stays flat |
| `choose_inverted.aura` | inverted (demo: prove mutation changed choice) |
| `choose_broken.aura` | invalid body used to smoke `hot-strategy:heal!` |
| `choose_nosoft.aura` | M10 A/B: pre-soft normal (no eviction-rate budget) |

See `docs/aura-native-control.md` · `docs/mvp-plan.md`.

## Soft-goal (M10)

`choose_normal` / `choose_aggressive` / `choose_conservative` maximize hit%
subject to an eviction-CPU soft budget:

```text
erate = (devicted * 100) / ops
if erate ≥ 20 → "ttl_aware|flat|soft" or "lru|flat|soft"   # refuse lfu|+pin
```

See `docs/high-roi-iterations.md` (M10) and workload `flash_churn`.

## A12 — SLRU / approx TinyLFU

Named C kernels selectable via RESP `EVICT slru` or `EVICT tinylfu`.
`tinylfu` is an **alias** of sample-based SLRU (probationary vs protected via
`lfu_freq`; promote on GET). Not full paper W-TinyLFU (no Count-Min sketch /
window cache). Optional agent path: `AURA_REDIS_PREFER_SLRU=1`.

## A10 — Shadow A/B

`AURA_REDIS_SHADOW_AB=1` → dual dry-run vs `SHADOW_PROFILE` (default aggressive);
never EVICT-switches to loser. C `SHADOW sample-pct` samples GET hit/miss under
live champ; INFO `shadow_*` keys.

## A19 `choose_defensive`

Poison / unique-SET storm body: always emits `lfu|flat|soft` (LFU-oriented keep*
defense; refuse `|pin`). See `tests/test_poison_keys.py` + `docs/diff-vs-redis.md` E2.

