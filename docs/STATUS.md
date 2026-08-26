# Status

Compact project tracker. Update at **every accepted git boundary** — this file plus the
commit is the audit trail.

---

## LIVE TRADING: DISABLED

`maker5m.safety.LIVE_TRADING_ENABLED` is `False`. P7 adds a complete execution architecture
including a real authenticated write adapter, and **it cannot be armed**: `VenueAdapter.arm_live`
raises before any credential is read or any socket is opened.

P9 adds operational safety only, and its two real-market runs placed **zero** orders. The risk
package cannot construct an order at all — a halt empties intent, and there is no SELL, hedge,
flatten, merge, split, or convert anywhere in it.

P8 adds measurement only. The instrumented market run placed **zero** orders: no credential was
requested, no authenticated socket was opened, and the order side is a shadow simulation whose
figures are labelled `SHADOW_ESTIMATE`.

There is deliberately no `--live` flag, no `LIVE=true` environment variable, and no config key
that bypasses the constant. Every one of those would be a way to enable real trading without
the P14 review that is supposed to gate it. Unlocking requires a source edit and code review.

Asserted structurally in `tests/execution/test_safety.py`: the gate refuses and the transport
is never even constructed; no execution module reads the environment or implements a bypass
switch; secrets never appear in a repr; `SELL`, `FOK`, `FAK`, and `post_only=False` are not
representable; no `float()` exists on the order path; and no test constructs a real SDK client.

---

## P7 correction: concurrent dispatch

Independent review found that the P7 report claimed "independent UP/DOWN actions may dispatch
concurrently" while the implementation dispatched them **sequentially**. The claim was wrong,
and the test that appeared to support it —
`test_up_and_down_are_independent_so_they_may_dispatch_concurrently` — only proved the *plan*
was independent of evaluation order. It said nothing about the network calls, and would have
passed against a strictly sequential executor.

The gap was real: `records = [self._dispatch(side, now_ns) for side in plan.sides]` over
synchronous transport methods, with no async path in the codebase at all.

**Corrected.** `Executor.run_cycle_async` dispatches independent outcome requests with
`asyncio.gather` over `AsyncVenueAdapter`. The official SDK provides a genuine async client —
`AsyncSecureClient` with coroutine `create_limit_order` / `post_order` / `cancel_order` over
`httpx[http2]` — so this is real concurrency, not a blocking call wrapped in an executor.

Reservation is deliberately separated from dispatch. `Executor.reserve` allocates client order
ids, registers `PENDING_PLACE` / `PENDING_CANCEL`, and takes rate-limiter capacity
**synchronously, before any await**. A structural test asserts `reserve()` contains no
`Await` node: a suspension point in there would let a concurrent cycle observe no in-flight
request and create a duplicate.

Concurrency is across independent outcomes only. Within one side replacement remains
`CANCEL_THEN_PLACE`, and completion order cannot influence replay — authenticated order and
fill events re-enter through the P6 ingress merger and receive their ordinal there.

**The proof is discriminating.** Temporarily replacing `asyncio.gather` with a sequential loop
makes three barrier tests fail with `TimeoutError`; restoring it makes them pass. The barrier
holds each request inside the transport until the other side enters, so a sequential
implementation deadlocks rather than merely running slowly — no wall-clock timing is involved.

The synchronous `run_cycle` is retained for unit tests and is documented as test support, not
production.

---

## P10A: O11 closed from real settlement evidence

**P10 itself is NOT implemented.** This round closed the prerequisite only — no `Redeemer`, no
`ResolutionVerifier`, no wallet, no key, no transaction. Evidence:
[`evidence/P10A-O11-RESOLUTION-RESEARCH.md`](evidence/P10A-O11-RESOLUTION-RESEARCH.md).

O11 asked which source authoritatively determines a settled market's outcome. The answer keeps
three things apart that are easy to collapse into one:

```text
AUTHORITATIVE FINAL   CTF payout vector on Polygon, 0x4D97DCd9...0476045
                      payoutDenominator > 0 gates redeemability
ADVISORY CROSS-CHECK  Gamma outcomePrices, CLOB tokens[].winner
RULE SOURCE           Chainlink BTC/USD TWAP 60 s, named by the markets themselves
PRE-ON-CHAIN          denominator == 0 is not yet authoritative, whatever a venue says
```

### The evidence

55 **consecutive** real settled markets over 4.5 hours, plus 6 **consecutive** settlements watched
live, plus four independent Polygon RPC providers. No synthetic data.

```text
agreement with final CTF payout   Gamma 55/55    CLOB 55/55    disagreements 0    missing 0
outcome-index mapping             slot 0 = Up (27)   slot 1 = Down (28)   unanimous
RPC agreement                     55/55, minimum 3 independent providers per market
payout shapes observed            [1,0] x27, [0,1] x28 - no fractional, no non-binary
on-chain resolution after end      p50 85 s (min 52, max 172) from block timestamps
live CTF availability after end    p50 +85.6 s (min 54.3, max 86.6)
live Gamma / CLOB availability     NOT observed within ~206 s in 6 of 6 markets
Polygon finality lag               1-4 blocks (1-6 s), measured across 3 providers
```

The chain is not only authoritative but **earliest**, by more than two minutes. The usual
speed-versus-correctness trade-off does not arise, and the strategy has already stopped quoting
and is holding balances by then (Canonical §18) so there is nothing to gain by guessing.

### The finding that most needed checking

These markets do **not** use the UMA adapter. Reading `ConditionResolution` for all 55 gives
oracle `0x58e1745bedda7312c4cddb72618923da1b90efde`, against the officially documented UMA
Adapter `0x6A9D2226…` — a different address. It is a real deployed contract whose `ctf()` returns
the official Conditional Tokens address, so it is a purpose-built adapter for this market family.
Its official **name could not be verified** and is recorded as unverified rather than guessed.
Gamma still reports `umaResolutionStatus: "resolved"`, which is a field name and not evidence of
the UMA path.

### Stated honestly rather than assumed

* **Chainlink raw recomputation: UNAVAILABLE / UNRUN.** Data Streams is a credentialed API. The
  research proves the markets *name* that source; it does not prove the source's arithmetic.
* **Confirmation depth stays `OPERATIONAL`** — no Polymarket requirement was found, so P10 must
  expose it rather than hard-code a number.
