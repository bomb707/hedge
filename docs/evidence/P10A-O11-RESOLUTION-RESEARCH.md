# P10A — O11 resolution-source research

**Provenance: `REAL_PUBLIC_MARKET_DATA`.** Real Polymarket Gamma and CLOB metadata, real Polygon
on-chain state read from four independent RPC providers, real settled `btc-updown-5m` markets.
No synthetic market, generated payout, or mocked response appears anywhere in this evidence
(`ARCHITECTURE_SSOT.md` §4.4).

Read-only throughout. No credential, no key, no signature, no transaction. `LIVE_TRADING_ENABLED`
is `False` and P10A adds no write path of any kind.

**Capture date:** 2026-08-26 (UTC).

## Three things O11 must not conflate

| | What it is | What it can prove |
| --- | --- | --- |
| **Rule source** | what the market's own rules name as determining the outcome | what the outcome *ought* to be |
| **Venue metadata** | what Gamma and the CLOB report | what Polymarket's indexers currently believe |
| **On-chain settlement** | the Conditional Tokens payout vector | what a redemption can actually be paid from |

Only the third is a claim about money. A redemption pays from `payoutNumerators` over
`payoutDenominator`, so a source that disagrees with it is wrong about the thing that matters,
however authoritative it is about the thing it describes.

## Official contracts, reverified 2026-08-26

From `https://docs.polymarket.com/resources/contracts`:

| Contract | Address | Network |
| --- | --- | --- |
| **Conditional Tokens (CTF)** | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | Polygon, chain id 137 |
| CTF Exchange | `0xE111180000d2663C0091e4f400237545B87B996B` | Polygon |
| Neg Risk CTF Exchange | `0xe2222d279d744050d28e00520010520000310F59` | Polygon |
| UMA Adapter | `0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74` | Polygon |
| UMA Optimistic Oracle | `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` | Polygon |
| pUSD collateral (proxy) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | Polygon |

**A discrepancy worth recording:** the collateral token now published is **pUSD**, not the USDC
that older Polymarket examples assume. Nothing in P10A depends on it, but a redeemer written
from an archived example would use the wrong collateral address.

Selectors and topics were computed from the signatures rather than copied:

```text
payoutDenominator(bytes32)                                0xdd34de67
payoutNumerators(bytes32,uint256)                         0x0504c814
getOutcomeSlotCount(bytes32)                              0xd42dc0c2
redeemPositions(address,bytes32,bytes32,uint256[])        0x01b7037c
ConditionResolution(bytes32,address,bytes32,uint256,uint256[])
    topic0 0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894
```

`payoutDenominator(conditionId) > 0` meaning "resolved", and `payoutNumerators` being the payout
vector, were both confirmed against real conditions: every settled market in the corpus returns a
denominator of 1 and a two-slot numerator vector, and every unsettled one returns 0.

## The resolver these markets actually use — **not** the UMA adapter

This is the finding that most needed checking, because the general Polymarket documentation
describes a UMA path and it would have been easy to assume it applied here.

Reading the `ConditionResolution` log for all 55 corpus markets gives the oracle that resolved
each condition:

```text
oracle observed, 55 of 55 markets:  0x58e1745bedda7312c4cddb72618923da1b90efde
UMA Adapter per official docs:      0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74
```

They are different addresses. The resolver is a deployed contract (4,295 bytes of code) whose
`ctf()` getter returns `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` — the official Conditional
Tokens address — so it is a purpose-built CTF adapter for this market family.

**Its official name could not be verified.** It does not appear on Polymarket's current contracts
page, in any repository under the `Polymarket` GitHub organisation that a code search reaches, or
in documentation this research could fetch. PolygonScan returns HTTP 403 to automated fetches.
Third-party repositories reference the address, which is not evidence of anything official.

Recorded as: **a specialised resolver, address verified from real on-chain events, official
identity unverified.** Gamma nonetheless reports `umaResolutionStatus: "resolved"` for all 55
markets, which is a venue field name that does *not* imply the UMA oracle path was used.

## Rule source, from the markets themselves

Every market in the corpus carries, identically:

```text
resolutionSource     https://data.chain.link/streams/btc-usd-twap-60s-streams
cryptoMarketConfig   {"id": "btc-5m-twap-60", "asset": "btc", "duration": "5m",
                      "twapEnabled": true, "twapLookbackSeconds": 60}
```

55 of 55, with no variation. The 5-minute BTC series consistently names a **Chainlink BTC/USD
TWAP 60 s data stream** as its rule source.

One contradiction is recorded rather than smoothed: press coverage of the August 2026 change
states that 5-minute markets use a **30-second** averaging window. The markets' own metadata says
60 seconds, in three independent places (`resolutionSource`, `cryptoMarketConfig.id`, and
`twapLookbackSeconds`). Real market data is taken over an article.

### Direct Chainlink recomputation: **UNAVAILABLE / UNRUN**

Chainlink Data Streams is a credentialed API — its documentation has a dedicated authentication
section and requires sign-up. No unauthenticated read of the raw stream reports was available in
this environment, and `https://data.chain.link/streams/btc-usd-twap-60s-streams` returned HTTP
429 to an automated fetch.

