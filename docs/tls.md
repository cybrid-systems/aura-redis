# TLS (P2.15)

**Native OpenSSL** accept on a dedicated `--tls-port` (optional build). Cleartext `--port` remains for local / `policy_agent`; clients that need encryption use the TLS port.

## Build

OpenSSL is **soft-linked** (optional):

```bash
./scripts/build-native.sh                 # TLS ON if libssl found
AURA_REDIS_TLS=OFF ./scripts/build-native.sh   # force cleartext-only
```

CMake: `-DAURA_REDIS_TLS=ON|OFF`. Without OpenSSL headers/libs the server still builds; TLS CLI flags then fail at startup with a clear message.

## Run

```bash
# self-signed example
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj /CN=localhost

./native/build/aura_redis_server \
  --port 6379 \
  --tls-port 6380 \
  --tls-cert-file cert.pem \
  --tls-key-file key.pem
# equivalent: --tls yes  (defaults tls-port to 6380)
```

| Knob | CLI | Env |
|------|-----|-----|
| Enable / port | `--tls yes`, `--tls-port N` | `AURA_REDIS_TLS`, `AURA_REDIS_TLS_PORT` |
| Cert | `--tls-cert-file` | `AURA_REDIS_TLS_CERT_FILE` |
| Key | `--tls-key-file` | `AURA_REDIS_TLS_KEY_FILE` |
| CA (optional mTLS) | `--tls-ca-file` | `AURA_REDIS_TLS_CA_FILE` |

When `--tls-ca-file` is set, the server requests and verifies client certificates.

## policy_agent

Keep **cleartext localhost** for Aura `policy_agent` (and `AURA_REDIS_DENY_PLUGIN=1` demos). Point external clients at `--tls-port`. Same keyspace; INFO flat keys include `tls_port` / `tls_enabled`.

## Stunnel fallback

If a deployment cannot use the native build, see [`scripts/tls-stunnel-example.conf`](../scripts/tls-stunnel-example.conf) — front cleartext `--port` with stunnel. Native TLS is preferred.

## Test

```bash
python3 tests/test_prod_tls.py
```