* **Non-binary payouts stay representable.** None occurred in 55 markets, which is an observation
  about 55 markets and not a property of the contract. Anything that is not exactly one non-zero
  slot summing to the denominator is an explicit ambiguous branch.
* **O14 is not closed.** `twapLookbackSeconds: 60` is suggestive and is not O14's evidence.

### Two flaws in the research method, found and fixed

The live study originally **stopped polling at the chain event**, so only sources that beat the
chain could ever have been observed — the question would have been answered by the method rather
than the data. It now follows for 120 s afterwards.

The RPC agreement count originally treated a **rate-limited provider as part of the agreeing
set**. Providers that did not answer are now recorded as absent, and the minimum independent
confirmation count is reported.

---

## P9C: the risk sequence had to be proved, not just used

Independent review accepted everything about P9B except one narrow audit-integrity defect in the
replay verifier. Evidence:
[`evidence/P9C-RISK-AUDIT-CLOSURE.md`](evidence/P9C-RISK-AUDIT-CLOSURE.md).

### The defect

The verifier addressed records *by* their risk sequence without ever verifying it.

```python
expected = records[0].risk_sequence if records else 0
```

The expectation came from the data, so a trace whose prefix had been lost — `3, 4, 5` — verified
as "internally contiguous". The scan only looked for values that skipped ahead, so `0, 1, 1, 2`
and `0, 1, 2, 1` passed too. And `verify_risk_replay` checked `state`, `active`, `latched`,
`allows_place`, and `allows_cancel` against each record while never comparing the sequence it
produced against the sequence recorded.

### The contract now

**`record[i].risk_sequence == i`**, positional and absolute, checked before any replay. The
expectation is not inferred from the file: a trace starting at 5 is a trace whose first five
permission decisions are unaccounted for. `produced.risk_sequence` is then compared to
`recorded.risk_sequence` alongside the verdict fields.

Partial replay stays unsupported — it would need an explicit initial sequence *and* an explicit
initial `RiskSnapshot`, since a tail without the state it inherited proves nothing.

A bounded `RiskTrace` that has **dropped** records therefore cannot verify. That is correct:
drop-oldest is right for the hot path, and a dropped trace is **audit incomplete**. Trading may
continue under the existing safety policy; the evidence may not claim deterministic full-risk
replay. Nothing renumbers the tail or invents the state it inherited.

### Malformed traces, all rejected

```text
valid            0,1,2,3…      PASS
lost prefix      3,4,5…        FAIL  expected 0, actual 3
missing middle   0,1,3,4…      FAIL  expected 2, actual 3
duplicate        0,1,1,2…      FAIL  expected 2, actual 1
backwards        0,1,2,1…      FAIL  expected 3, actual 1
shifted          100,101,102…  FAIL  expected 0, actual 100
tampered index   verdicts OK   FAIL  on risk_sequence
overflowed trace cap 16/100    FAIL  expected 0, actual 84
```

The tests discriminate: restoring the previous verifier fails seven of them. Two further tests
pin what must not change — several distinct risk sequences may legally share one
`as_of_ingress_ordinal`, and the out-of-order and reconciliation rules are untouched.

### Fresh real market

`btc-updown-5m-1787692200`, `CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`, 159,952 cycles, 155,549
CLOB and 7,548 BTC messages, **0 orders sent**.

```text
first risk sequence        0
last risk sequence   159,972
record count         159,973      distinct 159,973
duplicates 0    gaps 0    dropped 0    contiguous_from_zero true
verify_risk_replay   PASS
non-evaluation signals    18

11 halts, 33 transitions, EVERY one through RECOVERING
PLACE  473, ALL in SAFE.  Zero while HALTED, zero while RECOVERING.
CANCEL 361 in SAFE, 3 while HALTED   keep_ratio 0.99534
```

The passing replay is itself the proof of integrity: a trace that started late, skipped,
repeated, or went backwards could not have reached that line.

Risk behaviour is unchanged — the change is confined to `risk/replay.py`. The P9B market
findings stand; only P9B's *sequence-integrity claim* is superseded, and both P9B traces were in
fact complete and contiguous from zero, which the verifier of the day simply could not prove.

---

## P9B: staleness ownership and the ordered risk audit

Independent review accepted the risk model, the overlay, the halt semantics, the real-market
evidence, and the `STALE`-recovery defect it found — then identified two architectural gaps.
Evidence: [`evidence/P9B-REAL-MARKET-BASELINE.md`](evidence/P9B-REAL-MARKET-BASELINE.md) and
[`evidence/P9B-REAL-MARKET-FAULTS.md`](evidence/P9B-REAL-MARKET-FAULTS.md).

### Gap A — two authorities for one question

`RiskEngine` carried its own `_stale(last_at, now, threshold)` against its own copies of
`DEFAULT_CLOB_STALE_AFTER` and `DEFAULT_SPOT_STALE_AFTER`, while P6 already owned
`StalenessMonitor`, `StreamHealth`, and the `STALE` transition. Two answers to "has this stream
been quiet too long?" can disagree, and the wrong one stays invisible until it matters.

P6 now owns it entirely. P9 reads `HealthStatus` and decides what to do about it. Removed rather
than deprecated: `_stale`, both `last_message_at` inputs, and both thresholds — without the
inputs the comparison cannot come back by accident. Structural tests assert `risk/` imports no
staleness constant, builds no monitor, holds no last-message timestamp, and compares no age
against a limit; and that changing P6's threshold changes when `STALE` fires with no `RiskConfig`
edit at all.

Detection did not become lazy. `pipeline.check_staleness(now)` already ran on three capture-loop
paths — the queue-idle timeout, the fault-gated path, and after each payload — all before
`on_tick`, so silence is noticed without waiting for a market event. Proven live: the real BTC
pause halted **4,083 ms** after injection, which is P6's 5-second threshold.

### Gap B — permission could change with no ordered record

`RiskEngine.reconciled` mutated the latched snapshot directly and the fault scheduler called it.
Clock drift, API error rate, rate-limit uncertainty, and every reconciliation result could flip
`allows_place` without entering any ordered stream. A run could be replayed for its economics and
not for its permissions.

Risk now has its own versioned stream, **beside** the P5 journal and never inside it:

```text
RiskSignal      kind, as_of_ingress_ordinal, timestamp, provenance, reason, payload
RiskRecord      schema version, strict risk_sequence, the signal, the health frame it saw,
                and the resulting state / active / latched / allows_place / allows_cancel
RiskController  the single owner of risk state, and the only path that may change it
```

`as_of_ingress_ordinal` means: the signal was applied after every market event through that
ordinal had been consumed, and before the next permission decision referencing this risk
sequence. Nothing depends on coroutine scheduling. Feed health is not duplicated — `HealthEvent`
already exists in the market stream.

Order is enforced: a signal preceding the last applied ordinal is refused, and a
`RECONCILIATION_CONFIRMED` for a reason that is not latched is refused as a duplicate or a claim
about something never in doubt. A structural test asserts nothing outside the controller calls
`engine.reconciled`.

`verify_risk_replay` re-derives every verdict and fails closed at the first divergence, naming
the risk sequence, the ingress ordinal, and both values. A sequence gap fails immediately: an
audit missing records cannot explain the cycles it lost.

### Fresh real-market evidence

Baseline `btc-updown-5m-1787678100` — 153,082 cycles, 152,738 CLOB and 3,196 BTC messages, 0
malformed, 0 reconnects.

```text
risk records 153,082   dropped 0   gaps 0   REPLAY VERIFIED
SAFE 152,914 (99.89%)  HALTED 167 (the explained T0 spot-UNKNOWN halt)  RECOVERING 1
PLACE 679, ALL in SAFE     1,217 actions each attributed to a risk sequence
```

Faults `btc-updown-5m-1787679300` — 162,644 cycles, 12 halts, **36 transitions, every one
through RECOVERING**.

```text
risk records 162,666   non-evaluation signals 18   dropped 0   gaps 0   REPLAY VERIFIED
PLACE  805, ALL in SAFE.  Zero while HALTED, zero while RECOVERING, across 29,401 halted cycles.
CANCEL 693 in SAFE, 9 while HALTED  -- resting quotes withdrawn.

btc_stale             halt seq  24,857   4,083 ms reaction (P6's threshold)   SAFE seq  34,268
clob_disconnect       halt seq  61,862      24 ms                             SAFE seq  61,865
genuine venue drop    halt seq  84,498   real incident, not induced           SAFE seq  84,502
continuity_uncertain  halt seq  99,983       0 ms                             SAFE seq  99,987
clock_drift           halt seq 111,105       0 ms                             SAFE seq 112,983
order_state_uncertain halt seq 120,352  LATCHED                               SAFE seq 124,971
position_mismatch     halt seq 128,786  LATCHED                               SAFE seq 131,556
cost_ledger_mismatch  halt seq 137,119  LATCHED                               SAFE seq 141,957
api_error_rate        halt seq 147,791       0 ms                             SAFE seq 150,779
rate_limit_uncertain  halt seq 155,892       0 ms                             SAFE seq 158,117
resolution_ambiguous  halt seq 159,993       0 ms                             SAFE seq 160,662
```

The three latching reasons show the required three-step in the trace: the condition clears and
**permission does not return** — the state goes to `RECOVERING` with the reason still latched —
and only the explicit `RECONCILIATION_CONFIRMED` signal restores `SAFE`.

**One halt was a genuine venue incident**, not induced: the feed counters record two reconnects
where only one was forced. It is labelled as such rather than folded into the induced count, and
it recovered in 1,071 ms by the same path.

### Two defects these runs found

The first P9B fault run halted on `API_ERROR_RATE` for **0 ms**: the injector forced the
condition on and the real `ApiErrorMonitor` cleared it on the next evaluation. Two sources
writing one condition. Fixed in the injector; the corrected run holds it for 3,907 ms. **The
ordered trace is what made it visible** — two opposite `API_ERROR_STATE_UPDATE` records one
sequence apart.

Measuring the ordered path also found the third frozen-dataclass cost in this codebase.
`RiskInputs` alone cost 1,907 ns to construct; as a `NamedTuple` the whole path went **6,390 →
4,728 ns**. P8 found the same thing twice.

---

## P9: risk, health, and recovery (first round — read with the correction above)

The governing rule, from Canonical §28 and Detailed §38: **if the bot cannot trust its own
state, stop new quoting, reconcile, and resume only when safe.** Detailed §38 closes with the
sentence the whole phase turns on — *accuracy is more important than continuing to trade*.

Evidence: [`evidence/P9-REAL-MARKET-BASELINE.md`](evidence/P9-REAL-MARKET-BASELINE.md) and
[`evidence/P9-REAL-MARKET-FAULTS.md`](evidence/P9-REAL-MARKET-FAULTS.md).

### Three gates, deliberately not merged into one word

```text
implementation          PASSED    all twelve conditions exist, trip, and recover
real-market integration PASSED    one healthy live market + one live market with
                                  controlled local faults
authenticated execution UNRUN / DEFERRED TO P14
```

The third covers taker fill, real order uncertainty, real account position and cost
reconciliation, and real write-API behaviour. None of it can be claimed: no credential exists,
no authenticated socket has been opened, and no order of any size has been sent. The mechanisms
are implemented and unit-tested; their **empirical** status is unrun, and a mock result would
not change that.

### What it is, and three things it is not

`RiskState` is `SAFE` / `HALTED` / `RECOVERING` — one verdict carrying its typed reasons, not a
bag of unrelated booleans. Twelve reasons: Canonical §28.1's eleven plus Detailed §38's taker
fill. A fresh engine starts `RECOVERING`, because before anything is observed permission must
not be the default.

* **Not a stop-loss.** Canonical §28 opens by saying the target strategy does not use one. No
  SELL, hedge, flatten, merge, split, convert, or directional rescue exists in the package —
  asserted structurally over function, class, and attribute names. A halt withdraws quotes and
  **holds the balances** (I15).
* **Not `band_hard`.** P4 owns the one-sided wall and it stays a wall: at `I >= +band_hard` UP
  is blocked while DOWN remains a legitimate order (I17).
* **Not part of the strategy.** `StrategyEngine.decide` is untouched and has no stale-feed,
  drift, API-error, or order-state branch. A healthy verdict returns the **identical**
  `DecisionResult` object, not an equal copy.

### Halt and recovery semantics

