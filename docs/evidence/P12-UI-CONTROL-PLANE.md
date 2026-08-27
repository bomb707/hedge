# P12 — operator UI and control plane

**Provenance: `REAL_PUBLIC_MARKET_DATA`.** Real Polymarket CLOB, real BTC spot, real Polygon
settlement, with the operator UI running as a separate process throughout. The operator commands
are local and operational — that is what an operator command is — but the pipeline and the market
they acted on are real.

**No order was placed. No credential exists. No chain write occurred.**
`LIVE_TRADING_ENABLED` is `False`; `REDEMPTION_ENABLED` is `False`, and **there is no endpoint,
button, form field or config path in P12 capable of changing either.** Not disabled ones — none.

**Capture date:** 2026-08-27 (UTC).

## Architecture

```
BOT PROCESS                                      UI PROCESS
  Plane 1/2  event -> decide -> risk_adjust
  Plane 3    persistence worker
               -> snapshot.json  (atomic rename) ->  read, render
               <- inbox/*.json   (bounded)       <-  POST /control
             control tick: listdir, apply, publish
```

Two processes and a directory between them. The bot writes the snapshot to a temporary file and
renames it, which is atomic, so a reader sees the previous frame or the next one and never half of
either. The UI writes commands as individual files into a bounded inbox; the bot lists the
directory on its control tick and never waits for anyone.

**Files rather than a socket, a queue, or a broker**, because the acceptance gate is someone
killing the UI mid-market. That rules out every transport where the bot holds something the UI
can be holding when it dies — a socket it accepts on, a lock, a condition variable, a shared
connection — and it rules out a broker, which would be a third process to keep alive in order to
prove that a process dying is survivable. Killing either side leaves a directory, which needs no
recovery because it was never a connection.

The asymmetry I19 demands falls out of it: the UI may block writing a file; the bot's read is a
`listdir` with a bound on it.

### Read model

`UiSnapshot` is immutable and holds **no reference to any trading object** — no `MarketState`, no
`LedgerState`, no `LiveOrderTable`, not even to read. It is built on the persistence worker's own
thread from the `DecisionRecord` P11 already made for that cycle, and published four times a
second rather than per cycle: a real market produces hundreds of decisions a second and no
operator reads at that rate, so a slow disk delays a *frame*, not a decision. **Plane 1 is not
involved, so the cost to it is zero.**

Nothing in the UI recomputes an economic quantity. The licence is scale — `1_230_000` may be
rendered `1.23` — and that is all. A second PnL implementation living in a dashboard would be a
second thing to be wrong, in the place people look when they want to know what is true.

**Absence is not zero.** Every unknown renders `—`; a missing snapshot renders `NO SNAPSHOT`
with the words "this is not an empty market; it is no data"; a snapshot older than five seconds
renders `STALE` and says the values are the last known ones. An operator reading a blank inventory
and an operator reading `0` have been told different things.

### Views

Live market (phase, ordinal, elapsed/remaining, snapshot age) · risk & health (state, sequence,
active, latched, allows_place, allows_cancel, CLOB, spot) · accounting (n_up, n_down, inventory,
costs, fees, both rebate views, **all four PnL branches**, favourite, target) · centre (raw
rational, quantized, source, status) · execution (strategy wanted vs execution allowed, per side,
with action, reason, resting order, queue ahead and confidence, post-only outcome) · settlement ·
telemetry · strategy parameters with I18 labels · operator command audit · history.

### Strategy parameters

Read off the config's own status labels rather than a second table that could drift. `OPEN`,
`FITTED` and `OPERATIONAL` are shown as what they are, with the OPEN item id where one applies —
O01 quote centre, O03 base lot, O04 grid policy, O07 rebate, O13 tick rounding, O05/O06 endgame.
The page says in words that OPEN means the frozen sources do not establish the value. **Nothing
in P12 can edit any of them**; P15 owns strategy change.

### History

Uses P11's accepted verified reader: archive identity proved, then `verify_store` for
completeness. `COMPLETE` / `INCOMPLETE` / `UNSUPPORTED` / `CORRUPT` are shown distinctly, and an
incomplete market is **displayed rather than hidden** — it is still the record of what happened —
carrying the label **NOT ELIGIBLE FOR EMPIRICAL STRATEGY EVIDENCE**. Restored archives are cached
for a minute so a page refresh does not decompress 10 MB again; nothing in the trading path points
at that cache.

## Control

Two commands, and deliberately almost nothing:

| Command | Meaning |
|---|---|
| `OPERATOR_HALT` | stop placing; existing quotes reconcile toward CANCEL through the normal minimal-action rule |
| `RELEASE_OPERATOR_HALT` | withdraw *that* halt, and nothing else |

There is no command to place an order, change a parameter, clear risk wholesale, bypass
post-only, force a settlement, or enable live trading. A command that does not exist cannot be
issued by accident, misconfigured, or found later by someone wondering what it would do.

A command is **inert** until the bot accepts it, and accepting it means emitting a `RiskSignal`
through the same `RiskController` that owns every other permission change. Ordering comes from the
market: the command carries the UI's wall clock for the record and it is used for nothing else,
because a click has no position in an event stream until the stream gives it one. That is what
makes the control replayable, and the tests run the same ordered stream twice and compare it
record for record.

