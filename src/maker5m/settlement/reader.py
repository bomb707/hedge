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
from maker5m.settlement.resolution import PayoutVector, ProviderResolution

__all__ = [
    "DEFAULT_RPC_ENDPOINTS",
    "USER_AGENT",
    "CtfReader",
    "ProviderIdentity",
    "RpcEndpoint",
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


@dataclass(frozen=True, slots=True)
class RpcEndpoint:
    provider_id: str
    url: str
    timeout_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """What a provider claims about itself and the contracts, before it is trusted."""

    provider_id: str
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

    def summary(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
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
                self.endpoint.provider_id, error=f"{type(error).__name__}: {error}"
            )
        return ProviderIdentity(
            provider_id=self.endpoint.provider_id,
            chain_id=chain_id,
            ctf_code_bytes=(len(ctf_code) - 2) // 2,
            collateral_code_bytes=(len(collateral_code) - 2) // 2,
            collateral_decimals=decimals,
            finalized_block=None if finalized is None else int(finalized["number"], 16),
            latest_block=None if latest is None else int(latest["number"], 16),
        )

    def read_condition(self, condition_id: str, *, block_tag: str) -> ProviderResolution:
        """The full payout state for one condition at one block tag."""
        word = condition_id.removeprefix("0x").rjust(64, "0")
        try:
            chain_id = int(self._rpc("eth_chainId", []), 16)
            block = self._rpc("eth_getBlockByNumber", [block_tag, False])
            block_number = None if block is None else int(block["number"], 16)
            denominator = self._call(CTF_ADDRESS, SELECTOR_PAYOUT_DENOMINATOR + word, block_tag)
            slot_count = self._call(CTF_ADDRESS, SELECTOR_GET_OUTCOME_SLOT_COUNT + word, block_tag)
            numerators: list[int] = []
            for index in range(slot_count or 0):
                value = self._call(
                    CTF_ADDRESS,
                    SELECTOR_PAYOUT_NUMERATORS + word + encode_uint(index),
                    block_tag,
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


def read_all(
    endpoints: tuple[RpcEndpoint, ...],
    condition_id: str,
    *,
    block_tag: str,
) -> tuple[ProviderResolution, ...]:
    """Read one condition from every endpoint.

    A provider that fails is represented by a reading carrying its error, so downstream counts a
    timeout as absent rather than as concurrence.
    """
    return tuple(
        CtfReader(endpoint).read_condition(condition_id, block_tag=block_tag)
        for endpoint in endpoints
    )