A halt turns desired intent into *nothing*, which is exactly what P7's minimal-action
reconciler needs to plan `CANCEL` for anything resting — withdrawal falls out of the existing
rule rather than needing a code path that knows how to retreat. `CANCEL` is permitted
throughout; `PLACE` and the placement half of `CANCEL_THEN_PLACE` are not. An `UNKNOWN` order is
`WAIT`ed on, never cancelled again and never replaced.

`HALTED → RECOVERING → SAFE`, always. `SAFE` needs no active condition, nothing latched, **and**
two consecutive clear evaluations. Unknown order state, position mismatch, cost-ledger
mismatch, and taker fill latch past their own condition: an unknown order does not become known
because a socket reconnected. A CLOB reconnect alone never restores `SAFE` — the condition
clears only when P6 reports `HEALTHY` and is no longer awaiting a snapshot.

### Real market: baseline

`btc-updown-5m-1787672100`, 154,882 cycles, 148,204 CLOB and 9,777 BTC messages, 0 malformed,
0 reconnects, 0 observation drops, **0 orders**.

```text
SAFE       154,876 cycles   99.996%
HALTED           5 cycles   one halt, at T0, explained below
RECOVERING       1 cycle
PLACE          399, ALL in SAFE          keep_ratio 0.99608
RiskEngine.evaluate  p50 9,549 ns   p99 59,052 ns
```

The single halt is `SPOT_STALE` at ingress ordinal 2 and is **correct, not a false positive**:
during pre-arm, spot payloads are parsed for precision but deliberately not routed through
`on_spot`, so at `T0` the bot has not yet seen a BTC price through its own pipeline and the feed
status is genuinely `UNKNOWN`. Refusing to quote a BTC-referenced market before seeing a BTC
price is the behaviour the engine is supposed to have. It cleared in 457 ms.

### Real market: controlled local faults

`btc-updown-5m-1787674200`, 112,958 cycles. Market data real throughout; the faults are induced
**local** failures and are labelled as such, never as venue incidents.

```text
fault                  inject ord   halt ord   reaction   reason                       SAFE ord
btc_stale                  28,087     30,822   4,754 ms   SPOT_STALE                     41,265
clob_disconnect            68,519     68,521      25 ms   CLOB_CONTINUITY_UNCERTAIN      68,525
continuity_uncertain       96,129     96,130       0 ms   CLOB_CONTINUITY_UNCERTAIN      96,133
clock_drift               101,594    101,594       0 ms   CLOCK_DRIFT                   102,688
order_state_uncertain     104,451    104,451       0 ms   ORDER_STATE_UNCERTAIN         105,365
position_mismatch         106,331    106,331       0 ms   POSITION_MISMATCH             107,388
cost_ledger_mismatch      108,999    108,999       0 ms   COST_LEDGER_MISMATCH          109,430
api_error_rate            110,339    110,340      16 ms   API_ERROR_RATE                110,809
rate_limit_uncertain      111,761    111,761       0 ms   RATE_LIMIT_UNCERTAIN          112,142
resolution_ambiguous      112,899    112,899       0 ms   RESOLUTION_AMBIGUOUS          113,258

33 transitions, EVERY one through RECOVERING. No direct HALTED -> SAFE anywhere.
PLACE  245, ALL in SAFE      0 while HALTED      0 while RECOVERING
CANCEL 175 in SAFE, 2 while HALTED, 1 while RECOVERING   -- resting quotes withdrawn
```

The 4,754 ms reaction to `btc_stale` **is** the 5 s staleness threshold, not a delay; halting
sooner would mean halting on ordinary quiet periods. The 723 ms `clob_disconnect` halt is the
genuine reconnect round trip — socket close, backoff, reconnect, resubscribe, fresh
authoritative snapshot — and `SAFE` was unreachable for its whole duration.

`ORDER_STATE_UNCERTAIN`, `POSITION_MISMATCH`, and `COST_LEDGER_MISMATCH` visibly stayed
**latched** through `RECOVERING` and only cleared when `RiskEngine.reconciled` was called
explicitly. Releasing the signal alone did not restore `SAFE`, which is the point.

**Two conditions were not induced and are not claimed.** `TAKER_FILL` is **UNRUN / DEFERRED TO
P14** — no order was sent, so no fill occurred. `MAKER_ONLY_UNCERTAIN` has supporting unit tests
only. `CLOB_STALE` was never reached because the CLOB never went quiet for 10 s; the same
staleness code path was exercised by `SPOT_STALE`.

### The defect fault injection found

The **first** fault run halted correctly on `btc_stale` and then never recovered, staying
`HALTED` for 132,717 of 160,917 cycles and masking the two later faults entirely.
`StreamHealth` had no path out of `STALE`: `mark_message` updated only the timestamp, and
`mark_snapshot` was reachable only while `awaiting_snapshot` was set — which `STALE` does not
set. One quiet BTC feed would have halted the bot for the rest of a market.

Fixed in `1584dee`. `DISCONNECTED` and `SEQUENCE_GAP` are deliberately still not cleared by a
message, because they set `awaiting_snapshot` and one message after a continuity break says
nothing about the messages that were missed.

**A green unit-test suite did not catch this.** It appeared the moment a real adapter was paused
during a real market, which is precisely the case for the evidence policy now recorded in
[`ARCHITECTURE_SSOT.md`](ARCHITECTURE_SSOT.md) §4.4.

---

## P8C: the telemetry offload

P8B's measurement was correct and synchronous, and that was the problem: every cycle mutated
shadow queue slots, counted actions, classified both sides, and updated distributions before the
trading loop could continue. **+4,902 ns (+15.1%)** on an ordinary unsampled book update, for
work no trading decision depends on. That gate was reported as failed rather than waived.

Full evidence: [`evidence/P8C-PERFORMANCE-CLOSURE.md`](evidence/P8C-PERFORMANCE-CLOSURE.md).

### Observation is now split in two

The trading path captures *facts* — the displayed depth at our own price, the reconcile plan,
stage timestamps for sampled cycles — into a bounded non-blocking buffer, and returns.
`TelemetryAnalyzer` reconstructs queue estimates, classification, counters, and distributions
downstream, in ingress order, after the market.

The split is at **analysis**, not at **simulation**. Preparation, reconciliation, and the shadow
order-table lifecycle model what production does every cycle, so they stay hot and run in both
benchmark arms. Charging them to telemetry is exactly the error that produced P8's earlier +133%
and +217% figures. Depth cannot move either: the book is mutable, and the size at our own price
has to be read at the moment the cycle sees it.

