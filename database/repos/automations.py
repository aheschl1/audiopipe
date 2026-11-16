"""Automation rules and their runs."""

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from database.repos.base import BaseRepo
from database.schema.automations import (
    Automation,
    AutomationRun,
    AutomationWrite,
    PendingRun,
    RunListing,
    RunStatus,
)

COLUMNS = (
    "id, user_id, name, enabled, trigger, conditions, actions, active_since, created_at, updated_at"
)
RUN_COLUMNS = "id, automation_id, moment_id, status, output, error, created_at"
_R_COLUMNS = ", ".join(f"r.{c.strip()}" for c in RUN_COLUMNS.split(","))
MAX_PER_USER = 50


def _params(spec: AutomationWrite) -> tuple[Any, ...]:
    return (
        spec.name,
        spec.enabled,
        spec.trigger,
        Jsonb(spec.conditions.model_dump(mode="json")),
        Jsonb([a.model_dump(mode="json") for a in spec.actions]),
    )


class AutomationsRepo(BaseRepo):
    async def count(self, user_id: UUID) -> int:
        return int(
            await self._fetch_value("SELECT count(*) FROM automations WHERE user_id = %s", (user_id,)) or 0
        )

    async def create(self, user_id: UUID, spec: AutomationWrite) -> Automation:
        row = await self._fetch_one(
            Automation,
            f"""
                INSERT INTO automations (user_id, name, enabled, trigger, conditions, actions)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {COLUMNS}
            """,
            (user_id, *_params(spec)),
        )
        assert row is not None
        return row

    async def update(self, automation_id: UUID, user_id: UUID, spec: AutomationWrite) -> Automation | None:
        """Replace the rule. Turning it on restarts active_since, so it skips what it missed."""
        return await self._fetch_one(
            Automation,
            f"""
                UPDATE automations SET
                    name = %s, enabled = %s, trigger = %s, conditions = %s, actions = %s,
                    active_since = CASE WHEN %s AND NOT enabled THEN now() ELSE active_since END,
                    updated_at = now()
                WHERE id = %s AND user_id = %s
                RETURNING {COLUMNS}
            """,
            (*_params(spec), spec.enabled, automation_id, user_id),
        )

    async def delete(self, automation_id: UUID, user_id: UUID) -> bool:
        return bool(
            await self._execute(
                "DELETE FROM automations WHERE id = %s AND user_id = %s", (automation_id, user_id)
            )
        )

    async def get(self, automation_id: UUID, user_id: UUID | None = None) -> Automation | None:
        """One rule; user_id scopes it to its owner (the worker passes none)."""
        if user_id is None:
            return await self._fetch_one(
                Automation, f"SELECT {COLUMNS} FROM automations WHERE id = %s", (automation_id,)
            )
        return await self._fetch_one(
            Automation,
            f"SELECT {COLUMNS} FROM automations WHERE id = %s AND user_id = %s",
            (automation_id, user_id),
        )

    async def list_for_user(self, user_id: UUID) -> list[Automation]:
        return await self._fetch_all(
            Automation,
            f"SELECT {COLUMNS} FROM automations WHERE user_id = %s ORDER BY created_at, id",
            (user_id,),
        )

    async def pending(self, pipeline: str, limit: int, lookback_hours: float) -> list[PendingRun]:
        """Recent (rule, moment) pairs with neither a run nor a job yet, newest moments first."""
        return await self._fetch_all(
            PendingRun,
            """
                SELECT a.id AS automation_id, m.id AS moment_id, a.user_id
                FROM automations a
                JOIN moments m ON m.user_id = a.user_id AND m.created_at >= a.active_since
                WHERE a.enabled AND m.created_at > now() - make_interval(secs => %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM automation_runs r
                      WHERE r.automation_id = a.id AND r.moment_id = m.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM processing_jobs j
                      WHERE j.pipeline = %s AND j.dedup_key = %s || ':' || a.id || ':' || m.id
                  )
                ORDER BY m.created_at DESC
                LIMIT %s
            """,
            (lookback_hours * 3600, pipeline, pipeline, limit),
        )

    async def record_run(
        self,
        automation_id: UUID,
        moment_id: UUID,
        status: RunStatus,
        output: dict[str, Any],
        error: str | None = None,
    ) -> AutomationRun | None:
        """None when the moment or rule went away mid-run. Callers keep the
        rule and moment to one user, as pending() does."""
        return await self._fetch_one(
            AutomationRun,
            f"""
                INSERT INTO automation_runs (automation_id, moment_id, status, output, error)
                SELECT %s, %s, %s, %s, %s
                WHERE EXISTS (SELECT 1 FROM moments WHERE id = %s)
                  AND EXISTS (SELECT 1 FROM automations WHERE id = %s)
                ON CONFLICT (automation_id, moment_id) DO UPDATE SET
                    status = EXCLUDED.status, output = EXCLUDED.output,
                    error = EXCLUDED.error, created_at = now()
                RETURNING {RUN_COLUMNS}
            """,
            (automation_id, moment_id, status, Jsonb(output), error, moment_id, automation_id),
        )

    async def runs(
        self, user_id: UUID, *, automation_id: UUID | None = None, limit: int = 50
    ) -> list[RunListing]:
        """The user's runs, newest first; matched and failed only unless asked for one rule."""
        conditions, params = ["a.user_id = %s"], [user_id]
        if automation_id is not None:
            conditions.append("a.id = %s")
            params.append(automation_id)
        else:
            conditions.append("r.status <> 'skipped'")
        return await self._fetch_all(
            RunListing,
            f"""
                SELECT {_R_COLUMNS}, a.name AS automation_name,
                       m.title AS moment_title, m.start_at AS moment_start_at
                FROM automation_runs r
                JOIN automations a ON a.id = r.automation_id
                JOIN moments m ON m.id = r.moment_id
                WHERE {" AND ".join(conditions)}
                ORDER BY r.created_at DESC, r.id LIMIT %s
            """,
            [*params, limit],
        )

    async def matched_for_moment(self, moment_id: UUID) -> list[RunListing]:
        """The runs that matched one moment, in rule order, for the moment page."""
        return await self._fetch_all(
            RunListing,
            f"""
                SELECT {_R_COLUMNS}, a.name AS automation_name,
                       m.title AS moment_title, m.start_at AS moment_start_at
                FROM automation_runs r
                JOIN automations a ON a.id = r.automation_id
                JOIN moments m ON m.id = r.moment_id
                WHERE r.moment_id = %s AND r.status = 'matched'
                ORDER BY a.created_at, a.id
            """,
            (moment_id,),
        )