`RiskReason.OPERATOR_HALT` is deliberately **not** in `REQUIRES_RECONCILIATION` — it is the one
condition whose evidence is a person deciding — and it clears **only itself**. Tests prove a
release leaves a stale feed stale, and leaves a `POSITION_MISMATCH` latch latched.

**Security posture:** loopback only, because no authentication exists in this build. That is an
OPERATIONAL choice recorded as one, not a claim to have solved access control. GET has no write
path at all; controls are POST and answer 303 so a refresh re-reads rather than re-posts; the
inbox is bounded at 64 and refuses the *sender* when full rather than growing, because a command
silently queued behind sixty-four others is not a safety control.

## Real market — `btc-updown-5m-1787803500`

| | |
|---|---|
| Cycles / decisions | 66,174 |
| CLOB messages | 65,667 (1,548 books, 62,583 price changes, 749 trades) |
| Risk records | **66,176** — the two extra are the operator's commands |
| Snapshots published | 1,166, write errors 0 |
| Drops / gaps / sink errors | **0 / 0 / 0** |
| Verification | **COMPLETE**, no failures |
| Archive | 52.6×, restore verified, read-back re-verified COMPLETE |

### Control, through the real transport

```text
[ui_started]          http=200  renders_market=True
[get_control_refused] http=405  post_only=True
[market_safe]         phase=QUOTE risk_state=SAFE ordinal=1778
[halted]              command 4b2a5f04ed7642c4  ordinal 1835  risk_sequence 1771
                      HALTED  allows_place=False  allows_cancel=True
[still_halted_15s]    ordinal 8087, still HALTED
[released]            command 651f8ae4bbf644b9  ordinal 8131  risk_sequence 7924
                      RECOVERING -> SAFE
```

The release produced `RECOVERING`, not `SAFE` — P9's recovery hold. That is the correct answer and
the reason the command is named for what it does rather than "resume": an operator cannot decide
that the rest of the risk state is fine.

Audited from the durable record, through the verified archive:

```text
places_by_risk_state   {"SAFE": 183}
risk_states            {"SAFE": 59964, "HALTED": 6209, "RECOVERING": 1}
actions   KEEP 48,779 · BLOCKED 46,517 · NOTHING 36,686 · PLACE 183 · REPLACE 105 · CANCEL 78
```

**PLACE while HALTED: 0. PLACE while RECOVERING: 0.** 6,209 decisions were taken while the
operator halt stood, and not one of them placed.

### The UI kill

```text
[ui_killed]            SIGKILL pid=2442756  at ordinal 8229, 8,021 decisions persisted
[ui_unreachable]       reachable=False
[bot_alive_after_kill] ordinal 26,342   decisions 25,652
                       events since kill  18,113
                       decisions since kill 17,631
                       risk_state SAFE   seconds since kill 47.0
```

In the 47 seconds after the UI was killed the bot processed **18,113 more real market events** and
wrote **17,631 more decisions**. It did not restart, did not reconnect a feed, and did not acquire
a new risk condition. Zero drops, zero sequence gaps and zero sink errors across the whole market
— which spans the kill — so the kill caused no journal gap and no persistence gap.

### The UI restart

Restarted into the same running market, on the same port: HTTP 200, rendered the live market from
the published snapshot, and showed **both** earlier commands. The bot was not restarted, risk was
not reset, orders were not reset, and **reconnecting produced no control event** — the command
count after restart is 2, the same two that were issued.

## Isolation, stated precisely

* No UI module imports `MarketState`, `LedgerState`, `LiveOrderTable`, `StrategyEngine` or
  `Executor` — asserted by test, not by inspection.
* No UI module contains `threading.Lock`, `threading.Condition`, `queue.Queue` or an `acquire(` —
  asserted by scanning the source.
* The bot's only knowledge of the UI is a file it writes and a directory it lists.
* `SnapshotChannel.publish` never raises into its caller; a full disk is counted, like the
  telemetry sink, for the same reason.

## The defect the first real market found

The first attempt verified **INCOMPLETE**, and the two records missing from the durable risk
stream were **the operator's own two commands**. `ControlIngress` applied each command to the
controller — so it appeared in the in-memory trace and on the dashboard — and never published the
resulting record to the persistence channel. 107,250 of 107,252 risk records landed; the two that
did not were the ones describing a person changing what the bot was allowed to do.

The drop accounting was self-consistent throughout (accepted 107,250, persisted 107,250, dropped
0), so nothing but P11's sequence-exactness check would have noticed. A control action with no
durable record is the worst possible thing to lose, and it was the only thing lost. Fixed, tested
in both directions, and the market above is the re-run.

## Still not done

* **Real own-fill durable record: UNRUN / P14.**
* **Real maker fraction: UNRUN / P14.**
* **Real taker-fill persistence: UNRUN / P14.**
* **Real nonzero own-ledger settlement analytics: UNRUN / P14.**

P12 closes no strategy open item, changes no strategy value, and cannot. O07 remains OPEN.