Representation was measured, not assumed — a tuple of references costs 76 ns against 1,791 ns
for the "clean" frozen dataclass, and `deque.append` beat a hand-rolled ring.

### Order is authoritative, gaps are not bridged

Observations carry a capture sequence and are folded strictly in order. Out-of-order input
**fails closed**; a sequence gap means an unseen depth change at our own price, so the estimate
goes `STALE` rather than being continued. Trading is unaffected by a telemetry drop — the loss
is in observation, not execution — but the measurement says so.

### Sampling now prevents timing work, not just output

The decision is made before reduce and decide, so an unsampled ordinary event takes no
perf-counter readings at all. An action discovered after reconciliation is still recorded, with
one reading for the action and its earlier stages left `NOT_CAPTURED`. Nothing is imputed.

### Equivalence proven, not assumed

The offloaded pipeline reproduces the synchronous model **exactly** — slot counts, typed loss
reasons, every action and quality counter, and the complete 220-element ordered queue-ahead
sequence — checked against a golden snapshot taken by running the same tool inside a worktree at
`c5cec7f`. Frozen at `tests/telemetry/golden/synchronous_queue_semantics.json`.

### Limits met, without moving them

```text
                                      target        P8B        P8C
unsampled full-cycle p50 overhead   <= 5,000 ns    4,902        955   MET
unsampled full-cycle p50 overhead   <= 5%          15.1%       2.9%   MET
decide p50 (process-isolated)       <= 1,000 ns    1,968*       454   MET
decide p50 (process-isolated)       <= 3%           7.7%*     1.73%   MET
```

`*` P8B's decide figures were same-process and therefore contaminated by allocator and cache
pressure; they are shown for continuity, not as a comparable measurement. P8C runs each
configuration in a fresh interpreter across twelve alternating pairs.

Hot-path capture is now **347 ns** on an unsampled cycle, from ~1,970 ns. Sampled cycles
(+1,360 ns, +4.1%) legitimately cost more and are reported separately. The p99 tail is
GC-dominated — disabling GC halved the instrumented p99 delta — and that is stated rather than
smoothed away.

### Confirmed on a full real market

`btc-updown-5m-1787663400`, **204,440 cycles**, `live_trading_enabled: false`, **0 orders
sent**, **0 observation drops**, **0 gaps**, 0 malformed feed messages, 0 reconnects.

```text
observations captured / capacity   204,440 / 320,000     zero dropped, zero lost
cycles with stage timing            20,440 = 10.0%       sampling is 1-in-10
keep_ratio                         0.99259
shadow slots acquired                1,500               = PLACE actions exactly
shadow slot losses                   1,500               reconciles to typed reasons
reconcile_duration p50              11,154 ns            O15 holds; smallest stage
receive_to_reconcile p50           178,857 ns
queue_ahead                        p50 0, p95 166, p99 434 shares
BLOCKED                    173,215 of 408,880 sides (42.4%)
```

`OFF_PRICE` is 0 structurally — instantaneous shadow acknowledgement means no order can rest at
a stale price while a replacement is in flight. `STALE` is 0 because continuity held throughout.
The `POST_ONLY_BLOCK` finding is unchanged and still **not acted on**; no spread was introduced.

### One defective run, retained

The first P8C market produced stage timings for **126 cycles instead of ~15,400**, and nothing
raised. The sampling decision was being made twice — once on the hot path with
`meta.ingress_ordinal`, once downstream with the observation's ordinal — and `next_meta`
increments after assigning, so at `sample_every = 10` the two answers could essentially never
agree. Whether a cycle was sampled is now a captured fact rather than a recomputation, and a
regression test asserts the arithmetic that would have caught it. That run is retained as
`p8c-measurement-SUPERSEDED-*`; its queue and action results were unaffected and agree with the
corrected run.

---

## P8 correction: shadow queue lifecycle, O15, and telemetry overhead

Independent review found three closure issues in the accepted P8 work. All three were real.
The original evidence is retained and labelled, not rewritten
([`evidence/P8-MEASUREMENT.md`](evidence/P8-MEASUREMENT.md)); corrected evidence is in
[`evidence/P8B-MEASUREMENT.md`](evidence/P8B-MEASUREMENT.md).

### 1. Shadow queue slots followed desired price, not the order lifecycle

`ShadowQueueTracker.on_desired(outcome, desired_price, depth)` opened and advanced a slot
whenever the strategy *wanted* a price — including every side the reconciler had just refused
to submit. The first P8 market produced **119,116** `POST_ONLY_BLOCK` sides, so this was not a
corner case: blocked intent acquired queue estimates, aged them, banked every depth decrease at
that level as consumption ahead of it, and reported itself at the front of a queue no order had
ever joined.

Slots now follow the executable lifecycle and are keyed by **client order id**, never by
`(outcome, price)`:

```text
PLACE                    acquire, at the depth displayed immediately before dispatch
KEEP                     preserve, update from current depth
partial fill + KEEP      preserve the same identity; ahead becomes zero
CANCEL                   close
REPLACE                  close and grant nothing - P7 is CANCEL_THEN_PLACE, so the
                         replacement's slot begins only when a later cycle reaches PLACE
BLOCKED / WAIT / NOTHING no slot whatsoever
continuity loss          slot survives, confidence does not
```

`classify()` no longer takes a `resting_price` argument at all: the resting price is read from
the queue estimate, and an estimate exists only while an order holds a slot. `AT_FRONT` is
therefore structurally unreachable without a real shadow order, rather than merely unlikely.

The regression suite was checked for discrimination the same way the P7 concurrency test was:
restoring the desired-price model makes **5** of the 13 lifecycle tests fail, including the
mandatory post-only regression.

### 2. O15 — `current()` scanned all retained history

Measured, then fixed. `LiveOrderTable` now maintains an incremental per-outcome index of
occupying order ids, updated on every lifecycle transition and never rebuilt by rescanning.
History retention is untouched — it is required for idempotency — and the occupancy index sits
alongside it.

```text
retained terminal orders        0     200     1,049      10,000
before (ns per cycle)       1,311  52,097   251,406   2,512,039
after  (ns per cycle)         477     467       498         461
```

Slope: **251.0 ns per retained order before, -0.0016 after**. At 10,000 retained orders the
lookup is ~5,450x cheaper and no longer grows at all.

