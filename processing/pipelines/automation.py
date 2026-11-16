"""Tier: automations, the user's if-this-then-that rules over new moments.

Discovery pairs each enabled rule with recent moments created while it was on
that have no run or job yet. A job checks the cheap conditions, then asks the
model once to answer the rule's question and fill its fields. Every outcome is
a run: matched, skipped (with why) or failed (with the error); a failure also
fails the job, so the worker retries it.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from database.pipe import DatabasePipe
from database.repos.moments import Moment
from database.schema.automations import Automation, RunStatus
from database.schema.jobs import Job, JobCreate
from processing.automations import SYSTEM_PROMPT, parse, prefilter, render
from processing.base import Pipeline
from processing.config import get_settings
from processing.llm import Llm, LlmError, LlmUnavailable


class RunFailed(Exception):
    """Recorded as a failed run, then raised so the worker retries the job."""


class AutomationPipeline(Pipeline):
    name = "automation"

    async def setup(self) -> None:
        self._settings = get_settings()
        self._llm = Llm(self._settings)
        tz = self._settings.moment_timezone
        self._tz = ZoneInfo(tz) if tz else None

    async def teardown(self) -> None:
        await self._llm.close()

    async def discover(self, limit: int) -> Sequence[JobCreate]:
        settings = get_settings()
        if not settings.llm_enabled:
            return ()
        async with DatabasePipe() as pipe:
            pending = await pipe.automations.pending(self.name, limit, settings.automation_lookback_hours)
        return [
            JobCreate(
                pipeline=self.name,
                payload={
                    "automation_id": str(p.automation_id),
                    "moment_id": str(p.moment_id),
                    "user_id": str(p.user_id),
                },
                dedup_key=f"{self.name}:{p.automation_id}:{p.moment_id}",
            )
            for p in pending
        ]

    async def process(self, job: Job) -> dict[str, Any]:
        automation_id = UUID(job.payload["automation_id"])
        moment_id = UUID(job.payload["moment_id"])
        user_id = UUID(job.payload["user_id"])
        async with DatabasePipe() as pipe:
            rule = await pipe.automations.get(automation_id, user_id)
            moment = await pipe.moments.get(moment_id, user_id)
        if rule is None or moment is None:
            return {"missing": True}
        if not rule.enabled:
            return {"disabled": True}

        try:
            status, output = await self._run(rule, moment)
        except LlmUnavailable:
            raise  # refunded and retried by the worker
        except LlmError as exc:
            await self._record(rule, moment, "failed", {}, str(exc)[:500])
            raise RunFailed(str(exc)) from exc
        await self._record(rule, moment, status, output, None)
        return {"status": status, **output}

    async def _record(
        self, rule: Automation, moment: Moment, status: RunStatus, output: dict[str, Any], error: str | None
    ) -> None:
        async with DatabasePipe() as pipe:
            await pipe.automations.record_run(rule.id, moment.id, status, output, error)

    async def _run(self, rule: Automation, moment: Moment) -> tuple[RunStatus, dict[str, Any]]:
        skipped = prefilter(rule, moment)
        if skipped:
            return "skipped", {"reason": skipped}
        reply = await self._llm.complete_json(SYSTEM_PROMPT, render(rule, moment, self._tz))
        try:
            verdict = parse(reply.body, rule)
        except ValueError as exc:
            raise LlmError(str(exc)) from exc
        output: dict[str, Any] = {"reason": verdict.reason, "model": reply.model, "usage": reply.usage}
        if not verdict.applies:
            return "skipped", output
        return "matched", {**output, "fields": verdict.fields}
