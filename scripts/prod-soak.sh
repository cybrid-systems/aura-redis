#!/usr/bin/env bash
# P0.7 — short production soak under maxmemory (pipeline load).
# Usage: ./scripts/prod-soak.sh [seconds]   default 45
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DUR="${1:-${AURA_REDIS_SOAK_SEC:-45}}"
PORT="${AURA_REDIS_TEST_PORT:-26907}"
MAXMEM="${AURA_REDIS_SOAK_MAXMEMORY:-200000}"

./scripts/build-native.sh >/dev/null
fuser -k "${PORT}/tcp" 2>/dev/null || true
sleep 0.05
LOG="/tmp/prod-soak-${PORT}.log"
BIN="$ROOT/native/build/aura_redis_server"
AURA_REDIS_DENY_PLUGIN=1 "$BIN" --port "$PORT" --evict lru --maxmemory "$MAXMEM" \
  >"$LOG" 2>&1 &
PID=$!
cleanup() {
  kill -TERM "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  fuser -k "${PORT}/tcp" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 80); do
  grep -q "listening on" "$LOG" 2>/dev/null && break
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "server died:" >&2
    cat "$LOG" >&2
    exit 1
  fi
  sleep 0.05
done
grep -q "listening on" "$LOG"

echo "prod-soak: ${DUR}s pipeline load on :${PORT} maxmemory=${MAXMEM}"
python3 - "$PORT" "$DUR" "$MAXMEM" <<'PY'
import socket, sys, time
# inline minimal RESP
port = int(sys.argv[1]); dur = float(sys.argv[2]); maxmem = int(sys.argv[3])

def enc(*args):
    out = f"*{len(args)}\r\n".encode()
    for a in args:
        b = str(a).encode()
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return out

def read_one(sock, buf):
    while True:
        if buf.startswith(b"+") or buf.startswith(b"-") or buf.startswith(b":"):
            i = buf.find(b"\r\n")
            if i < 0:
                buf.extend(sock.recv(65536)); continue
            line = bytes(buf[:i]); del buf[:i+2]
            return line
        if buf.startswith(b"$"):
            i = buf.find(b"\r\n")
            if i < 0:
                buf.extend(sock.recv(65536)); continue
            n = int(buf[1:i]); start = i+2
            if n < 0:
                del buf[:start]; return None
            if len(buf) < start + n + 2:
                buf.extend(sock.recv(65536)); continue
            payload = bytes(buf[start:start+n]); del buf[:start+n+2]
            return payload
        buf.extend(sock.recv(65536))

sock = socket.create_connection(("127.0.0.1", port), timeout=5)
buf = bytearray()
t0 = time.time()
ops = 0
i = 0
# Fill past maxmemory with unique keys, then churn
val = "x" * 200
while time.time() - t0 < dur:
    pipe = b""
    for _ in range(32):
        k = f"sk{i}"
        pipe += enc("SET", k, val)
        if i > 100:
            pipe += enc("GET", f"sk{i - 50}")
        else:
            pipe += enc("GET", k)
        i += 1
    sock.sendall(pipe)
    for _ in range(64):
        read_one(sock, buf)
    ops += 64
# INFO check
sock.sendall(enc("INFO"))
info = read_one(sock, buf)
if isinstance(info, bytes):
    info = info.decode()
m = {}
for line in info.splitlines():
    if ":" in line and not line.startswith("#"):
        k,v = line.split(":",1)
        m[k.strip()] = v.strip()
used = int(m.get("used_memory", "0"))
evicted = int(m.get("evicted", "0"))
print(f"prod-soak done: ops≈{ops} used_memory={used} maxmemory={maxmem} evicted={evicted} keys={m.get('keys')}")
if used > maxmem + 8192:
    print(f"FAIL: used_memory {used} exceeds maxmemory+slack", file=sys.stderr)
    sys.exit(1)
if evicted < 1 and used + 4096 < maxmem:
    print(f"FAIL: expected eviction under soak fill (evicted={evicted} used={used})", file=sys.stderr)
    sys.exit(1)
sock.close()
sys.exit(0)
PY

echo "prod-soak: OK"
