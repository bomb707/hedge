# Documentation Index

This project implements the Polymarket BTC 5-minute maker bot described by the two
frozen strategy documents below. Read this page first; it defines which document wins
when they disagree, and which project document answers which question.

---

## 1. Frozen strategy sources

These two files are **inputs, not project documents**. They are preserved byte-for-byte
and must never be edited, reformatted, corrected, summarised in place, or regenerated.

| Role | File |
|---|---|
| **Canonical strategy SSOT** | [`strategy/Polymarket_5m_Maker_Bot_Canonical_Strategy_Spec.md`](strategy/Polymarket_5m_Maker_Bot_Canonical_Strategy_Spec.md) |
| **Detailed operational / role reference** | [`strategy/Polymarket_5m_Maker_Bot_Detailed_Strategy_By_Step_and_Role.md`](strategy/Polymarket_5m_Maker_Bot_Detailed_Strategy_By_Step_and_Role.md) |
| Supporting figures | [`strategy/figures/`](strategy/figures/) |

### 1.1 Precedence rule

```text
Canonical Strategy Spec   >   Detailed Strategy By Step and Role   >   any project document
```

* If the two strategy documents conflict, **the Canonical Strategy Spec wins**.
* If a project document in `docs/` conflicts with either strategy document, the **strategy
  document wins and the project document is a defect** to be corrected.
* The Detailed document remains authoritative for *operational decomposition* (which role
  owns which step) wherever the Canonical document is silent on that question.

Note: the Canonical Spec carries its own internal precedence rule in its §1.1 — the later
*Target Wallet Strategy — Complete Specification* supersedes the earlier strategy
specification. Both of those predecessor documents are upstream of this repository and are
not present here; the Canonical Spec is the settled result of applying that rule.

### 1.2 Integrity

`strategy/CHECKSUMS.sha256` pins both files. The check is enforced by
`tests/unit/test_strategy_sources_frozen.py` and runs on every `pytest` invocation.
A failure there means a frozen source was modified — revert it; do not update the checksum
unless the user has supplied a genuinely new revision of the source document.

---

## 2. Project documents

Written to be read **individually**. A future session should not need to re-read the full
strategy sources to work on a phase.

| File | Answers |
|---|---|
| [`ARCHITECTURE_SSOT.md`](ARCHITECTURE_SSOT.md) | What are the components, the three planes, the event flow, and the numeric contract? |
| [`INVARIANTS.md`](INVARIANTS.md) | What must always be true? What breaks the strategy if violated? |
| [`OPEN_ITEMS.md`](OPEN_ITEMS.md) | What is genuinely unresolved and must stay configurable? |
| [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md) | What is built in which phase, and what gate closes each phase? |
| [`STATUS.md`](STATUS.md) | Where are we right now? |

---

## 3. Reading order for a new session

```text
STATUS.md                       -> current phase, branch, blockers
INVARIANTS.md                   -> the rules you may not break
DEVELOPMENT_PLAN.md (one phase) -> the phase you were asked to work on
ARCHITECTURE_SSOT.md (section)  -> only the component you are touching
OPEN_ITEMS.md                   -> only if the phase touches an OPEN parameter
strategy/<file> §<section>      -> only when a precise strategy rule is needed
```

Project documents reference strategy sources **by section number**, never by copying prose.
When a project document says "Canonical §12.2", open that section — do not trust a
paraphrase.

---

## 4. Strategy status vocabulary

Every strategy-relevant statement and parameter in this repository carries one label,
defined in Canonical §1.2 and reproduced in `INVARIANTS.md`:

```text
CONFIRMED     supported by the reconstructed evidence
FITTED        selected by replay/small-sample fitting; likely, not established
OPEN          unresolved; must remain configurable and experimentally testable
OPERATIONAL   engineering choice; not proven to be target-wallet logic
```

An `OPEN` item must never become a hard-coded assumption without an explicit closing
experiment recorded in `OPEN_ITEMS.md`.
