"""One owner for every audit file the collector touches, and it is never the event loop.

The problem this exists for
---------------------------
`attempts.jsonl` and `corpus.jsonl` are opened, written, fsynced and re-read by the supervisor,
and the latency artifacts are read, hashed and LZMA-decompressed to validate them. All of it was
happening synchronously inside `_finalize`, which is a coroutine on the same event loop that is
consuming **the next market's** websocket frames. A slow disk or a large decompression in market
N's bookkeeping could delay market N+1's ingestion, its phase scheduling, its staleness checks and
its risk evaluation.

That is the P12B lesson at a different layer. "Plane 3 does not wait for the UI" was never the
rule; the rule is that nothing outside the trading path may make the trading path wait, and an
fsync qualifies whether or not anyone is watching it.

So every audit read and write runs on a **single dedicated worker thread**. Single, not a pool:
these are append-only files whose ordering is their meaning, and handing concurrent writes to the
same JSONL from several threads would trade a latency problem for a corruption one. One owner
gives serialisation for free, and the coroutine that wants an answer awaits it without holding the
loop.

What the loop still waits for is a *future market*: an attempt must be durably registered before
its session is launched. That wait is correct and it costs an already-running market nothing,
because the filesystem work is happening on another thread.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from time import perf_counter_ns
from typing import Any

from maker5m.bot.attempts import AttemptLedger
from maker5m.bot.corpus import CorpusIndex
from maker5m.bot.qualify import AttemptIndex, Qualification, QualificationReport, qualify_all

__all__ = ["AuditIO"]


@dataclass(slots=True)
class AuditIO:
    """Serialised, off-loop access to the corpus index, the attempt ledger and the artifacts."""

    corpus: CorpusIndex
    ledger: AttemptLedger
    _pool: ThreadPoolExecutor | None = field(default=None, repr=False)
    calls: dict[str, int] = field(default_factory=dict)
    durations_ns: dict[str, int] = field(default_factory=dict)
    slowdown_s: float = 0.0
    """Controlled local fault injection: an artificial delay inside the audit thread.

    Present so a real market can be run with the audit path deliberately slow, which is the only
    way to show that its slowness costs the trading loop nothing. Zero in every normal run."""

    def start(self) -> None:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maker5m-audit")

    def stop(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    async def _run(self, name: str, work: Any) -> Any:
        """Run one audit operation on the audit thread and await it without holding the loop."""
        self.calls[name] = self.calls.get(name, 0) + 1
        delay = self.slowdown_s

        def timed() -> Any:
            started = perf_counter_ns()
            try:
                if delay:
                    time.sleep(delay)
                return work()
            finally:
                self.durations_ns[name] = max(
                    self.durations_ns.get(name, 0), perf_counter_ns() - started
                )

        if self._pool is None:  # pragma: no cover - only outside a started collector
            return timed()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, timed)

    # -- the attempt ledger ------------------------------------------------------------------

    async def start_attempt(self, **fields: Any) -> str:
        """Register an attempt durably. Raises `LedgerWriteError` if it could not be written."""
        result = await self._run("start_attempt", partial(self.ledger.start, **fields))
        return str(result)

    async def finish_attempt(self, attempt_id: str, **fields: Any) -> bool:
        result = await self._run(
            "finish_attempt", partial(self.ledger.finish, attempt_id, **fields)
        )
        return bool(result)

    async def recover(self, inventory: Any = None) -> list[dict[str, Any]]:
        result = await self._run("recover", partial(self.ledger.recover, inventory=inventory))
        return list(result)

    # -- the corpus --------------------------------------------------------------------------

    async def append_row(self, entry: dict[str, Any]) -> bool:
        result = await self._run("append_row", partial(self.corpus.append, entry))
        return bool(result)

    # -- judging -----------------------------------------------------------------------------

    async def judge_row(self, entry: dict[str, Any], expect: dict[str, Any]) -> Qualification:
        """Judge **one** newly written row, reading only its own latency artifact.

        The same shared qualifier the full audit uses, applied to one row. Re-validating every
        historical artifact after every market is 20,100 decompressions across two hundred
        markets, and it answers a question the startup audit already answered.
        """

        def work() -> Qualification:
            from maker5m.bot.qualify import qualification_of

            attempts = AttemptIndex.build(self.ledger.events())
            return qualification_of(entry, attempts, **expect)

        result = await self._run("judge_row", work)
        assert isinstance(result, Qualification)
        return result

    async def full_audit(self, expect: dict[str, Any]) -> QualificationReport:
        """Judge the whole corpus, artifacts and all. Startup, and the final confirmation."""

        def work() -> QualificationReport:
            return qualify_all(self.corpus.entries(), self.ledger.events(), **expect)

        result = await self._run("full_audit", work)
        assert isinstance(result, QualificationReport)
        return result

    def summary(self) -> dict[str, Any]:
        return {
            "owner": "single dedicated audit thread",
            "calls": dict(sorted(self.calls.items())),
            "max_duration_ns": dict(sorted(self.durations_ns.items())),
            "injected_slowdown_s": self.slowdown_s,
            "note": (
                "Every corpus, ledger and latency-artifact operation runs here, off the event "
                "loop that consumes market data. One worker, because these are append-only "
                "files whose order is their meaning."
            ),
        }
