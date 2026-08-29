# P13F — Plane-3 audit isolation and result uniqueness

**Provenance: `REAL_PUBLIC_MARKET_DATA`.** One induced fault, labelled
**`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`**: our own audit worker was deliberately slowed by 500
milliseconds per operation while the Polymarket, Binance and Polygon feeds stayed real and healthy.

**No order. No credential. No chain write.** `LIVE_TRADING_ENABLED` and `REDEMPTION_ENABLED` are
both `False`.

**Date:** 2026-08-28 (UTC).

## `p13-corpus-5` stopped and superseded

`SIGTERM` to PID 585754, verified through `ps`, `/proc/585754/cwd` and `/proc/585754/cmdline`
first. **52 durable rows, all COMPLETE and replay-EXACT**, from 54 attempts; the two in flight
remain open in the ledger. Preserved entire; inventory in `p13-corpus-5-superseded.json`, audit in
`p13-corpus-5-audit.json`.

Re-read with the corrected verifier: 52 rows, **52 qualifying, 52 unique attempts, 52 unique
market slugs**, no duplicate starts, terminals, result rows or markets, 52 identity-valid latency
artifacts, and the runtime counter agreeing with the durable count at every market — the P13E
correction held. What was wrong was *where the work ran*.

## A — the audit was running on the trading loop

Every corpus append, every ledger append and every fsync happened inside `_finalize`, a coroutine
on the same event loop that consumes **the next market's** websocket frames. `_recount` then read
the whole corpus, the whole ledger and LZMA-decompressed **every historical latency artifact**,
synchronously, on that loop. By market 52 that was 52 decompressions per market; over two hundred
markets it is 20,100.

That is the P12B rule at a different layer. "Plane 3 does not wait for the UI" was never the rule;
the rule is that nothing outside the trading path may make the trading path wait, and an fsync
qualifies whether or not anyone is watching it.

`maker5m.bot.audit.AuditIO` owns all of it on a **single dedicated worker thread**. Single, not a
pool: these are append-only files whose order is their meaning, and concurrent writers would trade
a latency problem for a corruption one. The supervisor's `corpus` and `ledger` are read-only views
onto that owner rather than second handles — a test that replaced one and not the other is how
that possibility was found and closed.

An attempt is still registered durably *before* its session launches. That wait is correct: it is
a market that does not exist yet, and the market that does exist is not waiting, because the
filesystem work is on another thread.

### Measured, not asserted

The test runs a heartbeat coroutine — standing in for the ingress consumer — while an audit
operation blocks for 250 ms:

```text
through the audit owner   the caller waits 0.25 s; the loop keeps 40+ heartbeats
called inline (P13E)      the loop makes exactly 0 heartbeats while it waits
```

Both numbers come from the same test file. The second is what the previous version did.

## Counting: O(1) per market, full audit at the boundaries

The complete joined audit runs **once at startup**, and again **when the target is reached**.
Nowhere else. Each finalised market is judged on its own by the same shared qualifier, reading one
artifact — verified by a test that builds 200 qualifying rows, counts artifact reads during one
incremental finalisation (**1**), and then during an explicit full audit (**201**).

`completed` is the size of a set of qualified attempt ids, seeded from the startup audit.
Membership, not arithmetic: an attempt is in it or it is not, so no sequence of observations can
count a market twice. And the target is only *met* once the full audit agrees — if it comes back
short, the durable answer replaces the running set and collection continues.

## B — two rows could each count

`qualify_all` now judges the collection as well as the rows:

* **one result per attempt** — two rows naming one attempt are two claims about one market;
  choosing between them silently would be inventing an answer, so neither counts;
* **one result per market** — the gate is two hundred *markets*, not two hundred JSON lines, so a
  slug counts once at most and a second qualifying result for it is an integrity fault.