### 3. Instrumentation overhead was real, and was described too kindly

The previous report's +21.3% was measured by a method that had already produced three wrong
answers. It is replaced by a paired, interleaved, warmed, per-repeat, tier-split benchmark run
against a production-shaped steady-state stream as well as the replay corpus.

State maintenance and emission are now separated: shadow slot transitions and action counters
run on every cycle of a measuring run — skipping them for unsampled events would make the queue
estimate depend on the sampling rate — while stage timestamps, distributions, classification
and the sink run only for traced cycles. Removing a discarded six-field `QueueEstimate` from
`on_keep` alone was worth about 1 µs per side.

**The performance limit was not met in this round, and was reported as not met.** On an
ordinary unsampled production-shaped cycle: **+4.90 µs (+15.1%)**. The residue was the two-side
analytical state loop (1,570 ns) that maintained slot depth and action counts synchronously.
That loop no longer runs on the trading path at all — see the telemetry offload above, which
closes this gate at +955 ns (+2.9%).

### Confirmed on a fresh real market

`btc-updown-5m-1787658900`, 117,772 cycles, `live_trading_enabled: false`, **0 orders sent**,
0 telemetry drops. Full evidence: [`evidence/P8B-MEASUREMENT.md`](evidence/P8B-MEASUREMENT.md).

```text
reconcile_duration p50   171,659 ns  ->   14,882 ns    -91.3%   (O15 CLOSED)
receive_to_reconcile p50 323,138 ns  ->  240,367 ns    -25.6%
keep_ratio                 0.99339   ->    0.99568
shadow slots acquired        1,049   ->        462     = PLACE actions exactly
queue_ahead p99          249 shares  -> 367 shares     phantom-slot optimism removed
```

Reconciliation is now the **smallest** stage on the critical path, below `decide` (66,105 ns)
and `prepare` (17,764 ns). Slots acquired equalling PLACE actions exactly is the defining
property of the corrected model.

Two counts that need reading carefully: `BLOCKED` is exact over every side (**97,534 of
235,544, 41.4%**), while the quality classification is now sampled 1-in-10, so
`POST_ONLY_BLOCK` reads 10,117. The finding is unchanged and still **not acted on** — no
spread was introduced. `OFF_PRICE` is 0 for a structural reason: shadow acknowledgement is
instantaneous, so no order can rest at a stale price while a replacement is in flight. It is
unit-tested at the classifier and needs real dispatch latency (P13/P14) to occur in a run.

---

## P8: what measurement found (first run — read with the correction above)

Instrumentation only. **No strategy parameter was changed to improve any number below.**
Queue figures in this section are superseded; latency, actions, and `keep_ratio` stand.
Full evidence: [`evidence/P8-MEASUREMENT.md`](evidence/P8-MEASUREMENT.md).

Measured against one real market, `btc-updown-5m-1787652900`, 137,752 cycles,
`live_trading_enabled: false`, **0 orders sent**, 0 telemetry drops.

### The queue-preservation property holds

**`keep_ratio` = 0.99339** — 133,400 KEEPs across 134,287 cycles that had a live order. A
resting order survives unmodified through better than 99.3% of the cycles in which it exists.
That is the single most important behaviour in the system, and it is now measured on real data
rather than asserted. 1,049 slots acquired, 887 lost (510 `PRICE_CHANGED`, 377
`UNSAFE_REPLACEMENT`).

### Critical path

`receive_to_reconcile` p50 **323 µs**, p99 2.24 ms. The dominant stage is not the strategy:
`decide_duration` is p50 40 µs and `prepare_duration` p50 12 µs, while `reconcile_duration` is
p50 **172 µs**. See O15 — that is mostly an execution-layer data-structure cost, now quantified.

`real_order_rtt` is **UNRUN and deferred to P14**, not estimated. Every latency figure comes from
`time.perf_counter_ns()` alone; no exchange timestamp is ever subtracted from a local one.

### Finding: `POST_ONLY_BLOCK` on 119,116 sides

Of ~140,707 `NOT_QUOTING` sides, **119,116** were suppressed because the desired price would have
crossed or equalled the same-outcome ask. With zero synthetic spread this happens constantly.

**Reported, not acted on.** It may mean the zero-spread reading is wrong, or that the strategy
genuinely quotes only in the minority of moments when the book leaves room. Deciding between
those is a strategy question with an unresolved source conflict behind it (O01, O04); inventing a
spread to raise the quoting rate would be exactly the kind of optimization P8 forbids.

### Queue estimates are `SHADOW_ESTIMATE`, and biased

The order side is modelled, not real. `queue_ahead` p50 is 0 shares, p90 82, p99 249. Confidence
is `ESTIMATED`, `STALE`, or `UNKNOWN` — there is **no `EXACT`**, because the venue publishes no
queue index. The estimate is knowingly **optimistic**: a decrease in displayed size may include
size that joined after we did. Clamped to displayed size, otherwise uncorrected and documented.

None of this is evidence about the target wallet. It describes our strategy against real books.

### Instrumentation overhead

Deterministic benchmark, 1,560 cycles per configuration: `decide_ns` p50 **+1,761 ns (+6.4%)**,
`cycle_ns` p50 **+20,346 ns (+21.3%)**. Sampling is every 10th event, with fills, order states,
phase changes, and health events always traced — sampling reduces telemetry volume and is never
used to hide a latency figure.

Three earlier overhead numbers were wrong (+133%, +49%, +217%) and all three are retained in the
evidence manifest with the reason each was wrong. Two came from charging production work to
instrumentation; one from profiling that found per-call dict literals. None was visible by
reading the source.

### O08 / O09 remain OPEN

The latency distribution is now *measurable*, which is what P8 owed. It does not by itself
establish whether latency or queue position dominates fill probability — that needs real resting
orders, which P8 does not place. Both stay OPEN.

---

## Two different kinds of "done"

| | |
|---|---|
| **Implementation gate** | The code does what this phase specified, proven by tests. |
| **Empirical replication correctness** | The code does what the *target wallet* did. Only replay against the wallet's own history can establish that. |

---

## Current position

