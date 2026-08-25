"""An opaque credential boundary.

Secrets must never reach a repr, an exception message, a log line, a journal, a snapshot, or a
test fixture. The failure mode is quiet and permanent — once a private key appears in a
recorded journal it is compromised — so the type is designed to make leaking it awkward rather
than relying on every call site to remember.

``__repr__`` and ``__str__`` are redacted, the dataclass is not comparable by value, and the
secret is only reachable through an explicitly named accessor.

Nothing here reads the environment or a ``.env`` file. Credentials are supplied explicitly by
outer application wiring, so a core module can never pick one up by accident. There is no
loader to misconfigure.
"""

from dataclasses import dataclass, field

__all__ = ["ExecutionCredentials", "Secret"]


class Secret:
    """A string that refuses to print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("secret must be a non-empty string")
        self._value = value

    def reveal(self) -> str:
        """The only way out. Named so that every use site is greppable."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def __eq__(self, other: object) -> bool:
        # Deliberately not comparable: equality invites logging a diff of two secrets.
        return NotImplemented

    def __hash__(self) -> int:
        raise TypeError("Secret is not hashable; do not use it as a dict key or in a set")


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExecutionCredentials:
    """Everything the authenticated venue adapter needs, and nothing that may be printed."""

    private_key: Secret
    api_key: Secret
    api_secret: Secret
    api_passphrase: Secret
    funder_address: str = ""
    """A public wallet address. Not secret, so it is allowed to appear in diagnostics."""

    _redaction: str = field(default="<redacted>", init=False)

    def __repr__(self) -> str:
        return f"ExecutionCredentials(funder_address={self.funder_address!r}, secrets=<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()
