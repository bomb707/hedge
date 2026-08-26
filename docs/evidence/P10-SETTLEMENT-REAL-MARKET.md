# P10 — settlement against real markets

**Provenance: `REAL_PUBLIC_MARKET_DATA`,** except where a section is explicitly labelled
`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`. Every market, block, payout vector and provider answer
below was read from real Polygon RPC providers and real Polymarket `btc-updown-5m` markets. No
synthetic market, generated payout or mocked provider appears in this evidence
(`ARCHITECTURE_SSOT.md` §4.4).

Read-only throughout. **No redemption transaction was submitted, no wallet key exists in this
repository, no credential was requested, and no authenticated socket was opened.**
`LIVE_TRADING_ENABLED` is `False`; `REDEMPTION_ENABLED` is `False`.

**Capture date:** 2026-08-26 (UTC).

## What this evidence does and does not close

| Claim | Status |
| --- | --- |
| The verifier reads real CTF resolution correctly | **CLOSED** — 55 historical + 15 live markets |
| Multi-provider quorum and finality behave on the real chain | **CLOSED** |
| Ambiguity halts execution through the ordered P9 risk stream | **CLOSED** — controlled injection |
| `redeemPositions` calldata is accepted by the real contract | **CLOSED** — `eth_call`, 8/8 |
| Paper settlement arithmetic matches the on-chain payout | **CLOSED** — against a real payout vector |
| A real redemption pays what the plan says it pays | **UNRUN / DEFERRED to P14** |
| Own-ledger settlement economics on a nonzero position | **UNRUN / DEFERRED to P14** |

The last two rows are the honest limit of P10. Every position ledger settled here is empty,
because this bot has never placed an order. What has been demonstrated is that the arithmetic,
the encoding and the authorisation gate all behave on real data — not that money moved.

## The runs

Twenty-one real `btc-updown-5m` markets, consecutive within each run, across four runs on
2026-08-26. Every one settled; none was skipped or retried.

| Run | Markets | Slugs | Verifier | Purpose |
| --- | --- | --- | --- | --- |
| `p10-live-resolution-1787709600-1787711400.json` | 7 | `…709600` → `…711400` | pre-correction | §31 consecutive settlement + advisory injection |
| `p10-live-resolution-1787711700-1787712000.json` | 2 | `…711700` → `…712000` | pre-correction | §32 provider injection + recovery |
| `p10-live-resolution-1787712600-1787714100.json` | 6 | `…712600` → `…714100` | pre-correction | per-poll provider detail |
| `p10-live-resolution-1787714700-1787716200.json` | 6 | `…714700` → `…716200` | **corrected** | confirmation |

Six markets in the first run and six in the last are two disjoint sets of at least six new
consecutive real settlements, and neither overlaps the six used to close O11 in P10A.

### Resolution timing

Every market moved `UNRESOLVED → RESOLVED` on chain, observed at three independent providers
under the `finalized` tag. Lag from market end to accepted resolution, across all 21:

| | seconds after market end |
| --- | --- |
| fastest | 55.6 |
| slowest | 158.9 |
| typical | 88–95 |

`1rpc` answered no request in any run and was reported `trustworthy=False` at identification.
It is recorded as **absent**, never as agreeing — the P10A correction that made rate-limited
providers stop inflating the quorum is still holding.

## What the corrected verifier does on real data

The last run is the one that matters, because it is the only one using the code being shipped.

| Market | States seen | Outcome | Authoritative block | Redeem plan |
| --- | --- | --- | --- | --- |
| `…714700` | UNRESOLVED → RESOLVED | DOWN | 92673376 | produced |
| `…715000` | UNRESOLVED → RESOLVED | DOWN | 92673574 | produced |
| `…715300` | UNRESOLVED → RESOLVED | UP | 92673755 | produced |
| `…715600` | UNRESOLVED → RESOLVED | UP | 92673977 | produced |
| `…715900` | UNRESOLVED → RESOLVED | DOWN | 92674173 | produced |
| `…716200` | UNRESOLVED → RESOLVED | UP | 92674350 | produced |

