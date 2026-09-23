#!/usr/bin/env bash
# Tier-2 hour-scale soak: eviction + TTL + typed keys + optional policy_agent
# mutate + kill/reconnect. Default 3600s; CI smoke via AURA_REDIS_SOAK_SEC or argv.
# Short P0.7 gate remains scripts/prod-soak.sh (ci-prod default ~30s).
#
# Usage:
#   ./scripts/soak-prod.sh                 # 3600s
#   ./scripts/soak-prod.sh 90
#   AURA_REDIS_SOAK_SEC=120 ./scripts/soak-prod.sh
#   AURA_REDIS_SOAK_WITH_AGENT=1 ./scripts/soak-prod.sh 300
#   AURA_REDIS_SOAK_KILL_EVERY=60 ./scripts/soak-prod.sh 600
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DUR="${1:-${AURA_REDIS_SOAK_SEC:-3600}}"
PORT="${AURA_REDIS_TEST_PORT:-26987}"
MAXMEM="${AURA_REDIS_SOAK_MAXMEMORY:-400000}"
WITH_AGENT="${AURA_REDIS_SOAK_WITH_AGENT:-0}"
KILL_EVERY="${AURA_REDIS_SOAK_KILL_EVERY:-0}"

./scripts/build-native.sh >/dev/null

fuser -k "${PORT}/tcp" 2>/dev/null || true
sleep 0.1
LOG="/tmp/soak-prod-${PORT}.log"
BIN="$ROOT/native/build/aura_redis_server"
DATADIR="/tmp/soak-prod-data-${PORT}"
rm -rf "$DATADIR"
mkdir -p "$DATADIR"

SPID=""
AGENT_PID=""

start_server() {
  fuser -k "${PORT}/tcp" 2>/dev/null || true
  sleep 0.05
  : >"$LOG"
  AURA_REDIS_DENY_PLUGIN=1 "$BIN" \
    --port "$PORT" --evict lru --maxmemory "$MAXMEM" \
    --dir "$DATADIR" --dbfilename "soak.aura-rdb" \
    >"$LOG" 2>&1 &
  SPID=$!
  for _ in $(seq 1 160); do
    if grep -q "listening on" "$LOG" 2>/dev/null; then
      return 0
    fi
    if ! kill -0 "$SPID" 2>/dev/null; then
      echo "soak-prod: server died during boot:" >&2
      cat "$LOG" >&2
      return 1
    fi
    sleep 0.05
  done
  echo "soak-prod: timeout waiting for listen" >&2
  cat "$LOG" >&2
  return 1
}

cleanup() {
  if [[ -n "${AGENT_PID}" ]]; then
    kill -TERM "$AGENT_PID" 2>/dev/null || true
    wait "$AGENT_PID" 2>/dev/null || true
  fi
  if [[ -n "${SPID}" ]]; then
    kill -TERM "$SPID" 2>/dev/null || true
    wait "$SPID" 2>/dev/null || true
  fi
  fuser -k "${PORT}/tcp" 2>/dev/null || true
  rm -rf "$DATADIR"
}
trap cleanup EXIT

start_server

if [[ "$WITH_AGENT" == "1" ]]; then
  AURA_BIN="$ROOT/.deps/aura/build/aura"
  AGENT="$ROOT/src/redis/policy_agent.aura"
  if [[ -x "$AURA_BIN" && -f "$AGENT" ]]; then
    # Prefer Soft/off until Tenant Admin unlocks Restricted (A11).
    # shellcheck disable=SC1091
    source "$ROOT/scripts/sandbox-policy-profile.sh" >/dev/null 2>&1 || true
    export AURA_REDIS_HOST=127.0.0.1 AURA_REDIS_PORT="$PORT"
    export AURA_REDIS_DENY_PLUGIN=1
    "$AURA_BIN" "$AGENT" >"/tmp/soak-prod-agent-${PORT}.log" 2>&1 &
    AGENT_PID=$!
    echo "soak-prod: policy_agent pid=$AGENT_PID"
  else
    echo "soak-prod: WARN agent requested but aura/policy_agent missing — continuing without"
  fi
fi

echo "soak-prod: duration=${DUR}s port=${PORT} maxmemory=${MAXMEM} agent=${WITH_AGENT} kill_every=${KILL_EVERY}"

python3 - "$PORT" "$DUR" "$MAXMEM" "$KILL_EVERY" "$SPID" <<'PY'
import os, signal, socket, sys, time

port = int(sys.argv[1])
dur = float(sys.argv[2])
maxmem = int(sys.argv[3])
kill_every = float(sys.argv[4])
server_pid = int(sys.argv[5])

def enc(*args):
    out = f"*{len(args)}\r\n".encode()
    for a in args:
        b = str(a).encode()
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return out

