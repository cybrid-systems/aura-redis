#!/usr/bin/env python3
"""Fail when this repo drifts from the orchestra Aura baseline (#13)."""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "baseline" / "aura-baseline.json"
IMAGE_RE = re.compile(r"ghcr.io/cybrid-systems/aura-ci(?:[:@][A-Za-z0-9._:-]+)?")


def main() -> int:
    data = json.loads(BASELINE.read_text())
    aura, ci = data["aura"], data["aura_ci"]
    pinned = f"{ci['image']}@{ci['digest']}"
    issues: list[str] = []

    if not re.fullmatch(r"[0-9a-f]{40}", aura["commit_sha"]):
        issues.append("baseline aura.commit_sha is malformed")
    if not ci["digest"].startswith("sha256:"):
        issues.append("baseline aura_ci.digest is malformed")

    toml = ROOT / "aura.toml"
    if toml.is_file():
        deps = tomllib.loads(toml.read_text()).get("dependencies", {})
        if deps.get("aura") != "nightly":
            issues.append("aura.toml: dependencies.aura must be 'nightly'")
        if deps.get("aura_commit") != aura["commit_sha"]:
            issues.append(f"aura.toml: aura_commit drift (want {aura['commit_sha']})")
        if deps.get("aura_ci_digest") != ci["digest"]:
            issues.append(f"aura.toml: aura_ci_digest drift (want {ci['digest']})")
        for arch, asset in aura["assets"].items():
            key = "aura_sha256_" + arch.replace("-", "_")
            if deps.get(key) != asset["sha256"]:
                issues.append(f"aura.toml: {key} drift")

    ref = ROOT / "AURA_REF"
    if ref.is_file() and ref.read_text().strip() != aura["commit_sha"]:
        issues.append(f"AURA_REF drift (want {aura['commit_sha']})")

    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    if not workflows:
        issues.append(".github/workflows: no workflow found")
    seen = False
    for wf in workflows:
        text = wf.read_text()
        if pinned in text:
            seen = True
        for found in IMAGE_RE.findall(text):
            if found != pinned:
                issues.append(f"{wf.name}: floating/foreign aura-ci reference {found}")
        if "ghcr.io/cybrid-systems/dev:" in text:
            issues.append(f"{wf.name}: legacy dev image reference")
    if workflows and not seen:
        issues.append(f"workflows: no pinned image {pinned}")

    installer = ROOT / "scripts" / "install-aura.sh"
    if not ref.is_file() and not installer.is_file():
        issues.append("scripts/install-aura.sh: missing pinned installer")

    if issues:
        for i in issues:
            print(f"baseline drift: {i}", file=sys.stderr)
        return 1
    print(f"baseline ok: {ROOT} @ {aura['commit_sha'][:9]} {ci['digest'][:19]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