No `AMBIGUOUS`, no `INSUFFICIENT_EVIDENCE`, no blocker, and **no risk signal emitted at all** —
the run's whole risk sequence is its single startup evaluation. Three of the six passed through
an explicit wait (`"1 of 3 required providers report the resolution; waiting on drpc,
publicnode"`) and then resolved normally.

## A defect found by running it: absence read as contradiction

The third run halted three of six markets with `FINALITY_DISAGREEMENT`, and the first run had
halted three of nine. Nothing was wrong with any of those markets. This is the P10 finding.

### The first explanation was wrong

The obvious reading was finality-head skew: P10A had measured providers' `finalized` heads 1–4
blocks apart, so for a few seconds the resolving block sits above one head and below another.
A fix was written on that premise — compare block numbers, treat a provider strictly behind as
lagging. **The recorded polls disproved it.**

| Market | provider reporting nothing | its block | earliest block where anyone had it |
| --- | --- | --- | --- |
| `…712900` | drpc | 92672148 | 92672148 — *the same block* |
| `…713200` | drpc | 92672371 | 92672371 — *the same block* |
| `…714100` | drpc | 92672974 | 92672972 — *two blocks ahead* |

A provider cannot be "behind" at a block it is ahead of. The premise was false, and the fix
built on it would not have prevented a single one of these halts.

### Two real defects underneath

**The reads were not atomic.** `read_condition` resolved `finalized` once for
`eth_getBlockByNumber` and again inside every `eth_call`. At a load-balanced provider those are
different requests that may reach different backends, so the block number written into the audit
record was not necessarily the block the payout came from. The record was wrong, and a healthy
provider looked like it was contradicting the chain. Every call is now pinned to the concrete
block number already resolved.

The effect is directly measurable in the recorded polls:

| | splits observed | where the silent provider was strictly behind |
| --- | --- | --- |
| before pinning | 3 | **0 of 3** |
| after pinning | 3 | **3 of 3** |

Same phenomenon, same providers, minutes apart. Before the fix the `(block, payout)` pairs were
incoherent; after it they describe ordinary lag, which is what the chain was doing all along.

**The quorum counted the wrong thing.** `minimum_agreeing_providers` was applied to providers
that merely *answered*, so requiring three of three answering providers demanded unanimity by
accident. It now counts providers positively agreeing on one payout vector. A provider reporting
nothing yet leaves the state `UNRESOLVED` — an absence of evidence, not contrary evidence — and
contradiction is checked *before* the count, so a genuine disagreement is never reported as
"waiting for a third opinion".

### Cost of the old behaviour, and of leaving it

Six false halts across fifteen live settlements: roughly every second or third market. Each one
raised a P9 `RESOLUTION_AMBIGUOUS` that **does not self-clear**, so in production the bot stops
placing and stays stopped until an operator intervenes. Replaying the recorded polls through the
corrected verifier removes **3 of 3** false halts in that run and makes **nothing** newly
ambiguous (`p10-finality-replay.json`).

The strict reading is still reachable as `SettlementPolicy.require_unanimous_resolution` and is
tested, because the argument for it — do not trust a provider that is silent — is about trust in
providers rather than about what the chain said.

## Controlled ambiguity injection

**Provenance: `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`.** The markets, the chain reads and the
venue metadata below are real and were correct. The corruption is ours, applied to our own copy
of the readings after they arrived. **Nothing here is a Polymarket or Polygon incident**, and in
21 real markets neither ever contradicted itself.

### Wrong advisory winner — market `btc-updown-5m-1787710500`

The market resolved `UP` at +90.0 s with three providers agreeing on `numerators=[1,0]`. One
advisory source was then flipped to name slot 1.

```
state:    AMBIGUOUS
reasons:  ["ADVISORY_DISAGREEMENT"]
detail:   advisory sources name a different winner than the payout vector:
          gamma_outcome_prices=slot 1
payout:   still reported verbatim — denominator 1, numerators [1, 0]
```

`RedeemPlan` = **NONE**. Blockers = `["RESOLUTION_AMBIGUOUS"]`. `winning_outcome` = `null`: the
verifier does not name a winner it cannot vouch for, but it does not discard the payout vector
it read either.

### Disagreeing provider payout — market `btc-updown-5m-1787711700`

The market resolved `DOWN` at +92.7 s. One provider's payout vector was then reversed.

