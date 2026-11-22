#!/usr/bin/env python3
"""issue #14 — cross-process bus host contract test.

Starts a minimal host harness in bus mode (the policy_agent.aura
integration point needs the full C data plane; the harness mirrors the
same contract: seed choose-fn + protected define -> bus:host ->
maintainer self-attach -> pure-define-only constraint -> std/contract
protection mirror -> project-nodes -> metric publish loop).

Verifies:
  1. bus:host is active (host? = 1) and the host socket exists
  2. a peer can attach read-only (role 1 perf, caps 1)
  3. window metrics are published on the Metrics channel (keys 1..7)
  4. a protected define cannot be patched (KEEP refused, live unchanged)
  5. an impure choose-fn body is refused (pure-define-only)
  6. a pure choose-fn KEEP applies to the live AST (closed loop)

Uses ~/code/grok-dev/aura-grok/build/aura (AURA_BIN overrides) with
AURA_SANDBOX=off. Canary/evidence host params are set HERE (the runner
environment) before spawning aura — Aura env is read-only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AURA_BIN = os.environ.get(
    "AURA_BIN", str(Path.home() / "code/grok-dev/aura-grok/build/aura")
)
WS = int(os.environ.get("AURA_REDIS_BUS_TEST_WS", "26971"))

# Same metric key table as policy_agent.aura bus-publish-metrics! / docs/bus.md
METRIC_KEYS = {1: "dgets", 2: "dsets", 3: "dhits", 4: "dmisses",
               5: "devicted", 6: "erate", 7: "hit-ewma"}

HOST_SCRIPT = r"""
; Minimal bus host harness — mirrors the policy_agent.aura issue #14
; integration point (seed -> host -> self-attach -> constraint ->
; contract protection -> project-nodes -> metric ticks).
(require "std/bus" all:)
(require "std/contract" all:)
(set-code "(define (choose-fn x) (+ x 1)) (define (admit-fn x) 1) (define (resp-send x) x)")
(eval-current)
(contract:activate! (contract:protect (contract:set) "resp-send"))
(define ws (string->number (getenv "AURA_REDIS_BUS_WORKSPACE")))

