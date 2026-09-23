#!/usr/bin/env bash
# Healthcheck for aura-redis: PING (+ optional AUTH) + INFO basics.
# Exit 0 on success, nonzero on fail. Pure Python RESP — no redis-cli.
#
# Usage:
#   scripts/healthcheck.sh [-h host] [-p port] [-a password]
# Env: AURA_REDIS_HOST AURA_REDIS_PORT AURA_REDIS_REQUIREPASS / AURA_REDIS_PASSWORD
#
# Gates:
#   - TCP connect + PING → PONG (always)
#   - If password set: AUTH must return OK, then INFO must include used_memory
#   - If no password: INFO if allowed; NOAUTH on INFO is OK (PING still gates)
set -euo pipefail

HOST="${AURA_REDIS_HOST:-127.0.0.1}"
PORT="${AURA_REDIS_PORT:-6379}"
PASS="${AURA_REDIS_PASSWORD:-${AURA_REDIS_REQUIREPASS:-}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--host) HOST="$2"; shift 2 ;;
    -p|--port) PORT="$2"; shift 2 ;;
    -a|--auth|--password) PASS="$2"; shift 2 ;;
    -*)
      echo "usage: $0 [-h host] [-p port] [-a password]" >&2
      exit 2
      ;;
    *)
      echo "usage: $0 [-h host] [-p port] [-a password]" >&2
      exit 2
      ;;
  esac
done

export HC_HOST="$HOST" HC_PORT="$PORT" HC_PASS="$PASS"
exec python3 - <<'PY'
import os, socket, sys

host = os.environ["HC_HOST"]
port = int(os.environ["HC_PORT"])
password = os.environ.get("HC_PASS") or ""

def encode(*args: str) -> bytes:
    out = f"*{len(args)}\r\n".encode()
    for a in args:
        b = a.encode()
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return out

def read_line(sock: socket.socket) -> bytes:
    buf = bytearray()
    while True:
        ch = sock.recv(1)
        if not ch:
            raise RuntimeError("connection closed")
        buf += ch
        if len(buf) >= 2 and buf[-2:] == b"\r\n":
            return bytes(buf[:-2])

def read_reply(sock: socket.socket):
    line = read_line(sock)
    if not line:
        raise RuntimeError("empty reply")
    t = line[:1]
    if t in (b"+", b"-", b":"):
        payload = line[1:].decode()
        if t == b"-":
            raise RuntimeError(payload)
        if t == b":":
            return int(payload)
        return payload
    if t == b"$":
        n = int(line[1:])
        if n < 0:
            return None
        data = b""
        while len(data) < n + 2:
            chunk = sock.recv(n + 2 - len(data))
            if not chunk:
                raise RuntimeError("bulk truncated")
            data += chunk
        return data[:n].decode(errors="replace")
    if t == b"*":
        n = int(line[1:])
        if n < 0:
            return None
        return [read_reply(sock) for _ in range(n)]
    raise RuntimeError(f"bad RESP type {t!r}")

def call(sock, *args):
    sock.sendall(encode(*args))
    return read_reply(sock)

try:
    with socket.create_connection((host, port), timeout=3.0) as sock:
        sock.settimeout(3.0)
        if password:
            auth = call(sock, "AUTH", password)
            if auth != "OK":
                print(f"healthcheck: AUTH unexpected: {auth!r}", file=sys.stderr)
                sys.exit(1)
        pong = call(sock, "PING")
        if pong != "PONG":
            print(f"healthcheck: PING unexpected: {pong!r}", file=sys.stderr)
            sys.exit(1)
        try:
            info = call(sock, "INFO")
        except RuntimeError as e:
            msg = str(e)
            if (not password) and "NOAUTH" in msg:
                print("healthcheck: OK PING (INFO skipped: NOAUTH, no password)")
                sys.exit(0)
            raise
        if not isinstance(info, str) or "used_memory" not in info:
            print("healthcheck: INFO missing used_memory", file=sys.stderr)
            sys.exit(1)
        # Soft presence checks (do not fail on missing optional keys)
        _ = ("role" in info, "evicted" in info.lower() or "evicted_keys" in info)
        print("healthcheck: OK PING + INFO")
        sys.exit(0)
except Exception as e:
    print(f"healthcheck: FAIL {e}", file=sys.stderr)
    sys.exit(1)
PY
