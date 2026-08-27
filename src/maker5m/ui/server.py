"""The operator dashboard, as a small stdlib HTTP server. Its own process, on loopback.

No framework. `http.server` is enough for six read-only pages and two POST buttons, and a
dependency here would have to earn its place against a phase whose acceptance gate is *killing
this process abruptly* — the fewer moving parts on this side of the boundary, the more that test
proves about the other side.

Bound to 127.0.0.1 by default. There is no authentication in this build, so the only defensible
posture is that the control surface is not reachable from the network at all. That is an
OPERATIONAL choice recorded as one, not a claim to have solved access control: P12 is not a
production service and inventing an IAM here would be inventing something nobody has reviewed.

Nothing this server can do changes trading state. It reads a file the bot publishes and writes a
file the bot may or may not read. If the bot is gone, the pages say so; if this process is gone,
the bot does not notice.
"""

from __future__ import annotations

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from maker5m.ui.channel import ChannelFullError, CommandInbox, SnapshotChannel
from maker5m.ui.model import COMMAND_SCHEMA_VERSION, CommandKind, OperatorCommand
from maker5m.ui.render import render_dashboard, render_history

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "OperatorServer", "serve"]

DEFAULT_HOST: Final[str] = "127.0.0.1"
"""Loopback. See the module docstring: no authentication exists, so nothing is exposed."""

DEFAULT_PORT: Final[int] = 8787
MAX_BODY_BYTES: Final[int] = 8192


class OperatorServer:
    """Holds the two file endpoints and the history cache. No trading object is reachable."""

    def __init__(
        self, snapshot: SnapshotChannel, inbox: CommandInbox, history: Path | None = None
    ) -> None:
        self.snapshot = snapshot
        self.inbox = inbox
        self.history = history
        self._history_cache: tuple[float, list[dict[str, Any]]] | None = None

    def history_rows(self) -> list[dict[str, Any]]:
        """Verified P11 markets, cached for a minute.

        Cached because restoring a 10 MB archive and re-verifying it on every page refresh would
        be minutes of CPU for a view nobody is watching that closely. The cache holds only the
        summary; nothing in the trading path points at it, and a stale entry can do no more than
        show a market that has since been re-verified.
        """
        if self.history is None:
            return []
        now = time.time()
        if self._history_cache is not None and now - self._history_cache[0] < 60.0:
            return self._history_cache[1]
        rows = _summarise_history(self.history)
        self._history_cache = (now, rows)
        return rows


def _summarise_history(directory: Path) -> list[dict[str, Any]]:
    from tools.p11_query import summarise

    rows: list[dict[str, Any]] = []
    for archive in sorted(directory.glob("*.sqlite3.xz")):
        sidecar = archive.with_name(archive.name[: -len(".sqlite3.xz")] + ".manifest.json")
        try:
            rows.append(summarise(archive, sidecar if sidecar.exists() else None))
        except Exception as error:
            rows.append(
                {
                    "source": archive.name,
                    "slug": archive.stem,
                    "verification_status": "CORRUPT",
                    "evidence_eligible": False,
                    "decisions": "—",
                    "risk_records": "—",
                    "places_by_risk_state": f"{type(error).__name__}: {error}",
                }
            )
    return rows


class _Handler(BaseHTTPRequestHandler):
    server_version = "maker5m-operator"
    operator: OperatorServer

    def log_message(self, fmt: str, *args: Any) -> None:
        del fmt, args  # the dashboard is not a log source

    def _send(self, body: str, status: int = 200, content_type: str = "text/html") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        """Reads only.

        A GET never changes anything — not even a disabled command. A browser prefetch, a
        bookmark, a refresh or a crawler must not be able to halt a live market, and the way to
        guarantee that is for the read verb to have no write path at all.
        """
        path = urlparse(self.path).path
        if path == "/history":
            self._send(render_history(self.operator.history_rows()))
            return
        if path == "/snapshot.json":
            snapshot = self.operator.snapshot.read()
            self._send(json.dumps(snapshot or {}, indent=2), content_type="application/json")
            return
        if path == "/control":
            self._send(
                render_dashboard(
                    self.operator.snapshot.read(),
                    self.operator.snapshot.age_seconds(time.time()),
                    "Controls are POST-only. A link or a refresh cannot issue a command.",
                ),
                status=405,
            )
            return
        if path != "/":
            self._send("<h1>404</h1>", status=404)
            return
        self._send(
            render_dashboard(
                self.operator.snapshot.read(), self.operator.snapshot.age_seconds(time.time())
            )
        )

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/control":
            self._send("<h1>404</h1>", status=404)
            return
        length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        kind = (fields.get("kind") or [""])[0]
        if kind not in {member.value for member in CommandKind}:
            self._send(
                render_dashboard(
                    self.operator.snapshot.read(),
                    self.operator.snapshot.age_seconds(time.time()),
                    f"{kind!r} is not a command this build accepts.",
                ),
                status=400,
            )
            return

        command = OperatorCommand(
            schema_version=COMMAND_SCHEMA_VERSION,
            command_id=uuid.uuid4().hex[:16],
            kind=kind,
            issued_at_ns=time.time_ns(),
            source="operator-ui",
        )
        try:
            self.operator.inbox.submit(command)
            message = (
                f"{kind} submitted as {command.command_id}. It takes effect when the bot accepts "
                "it into the ordered risk stream; watch the command table below."
            )
        except (ChannelFullError, OSError) as error:
            # The operator is told plainly. Trading is never told anything.
            message = f"command NOT submitted: {error}"

        # 303 so a refresh re-reads the dashboard rather than re-posting the command.
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("X-Operator-Message", message.replace("\n", " ")[:200])
        self.end_headers()


def serve(
    snapshot_path: Path,
    inbox_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    history: Path | None = None,
) -> None:
    operator = OperatorServer(SnapshotChannel(snapshot_path), CommandInbox(inbox_path), history)
    handler = type("BoundHandler", (_Handler,), {"operator": operator})
    with ThreadingHTTPServer((host, port), handler) as httpd:
        print(f"operator UI on http://{host}:{port}  (loopback only)", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    serve(args.snapshot, args.inbox, host=args.host, port=args.port, history=args.history)