; Split on a separator: "a|b|c" -> ("a" "b" "c"). The multi-target
; command carries two node ids and two bodies, and a body carries
; spaces, so the fields are separated by a vertical bar.
(define (split-on sep s)
  (let loop ((i 0) (cur "") (acc '()))
    (if (>= i (string-length s))
        (reverse (if (string=? cur "") acc (cons cur acc)))
        (let ((c (string-ref s i)))
          (if (char=? c sep)
              (loop (+ i 1) "" (cons cur acc))
              (loop (+ i 1) (string-append cur (substring s i (+ i 1))) acc))))))

; The workspace generation the candidates are built on: every live KEEP
; advances it by one, and a stale baseline is refused.
(define *gen* 0)

(define h (bus:host ws))
(display (if (bus:ok? h) "host-ok" "host-FAIL")) (newline)
(define ms (bus:attach "" ws 3 127 255))
(define msid (car (cdr ms)))
(bus:install-constraint msid "pure-define-only" "(lambda (ctx) #t)")
(bus:project-nodes msid)
(define (metric-tick)
  (bus:publish msid 1 10)
  (bus:publish msid 2 4)
  (bus:publish msid 3 8)
  (bus:publish msid 4 2)
  (bus:publish msid 5 1)
  (bus:publish msid 6 7)
  (bus:publish msid 7 80)
  (bus:project-nodes msid))
(metric-tick)
(display "ready") (newline)
; One long-lived session opens the trial, publishes patch + evidence,
; derives a scoped keep grant, activates it, KEEPs, then re-attaches to
; restore full caps (grant-activate narrows the session).
(define (do-patch node body)
  (define tr (bus:trial msid 3))
  (define wl (car (cdr tr)))
  (define src (bus:publish-source msid body))
  (bus:publish-patch msid node bus:source-field (car (cdr src)) wl 0)
  (define d (car (cdr (bus:candidate-digest msid wl))))
  (bus:bind-evidence msid wl *gen* d 7 42 1)
  (define g (bus:grant msid 24 255 0 0 wl))
  (bus:grant-activate msid (car (cdr g)))
  (define k (bus:keep msid wl 0 0))
  (if (bus:ok? k) (set! *gen* (+ *gen* 1)))
  (display "keep=") (display (if (bus:ok? k) "ok" (bus:code k)))
  (display " reason=") (display (bus:message k)) (display " patch-done|") (newline)
  (bus:detach msid)
  (set! ms (bus:attach "" ws 3 127 255))
  (set! msid (car (cdr ms))))
(let loop ()
  (let ((l (read-line)))
    (cond
      ((eof-object? l) #f)
      ((string=? l "host?")
       (define r (bus:host? ws))
       (display "host?=") (display (car (cdr r))) (display "|") (newline)
       (loop))
      ((string=? l "defs")
       ; The host's own define names and node ids (aura#5397): a peer
       ; asks the host instead of guessing an id that is host-local.
       (define d (bus:defines msid))
       (display "defs=") (display (if (bus:ok? d) (list-ref d 3) '())) (display "|") (newline)
       (loop))
      ((string=? l "tick")
       (metric-tick)
       (display "tick-ok|") (newline)
       (loop))
      ((string=? l "choose")
       (display "choose=") (display (choose-fn 5)) (display "|") (newline)
       (loop))
      ((string=? l "resp")
       (display "resp=") (display (resp-send 5)) (display "|") (newline)
       (loop))
      ((and (>= (string-length l) 6) (string=? (substring l 0 6) "multi "))
       ; "multi <node1> <body1> <node2> <body2>": one worldline carries a
       ; patch per function and the KEEP applies the set atomically.
       (define parts (split-on #\| (substring l 6 (string-length l))))
       (define tr (bus:trial msid 3))
       (define wl (car (cdr tr)))
       (define (patch node body)
         (define src (bus:publish-source msid body))
         (bus:publish-patch msid node bus:source-field (car (cdr src)) wl 0))
       (patch (string->number (car parts)) (car (cdr parts)))
       (patch (string->number (car (cdr (cdr parts)))) (car (cdr (cdr (cdr parts)))))
       (define d (car (cdr (bus:candidate-digest msid wl))))
       (bus:bind-evidence msid wl *gen* d 7 42 1)
       (define g (bus:grant msid 24 255 0 0 wl))
       (bus:grant-activate msid (car (cdr g)))
       (define k (bus:keep msid wl 0 0))
       (if (bus:ok? k) (set! *gen* (+ *gen* 1)))
       (display "multi-keep=") (display (if (bus:ok? k) "ok" (bus:code k)))
       (display " reason=") (display (bus:message k)) (display " multi-done|") (newline)
       (bus:detach msid)
       (set! ms (bus:attach "" ws 3 127 255))
       (set! msid (car (cdr ms)))
       (loop))
      ((and (>= (string-length l) 6) (string=? (substring l 0 6) "patch "))
       ; "patch <node> <body>"
       (define sp (let find ((i 6))
                    (cond ((>= i (string-length l)) #f)
                          ((char=? (string-ref l i) #\space) i)
                          (else (find (+ i 1))))))
       (define node (string->number (substring l 6 sp)))
       (define body (substring l (+ sp 1) (string-length l)))
       (do-patch node body)
       (loop))
      (else (loop)))))
(bus:detach msid)
(bus:unhost ws)
(display "HOST-DONE") (newline)
"""

# Read-only peer: role 1 (perf), caps 1 (read only), all channels.
PEER_SCRIPT = r"""
(require "std/bus" all:)
(define ws (string->number (getenv "AURA_REDIS_BUS_WORKSPACE")))
(define s (bus:attach "" ws 1 1 255))
(display (if (bus:ok? s) "peer-attach-ok" "peer-attach-FAIL")) (newline)
(define sid (car (cdr s)))
(display "peer-caps=") (display (car (cdr (cdr (cdr s))))) (display "|") (newline)
(define q (bus:query sid 1 0 0))
(display "peer-query=") (display (if (bus:ok? q) "ok" (bus:code q))) (display "|") (newline)
(display "rows=") (display (car (cdr (cdr (cdr (cdr q)))))) (display "|") (newline)
; read-only: publish must be refused
(define p (bus:publish sid 1 99))
(display "peer-publish=") (display (bus:code p)) (display "|") (newline)
; read-only: trial must be refused
(define t (bus:trial sid 3))
(display "peer-trial=") (display (bus:code t)) (display "|") (newline)
(bus:detach sid)
(display "PEER-DONE") (newline)
"""


def aura_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "AURA_SANDBOX": "off",
        "AURA_PIPELINE_STRICT": "0",
        "AURA_REDIS_BUS_WORKSPACE": str(WS),
        # host params read by the C++ coordinator at bus:host time
        "AURA_BUS_EVIDENCE": "1",
        "AURA_BUS_CANARY_WINDOW": "0",  # no observation in this test
    })
    env.pop("AURA_BUS_CANARY_FLOOR", None)
    env.pop("AURA_BUS_CANARY_KEY", None)
    if extra:
        env.update(extra)
    return env


class Proc:
    def __init__(self, script: str, tmp: Path, name: str, env: dict[str, str]):
        path = tmp / f"{name}.aura"
        path.write_text(script)
        self.log = tmp / f"{name}.log"
        argv = [AURA_BIN, str(path)]
        if shutil.which("stdbuf"):
            argv = ["stdbuf", "-oL", "-eL", *argv]
        self.p = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=self.log.open("w"),
            stderr=subprocess.STDOUT, env=env, cwd=str(ROOT), text=True,
        )

    def wait_line(self, needle: str, timeout: float = 15.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.log.read_text(errors="replace")
            if needle in text:
                return text
            if self.p.poll() is not None:
                raise RuntimeError(f"{self.p.args} died:\n{text}")
            time.sleep(0.05)
        raise TimeoutError(f"no {needle!r} in:\n{self.log.read_text()}")

    def wait_count(self, needle: str, n: int, timeout: float = 20.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.log.read_text(errors="replace")
            if text.count(needle) >= n:
                return text
            if self.p.poll() is not None:
                raise RuntimeError(f"{self.p.args} died:\n{text}")
            time.sleep(0.05)
        raise TimeoutError(f"{needle!r} x{n} not seen:\n{self.log.read_text()}")

    def send(self, line: str) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def stop(self) -> None:
        if self.p.poll() is None:
            try:
                if self.p.stdin:
                    self.p.stdin.close()  # EOF ends the host read loop
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()


def find_node_ids(host) -> tuple[int, int, int]:
    """The host's define names and node ids (aura#5397). The ids live in
    the host's own workspace AST, so the host is the only one that can
    answer: a peer asks (bus:defines) instead of guessing."""
    host.send("defs")
    line = host.wait_line("defs=")
    body = line.split("defs=", 1)[1].split("|", 1)[0]
    ids: dict[str, int] = {}
    # The reply is ((name node) (name node) ...).
    for chunk in body.split("(")[1:]:
        parts = chunk.replace(")", " ").split()
        if len(parts) >= 2:
            ids[parts[0]] = int(parts[1])
    for name in ("choose-fn", "admit-fn", "resp-send"):
        if name not in ids:
            raise RuntimeError(f"the host did not report {name}: {line}")
    return ids["choose-fn"], ids["admit-fn"], ids["resp-send"]


def main() -> int:
    if not Path(AURA_BIN).is_file():
        print(f"SKIP: aura binary not found at {AURA_BIN}")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="aura-redis-bus-"))
    env = aura_env({"XDG_RUNTIME_DIR": str(tmp)})
    sock = tmp / "aura" / "bus" / f"{WS}.sock"
    host = peer = None
    try:
        host = Proc(HOST_SCRIPT, tmp, "host", env)
        host.wait_line("ready")
        choose_node, admit_node, resp_node = find_node_ids(host)
        print(f"node ids: choose-fn={choose_node} admit-fn={admit_node} resp-send={resp_node}")
        assert "host-ok" in host.log.read_text(), "bus:host failed"
        assert sock.exists(), f"host socket missing: {sock}"
        host.send("host?")
        assert "host?=1|" in host.wait_line("host?=1|"), "host not active"
        print("1. host active OK")

        # 2+3. read-only peer attaches and reads published metrics
        peer = Proc(PEER_SCRIPT, tmp, "peer", env)
        out = peer.wait_line("PEER-DONE")
        assert "peer-attach-ok" in out, "read-only attach failed"
        assert "peer-caps=1|" in out, f"peer caps not read-only:\n{out}"
        assert "peer-query=ok|" in out, "peer metric query failed"
        rows_line = next(l for l in out.splitlines() if l.startswith("rows="))
        for key in METRIC_KEYS:
            assert f"({key} " in rows_line, f"metric key {key} missing: {rows_line}"
        assert ("peer-publish=permission|" in out
                or "peer-publish=loan-exhausted|" in out), \
            f"read-only publish not refused:\n{out}"
        assert "peer-trial=permission|" in out, "read-only trial not refused"
        print("2. read-only peer attach OK")
        print("3. metrics published OK (keys 1..7)")

        # more ticks must advance the channel
        host.send("tick")
        host.wait_line("tick-ok|")

        # 4. protected define patch refused, live value unchanged
        host.send(f"patch {resp_node} (lambda (x) (+ x 999))")
        k1 = host.wait_count("patch-done|", 1)
        assert ("keep=validation-failed" in k1 or "keep=constraint-rejected" in k1), \
            f"protected patch not refused:\n{k1}"
        assert "protect" in k1 or "constraint" in k1, f"refusal reason:\n{k1}"
        host.send("resp")
        assert "resp=5|" in host.wait_line("resp=5|"), "protected define changed"
        print("4. protected define patch refused OK")

        # 5. impure choose-fn body refused by pure-define-only
        host.send(f"patch {choose_node} (lambda (x) (display x) (+ x 10))")
        k2 = host.wait_count("patch-done|", 2)
        assert "keep=constraint-rejected" in k2.split("patch-done|")[1], \
            f"impure body not refused:\n{k2}"
        host.send("choose")
        assert "choose=6|" in host.wait_line("choose=6|"), "choose changed by impure patch"
        print("5. impure body refused OK")

        # 6. pure choose-fn KEEP applies to the live AST
        host.send(f"patch {choose_node} (lambda (x) (+ x 10))")
        k3 = host.wait_count("patch-done|", 3)
        assert "keep=ok" in k3.split("patch-done|")[2], f"pure KEEP failed:\n{k3}"
        host.send("choose")
        assert "choose=15|" in host.wait_line("choose=15|"), "KEEP not applied live"
        print("6. pure choose-fn KEEP applied live OK")

        # 7. a multi-function candidate: one worldline carries a patch per
        #    function and the KEEP applies the set atomically.
        host.send(f"multi {choose_node}|(lambda (x) (+ x 20))|{admit_node}|(lambda (x) 7)")
        k4 = host.wait_count("multi-done|", 1)
        assert "multi-keep=ok" in k4, f"multi-function KEEP failed:\n{k4}"
        host.send("choose")
        assert "choose=25|" in host.wait_line("choose=25|"), \
            "choose-fn not rewritten by the multi-function KEEP"
        print("7. multi-function KEEP applied atomically OK")

        host.stop()
        assert "HOST-DONE" in host.log.read_text(), "host did not shut down cleanly"
        assert not sock.exists(), "socket not removed on unhost"
        print("bus_host_test: ALL OK")
        return 0
    finally:
        for p in (peer, host):
            if p is not None:
                p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())