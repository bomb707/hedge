"""Reading Conditional Tokens state from Polygon, over several providers at once.

Read-only by construction: the client exposes ``eth_chainId``, ``eth_getCode``,
``eth_call``, and ``eth_getBlockByNumber``, and nothing that could write. There is no
``eth_sendRawTransaction``, no signer, and no key material anywhere in this module.

``urllib.request`` rather than a new dependency: JSON-RPC is a POST with a JSON body, and adding
an HTTP library — or Web3 — for four ``eth_call``s would be more surface area, not less. An
explicit honest ``User-Agent`` is set, because the default ``Python-urllib`` string is refused
with HTTP 403 by several public endpoints; P6 learned that against Gamma and it is the same
lesson here.

I/O lives here so that :mod:`maker5m.settlement.resolution` can stay pure. This module gathers;
it does not judge.
"""

import json
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

from maker5m.settlement.contracts import (
    CTF_ADDRESS,
    PUSD_ADDRESS,
    PUSD_DECIMALS,
    SELECTOR_DECIMALS,
    SELECTOR_GET_OUTCOME_SLOT_COUNT,
    SELECTOR_PAYOUT_DENOMINATOR,
    SELECTOR_PAYOUT_NUMERATORS,
    encode_uint,
)
from maker5m.settlement.resolution import (
    PayoutVector,
    ProviderAttestation,
    ProviderResolution,
)

__all__ = [
    "DEFAULT_RPC_ENDPOINTS",
    "USER_AGENT",
    "AttestationBindingError",
    "AttestedProvider",
    "CtfReader",
    "DuplicateEndpointError",
    "EndpointSet",
    "ProviderIdentity",
    "RpcEndpoint",
    "attest_all",
    "endpoint_fingerprint",
    "read_all",
]

USER_AGENT: Final[str] = (
    "maker5m/0.1 (Polymarket BTC 5m settlement reader; read-only; https://github.com/bomb707/hedge)"
)
"""Honest and identifying. Public RPCs refuse the default ``Python-urllib`` User-Agent."""

DEFAULT_RPC_ENDPOINTS: Final[tuple[tuple[str, str], ...]] = (
    ("publicnode", "https://polygon-bor-rpc.publicnode.com"),
    ("drpc", "https://polygon.drpc.org"),
    ("quiknode-public", "https://rpc-mainnet.matic.quiknode.pro"),
    ("1rpc", "https://1rpc.io/matic"),
)
"""Reference set, OPERATIONAL. Independence is the point, not the particular vendors: a quorum
drawn from one operator's infrastructure would agree with itself."""


def endpoint_fingerprint(url: str) -> str:
    """Normalised form used to tell two configured endpoints apart.

    Deliberately shallow. It catches the same URL written twice — trailing slash, capitalised
    host, a stray ``?`` — and nothing more. It cannot tell that two vendors resell one operator's
    infrastructure, and does not pretend to: organisational independence stays an OPERATIONAL
    assumption about the configured set, documented as such.
    """
    text = url.strip().rstrip("/?")
    scheme, separator, rest = text.partition("://")
    if not separator:
        return text.lower()
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


class DuplicateEndpointError(ValueError):
    """Two configured endpoints that are not independent evidence."""


@dataclass(frozen=True, slots=True)
class RpcEndpoint:
    provider_id: str
    url: str
    timeout_seconds: float = 15.0

    @property
    def fingerprint(self) -> str:
        return endpoint_fingerprint(self.url)


@dataclass(frozen=True, slots=True)
class EndpointSet:
    """A configured set of endpoints that has been checked for obvious duplication.

    Validated at construction rather than at use. A duplicated endpoint is a configuration
    mistake whose whole effect is to make a quorum look larger than it is, and the moment to
    refuse it is before any market depends on the answer.
    """

    endpoints: tuple[RpcEndpoint, ...]

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise DuplicateEndpointError("at least one endpoint is required")

        ids = Counter(endpoint.provider_id for endpoint in self.endpoints)
        repeated_ids = sorted(name for name, count in ids.items() if count > 1)
        if repeated_ids:
            raise DuplicateEndpointError(
                f"provider id configured more than once: {', '.join(repeated_ids)}; "
                "one endpoint under two names is one opinion, not two"
            )

        urls: dict[str, list[str]] = {}
        for endpoint in self.endpoints:
            urls.setdefault(endpoint.fingerprint, []).append(endpoint.provider_id)
        shared = sorted(
            f"{fingerprint} <- {', '.join(sorted(names))}"
            for fingerprint, names in urls.items()
            if len(names) > 1
        )
        if shared:
            raise DuplicateEndpointError(
                f"endpoint URL configured more than once: {'; '.join(shared)}; "
                "distinct names for one URL would count as independent evidence"
            )

    def __iter__(self) -> Iterator[RpcEndpoint]:
        return iter(self.endpoints)

    def __len__(self) -> int:
        return len(self.endpoints)


