"""Automations: rules that run on each new moment (if this, then that)."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from api.deps import CurrentUser, Pipe
from api.schema.automations import AutomationRead, AutomationRunRead
from database.repos.automations import MAX_PER_USER
from database.schema.automations import AutomationWrite, RunStatus

router = APIRouter(prefix="/automations", tags=["automations"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="automation not found")


@router.get("", summary="Your automations, oldest first")
async def list_automations(user: CurrentUser, pipe: Pipe) -> list[AutomationRead]:
    stats = await pipe.automations.stats(user.id)
    return [AutomationRead.from_model(a, stats.get(a.id)) for a in await pipe.automations.list_for_user(user.id)]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create an automation")
async def create_automation(user: CurrentUser, pipe: Pipe, body: AutomationWrite) -> AutomationRead:
    """It runs on moments created from now on, not on earlier ones."""
    if await pipe.automations.count(user.id) >= MAX_PER_USER:
        raise HTTPException(status.HTTP_409_CONFLICT, f"at most {MAX_PER_USER} automations")
    return AutomationRead.from_model(await pipe.automations.create(user.id, body))


@router.get("/runs", summary="Recent automation runs")
async def list_runs(
    user: CurrentUser,
    pipe: Pipe,
    automation_id: UUID | None = Query(None, description="One rule's runs, skipped ones included."),
    status: RunStatus | None = Query(None, description="Only runs with this result."),
    limit: int = Query(50, ge=1, le=200),
) -> list[AutomationRunRead]:
    """Newest first. Without ``automation_id`` or ``status``, only runs that matched or failed."""
    runs = await pipe.automations.runs(user.id, automation_id=automation_id, status=status, limit=limit)
    return [AutomationRunRead.from_model(r) for r in runs]


@router.get("/{automation_id}", summary="One automation")
async def get_automation(user: CurrentUser, pipe: Pipe, automation_id: UUID) -> AutomationRead:
    automation = await pipe.automations.get(automation_id, user.id)
    if automation is None:
        raise _NOT_FOUND
    return AutomationRead.from_model(automation, (await pipe.automations.stats(user.id, automation.id)).get(automation.id))


@router.put("/{automation_id}", summary="Replace an automation")
async def update_automation(
    user: CurrentUser, pipe: Pipe, automation_id: UUID, body: AutomationWrite
) -> AutomationRead:
    """Turning a rule back on doesn't catch it up on moments made while it was off."""
    automation = await pipe.automations.update(automation_id, user.id, body)
    if automation is None:
        raise _NOT_FOUND
    return AutomationRead.from_model(automation, (await pipe.automations.stats(user.id, automation.id)).get(automation.id))


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an automation")
async def delete_automation(user: CurrentUser, pipe: Pipe, automation_id: UUID) -> Response:
    if not await pipe.automations.delete(automation_id, user.id):
        raise _NOT_FOUND
    return Response(status_code=status.HTTP_204_NO_CONTENT)
