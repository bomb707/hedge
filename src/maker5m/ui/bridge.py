"""The Plane-3 side of the operator channel. Every filesystem call in P12 lives here.

The distinction this module exists to enforce
---------------------------------------------
"Does not wait for the UI process" is not the same as "cannot block the trading loop". P12's
first version polled the command inbox and wrote the snapshot from inside `on_tick`, which runs
in the single ingress consumer. Nothing there waited on the UI — and it did not need to: a
`listdir`, a `stat`, a `read_text` or a `rename` can stall on the *filesystem*, entirely
independently of whether any UI process is alive. A network mount hiccuping, a full disk, a
device queue behind fsync: none of those involve the UI at all, and all of them would have been
paid by a decision cycle.

So the file transport stays, and moves behind a thread. This bridge is the only thing that
lists, stats, reads, decodes or unlinks a command file, and the only thing that writes a
snapshot. What the ingress owner does instead is `popleft` from a bounded deque of
already-decoded immutable commands — no syscall, no serialization, no lock.

The bridge dying costs the operator their control channel and costs trading nothing. That is
reported as `CONTROL CHANNEL UNAVAILABLE` rather than papered over, because an operator whose
halt button silently does nothing is worse off than one who is told the channel is down.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from maker5m.ui.channel import CommandInbox, SnapshotChannel
from maker5m.ui.model import OperatorCommand, UiSnapshot

__all__ = [
    "DEFAULT_COMMAND_CAPACITY",
    "BridgeStats",
    "CommandBridge",
    "HotCommandChannel",
]

DEFAULT_COMMAND_CAPACITY: Final[int] = 32
"""How many decoded commands may wait for the ingress owner.

Small and explicit because commands are rare — two in a whole market is a busy one. The bound
exists so a wedged consumer cannot grow memory, not because contention is expected."""

DEFAULT_POLL_SECONDS: Final[float] = 0.1


@dataclass(slots=True)
class HotCommandChannel:
    """Bounded hand-off from the bridge to the ingress owner.

    A `deque` with a maximum length, for the reason P11 already established: `append` and
    `popleft` are individually atomic under CPython, so neither side takes a lock and the
    consumer cannot be made to wait by the producer. Not a `queue.Queue` — `put` and `get` both
    take one, and the hot side must never hold a lock Plane 3 can be holding.

    Unlike telemetry, this channel does **not** drop on overflow. A safety command silently
    discarded is worse than one that never arrives, so a full channel refuses the *push* and the
    bridge leaves the command file where it is, to be retried on the next pass.
    """

    capacity: int = DEFAULT_COMMAND_CAPACITY
    records: deque[OperatorCommand] = field(default_factory=deque, repr=False)
    accepted: int = 0
    high_water: int = 0

    def push(self, command: OperatorCommand) -> bool:
        """Offer one command. ``False`` means full — the caller must keep it, not drop it."""
        if len(self.records) >= self.capacity:
            return False
        self.records.append(command)
        self.accepted += 1
        occupancy = len(self.records)
        if occupancy > self.high_water:
            self.high_water = occupancy
        return True

    def pop_all(self, limit: int = DEFAULT_COMMAND_CAPACITY) -> list[OperatorCommand]:
        """Take whatever is waiting. The whole of the hot side's work, and it is not a syscall."""
        taken: list[OperatorCommand] = []
        while len(taken) < limit:
            try:
                taken.append(self.records.popleft())
            except IndexError:
                break
        return taken

    def __len__(self) -> int:
        return len(self.records)


@dataclass(slots=True)
class BridgeStats:
    """What the bridge managed and what it could not. Visible, never inferred."""

    polls: int = 0
    delivered: int = 0
    deferred: int = 0
    """Commands the hot channel could not take. Their files are still on disk."""

    unreadable: int = 0
    snapshots_published: int = 0
    snapshot_errors: int = 0
    errors: list[str] = field(default_factory=list)
    alive: bool = False

    def note_error(self, error: BaseException) -> None:
        self.unreadable += 1
        description = f"{type(error).__name__}: {error}"
        if description not in self.errors and len(self.errors) < 8:
            self.errors.append(description)

    def summary(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "delivered": self.delivered,
            "deferred": self.deferred,
            "unreadable": self.unreadable,
            "snapshots_published": self.snapshots_published,
            "snapshot_errors": self.snapshot_errors,
            "errors": list(self.errors),
            "alive": self.alive,
            "available": self.alive and not self.errors,
        }


@dataclass(slots=True)
class CommandBridge:
    """Owns the command directory and the snapshot file. One thread, no trading reference."""

    inbox: CommandInbox
    channel: HotCommandChannel
    snapshot: SnapshotChannel | None = None
    poll_seconds: float = DEFAULT_POLL_SECONDS
    stats: BridgeStats = field(default_factory=BridgeStats)

    stall: Any = None
    """Controlled fault injection: while this returns ``True`` the bridge does nothing.

    Present so a real market can be run with the control channel deliberately dead, which is the
    only way to show that its death costs trading nothing."""

    _pending: UiSnapshot | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def start(self) -> None:
        self.stats.alive = True
        self._thread = threading.Thread(target=self._run, name="maker5m-ui-bridge", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self.stats.alive = False

    def offer_snapshot(self, snapshot: UiSnapshot) -> None:
        """Hand the bridge the newest snapshot to write. Called from Plane 3, never from Plane 1.

        Assignment only — the write happens on the bridge thread. A caller that is already on a
        Plane-3 thread (the persistence worker) must still not pay a disk write inside the loop
        that is draining telemetry.
        """
        self._pending = snapshot

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.stall is not None and self.stall():
                self._stop.wait(self.poll_seconds)
                continue
            moved = self.poll_once()
            self.publish_pending()
            if not moved:
                self._stop.wait(self.poll_seconds)
        self.stats.alive = False

    def poll_once(self) -> int:
        """Move whatever the UI has left into the hot channel. Never raises.

        A command file is only deleted once its decoded form is safely in the hot channel. If the
        channel is full the file stays, the deferral is counted, and the next pass tries again —
        a safety command must not vanish because a consumer was briefly behind.
        """
        self.stats.polls += 1
        try:
            entries = sorted(self.inbox.directory.glob("*.json"))
        except OSError as error:
            self.stats.note_error(error)
            return 0

        moved = 0
        for entry in entries:
            try:
                command = self.inbox.read_one(entry)
            except OSError as error:
                self.stats.note_error(error)
                continue
            if command is None:
                # Malformed, oversized, or not a command this build knows. Consumed so it cannot
                # be retried forever, and counted so the operator can see it happened.
                self.stats.unreadable += 1
                self._discard(entry)
                continue
            if not self.channel.push(command):
                self.stats.deferred += 1
                break
            self._discard(entry)
            self.stats.delivered += 1
            moved += 1
        return moved

    def _discard(self, entry: Path) -> None:
        try:
            entry.unlink(missing_ok=True)
        except OSError as error:
            self.stats.note_error(error)

    def publish_pending(self) -> bool:
        """Write the newest offered snapshot, if there is one. Never raises."""
        snapshot = self._pending
        if snapshot is None or self.snapshot is None:
            return False
        self._pending = None
        before = self.snapshot.write_errors
        self.snapshot.publish(snapshot)
        if self.snapshot.write_errors != before:
            self.stats.snapshot_errors += 1
            return False
        self.stats.snapshots_published += 1
        return True