class AttestationBindingError(ValueError):
    """A proof of identity being attached to something it does not describe."""


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """What one endpoint proved about itself and the contracts, before it is trusted.

    The endpoint is part of the record, not something supplied afterwards. This says "I
    identified *this* endpoint" — an earlier version said only "I identified some provider", and
    left it to a later caller to assert which endpoint the proof belonged to, which is exactly
    the assertion that needed proving.
    """

    provider_id: str
    endpoint_fingerprint: str
    chain_id: int | None = None
    ctf_code_bytes: int = 0
    collateral_code_bytes: int = 0
    collateral_decimals: int | None = None
    finalized_block: int | None = None
    latest_block: int | None = None
    error: str | None = None

    @property
    def trustworthy(self) -> bool:
        """Whether this endpoint is describing the chain and contracts we mean.

        A provider that is healthy but pointed at another chain is worse than one that is down:
        it answers confidently about the wrong world.
        """
        from maker5m.settlement.contracts import CHAIN_ID

        return (
            self.error is None
            and self.chain_id == CHAIN_ID
            and self.ctf_code_bytes > 0
            and self.collateral_code_bytes > 0
            and self.collateral_decimals == PUSD_DECIMALS
        )

    def to_attestation(self) -> ProviderAttestation:
        """The carryable form of this check, for attaching to readings.

        Takes no endpoint. The previous signature accepted one and copied its fingerprint in,
        so the same proof could be re-labelled for any endpoint by changing an argument.
        """
        return ProviderAttestation(
            provider_id=self.provider_id,
            endpoint_fingerprint=self.endpoint_fingerprint,
            chain_id=self.chain_id,
            ctf_code_bytes=self.ctf_code_bytes,
            collateral_code_bytes=self.collateral_code_bytes,
            collateral_decimals=self.collateral_decimals,
            attested_at_block=self.finalized_block,
            error=self.error,
        )

    def summary(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "chain_id": self.chain_id,
            "ctf_code_bytes": self.ctf_code_bytes,
            "collateral_code_bytes": self.collateral_code_bytes,
            "collateral_decimals": self.collateral_decimals,
            "finalized_block": self.finalized_block,
            "latest_block": self.latest_block,
            "trustworthy": self.trustworthy,
            "error": self.error,
        }


