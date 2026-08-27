"""Operator interface. Plane 3. Built in P12.

Read-only views over published immutable snapshots, plus a narrow control channel that
enqueues control events into the ordered stream rather than mutating trading state.

Holds no lock reachable from Plane 1 or 2. Killing the UI must not stop trading
(invariant I19).
"""

from maker5m.ui.bridge import (
    DEFAULT_COMMAND_CAPACITY,
    BridgeStats,
    CommandBridge,
    HotCommandChannel,
)
from maker5m.ui.channel import (
    MAX_PENDING_COMMANDS,
    ChannelFullError,
    CommandInbox,
    SnapshotChannel,
)
from maker5m.ui.control import CommandOutcome, ControlIngress
from maker5m.ui.model import (
    COMMAND_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    CommandKind,
    OperatorCommand,
    ParameterView,
    SideView,
    UiSnapshot,
)
from maker5m.ui.render import render_dashboard, render_history
from maker5m.ui.snapshot import (
    DEFAULT_PUBLISH_INTERVAL_S,
    SnapshotPublisher,
    parameter_views,
)

__all__ = [
    "COMMAND_SCHEMA_VERSION",
    "DEFAULT_COMMAND_CAPACITY",
    "DEFAULT_PUBLISH_INTERVAL_S",
    "MAX_PENDING_COMMANDS",
    "SNAPSHOT_SCHEMA_VERSION",
    "BridgeStats",
    "ChannelFullError",
    "CommandBridge",
    "CommandInbox",
    "CommandKind",
    "CommandOutcome",
    "ControlIngress",
    "HotCommandChannel",
    "OperatorCommand",
    "ParameterView",
    "SideView",
    "SnapshotChannel",
    "SnapshotPublisher",
    "UiSnapshot",
    "parameter_views",
    "render_dashboard",
    "render_history",
]