```
state:    AMBIGUOUS
reasons:  ["PROVIDER_DISAGREEMENT"]
detail:   2 distinct payout vectors across answering providers
```

`RedeemPlan` = **NONE**. Blockers = `["RESOLUTION_AMBIGUOUS"]`.

### The halt reaches execution permission, and does not lift itself

Both injections emitted an ordered `RESOLUTION_SAFETY_UPDATE` through `RiskController`, landing
in the same audit trace as any other permission change:

```
risk_sequence 1  RESOLUTION_SAFETY_UPDATE  state=HALTED
                 active=['RESOLUTION_AMBIGUOUS']  allows_place=False  allows_cancel=True
```

`allows_cancel` stays `True` throughout. Being unable to price a market is a reason to stop
adding to a position, not a reason to be trapped in one.

The corruption was then removed and **fresh** readings taken from the real chain — not the copy
already held:

```
corruption removed, fresh read -> RESOLVED   risk=HALTED
```

Normal resolution returned immediately, and the halt did not. That is the intended behaviour and
the reason `maker5m.settlement.safety` never emits `flag=False`: a second look that happens to
agree is not evidence the first disagreement was imaginary. Lifting it takes a deliberate
operator signal, which is itself recorded.

**Recorded honestly:** P9 does not list `RESOLUTION_AMBIGUOUS` in `REQUIRES_RECONCILIATION`, so
the risk engine itself would let this condition clear with its flag. The stickiness above rests
on one module's restraint, not on the engine's contract. That is `O16`, left open rather than
settled by editing P9's latch set.

## End-to-end: one real market from arming to settlement

`btc-updown-5m-1787716800`, run start to finish through the production pipeline.

| | |
| --- | --- |
| Phases observed | `PREARM → QUOTE → ENDGAME → SETTLING → DONE` |
| Strategy cycles | 54,003 |
| CLOB messages | 52,276 (1,310 books, 49,459 price changes, 630 trades) |
| BTC spot messages | 3,230 |
| Reconnects / malformed / dropped telemetry | 0 / 0 / 0 |
| Settlement trajectory | `UNRESOLVED → RESOLVED` |
| Payout vector | `denominator 1`, `numerators [0, 1]` → **DOWN** |
| Authoritative block | 92674815, `finalized`, three providers |
| Ledger vs paper settlement | matches to the last money unit |
| Orders sent | **0** |
| Redemptions sent | **0** |

### The limitation, stated plainly

```
REAL MARKET LIFECYCLE VALIDATED. NONZERO OWN-LEDGER ECONOMICS UNRUN / P14: live trading is
disabled, so no order was placed and the ledger holds no position. The lifecycle, resolution,
payout vector, and reconciliation are real; the settled amounts are zero because there is
nothing to settle.
```

The reconciliation agreeing "to the last money unit" is an agreement between two zeros. It shows
the two paths are wired to the same ledger and the same payout vector; it does not show they
agree on an amount, because there is no amount. That requires a position, which requires an
order, which is P14.

## What was never done

* No redemption transaction was submitted. `REDEMPTION_ENABLED` is `False` and
  `Redeemer.submit()` raises `RedemptionDisabledError` before touching anything.
* No wallet key, private key, API key, secret or passphrase exists in this repository or was
  requested. There is no `--redeem-live` flag, environment bypass or config bypass.
* No authenticated socket was opened; every RPC call was one of `eth_chainId`, `eth_getCode`,
  `eth_call`, `eth_getBlockByNumber`, `eth_getLogs`, enforced by a behavioural guard test.
* No `eth_sendRawTransaction` or equivalent is reachable from any module.

The redemption calldata was validated against the real Conditional Tokens contract by `eth_call`
from a zero-balance sender — 8 of 8 accepted, 4 UP-resolved and 4 DOWN-resolved
(`p10-real-ethcall.json`). An `eth_call` proves the contract accepts the encoding and the index
sets. It does not prove a redemption pays, and it is not represented as doing so.


---

# P10 trust-boundary closure

Independent review accepted the settlement logic above and rejected the phase on three
production trust-boundary defects. All three had the same shape: a rule the code described
correctly in prose and never enforced.

**Capture date:** 2026-08-26 (UTC). Evidence:
`p10-trust-boundary-1787733300-1787735100.json`, `p10-production-verifier-p10a55.json`.