| | |
|---|---|
| **Current phase** | **P9 — risk / health / recovery, corrected** |
| P9 implementation gate | **PASSED** |
| P9 real-market integration gate | **PASSED** — fresh baseline + controlled-fault markets |
| P9 deterministic-risk-audit gate | **PASSED** — exact sequence contract, replay verified on real data |
| P9 authenticated execution gate | **UNRUN / DEFERRED TO P14** |
| P8 implementation gate | **PASSED** |
| P8 performance gate | **PASSED** — see the telemetry offload below |
| P7 implementation gate | **PASSED** (with the concurrency correction below) |
| Live execution | **NOT ARMED and not armable** — P14 owns that |
| Target-wallet empirical replay | **UNRUN / BLOCKED** |
| Real order round-trip latency | **UNRUN — P14** (measuring it requires sending an order) |
| Current branch | `feature/p8-queue-latency` |
| Venue-tick correction | `55977f4` — `fix: update current venue tick capabilities` |
| P7 boundary commit | `de96681` — `feat: add post-only execution reconciler` |
| P7 concurrency correction | `d333aeb` — `fix: dispatch independent outcome orders concurrently` |
| P8 boundary commit | `16bd4d4` — `feat: add queue and latency instrumentation` |
| P8 correction branch 1 | `fix/p8-measurement-hotpath-closure` (`c5cec7f`) |
| P8 correction commits 1 | `f96815f` O15 · `e1604bf` shadow lifecycle · `a6f903e` telemetry overhead |
| P8 correction branch 2 | `fix/p8-telemetry-offload` |
| P8 correction commits 2 | `bbeb9b5` analytics offload · `88a7807` stage sampling · `4cb9d5f` benchmarks |
| Current branch | `feature/p9-risk-recovery` |
| P9 boundary commit | `4c2ab1d` — `test: exercise every inducible risk condition on a real market` |
| P9 correction branch | `fix/p9-risk-ordering-staleness` |
| P9 correction boundary | `3ab5fa3` — `docs: record corrected P9 real-market evidence` |
| P9 correction commits | `ea2625e` staleness authority + ordered risk stream · `5478da5` NamedTuple hot types · `3ab5fa3` evidence |
| P9C audit-closure branch | `fix/p9-risk-sequence-integrity` |
| P9C boundary commit | `44e05e1` — carries the P9C evidence and docs |
| P9C commits | `63cc2b5` verifier + integrity tests · `44e05e1` evidence, docs, manifest block |
| P1 parser correction | `226663d` — `fix: accept leading-dot exact decimals` |
| P10A research branch | `research/p10-o11-resolution` |
| P10 status | **NOT STARTED / NOT IMPLEMENTED** — O11 prerequisite closed only |
| GitHub default branch | `main` (verified 2026-08-26; the earlier `bootstrap/phase-0` observation no longer holds) |
| P9 commits | `4be0032` risk engine · `6576de0` recovery + runner · `1584dee` stale recovery fix · `b2e715e` real-market evidence · `4c2ab1d` full fault market |
| Last accepted milestone | P7 — execution state + reconciler, corrected (`0f17bd2`) |
| Next milestone | P9 — awaiting acceptance of P8; not started |
| `main` | `226663d` — fast-forwarded through accepted P9C and the P1 parser correction, pushed |
| Remote | `origin` → `https://github.com/bomb707/hedge.git` |

Nothing merged by merge commit, rebased, squashed, or force-pushed. `main` advances by
fast-forward only; all milestone branches are retained.

---

## The official SDK, reverified

| | |
|---|---|
| Distribution | `polymarket-client`, pinned **`==0.6.0`** |
| Import name | `polymarket` |
| Repository | `Polymarket/py-sdk` (`https://github.com/Polymarket/py-sdk`) |
| Requires | Python `>=3.11` |
| Legacy client | `py-clob-client` (0.34.6) — archived, **not used** |

Verified by introspecting the installed package, not from documentation alone:

```text
OrderType = Literal["GTC", "GTD", "FAK", "FOK"]
OrderSide = Literal["BUY", "SELL"]
TickSize  = Literal["0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"]

SecureClient.create_limit_order(*, token_id, price, size, side,
                                post_only: bool = False, expiration=None, ...) -> SignedOrder
SecureClient.post_order(signed_order) -> AcceptedOrder | RejectedOrder
SecureClient.cancel_order(*, order_id) / cancel_orders(*, order_ids)
```

Three findings that shaped the design:

- **`post_only` defaults to `False`.** The SDK's default is the unsafe one for this strategy,
  so it is passed explicitly on every call and no code path can omit it.
- **`order_type = "GTC" if expiration is None else "GTD"`.** We never pass an expiration, so
  every order is GTC. The strategy cancels explicitly at SETTLING, so GTD is unnecessary.
- **Signing resolves market metadata through an internal cache**, and on a tick mismatch it
  re-fetches over REST *before* signing. That is exactly the latency hazard Canonical §22
  warns about, so the cache is prewarmed during pre-arm and preparation validates the tick
  itself, which keeps the refetch branch unreachable. The allowance lookup and on-chain
  approval are a rejection-recovery fallback, not on the happy path.

---

## Venue tick correction

`SUPPORTED_TICK_SIZES` was missing `0.005` and `0.0025`, which the venue added after the set
was written. Corrected against the SDK's own `TickSize` literal. Both are exactly
representable at `PRICE_SCALE = 1_000_000` (`5_000` and `2_500` price units, each dividing the
scale), so this is a venue-capability correction and **O10 is not reopened**.

`MarketDefinition.tick` remains `0.01`. The replica's quote grid comes from the frozen
strategy evidence; the venue's legal increment is a separate concept.

---

## What P7 delivered

- **`PreparedOrder`** — `outcome`, `token_id`, `strategy_price`, `submission_price`,
  `strategy_size`, `submission_size`, `venue_tick`, `min_order_size`, `outcome_status`,
  `observed_ask`, plus derived `size_quantization_delta` and `price_unchanged`. The
  `DesiredOrder` is never mutated.
- **Post-only guard** — typed outcomes: `SAFE`, `NO_BOOK`, `WOULD_CROSS`, `OFF_VENUE_TICK`,
  `OUT_OF_VENUE_RANGE`, `BELOW_MIN_SIZE`, `ZERO_AFTER_QUANTIZATION`, `UNKNOWN_VENUE_RULES`.
  Judged against the **same outcome's** observed ask; equality is blocked; the DOWN ask is
  never inferred from UP.
