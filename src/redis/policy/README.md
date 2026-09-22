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
  → "" | "lfu" | "lru" | "noop"
  → "lfu|hot_cold" | "lru|flat"     # joint EVICT+LAYOUT
  → "lfu|hot_cold|pin"              # optional pin hint
```

| File | Body |
|------|------|
| `choose_normal.aura` | write-heavy→`lfu\|hot_cold`; read-heavy+hit≥60%→`lru\|flat` |
| `choose_aggressive.aura` | lower min-ops; pin sooner on miss spikes |
| `choose_conservative.aura` | higher thresholds; layout often stays flat |
| `choose_inverted.aura` | inverted (demo: prove mutation changed choice) |
| `choose_broken.aura` | invalid body used to smoke `hot-strategy:heal!` |

See `docs/aura-native-control.md` · `docs/mvp-plan.md`.