## The three defects

### A — one RPC could be three votes

The quorum counted `provider_id`s without ever requiring them to differ. Three readings from one
endpoint satisfied `minimum_agreeing_providers=3`:

```
provider_id = "a"   ->  RESOLVED
provider_id = "a"       3 providers agree
provider_id = "a"
```

That is the one thing the quorum exists to prevent. Two names pointing at one URL did the same
thing more quietly.

**Now:** independence is a property of the evidence *set* and is checked before anything the
readings say. A repeated id, or two ids sharing one endpoint fingerprint, fails closed with
`DUPLICATE_PROVIDER_ID`. `EndpointSet` refuses both at configuration time — the moment to refuse
a mistake whose whole effect is to make a quorum look larger than it is.

The fingerprint compares normalised URLs and nothing more. It cannot show that two vendors are
organisationally independent, and does not claim to; **the independence of the configured
endpoint set stays an OPERATIONAL assumption**, now stated in `OPEN_ITEMS.md`.

### B — identity was printed, not enforced

`CtfReader.identify()` checked the right things — chain id 137, CTF bytecode present, pUSD
bytecode present, pUSD `decimals() == 6`. The runner then printed the result and **built readers
from every endpoint anyway**, so a provider that failed those checks could still produce a
reading that counted.

**Now:** `ProviderAttestation` carries what an endpoint proved, `AttestedProvider` is the only
way to obtain one, and `attest_all` is the only way to obtain that. A reading whose attestation
is missing or invalid is refused with `PROVIDER_NOT_ATTESTED`.

Refused rather than silently dropped, deliberately: dropping it would turn a wiring bug into a
smaller-than-configured quorum that still looked like consensus. The *runner* excludes untrusted
endpoints so they never produce a reading at all, and refuses to start if fewer than the
configured number pass.

### C — a moving-tag fallback defeated the atomic read

```
at = block_tag if block_number is None else hex(block_number)
```

Exactly when the block lookup fails — when the provider is least reliable — the payout calls
went back to the moving tag, reintroducing the incoherent `(block, payout)` pairing that the
atomicity fix above existed to remove.

**Now:** no concrete block, no reading. The provider raises and contributes nothing. Proven by a
scripted transport, because proving a refusal means proving a call that never happens: with
`eth_getBlockByNumber` returning `None`, **zero** `eth_call` requests follow.

### Also fixed

* **Finality policy was decorative.** `ResolutionDecision.block_tag` came from
  `provider_readings[0]`, so whichever provider happened to be first defined what the audit
  record claimed. Two providers on `latest` and one on `finalized` passed as three finalized
  confirmations. The tag now comes from the policy, and a reading taken under another rule is
  refused with `FINALITY_POLICY_MISMATCH`.
* **A RESOLVED verdict could name no block.** Agreement with nothing concrete behind it cannot
  authorise a redemption, because the audit could not say which finalized state authorised it.
  Now `MISSING_AUTHORITATIVE_BLOCK`.
* **`SettlementPolicy.confirmation_depth` was exposed and did nothing.** Removed rather than
  left as a knob with no effect. A confirmation-depth fallback can be added later with its own
  evidence if an endpoint we need ever lacks `finalized`.

  `p10-end-to-end-btc-updown-5m-1787716800.json` still records `"confirmation_depth": null`,
  because it was captured before the removal and describes the code that produced it. Recordings
  are not edited to match later code; this note is how the difference is reconciled.

## The 55-market corpus, re-run under the boundary

```
markets 55   RESOLVED 55   UP 27   DOWN 28   mismatches 0
every market: exactly 3 distinct attested providers
```

Nothing in the historical data was modified and the boundary was not weakened to keep the count.
Two things the replay states rather than glosses:

* **The attestation is contemporaneous with the replay, not the capture.** The corpus predates
  the attestation boundary, so no proof of endpoint identity exists for the moment those readings
  were taken. None was invented; the endpoints are attested now and the record says so.
* **The corpus was captured at `latest`, not `finalized`.** The replay policy names that rule.
  A `finalized` policy correctly refuses the same readings, which is asserted as its own test —
  that refusal is the finality check working.

