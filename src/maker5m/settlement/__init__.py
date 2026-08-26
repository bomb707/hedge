"""Settlement: resolution, payout, and redemption planning (Canonical §18; Detailed §32, §33).

The one operation here that may ever destroy tokens is ``redeemPositions``, and only after
authoritative final resolution. There is no sell, hedge, flatten, merge, split, or convert path,
and the redemption transport refuses before touching a credential — P14 owns sending it.
"""

from maker5m.settlement.audit import SETTLEMENT_SCHEMA_VERSION, SettlementRecord
from maker5m.settlement.contracts import (
    CHAIN_ID,
    CTF_ADDRESS,
    PARENT_COLLECTION_ID,
    PUSD_ADDRESS,
    PUSD_DECIMALS,
    binary_index_sets,
    encode_redeem_positions,
)
from maker5m.settlement.payout import PaperSettlement, outcome_payout, settle_on_paper
from maker5m.settlement.reader import (
    DEFAULT_RPC_ENDPOINTS,
    AttestedProvider,
    CtfReader,
    DuplicateEndpointError,
    EndpointSet,
    ProviderIdentity,
    RpcEndpoint,
    attest_all,
    endpoint_fingerprint,
    read_all,
)
from maker5m.settlement.redeem import (
    REDEMPTION_ENABLED,
    RedeemBlocker,
    Redeemer,
    RedeemPlan,
    RedemptionDisabledError,
    SettlementPreconditions,
    plan_redemption,
)
from maker5m.settlement.resolution import (
    DEFAULT_SETTLEMENT_POLICY,
    AdvisoryResolution,
    AmbiguityReason,
    MarketResolutionTarget,
    PayoutVector,
    ProviderAttestation,
    ProviderResolution,
    ResolutionDecision,
    ResolutionState,
    SettlementPolicy,
    verify,
)
from maker5m.settlement.safety import report_resolution, resolution_safety_signal

__all__ = [
    "CHAIN_ID",
    "CTF_ADDRESS",
    "DEFAULT_RPC_ENDPOINTS",
    "DEFAULT_SETTLEMENT_POLICY",
    "PARENT_COLLECTION_ID",
    "PUSD_ADDRESS",
    "PUSD_DECIMALS",
    "REDEMPTION_ENABLED",
    "SETTLEMENT_SCHEMA_VERSION",
    "AdvisoryResolution",
    "AmbiguityReason",
    "AttestedProvider",
    "CtfReader",
    "DuplicateEndpointError",
    "EndpointSet",
    "MarketResolutionTarget",
    "PaperSettlement",
    "PayoutVector",
    "ProviderAttestation",
    "ProviderIdentity",
    "ProviderResolution",
    "RedeemBlocker",
    "RedeemPlan",
    "Redeemer",
    "RedemptionDisabledError",
    "ResolutionDecision",
    "ResolutionState",
    "RpcEndpoint",
    "SettlementPolicy",
    "SettlementPreconditions",
    "SettlementRecord",
    "attest_all",
    "binary_index_sets",
    "encode_redeem_positions",
    "endpoint_fingerprint",
    "outcome_payout",
    "plan_redemption",
    "read_all",
    "report_resolution",
    "resolution_safety_signal",
    "settle_on_paper",
    "verify",
]