def read_one(sock, buf):
    while True:
        if not buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("eof")
            buf.extend(chunk)
            continue
        if buf.startswith(b"+") or buf.startswith(b"-") or buf.startswith(b":"):
            i = buf.find(b"\r\n")
            if i < 0:
                buf.extend(sock.recv(65536)); continue
            line = bytes(buf[: i + 2]); del buf[: i + 2]
            return line
        if buf.startswith(b"$"):
            i = buf.find(b"\r\n")
            if i < 0:
                buf.extend(sock.recv(65536)); continue
            n = int(buf[1:i]); start = i + 2
            if n < 0:
                del buf[:start]; return None
            if len(buf) < start + n + 2:
                buf.extend(sock.recv(65536)); continue
            payload = bytes(buf[start : start + n]); del buf[: start + n + 2]
            return payload
        if buf.startswith(b"*"):
            i = buf.find(b"\r\n")
            if i < 0:
                buf.extend(sock.recv(65536)); continue
            n = int(buf[1:i]); del buf[: i + 2]
            items = []
            for _ in range(max(n, 0)):
                items.append(read_one(sock, buf))
            return items
        # resync: discard one byte
        del buf[0:1]

def connect():
    last = None
    for _ in range(100):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.settimeout(15)
            return s, bytearray()
        except OSError as e:
            last = e
            time.sleep(0.1)
    raise RuntimeError(f"connect failed: {last}")

sock, buf = connect()
t0 = time.time()
ops = 0
i = 0
last_kill = time.time()
typed_cycle = 0

def info_map():
    global sock, buf
    sock.sendall(enc("INFO"))
    raw = read_one(sock, buf)
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    m = {}
    for line in str(raw).splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            m[k.strip()] = v.strip()
    return m

while time.time() - t0 < dur:
    if kill_every > 0 and (time.time() - last_kill) >= kill_every:
        try:
            sock.close()
        except Exception:
            pass
        # SIGKILL data plane; parent trap will not restart — we exit with note.
        # Soft reconnect stress: drop client only (server stays). For full kill,
        # set AURA_REDIS_SOAK_KILL_SERVER=1.
        if os.environ.get("AURA_REDIS_SOAK_KILL_SERVER") == "1":
            try:
                os.kill(server_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.5)
            print("soak-prod: server killed; reconnect stress requires external restart", file=sys.stderr)
            # Attempt reconnect (may fail if no supervisor)
            try:
                sock, buf = connect()
            except Exception as e:
                print(f"soak-prod: post-kill reconnect failed (expected without supervisor): {e}", file=sys.stderr)
                break
        else:
            time.sleep(0.05)
            sock, buf = connect()
        last_kill = time.time()

    pipe = b""
    val = "x" * 180
    extra_ex = 0
    for _ in range(16):
        k = f"sk{i}"
        pipe += enc("SET", k, val)
        pipe += enc("GET", f"sk{max(0, i - 40)}")
        if i % 7 == 0:
            pipe += enc("SET", f"ttl{i}", "t", "EX", "15")
            extra_ex += 1
        i += 1
    tc = typed_cycle % 50
    pipe += enc("HSET", f"h{tc}", "f", f"v{i}")
    pipe += enc("RPUSH", f"l{tc}", f"e{i}")
    pipe += enc("ZADD", f"z{tc}", str(i % 100), f"m{i % 20}")
    typed_cycle += 1
    do_save = typed_cycle % 200 == 0
    if do_save:
        pipe += enc("SAVE")

    nreply = 16 + 16 + extra_ex + 3 + (1 if do_save else 0)
    try:
        sock.sendall(pipe)
        for _ in range(nreply):
            read_one(sock, buf)
        ops += nreply
    except (ConnectionError, OSError, TimeoutError, ValueError):
        time.sleep(0.15)
        try:
            sock, buf = connect()
        except Exception:
            print("soak-prod: reconnect failed mid-run", file=sys.stderr)
            raise

m = info_map()
used = int(m.get("used_memory", "0"))
evicted = int(m.get("evicted", m.get("evicted_keys", "0")))
keys = m.get("keys", m.get("db0_keys", "?"))
print(
    f"soak-prod done: ops≈{ops} used_memory={used} maxmemory={maxmem} "
    f"evicted={evicted} keys={keys} dur={dur}s"
)
if used > maxmem + 65536:
    print(f"FAIL: used_memory {used} exceeds maxmemory+slack", file=sys.stderr)
    sys.exit(1)
if dur >= 20 and evicted < 1 and used + 8192 < maxmem:
    print(
        f"FAIL: expected eviction under soak (evicted={evicted} used={used})",
        file=sys.stderr,
    )
    sys.exit(1)
sock.close()
sys.exit(0)
PY

echo "soak-prod: OK"