`1rpc` fails identity for real (a vendor usage-limit error), so its 55 readings never enter the
evidence — the 26 good ones for the same reason as the 29 rate-limited ones. Demanding a quorum
of four is now unreachable on all 55 markets; before the boundary it was met on 26 of them by a
provider that had merely *answered*.

## Fresh real markets under the corrected code

Seven consecutive real `btc-updown-5m` settlements, `1787733300` → `1787735100`, all newer than
every market in the P10 evidence above.

```
attested   publicnode, drpc, quiknode-public
untrusted  1rpc — RpcError -32001, vendor usage limit; excluded, contributes nothing
distinct provider ids          3
distinct endpoint fingerprints 3
policy     finalized, minimum_agreeing_providers=3
```

| Market | States | Outcome | Quorum | Authoritative block | Tag |
| --- | --- | --- | --- | --- | --- |
| `…733300` | UNRESOLVED → RESOLVED | DOWN | 3 distinct | 92685772 | finalized |
| `…733600` | UNRESOLVED → RESOLVED | UP | 3 distinct | 92685973 | finalized |
| `…733900` | UNRESOLVED → RESOLVED | DOWN | 3 distinct | 92686173 | finalized |
| `…734200` | UNRESOLVED → RESOLVED | DOWN | 3 distinct | 92686372 | finalized |
| `…734500` | UNRESOLVED → RESOLVED | DOWN | 3 distinct | 92686573 | finalized |
| `…734800` | UNRESOLVED → RESOLVED | UP | 3 distinct | 92686773 | finalized |
| `…735100` | UNRESOLVED → RESOLVED | DOWN | 3 distinct | 92686973 | finalized |

Resolution lag 88.0–92.6 s after market end. No duplicate provider vote, no moving-tag fallback,
no false ambiguity, and six of the seven emitted no risk signal at all.

## O16 on a real market

**Provenance: `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`.** Market `btc-updown-5m-1787734200` is
real and resolved correctly to DOWN at +90.7 s. The contradiction is ours, applied to our local
copy of readings that had already arrived. **Not a venue incident.**

```
RESOLVED at +90.7s  DOWN  providers=3
injected 'provider' -> AMBIGUOUS reasons=['PROVIDER_DISAGREEMENT'] plan=False
corruption removed, fresh read -> RESOLVED  risk=HALTED  latched=['RESOLUTION_AMBIGUOUS']
reconciled -> risk=SAFE latched=[] allows_place=True
```

The ordered risk sequence, in full:

| seq | signal | state | active | latched | place |
| --- | --- | --- | --- | --- | --- |
| 1 | `RESOLUTION_SAFETY_UPDATE` | HALTED | `RESOLUTION_AMBIGUOUS` | `RESOLUTION_AMBIGUOUS` | False |
| 2 | `RESOLUTION_SAFETY_UPDATE` (flag=False) | RECOVERING | — | `RESOLUTION_AMBIGUOUS` | False |
| 3 | `RECONCILIATION_CONFIRMED` | SAFE | — | — | True |
| 4 | `RISK_EVALUATION` | SAFE | — | — | True |

Sequence 2 is the point of the change. Fresh **real** readings resolved cleanly and the generic
clearing signal took the condition away — and the halt stayed up, because the reason is latched.
Only sequence 3, an explicit statement that somebody established what happened, lifted it.

**O16: CLOSED — OPERATIONAL safety policy.** It latches. This is an engineering fail-safe
decision about what our engine should do after contradictory settlement evidence, not a claim
about the venue.

**GENUINE REAL SETTLEMENT CONTRADICTION: UNOBSERVED.** Across 76 real markets neither Polygon
nor Polymarket ever contradicted itself. Every ambiguity observed was either injected by us or
was the verifier's own defect, since fixed.

## Still not done

Unchanged by this round, and still the honest limit of P10:

* **Real own-wallet redemption: UNRUN / P14.** No transaction was sent; no wallet key exists.
* **Nonzero own-ledger settlement: UNRUN / P14.** Every ledger settled is empty.
* **`PayoutRedemption` for this bot: UNRUN / P14.**

`LIVE_TRADING_ENABLED` is `False`. `REDEMPTION_ENABLED` is `False`. Zero real venue or chain
writes were performed in this round or any before it.