**This research therefore did not independently recompute the winner from Chainlink data.** It
proves the markets *name* that source; it does not prove the source's arithmetic. That
distinction is preserved deliberately — and it does not block choosing the on-chain payout vector
as the authoritative final settlement state, because the two questions are separate.

## Historical corpus — 55 consecutive real settled markets

| Field | Value |
| --- | --- |
| Markets | **55** |
| Consecutive | yes — all 54 gaps are exactly 300 s |
| `T0` span | 2026-08-25T19:35:00Z → 2026-08-26T00:05:00Z (4.5 hours) |
| Outcome slot count | 2, in all 55 |
| `payoutDenominator` | 1, in all 55 |
| Payout vectors observed | `[1,0]` ×27, `[0,1]` ×28 |
| Non-binary / fractional | **0** |
| `negRisk` | false, in all 55 |
| `automaticallyResolved` | true, in all 55 |
| `ConditionResolution` log found | 55 of 55 |
| Raw data | `p10a-o11-historical.json` (193 KB), analysis `p10a-o11-agreement.json` |

### Outcome-index mapping — proven in both directions

Not assumed from memory. For each market, the slot the chain pays is compared against the label
Gamma gives that index:

```text
SLOT_0 = "Up"     27 markets
SLOT_1 = "Down"   28 markets
```

Both directions are represented, both are unanimous, and there is no market in which the mapping
differs. `clobTokenIds[0]` is the Up token and `clobTokenIds[1]` the Down token, in the same order
as `outcomes`.

### Agreement matrix

Compared against the **final on-chain CTF payout vector**. Missing is counted as missing and
never as agreement.

| Source | Available | Agree | Disagree | Ambiguous | Missing |
| --- | ---: | ---: | ---: | ---: | ---: |
| Gamma `outcomePrices` | 55 | **55** | 0 | 0 | 0 |
| CLOB `tokens[].winner` | 55 | **55** | 0 | 0 | 0 |
| CTF payout vector | 55 | — (this is the reference) | — | — | — |

No candidate source disagreed with the final on-chain state anywhere in the corpus.

### Independent RPC verification

| Provider | Endpoint |
| --- | --- |
| publicnode | `https://polygon-bor-rpc.publicnode.com` |
| drpc | `https://polygon.drpc.org` |
| quiknode-public | `https://rpc-mainnet.matic.quiknode.pro` |
| 1rpc | `https://1rpc.io/matic` |

| Metric | Value |
| --- | ---: |
| Markets where every answering provider agreed | **55 of 55** |
| Markets with any provider disagreement | **0** |
| Minimum providers answering per market | **3** |
| Markets with 4 providers answering | 26 |
| Markets with 3 providers answering | 29 |

The 29 shortfalls are all `1rpc` rate-limiting this research (`-32005 Rate limit exceeded`), which
is our load, not a chain disagreement. A provider that did not answer is recorded as absent, never
as concurring. Every market still had at least three independent confirmations.

### On-chain resolution timing, from block timestamps

Seconds between a market's scheduled end (`T0 + 300`) and the block containing its
`ConditionResolution`:

| n | min | p50 | p90 | max |
| ---: | ---: | ---: | ---: | ---: |
| 55 | 52 | **85** | 88 | 172 |

## Live time-to-availability — 6 consecutive real settlements

Historical snapshots cannot answer which source becomes final *first*: an hour later they all
look the same. So six consecutive markets were followed from before they ended until the chain
reported a payout, on one synchronized local clock (`time.time()`), polling each source once per
second and continuing for 120 s after the chain event so that a slower source could still be
caught.

| Market | Payout | CTF available at |
| --- | --- | ---: |
| `btc-updown-5m-1787705700` | `[0,1]` Down | **+86.6 s** |
| `btc-updown-5m-1787706000` | `[1,0]` Up | **+86.3 s** |
| `btc-updown-5m-1787706300` | `[1,0]` Up | **+85.6 s** |
| `btc-updown-5m-1787706600` | `[0,1]` Down | **+54.3 s** |
| `btc-updown-5m-1787706900` | `[1,0]` Up | **+85.4 s** |
| `btc-updown-5m-1787707200` | `[1,0]` Up | **+84.9 s** |

Seconds after each market's scheduled end (`T0 + 300`). All six `T0` gaps are exactly 300 s, so
the markets are consecutive.

| Source | n | min | p50 | max |
| --- | ---: | ---: | ---: | ---: |
| `payoutDenominator > 0` (CTF) | 6 | 54.3 s | **85.6 s** | 86.6 s |
| Gamma `closed` | **0 observed** | — | — | — |
| Gamma `outcomePrices` | **0 observed** | — | — | — |
| CLOB `tokens[].winner` | **0 observed** | — | — | — |

**In every one of the six markets, Gamma and the CLOB had still not reflected the outcome by the
end of the observation window** — roughly 206 s after the market ended, and roughly 120 s after
the chain had already paid out. They are recorded as *not observed within the window*, never as
"agreed" or "arrived at the same time".