- **`LiveOrderTable`** — many orders, not one per side, because a cancel racing an
  acknowledgement cannot otherwise be represented. `PENDING_PLACE`, `LIVE`,
  `PARTIALLY_FILLED`, `PENDING_CANCEL`, `FILLED`, `CANCELLED`, `REJECTED`, `UNKNOWN`, with a
  deterministic per-outcome view for the reconciler and idempotent updates.
- **Pure `reconcile`** — `NOTHING` / `KEEP` / `PLACE` / `CANCEL` / `REPLACE` / `WAIT` /
  `BLOCKED`, each with a typed `SideReason`. No clock, no network, no logging.
- **Token bucket** — free under normal load, bounds excess, with reserved cancel capacity so a
  cancel can never be starved by placements. Time is an argument, never read internally.
- **`ReplacementTracker`** — `CANCEL_THEN_PLACE` with generation-bound staleness.
- **`VenueAdapter`** — the single SDK boundary, with `RecordingTransport` for tests.
- **User-stream normalization** — venue order updates and trades become P2 `OrderStateEvent`
  and `OwnFill`. P2 semantics were not bent to match SDK shapes.

### Measured

Long unchanged stream, 5 000 decision cycles × 2 sides = 10 000 side-decisions:

```text
PLACE     2        CANCEL    0        WAIT      0
KEEP   9998        REPLACE   0        BLOCKED   0

network requests issued: 2 placements, 0 cancels
```

Microbenchmarks (median): `prepare_order` 3.07 µs · `reconcile` KEEP 3.26 µs · `reconcile`
REPLACE 3.30 µs. KEEP is marginally cheaper, but both are dominated by frozen-dataclass
construction rather than by logic — the same effect measured in P4. P8 owns end-to-end latency.

---

## Tests

| | |
|---|---|
| Status | **green** |
| Suite | 1 404 passed (1 242 at the P8C boundary; +162 across P9 and its two corrections) |
| `ruff check` / `ruff format --check` | clean |
| `mypy` (strict) | clean — `src/`, `tests/`, `tools/`; **zero `type: ignore` in `execution/`** |
| Runtime dependencies | `websockets`, `polymarket-client==0.6.0` — both pinned or bounded |
| Dev dependencies | `pytest`, `mypy`, `ruff`, `pytest-asyncio` |

Two P6 guards were **rescoped, not relaxed**: they were written when the repository contained
no write path at all. The market-data plane must still contain no credential material, and
`execution/credentials.py` is now asserted to be the *single* module where such material is
even named.

Several structural guards read the **code** rather than the source text, because these modules
deliberately *describe* what they refuse to implement — a `min_requote_ms` delay, a `--live`
flag, `post_only=False`, `wait_for_order_fill_settlement`. A plain text scan would trip over
its own documentation.

---

## Open strategy items

Full detail in [`OPEN_ITEMS.md`](OPEN_ITEMS.md). **P8 added O15 and closed it**, confirmed on
real market data. O08 and O09 are now *measurable* but remain **OPEN**: neither can be settled
without real resting orders, which P8 does not place.

**P9 closed none and added none.** Operational safety observations do not resolve strategy
parameters, and the risk thresholds are `OPERATIONAL` engineering configuration rather than
reconstructed constants — so they get no open item either. The telemetry and risk architectures
are engineering concerns, not questions about the reconstructed strategy.

```text
O01 quote-centre source            OPEN      O08 latency for queue dominance OPEN
O02 volatility sigma               OPEN      O09 spot-to-CLOB timing model   OPEN
O03 base-lot L selection rule      OPEN      O10 venue precision / scales    CLOSED
O04 grid-target selection          OPEN      O11 resolution source           CLOSED
O05 endgame tilt magnitude         FITTED    O12 BTC spot scale              CLOSED
O06 endgame gate magnitude         FITTED    O13 tick tie-breaking           OPEN
O07 fee/rebate calibration         OPEN      O14 strike chaining unverified  OPEN
                                             O15 current() linear in orders  CLOSED
```

Replacement sequencing and the rate budget are labelled `OPERATIONAL`, not strategy OPEN
items: they are ordinary execution-policy choices that do not change the reconstructed
strategy.

---

## P6 live capture evidence (retained)

Two consecutive full `btc-updown-5m-*` markets captured read-only and verified end to end by
the P5 replay engine: every decision reproduced, final state matched, canonical bytes
round-tripped identically. Manifests are committed under [`docs/evidence/`](evidence/).

The journals are 150-200 MB each — every step records the complete `DecisionResult` — so they
are stored outside Git and identified by digest:

```text
path        /home/hr/p6-captures/btc-updown-5m-1787647500.journal.ndjson
bytes       159,464,961
sha256      dbd436b4d2bb46c23182390256e07ff8712246d311c83b52ecb08048e717d3aa
steps       108,617

path        /home/hr/p6-captures/btc-updown-5m-1787647200.journal.ndjson
bytes       193,408,732
steps       130,374
sha256      see docs/evidence/p6-capture-btc-updown-5m-1787647200.manifest.json

reproduce   .venv/bin/python tools/capture_market.py <output-directory>
```

These are one machine's local disk and are **not** durable artefacts; the committed manifests
are the record and the command above regenerates them. Observed precision stayed within the
frozen scales throughout (price 0-3 decimals, size 0-2, BTC 8), and the venue announced four
`tick_size_change` events during one market while the replica stayed on its `0.01` grid.

---

## Verification ladder

Canonical §34.

```text
L0  arithmetic                 PASSED
L1  historical reconstruction  BLOCKED  (needs target-wallet ledger data, not in repo)
L2  offline replay             ENGINE PASSED (on real data) / TARGET-WALLET EMPIRICAL UNRUN
L3  live paper                 not started  (P13 - sustained validation programme)
L4  minimum-size live          not started  (P14)
```

**L1 remains UNRUN and is not relabelled.** **L2's empirical half remains UNRUN**: the P6
journals record our own bot's trajectory, not the target wallet's history.

P7 performed **no live order test**: no credential was requested, no authenticated socket was
opened, and no order of any size was placed. P14 owns minimum-size live execution.

---

## Update ritual

At each accepted boundary, update: current phase, branch, boundary commit, last accepted
milestone, next milestone, blockers, open-item labels, and test status. Record the
implementation gate and the empirical status **separately**. If a gate was not actually
executed, say so — code existing is not completion
([`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), rule 1).
