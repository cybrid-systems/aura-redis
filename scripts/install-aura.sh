#!/usr/bin/env bash
# Install the Aura nightly pinned by baseline/aura-baseline.json.
# The archive sha256 is verified, so CI uses the exact same Aura as the
# orchestra baseline without compiling Aura from source.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
baseline="$root/baseline/aura-baseline.json"
[[ -f "$baseline" ]] || { echo "missing $baseline" >&2; exit 1; }

dest="$root/.deps/aura"
if [[ -x "$dest/build/aura" && -x "$dest/bin/aura" ]]; then
  printf '%s\n' "$dest"
  exit 0
fi

arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) arch="x86_64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) echo "unsupported architecture: $arch" >&2; exit 1 ;;
esac

key="linux-${arch}"
name="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["aura"]["assets"][sys.argv[2]]["name"])' "$baseline" "$key")"
want="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["aura"]["assets"][sys.argv[2]]["sha256"])' "$baseline" "$key")"
commit="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["aura"]["commit_sha"])' "$baseline")"
AURA_VERSION="${AURA_VERSION:-nightly}"
base="${AURA_DOWNLOAD_BASE:-https://github.com/cybrid-systems/aura/releases/download/${AURA_VERSION}}"

if [[ ! -x "$dest/build/aura" ]]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  echo "install-aura: nightly ${commit:0:9} (${arch}) -> .deps/aura" >&2
  curl -fsSL --retry 3 --retry-delay 1 -o "$tmp/$name" "$base/$name"
  printf '%s  %s\n' "$want" "$tmp/$name" | sha256sum -c - >&2
  rm -rf "$dest"
  mkdir -p "$dest"
  tar -xzf "$tmp/$name" -C "$dest" --strip-components=1
  mkdir -p "$dest/build"
  ln -sfn ../bin/aura "$dest/build/aura"
fi

[[ -x "$dest/build/aura" ]] || { echo "install-aura: no build/aura" >&2; exit 1; }
printf '%s\n' "$dest"
"$dest/bin/aura" --version >&2
