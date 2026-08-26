"""The on-chain constants P10 depends on, and the ABI encoding it needs.

Every address here was reverified against the official Polymarket contracts page and against the
deployed bytecode on 2026-08-26 — chain id ``0x89``, ``CTF`` 15,007 bytes of code, ``pUSD``
``decimals()`` returning 6 and ``symbol()`` returning ``pUSD``. They are not carried over from
P10A's research scripts on trust.

Nothing in this module reads the network. It holds constants, computes selectors, and encodes
calldata; the reader owns I/O and the verifier owns judgement.
"""

from typing import Final

__all__ = [
    "CHAIN_ID",
    "CTF_ADDRESS",
    "PARENT_COLLECTION_ID",
    "PUSD_ADDRESS",
    "PUSD_DECIMALS",
    "SELECTOR_DECIMALS",
    "SELECTOR_GET_OUTCOME_SLOT_COUNT",
    "SELECTOR_PAYOUT_DENOMINATOR",
    "SELECTOR_PAYOUT_NUMERATORS",
    "SELECTOR_REDEEM_POSITIONS",
    "TOPIC_CONDITION_RESOLUTION",
    "binary_index_sets",
    "encode_address",
    "encode_redeem_positions",
    "encode_uint",
]

CHAIN_ID: Final[int] = 137
"""Polygon mainnet. A settlement read that reaches any other chain is not evidence about this
market, so the reader checks this rather than assuming its endpoint is pointed correctly."""

CTF_ADDRESS: Final[str] = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
"""Conditional Tokens. The authoritative source of what is redeemable (O11)."""

PUSD_ADDRESS: Final[str] = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
"""Current Polymarket collateral. **Not** the USDC an archived example would use."""

PUSD_DECIMALS: Final[int] = 6
"""Verified by calling ``decimals()``. Equal to ``MONEY_SCALE``'s decimal count, which is why
the collateral migration does not reopen O10."""

PARENT_COLLECTION_ID: Final[str] = "0x" + "00" * 32
"""``bytes32(0)``: these conditions hang directly off collateral, not off a parent position."""

SELECTOR_PAYOUT_DENOMINATOR: Final[str] = "0xdd34de67"
SELECTOR_PAYOUT_NUMERATORS: Final[str] = "0x0504c814"
SELECTOR_GET_OUTCOME_SLOT_COUNT: Final[str] = "0xd42dc0c2"
SELECTOR_REDEEM_POSITIONS: Final[str] = "0x01b7037c"
SELECTOR_DECIMALS: Final[str] = "0x313ce567"

TOPIC_CONDITION_RESOLUTION: Final[str] = (
    "0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894"
)
"""``ConditionResolution(bytes32,address,bytes32,uint256,uint256[])``. Its second indexed topic
is the oracle that resolved the condition — the field that showed these markets do not use the
UMA adapter."""


def encode_uint(value: int) -> str:
    """One 32-byte ABI word. Rejects negatives rather than wrapping them."""
    if value < 0:
        raise ValueError(f"uint256 cannot be negative, got {value}")
    if value >= 1 << 256:
        raise ValueError(f"value does not fit in uint256: {value}")
    return format(value, "064x")


def encode_address(address: str) -> str:
    """An address, left-padded to a word. Case is preserved nowhere: calldata is lowercase."""
    body = address.removeprefix("0x").lower()
    if len(body) != 40 or any(character not in "0123456789abcdef" for character in body):
        raise ValueError(f"not a 20-byte hex address: {address!r}")
    return body.rjust(64, "0")


def encode_bytes32(value: str) -> str:
    body = value.removeprefix("0x").lower()
    if len(body) != 64 or any(character not in "0123456789abcdef" for character in body):
        raise ValueError(f"not a 32-byte hex value: {value!r}")
    return body


def binary_index_sets() -> tuple[int, ...]:
    """The two singleton index sets of a two-slot condition: ``(1, 2)``.

    **Bit masks, not slot indices.** Slot 0 is the set ``{0}`` = ``0b01`` = 1, and slot 1 is
    ``{1}`` = ``0b10`` = 2. Passing ``(0, 1)`` would ask the contract to redeem the empty set and
    slot 0 — the empty set is rejected and slot 1's balance would simply never be claimed. The
    two look interchangeable and are not, so they are named here once rather than written
    inline at a call site.
    """
    return (1, 2)


def encode_redeem_positions(
    *,
    collateral_token: str,
    parent_collection_id: str,
    condition_id: str,
    index_sets: tuple[int, ...],
) -> str:
    """Calldata for ``redeemPositions(address,bytes32,bytes32,uint256[])``.

    Hand-written rather than pulled from a library because the project has no ABI dependency and
    this is one static signature with a single dynamic tail — adding a general-purpose encoder
    for it would be more surface area, not less. The head is three static words plus an offset;
    the tail is the array length followed by its elements.
    """
    if not index_sets:
        raise ValueError("redeemPositions requires at least one index set")
    if any(value <= 0 for value in index_sets):
        raise ValueError(f"index sets are non-empty bit masks; got {index_sets}")

    offset = 4 * 32  # four head words precede the dynamic tail
    head = (
        encode_address(collateral_token)
        + encode_bytes32(parent_collection_id)
        + encode_bytes32(condition_id)
        + encode_uint(offset)
    )
    tail = encode_uint(len(index_sets)) + "".join(encode_uint(value) for value in index_sets)
    return SELECTOR_REDEEM_POSITIONS + head + tail
