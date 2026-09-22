# Aura policy bodies (hot-strategy)

These modules export **choose-fn code strings** for `std/hot-strategy`.
They are not loaded into `server_ffi.aura` (FFI+closure quirk on this Aura rev).

The **policy agent** (`../policy_agent.aura`) seeds a workspace define
`choose-fn`, then `hot-strategy:register!` / `swap!` / `heal!` mutates it.
Chosen names (`lru` / `lfu` / `noop`) are applied to the C data plane via
RESP **`EVICT`** (not PLUGIN/.so).

| File | Body |
|------|------|
| `choose_normal.aura` | write-heavy→`lfu`; read-heavy+hit≥60%→`lru` |
| `choose_inverted.aura` | inverted (demo: prove mutation changed choice) |
| `choose_broken.aura` | invalid body used to smoke `hot-strategy:heal!` |

See `docs/aura-native-control.md`.