The historical corpus shows they do catch up: at 1–4 hours old, all 55 markets agree. So the
venue metadata is accurate but late, by more than two minutes.

This makes the usual trade-off disappear. The source that is authoritative about redemption is
also the earliest available, so there is nothing to buy by preferring a faster one — and the
strategy has already stopped quoting and is holding balances by then anyway (Canonical §18), so
there is no pressure to guess.

## Finality — measured, not assumed

Polygon's `finalized` block was compared against `latest` on three providers:

| Provider | head − finalized (blocks) | (seconds) |
| --- | ---: | ---: |
| publicnode | 4 | 6 |
| drpc | 1 | 1 |
| quiknode-public | 2 | 3 |

Consistent with Polygon's post-Rio near-instant finality. **No Polymarket-specified confirmation
depth was found**, so the confirmation policy stays `OPERATIONAL` and configurable — a separate
decision from which *source* is authoritative, and one P10 must expose rather than hard-code.

## Non-binary payouts are possible and must stay representable

No market in this corpus produced a fractional or structurally unexpected payout: all 55 are
denominator 1 with a single non-zero slot out of two. That is an observation about 55 markets, not
a property of the contract. The Conditional Tokens framework permits fractional payout vectors,
multiple non-zero slots, and outcome slot counts other than two.

P10's future `ResolutionVerifier` must therefore treat anything that is not exactly one non-zero
slot summing to the denominator as an **explicit ambiguous branch**, not force it into UP or DOWN.
The analysis tool already classifies these cases separately (`FRACTIONAL_OR_TIED`,
`NON_BINARY_SLOTS_n`, `UNEXPECTED_DENOMINATOR`) so that a future occurrence is counted rather than
absorbed.

## Candidate evaluation

| Question | Gamma `outcomePrices` | CLOB `winner` | CTF payout vector |
| --- | --- | --- | --- |
| Ever disagreed with final CTF? | no (0/55) | no (0/55) | — |
| Can be absent or stale? | **yes** | **yes** | no once resolved |
| Becomes available earlier? | **no** — not observed within 206 s in 6/6 | **no** — same | **yes**, p50 +85.6 s |
| Proves redemption is possible? | **no** | **no** | **yes** |
| Can represent a non-binary payout? | no | no | **yes** |
| Public, read-only, reproducible? | yes | yes | yes, from any RPC |

## Reproducing

```bash
.venv/bin/python tools/o11_research.py --count 55 --out <historical.json>
.venv/bin/python tools/o11_live_timing.py --markets 6 --out <live-timing.json>
.venv/bin/python tools/o11_analyze.py <historical.json> --out <agreement.json>
```

Polling is bounded: 2 s between markets in the historical collector, 1 s per source while a market
is settling in the live study, stopping as soon as the chain reports a payout. All three are
read-only; none can reach a venue write endpoint or a signing path.

## O11 verdict

All fifteen closure requirements are met, so **O11 closes** with an explicit three-way
distinction rather than a single "source":

```text
AUTHORITATIVE FINAL   Conditional Tokens payout vector on Polygon
                      0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
                      payoutDenominator(conditionId) > 0 gates redeemability;
                      payoutNumerators is the payout. Nothing else can authorise
                      a redemption, because nothing else is what pays.

ADVISORY CROSS-CHECK  Gamma outcomePrices, CLOB tokens[].winner
                      55/55 agreement, 0 disagreements, but >2 minutes late and
                      capable of being absent. Never sufficient on its own; a
                      disagreement with the chain is a fault, not a tiebreak.

RULE SOURCE           Chainlink BTC/USD TWAP 60 s data stream, as named by the
                      markets themselves in 55/55. Describes what the outcome
                      ought to be. NOT independently recomputed here - the API is
                      credentialed - so it is a documented rule, not a verified
                      calculation.

PRE-ON-CHAIN          Unresolved. payoutDenominator == 0 means not yet
                      authoritative for redemption, whatever any venue says.
```

### Ambiguity rule — fail closed

Resolution is `AMBIGUOUS`, and no redemption is authorised, if any of:

* an advisory source names a different winner than the payout vector;
* two RPC providers disagree about the payout state at comparable finality;
* the payout vector is not exactly one non-zero slot summing to the denominator — including
  fractional payouts, ties, and outcome slot counts other than two;
* the outcome slot count or token mapping does not match the market's metadata.

`AMBIGUOUS` feeds P9's existing `RESOLUTION_AMBIGUOUS` kill switch, which halts rather than
guesses. There is no else-branch that picks a winner.

### What remains OPERATIONAL

Confirmation depth. No Polymarket-specified requirement was found, and measured finality lag was
1–4 blocks, so P10 must expose it as configuration rather than hard-code a number. The
authoritative-source decision and the confirmation-depth policy are separate choices.

### What this does not claim

* It does not close **O14** (strike/start-price chaining). The `twapLookbackSeconds: 60`
  observation is suggestive and is *not* the evidence O14 requires.
* It does not prove Chainlink's arithmetic — see the UNRUN note above.
* It does not identify the resolver contract's official name.
* It does not make P10 implementable-and-done: this closes the prerequisite only.
