from fastapi import APIRouter, Depends

from .contracts import PlanSnapshot
from .schemas import ApprovalRequest, BatchApprovalRequest, PlanEdit, ReplanRequest


def plan_router(plans, graph, current_user):
    router = APIRouter(prefix="/api/plans", tags=["银行办理计划"])

    @router.get("", response_model=list[PlanSnapshot])
    def list_plans(user=Depends(current_user)):
        return plans.list(user["id"])

    @router.get("/{plan_id}", response_model=PlanSnapshot)
    def get_plan(plan_id: str, user=Depends(current_user)):
        return plans.snapshot(user["id"], plan_id)

    @router.post("/{plan_id}/approve", response_model=PlanSnapshot)
    async def approve(plan_id: str, request: ApprovalRequest, user=Depends(current_user)):
        return await graph.amutate(user["id"], plan_id, "approve", request)

    @router.post("/{plan_id}/approve-batch", response_model=PlanSnapshot)
    async def batch(plan_id: str, request: BatchApprovalRequest, user=Depends(current_user)):
        return await graph.amutate(user["id"], plan_id, "approve-batch", request)

    @router.post("/{plan_id}/edit", response_model=PlanSnapshot)
    async def edit(plan_id: str, request: PlanEdit, user=Depends(current_user)):
        return await graph.amutate(user["id"], plan_id, "edit", request)

    @router.post("/{plan_id}/replan", response_model=PlanSnapshot)
    async def replan(plan_id: str, request: ReplanRequest, user=Depends(current_user)):
        return await graph.amutate(user["id"], plan_id, "replan", request)

    @router.post("/{plan_id}/resume", response_model=PlanSnapshot)
    async def resume(plan_id: str, user=Depends(current_user)):
        return await graph.amutate(user["id"], plan_id, "resume")

    @router.post("/{plan_id}/reconcile", response_model=PlanSnapshot)
    async def reconcile(plan_id: str, user=Depends(current_user)):
        return await graph.amutate(user["id"], plan_id, "reconcile")

    @router.post("/{plan_id}/cancel", response_model=PlanSnapshot)
    async def cancel(plan_id: str, user=Depends(current_user)):
        return await graph.amutate(user["id"], plan_id, "cancel")

    @router.post("/{plan_id}/handoff", response_model=PlanSnapshot)
    async def handoff(plan_id: str, user=Depends(current_user)):
        return await graph.amutate(user["id"], plan_id, "handoff")

    return router
