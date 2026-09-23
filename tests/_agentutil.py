"""Start Aura policy_agent for tests.

Prefer a native subprocess when the docker CLI is missing (GitHub Actions
`container:` jobs mount the socket but the job image often has no `docker`
binary). Fall back to `sudo docker run` for local laptop workflows.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
AURA_BIN = ROOT / ".deps/aura/build/aura"
AGENT_AURA = ROOT / "src/redis/policy_agent.aura"
IMG = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")

_NATIVE: dict[str, subprocess.Popen] = {}
_NATIVE_LOG: dict[str, Path] = {}


def prefer_native() -> bool:
    mode = os.environ.get("AURA_REDIS_AGENT_MODE", "").strip().lower()
    if mode in ("native", "local"):
        return True
    if mode in ("docker", "container"):
        return False
    if os.environ.get("AURA_REDIS_AGENT_NATIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True
    if shutil.which("docker") is None:
        return True
    # Actions job containers: aura is already built; nested docker CLI is absent.
    if os.environ.get("GITHUB_ACTIONS") == "true" and AURA_BIN.is_file():
        return True
    return False


def docker_path(host: Path) -> str:
    """Map a host path under ROOT to /work/... for docker mounts."""
    host = host.resolve()
    root = ROOT.resolve()
    try:
        rel = host.relative_to(root)
        return f"/work/{rel.as_posix()}"
    except ValueError:
        return str(host)


def start_agent(
    *,
    env: Mapping[str, str],
    log_path: Path,
    path_env: Mapping[str, Path] | None = None,
) -> str:
    """Start policy_agent. Returns docker cid or ``native:<pid>`` handle."""
    path_env = dict(path_env or {})
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")

    merged = {k: str(v) for k, v in env.items()}

    if prefer_native():
        if not AURA_BIN.is_file():
            raise FileNotFoundError(
                f"native agent needs {AURA_BIN}; build Aura or install docker CLI"
            )
        full = os.environ.copy()
        full.update(
            {
                "AURA_SANDBOX": "off",
                "AURA_PIPELINE_STRICT": "0",
                "AURA_PATH": str(ROOT / ".deps/aura/lib"),
            }
        )
        full.update(merged)
        for key, host in path_env.items():
            full[key] = str(Path(host).resolve())
        for k, v in list(full.items()):
            if isinstance(v, str) and v.startswith("/work/"):
                full[k] = str(ROOT / v[len("/work/") :])
        proc = subprocess.Popen(
            [str(AURA_BIN), str(AGENT_AURA)],
            stdout=log_path.open("w"),
            stderr=subprocess.STDOUT,
            env=full,
            cwd=str(ROOT),
        )
        handle = f"native:{proc.pid}"
        _NATIVE[handle] = proc
        _NATIVE_LOG[handle] = log_path
        return handle

    cmd = [
        "sudo",
        "docker",
        "run",
        "-d",
        "--network",
        "host",
        "--entrypoint",
        "",
        "-v",
        f"{ROOT}:/work",
        "-v",
        "/tmp:/tmp",
        "-w",
        "/work",
        "-e",
        "AURA_SANDBOX=off",
        "-e",
        "AURA_PIPELINE_STRICT=0",
        "-e",
        "AURA_PATH=/work/.deps/aura/lib",
    ]
    for k, v in merged.items():
        cmd.extend(["-e", f"{k}={v}"])
    for key, host in path_env.items():
        cmd.extend(["-e", f"{key}={docker_path(Path(host))}"])
    cmd.extend([IMG, "/work/.deps/aura/build/aura", "/work/src/redis/policy_agent.aura"])
    return subprocess.check_output(cmd, text=True).strip()


def agent_logs(handle: str, log_path: Path | None = None) -> str:
    if handle.startswith("native:"):
        path = _NATIVE_LOG.get(handle) or log_path
        if path is None:
            return ""
        return Path(path).read_text(errors="replace")
    out = subprocess.check_output(
        ["sudo", "docker", "logs", handle], text=True, stderr=subprocess.STDOUT
    )
    if log_path is not None:
        Path(log_path).write_text(out)
    return out


def agent_running(handle: str) -> bool:
    if handle.startswith("native:"):
        proc = _NATIVE.get(handle)
        return proc is not None and proc.poll() is None
    try:
        out = subprocess.check_output(
            ["sudo", "docker", "inspect", "-f", "{{.State.Running}}", handle],
            text=True,
        ).strip()
        return out == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def stop_agent(handle: str) -> None:
    if handle.startswith("native:"):
        proc = _NATIVE.pop(handle, None)
        _NATIVE_LOG.pop(handle, None)
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        return
    subprocess.run(["sudo", "docker", "kill", handle], capture_output=True)
    subprocess.run(["sudo", "docker", "rm", "-f", handle], capture_output=True)


def wait_log(
    handle: str,
    needles: list[str],
    *,
    timeout: float = 20.0,
    log_path: Path | None = None,
    poll: float = 0.15,
) -> str:
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        last = agent_logs(handle, log_path)
        if all(n in last for n in needles):
            return last
        if not agent_running(handle):
            if all(n in last for n in needles):
                return last
            raise RuntimeError(f"agent not running:\n{last[-3000:]}")
        time.sleep(poll)
    raise TimeoutError(f"missing {needles}:\n{last[-3000:]}")
