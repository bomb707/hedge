"""The boundary between the bot and the operator UI. Two processes, no shared anything.

Why files and not a socket, a queue, or a broker
------------------------------------------------
The P12 acceptance gate is that someone kills the UI mid-market and trading does not notice. That
rules out every transport where the bot holds something the UI can be holding when it dies: a
socket the bot accepts on, a lock, a condition variable, a database connection. It also rules out
a broker, which is a third process to keep alive in order to prove that a process dying is
survivable.

A directory is left. The bot writes the snapshot by writing a temporary file and renaming it,
which is atomic on POSIX, so a reader either sees the previous snapshot or the next one and never
half of either. The UI writes commands as individual files into an inbox. Killing either process
leaves a directory, which needs no recovery because it was never a connection.

Who reads the directory
-----------------------
Not the bot's ingress owner. That was P12's first shape and P12B moved it, because a `listdir`
can stall on the filesystem with no UI involved at all. The path is::

    UI process  ->  filesystem inbox  ->  Plane-3 CommandBridge  ->  bounded memory channel
                                                                        ->  ingress owner

The bridge is the only thing that lists, stats, reads, decodes or unlinks a command file. What
the ingress owner does is `popleft` from a deque of already-decoded immutable commands: no
syscall, no serialization, no lock. `CommandInbox.drain` remains for supporting tests and for a
single-process caller that is already off the ingress path.

The asymmetry the invariant demands falls straight out of it: the UI may block writing a file,
and nothing in Plane 1 can be made to wait by anything the UI does, including dying while
holding it.

Order is the transport's, not the clock's
-----------------------------------------
Pending commands were named `{issued_at_ns}-{command_id}.json` and read in sorted order, so a
browser's wall clock decided which of two waiting commands reached the bot first — and a UI on a
machine whose clock had drifted backwards could put a later command ahead of an earlier one.
`issued_at_ns` is audit metadata and nothing else. Delivery order now comes from a transport
sequence this inbox allocates on submission, which is monotonic within a process and continues
above whatever is already pending when a new one starts.

A transport sequence is not market causality either. What order a command *happened* in is
decided when the bot accepts it, by `ingress_ordinal` and `risk_sequence`, and that is what the
durable audit and any replay use.
"""

from __future__ import annotations

import itertools
import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Final

from maker5m.ui.model import COMMAND_SCHEMA_VERSION, OperatorCommand, UiSnapshot

__all__ = [
    "MAX_PENDING_COMMANDS",
    "ChannelFullError",
    "CommandInbox",
    "SnapshotChannel",
]

MAX_PENDING_COMMANDS: Final[int] = 64
"""How many unread commands the inbox will hold.

Bounded so a UI that has gone wrong cannot fill a disk, and so the bot's read stays O(small). An
operator submitting a 65th command while 64 are unread gets an explicit refusal — which is the
right answer, because the bot is evidently not reading, and pretending otherwise would be worse
than saying so."""

MAX_COMMAND_BYTES: Final[int] = 4096
"""A command is a few short fields. Anything larger is not one, and is refused unread."""