Judgements carry their row index and attempt id, and the report pairs them to rows **by position**.
Selecting by slug let a refused row's counts into the aggregates on a qualifying neighbour's
ticket; a test now plants a refused row carrying 999,999 of everything beside a good one for the
same slug and asserts none of it reaches the totals.

`read_latency`'s own schema check is typed too, so a caller passing no expected identity still
cannot read `True`, `1.0` or `"1"` as schema 1.

## The pilot — three real markets with the audit deliberately slowed

`p13f-pilot-1`, one process, clean tree at `d2c1ac7`, every audit operation delayed by 500 ms
(**`CONTROLLED_LOCAL_FAULT_ON_REAL_MARKET`**).

| | 1787874900 | 1787875200 | 1787875500 |
|---|---|---|---|
| store / replay | COMPLETE / EXACT | COMPLETE / EXACT | COMPLETE / EXACT |
| decisions | 87,223 | 106,178 | 153,847 |
| classified = actions = 2× | 174,446 | 212,356 | 307,694 |
| feed ready **at T0** | 29.02 s | 28.72 s | 28.97 s |
| latency (CLOB / spot) | 266 KB (8,570 / 154) | 322 KB (10,339 / 288) | 465 KB (15,177 / 211) |
| drops / gaps / sink | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| PLACE | SAFE 265 | SAFE 461 | SAFE 986 |
| settlement | RESOLVED | RESOLVED | RESOLVED |

3 rows, 3 qualifying, 3 unique attempts, 3 unique slugs, 0 open, no duplicates of any kind, 3
identity-valid artifacts, 0 refused. Buffer high-water 122–2,213 of 320,000.

**Audit durations, per market, with the fault active:**

```text
start_attempt   529 ms     append_row     517 ms
judge_row       535 ms     finish_attempt 533 ms
full_audit      500 ms     recover        500 ms
```

Half a second on every audit operation, and the markets did not notice: zero drops, zero gaps,
zero sink errors, and the first market's hot-path maximum was **1.3 ms** while its audit calls
were taking 500.

The larger hot-path maxima on markets two and three are the garbage collector, not the audit, and
the numbers say so exactly:

```text
market 2   gc gen-2 max 384 ms   hot-path max 272.7 ms
market 3   gc gen-2 max 721 ms   hot-path max 720.8 ms
```

That is the effect P13B measured and paced, unchanged and still visible. It is recorded, not
solved here, and no new threshold is proposed.

Merged live latency across the three: CLOB receive→decide p50 **106,769 ns**, p95 196,646, p99
524,217 over 34,086 samples; spot receive→decide p50 **73,774 ns**, p95 116,151, p99 190,036 over
653.

## Performance

The overhead benchmark exercises the P11 persistence stack and the P12 UI. **This round's diff
touches only `maker5m/bot/`, its tests and the report** — the benchmark does not import
`maker5m.bot` at all — so it is a regression check on unchanged code rather than a measurement of
this change.

| P8C limit | P13F | |
|---|---|---|
| decide p50 overhead ≤ 1,000 ns | +481 ns | MET |
| decide p50 overhead ≤ 3 % | +2.01 % | MET |
| full-cycle p50 overhead ≤ 5,000 ns | +2,476 ns | MET |
| full-cycle p50 overhead ≤ 5 % | +4.67 % | MET |

Both limits are met and neither was moved, but the honest caveat is that this machine was not idle
— an unrelated workload held the load average near 2.2 throughout, and an earlier attempt on the
same build under heavier contention read +5.16 %. Earlier rounds measured 1.3 % to 3.8 % on the
same benchmark and the same persistence code. The figure is a property of the machine on the day,
not of this change, and it is reported rather than re-run until it looked better.

## Retained and superseded

`p13-corpus-1` through `p13-corpus-5` and every earlier pilot are retained unedited with
supersession recorded beside them. None counts toward the ≥200 gate.

## Unchanged

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P13F closes no OPEN item, proposes no threshold, and changes no strategy value.
