"""Drive the P12 real-market gate: read, control, kill, restart. Records what actually happened.

Timing is scripted rather than hand-driven so the evidence has exact positions in it — an
operator halt is only interesting if you can say which ingress ordinal it landed on, and a UI
kill is only interesting if you can say how many real market events were processed after it.

The bot and the UI are separate processes started elsewhere. This drives the UI over HTTP, kills
it with SIGKILL, and reads the bot's own published snapshot throughout — including after the UI
is dead, which is how it observes that the bot is still working.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def snapshot(path: Path) -> dict[str, Any] | None:
    try:
        return dict(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError):
        return None


def wait_for(path: Path, predicate: Any, timeout: float, label: str) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = snapshot(path)
        if current is not None and predicate(current):
            return current
        time.sleep(0.3)
    print(f"    timed out waiting for {label}", flush=True)
    return snapshot(path)


def post(url: str, kind: str) -> int:
    request = urllib.request.Request(url, data=f"kind={kind}".encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return int(error.code), error.read().decode("utf-8", "replace")


def start_ui(
    snapshot_path: Path, inbox: Path, port: int, history: Path | None
) -> subprocess.Popen[bytes]:
    argv = [
        sys.executable,
        "-m",
        "maker5m.ui.server",
        "--snapshot",
        str(snapshot_path),
        "--inbox",
        str(inbox),
        "--port",
        str(port),
    ]
    if history is not None:
        argv += ["--history", str(history)]
    return subprocess.Popen(argv, cwd=str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--history", type=Path)
    args = parser.parse_args()

    snapshot_path = args.ui / "snapshot.json"
    inbox = args.ui / "inbox"
    base = f"http://127.0.0.1:{args.port}"
    log: dict[str, Any] = {"kind": "P12_UI_ACCEPTANCE", "steps": []}

    def step(name: str, **fields: Any) -> None:
        entry = {"name": name, "at": round(time.time(), 3), **fields}
        log["steps"].append(entry)
        print(f"    [{name}] " + " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)

    print("waiting for the bot to publish its first snapshot...", flush=True)
    first = wait_for(
        snapshot_path, lambda s: s.get("ingress_ordinal", 0) > 0, 600, "first snapshot"
    )
    if first is None:
        raise SystemExit("the bot never published a snapshot")
    log["slug"] = first.get("slug")
    step("snapshot_seen", slug=first.get("slug"), ordinal=first.get("ingress_ordinal"))

    ui = start_ui(snapshot_path, inbox, args.port, args.history)
    log["ui_pid"] = ui.pid
    time.sleep(1.5)
    status, page = get(base + "/")
    step("ui_started", pid=ui.pid, http=status, renders_market=str(first.get("slug")) in page)

    control_status, control_page = get(base + "/control")
    step("get_control_refused", http=control_status, post_only="POST-only" in control_page)

    quoting = wait_for(
        snapshot_path,
        lambda s: s.get("risk_state") == "SAFE" and s.get("phase") in {"QUOTE", "ENDGAME"},
        420,
        "a SAFE quoting market",
    )
    step(
        "market_safe",
        phase=None if quoting is None else quoting.get("phase"),
        risk_state=None if quoting is None else quoting.get("risk_state"),
        ordinal=None if quoting is None else quoting.get("ingress_ordinal"),
    )

    step("halt_posted", http=post(base + "/control", "OPERATOR_HALT"))
    halted = wait_for(snapshot_path, lambda s: s.get("risk_state") == "HALTED", 60, "HALTED")
    if halted is not None:
        commands = halted.get("accepted_commands") or []
        log["halt"] = commands[-1] if commands else None
        step(
            "halted",
            risk_state=halted.get("risk_state"),
            allows_place=halted.get("allows_place"),
            allows_cancel=halted.get("allows_cancel"),
            command=json.dumps(log.get("halt")),
        )

    time.sleep(15)
    during = snapshot(snapshot_path)
    step(
        "still_halted_after_15s",
        risk_state=None if during is None else during.get("risk_state"),
        ordinal=None if during is None else during.get("ingress_ordinal"),
    )

    step("release_posted", http=post(base + "/control", "RELEASE_OPERATOR_HALT"))
    released = wait_for(snapshot_path, lambda s: s.get("risk_state") != "HALTED", 60, "release")
    if released is not None:
        commands = released.get("accepted_commands") or []
        log["release"] = commands[-1] if commands else None
        step(
            "released",
            risk_state=released.get("risk_state"),
            allows_place=released.get("allows_place"),
            command=json.dumps(log.get("release")),
        )

    before_kill = wait_for(
        snapshot_path, lambda s: s.get("risk_state") == "SAFE", 120, "SAFE again"
    )
    before_kill = before_kill or snapshot(snapshot_path) or {}
    log["kill_ordinal"] = before_kill.get("ingress_ordinal")
    log["kill_decisions"] = before_kill.get("decisions_persisted")
    os.kill(ui.pid, signal.SIGKILL)
    killed_at = time.time()
    step(
        "ui_killed",
        signal="SIGKILL",
        pid=ui.pid,
        ordinal=before_kill.get("ingress_ordinal"),
        decisions=before_kill.get("decisions_persisted"),
    )

    time.sleep(2)
    reachable = True
    try:
        get(base + "/")
    except OSError:
        reachable = False
    step("ui_unreachable", reachable=reachable)

    time.sleep(45)
    after = snapshot(snapshot_path) or {}
    log["after_kill_ordinal"] = after.get("ingress_ordinal")
    log["after_kill_decisions"] = after.get("decisions_persisted")
    step(
        "bot_alive_after_kill",
        ordinal=after.get("ingress_ordinal"),
        decisions=after.get("decisions_persisted"),
        events_since_kill=int(after.get("ingress_ordinal", 0))
        - int(before_kill.get("ingress_ordinal", 0)),
        decisions_since_kill=int(after.get("decisions_persisted", 0))
        - int(before_kill.get("decisions_persisted", 0)),
        risk_state=after.get("risk_state"),
        seconds_since_kill=round(time.time() - killed_at, 1),
    )

    restarted = start_ui(snapshot_path, inbox, args.port, args.history)
    log["ui_restart_pid"] = restarted.pid
    time.sleep(2)
    status, page = get(base + "/")
    resumed = snapshot(snapshot_path) or {}
    step(
        "ui_restarted",
        pid=restarted.pid,
        http=status,
        renders_market=str(resumed.get("slug")) in page,
        commands_after_restart=len(resumed.get("accepted_commands") or []),
    )

    time.sleep(3)
    final = snapshot(snapshot_path) or {}
    step("final", ordinal=final.get("ingress_ordinal"), risk_state=final.get("risk_state"))
    log["final_snapshot"] = final
    restarted.terminate()

    args.out.write_text(json.dumps(log, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