class ChannelFullError(RuntimeError):
    """The inbox will not take another command. The *sender* is told; trading never is."""


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {name: _encode(item) for name, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_encode(item) for item in value]
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temporary file and rename, so a reader never sees a partial write."""
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class SnapshotChannel:
    """The bot publishes here; the UI reads. One file, replaced atomically."""

    __slots__ = ("path", "published", "write_errors")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.published = 0
        self.write_errors = 0

    def publish(self, snapshot: UiSnapshot) -> None:
        """Replace the current snapshot. Never raises into the caller.

        This runs on a Plane-3 thread and a full disk is an observability incident, so a failure
        is counted and dropped rather than propagated — the same rule the telemetry sink follows,
        for the same reason.
        """
        try:
            # Inside the try, not before it. An unwritable parent is exactly the case this
            # method promises to absorb, and creating the directory is the first thing that
            # can fail.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps(_encode(snapshot), separators=(",", ":")))
        except OSError:
            self.write_errors += 1
            return
        self.published += 1

    def read(self) -> dict[str, Any] | None:
        """The latest snapshot, or ``None`` if there is not one yet or it is unreadable.

        ``None`` means *no data*. It is the reader's job never to render that as zeros, and the
        UI's templates treat a missing snapshot as DISCONNECTED rather than as a flat market.
        """
        try:
            text = self.path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            parsed: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed

    def age_seconds(self, now: float) -> float | None:
        try:
            return now - self.path.stat().st_mtime
        except OSError:
            return None


class CommandInbox:
    """The UI writes commands here; the bridge drains them. Bounded, ordered, deduplicated."""

    __slots__ = ("_counter", "directory", "seen")

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.seen: set[str] = set()
        self._counter = itertools.count(1)

    def _allocate(self, pending: list[Path]) -> int:
        """The next transport position: monotonic here, and above anything already waiting.

        Restarting the UI must not put a fresh command ahead of one that has been sitting in the
        inbox since before the restart, so a new process skips past the highest pending name
        rather than starting from one.

        `next` on an `itertools.count` rather than a lock and a `+= 1`: it completes inside one
        C call, which is the same atomicity P11 relies on for `deque.append`, and this module is
        forbidden a lock for a good reason — nothing the UI holds may be something the trading
        path could end up waiting on.
        """
        highest = 0
        for entry in pending:
            head = entry.name.split("-", 1)[0]
            if head.isdigit():
                highest = max(highest, int(head))
        sequence = next(self._counter)
        while sequence <= highest:
            sequence = next(self._counter)
        return sequence

    # -- sender side (UI process) ---------------------------------------------------------

    def submit(self, command: OperatorCommand) -> Path:
        """Place one command in the inbox. May block on the filesystem; the bot never does.

        Refuses when the inbox is already full rather than growing it. The operator gets an
        explicit failure, which is honest: an unread inbox means the bot is not reading, and a
        command silently queued behind sixty-four others is not a safety control.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        pending = list(self.directory.glob("*.json"))
        if len(pending) >= MAX_PENDING_COMMANDS:
            raise ChannelFullError(
                f"{len(pending)} commands are already waiting to be read; refusing to queue "
                "another rather than pretending it will be acted on"
            )
        # The transport's own order, not the sender's clock. `issued_at_ns` is recorded inside
        # the file as audit metadata and takes no part in deciding what is read first.
        path = self.directory / f"{self._allocate(pending):020d}-{command.command_id}.json"
        _write_atomic(path, json.dumps(command.summary(), separators=(",", ":")))
        return path

    # -- receiver side (the Plane-3 bridge, in the bot process) ---------------------------

    def drain(self, limit: int = MAX_PENDING_COMMANDS) -> list[OperatorCommand]:
        """Take whatever commands are waiting, in submission order. Never blocks, never raises.

        Supporting-test and single-process path; the production reader is `CommandBridge`.
        Everything that can go wrong with a file someone else wrote — unreadable, truncated,
        not JSON, not a command this build knows, already seen — ends here as a skipped file,
        because a malformed command must not be able to interrupt a market.
        """
        try:
            entries = sorted(self.directory.glob("*.json"))
        except OSError:
            return []

        commands: list[OperatorCommand] = []
        for entry in entries[:limit]:
            command = self.read_one(entry)
            entry.unlink(missing_ok=True)
            if command is None:
                continue
            if command.command_id in self.seen:
                # A retried submission or a re-posted form. The same command, not a second one.
                continue
            self.seen.add(command.command_id)
            commands.append(command)
        return commands

    def read_one(self, entry: Path) -> OperatorCommand | None:
        """Decode one command file. ``None`` for anything that is not a command this build knows.

        Public because the Plane-3 bridge owns file intake now; `drain` remains for supporting
        tests and for a single-process caller that is already off the ingress path.
        """
        try:
            if entry.stat().st_size > MAX_COMMAND_BYTES:
                return None
            payload = json.loads(entry.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return OperatorCommand(
                schema_version=int(payload.get("schema_version", -1)),
                command_id=str(payload.get("command_id", "")),
                kind=str(payload.get("kind", "")),
                issued_at_ns=int(payload.get("issued_at_ns", 0)),
                source=str(payload.get("source", "")),
                detail=str(payload.get("detail", "")),
            )
        except (ValueError, TypeError):
            return None


def command_schema_version() -> int:
    return COMMAND_SCHEMA_VERSION