@dataclass(slots=True)
class CtfReader:
    """One provider's read-only view of the Conditional Tokens contract."""

    endpoint: RpcEndpoint
    calls: int = field(default=0, repr=False)

    def _rpc(self, method: str, params: list[Any]) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        request = urllib.request.Request(
            self.endpoint.url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        self.calls += 1
        with urllib.request.urlopen(request, timeout=self.endpoint.timeout_seconds) as response:
            payload = json.loads(response.read())
        if "error" in payload:
            raise RpcError(str(payload["error"]))
        return payload["result"]

    def _uint(self, result: str) -> int | None:
        return None if not result or result == "0x" else int(result, 16)

    def _call(self, to: str, data: str, block_tag: str) -> int | None:
        return self._uint(self._rpc("eth_call", [{"to": to, "data": data}, block_tag]))

    def identify(self) -> ProviderIdentity:
        """Establish that this endpoint is on Polygon and sees the contracts we expect."""
        try:
            chain_id = int(self._rpc("eth_chainId", []), 16)
            ctf_code = self._rpc("eth_getCode", [CTF_ADDRESS, "latest"])
            collateral_code = self._rpc("eth_getCode", [PUSD_ADDRESS, "latest"])
            decimals = self._call(PUSD_ADDRESS, SELECTOR_DECIMALS, "latest")
            latest = self._rpc("eth_getBlockByNumber", ["latest", False])
            finalized = self._rpc("eth_getBlockByNumber", ["finalized", False])
        except (RpcError, urllib.error.URLError, OSError, ValueError, KeyError) as error:
            return ProviderIdentity(
                provider_id=self.endpoint.provider_id,
                endpoint_fingerprint=self.endpoint.fingerprint,
                error=f"{type(error).__name__}: {error}",
            )
        return ProviderIdentity(
            provider_id=self.endpoint.provider_id,
            endpoint_fingerprint=self.endpoint.fingerprint,
            chain_id=chain_id,
            ctf_code_bytes=(len(ctf_code) - 2) // 2,
            collateral_code_bytes=(len(collateral_code) - 2) // 2,
            collateral_decimals=decimals,
            finalized_block=None if finalized is None else int(finalized["number"], 16),
            latest_block=None if latest is None else int(latest["number"], 16),
        )

    def _check_binding(self, attestation: ProviderAttestation) -> None:
        if attestation.provider_id != self.endpoint.provider_id:
            raise AttestationBindingError(
                f"attestation for {attestation.provider_id!r} offered to reader "
                f"{self.endpoint.provider_id!r}"
            )
        if attestation.endpoint_fingerprint != self.endpoint.fingerprint:
            raise AttestationBindingError(
                f"{self.endpoint.provider_id}: attestation describes "
                f"{attestation.endpoint_fingerprint!r}, not {self.endpoint.fingerprint!r}"
            )
        if not attestation.valid:
            raise AttestationBindingError(
                f"{self.endpoint.provider_id}: attestation does not pass its own checks"
            )

    def read_condition(
        self,
        condition_id: str,
        *,
        block_tag: str,
        attestation: ProviderAttestation | None = None,
    ) -> ProviderResolution:
        """The full payout state for one condition, pinned to one concrete block.

        ``attestation`` is what makes the result eligible for a quorum. Reading without one is
        still allowed — it is useful for diagnostics — but the verifier will refuse the result,
        which is the intended asymmetry: gathering is not trusting.

        An attestation that describes a *different* endpoint is refused outright, and refused
        before any request is sent: a foreign proof must never end up decorating a real reading,
        and there is no reason to spend network calls discovering that it would have.
        """
        if attestation is not None:
            self._check_binding(attestation)

        word = condition_id.removeprefix("0x").rjust(64, "0")
        try:
            chain_id = int(self._rpc("eth_chainId", []), 16)
            block = self._rpc("eth_getBlockByNumber", [block_tag, False])
            block_number = None if block is None else int(block["number"], 16)

            # No concrete block, no reading. There is deliberately no fallback to the tag here.
            #
            # Resolving "finalized" once per request is not the same read: live observation
            # caught drpc reporting a finalized head and, in the same breath, a payout from a
            # backend that did not have it, so the block in the audit record was not the block
            # the payout came from. Falling back to the moving tag when the block lookup fails
            # would reintroduce exactly that, at the moment the provider is least reliable.
            if block_number is None:
                raise RpcError(
                    f"no concrete block for tag {block_tag!r}; refusing to read against a "
                    "moving tag"
                )

            at = hex(block_number)
            denominator = self._call(CTF_ADDRESS, SELECTOR_PAYOUT_DENOMINATOR + word, at)
            slot_count = self._call(CTF_ADDRESS, SELECTOR_GET_OUTCOME_SLOT_COUNT + word, at)
            numerators: list[int] = []
            for index in range(slot_count or 0):
                value = self._call(
                    CTF_ADDRESS,
                    SELECTOR_PAYOUT_NUMERATORS + word + encode_uint(index),
                    at,
                )
                numerators.append(0 if value is None else value)
        except (RpcError, urllib.error.URLError, OSError, ValueError, KeyError) as error:
            return ProviderResolution(
                provider_id=self.endpoint.provider_id,
                chain_id=None,
                block_tag=block_tag,
                block_number=None,
                condition_id=condition_id,
                payout=None,
                error=f"{type(error).__name__}: {error}",
                source_endpoint_fingerprint=self.endpoint.fingerprint,
                attestation=attestation,
            )
        return ProviderResolution(
            provider_id=self.endpoint.provider_id,
            chain_id=chain_id,
            block_tag=block_tag,
            block_number=block_number,
            condition_id=condition_id,
            payout=PayoutVector(
                denominator=denominator or 0,
                numerators=tuple(numerators),
                outcome_slot_count=slot_count or 0,
            ),
            source_endpoint_fingerprint=self.endpoint.fingerprint,
            attestation=attestation,
        )

    def erc1155_balance(self, owner: str, token_id: str) -> int | None:
        """``balanceOf(address,uint256)`` on the CTF, for position consistency checks.

        The read side of P9's position reconciliation. Comparing it against the internal ledger
        is P10's job; obtaining an *own-wallet* balance to compare is P14's, because no wallet
        exists before then.
        """
        selector = "0x00fdd58e"  # balanceOf(address,uint256)
        data = (
            selector + owner.removeprefix("0x").lower().rjust(64, "0") + encode_uint(int(token_id))
        )
        try:
            return self._call(CTF_ADDRESS, data, "latest")
        except (RpcError, urllib.error.URLError, OSError, ValueError) as error:
            del error
            return None


class RpcError(RuntimeError):
    """A JSON-RPC error response. Recorded as an error, never as an answer."""


@dataclass(frozen=True, slots=True)
class AttestedProvider:
    """An endpoint that proved which chain and contracts it serves, and its proof.

    This type is the trust boundary. It exists so that "this provider passed identity" is
    something the code *holds* rather than something it printed earlier and hoped was still true.

    It is **not** an unforgeable capability, and nothing here pretends otherwise: this is Python,
    and a value constructor is not a cryptographic primitive. What can honestly be claimed is
    narrower and still useful:

    * :func:`attest_all` is the normal production factory;
    * this value refuses to exist unless its identity actually describes its endpoint;
    * the reading records its own source independently of the proof attached to it;
    * the pure verifier re-checks the binding, so a hand-built mismatch fails there too.

    A determined caller can still construct nonsense. It will not count.
    """

    endpoint: RpcEndpoint
    identity: ProviderIdentity

    def __post_init__(self) -> None:
        if not self.identity.trustworthy:
            raise AttestationBindingError(
                f"{self.endpoint.provider_id}: identity did not pass "
                f"({self.identity.error or 'chain or contract check failed'})"
            )
        if self.identity.provider_id != self.endpoint.provider_id:
            raise AttestationBindingError(
                f"identity for {self.identity.provider_id!r} attached to endpoint "
                f"{self.endpoint.provider_id!r}; a proof describes one endpoint only"
            )
        if self.identity.endpoint_fingerprint != self.endpoint.fingerprint:
            raise AttestationBindingError(
                f"{self.endpoint.provider_id}: identity was obtained from "
                f"{self.identity.endpoint_fingerprint!r} but is being attached to "
                f"{self.endpoint.fingerprint!r}"
            )

    @property
    def provider_id(self) -> str:
        return self.endpoint.provider_id

    def read_condition(self, condition_id: str, *, block_tag: str) -> ProviderResolution:
        return CtfReader(self.endpoint).read_condition(
            condition_id,
            block_tag=block_tag,
            attestation=self.identity.to_attestation(),
        )


def attest_all(
    endpoints: EndpointSet,
) -> tuple[tuple[AttestedProvider, ...], tuple[ProviderIdentity, ...]]:
    """Check every configured endpoint's identity, and split trusted from untrusted.

    Returns the providers that may contribute evidence, and the identities of those that may
    not. Both halves are returned on purpose: an excluded endpoint has to appear in the record,
    because a quorum that is smaller than configured should be visibly smaller rather than
    silently so.
    """
    trusted: list[AttestedProvider] = []
    rejected: list[ProviderIdentity] = []
    for endpoint in endpoints:
        identity = CtfReader(endpoint).identify()
        if identity.trustworthy:
            trusted.append(AttestedProvider(endpoint=endpoint, identity=identity))
        else:
            rejected.append(identity)
    return tuple(trusted), tuple(rejected)


def read_all(
    providers: tuple[AttestedProvider, ...],
    condition_id: str,
    *,
    block_tag: str,
) -> tuple[ProviderResolution, ...]:
    """Read one condition from every attested provider.

    A provider that fails is represented by a reading carrying its error, so downstream counts a
    timeout as absent rather than as concurrence.
    """
    return tuple(
        provider.read_condition(condition_id, block_tag=block_tag) for provider in providers
    )
