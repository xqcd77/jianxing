from fastapi import APIRouter, Depends
from pydantic import Field

from .contracts import PlanSnapshot, SupportCaseRecord
from .schemas import StrictModel


class SupportMessage(StrictModel):
    text: str = Field(min_length=1, max_length=500)


def support_router(service, current_user):
    router = APIRouter(prefix="/api/support/cases", tags=["业务协助工单"])

    @router.get("", response_model=list[SupportCaseRecord])
    def cases(user=Depends(current_user)):
        return service.list(user["id"])

    @router.get("/{case_id}", response_model=SupportCaseRecord)
    def get(case_id: str, user=Depends(current_user)):
        return service.get(user["id"], case_id)

    @router.post("/{case_id}/messages", response_model=SupportCaseRecord)
    def message(case_id: str, request: SupportMessage, user=Depends(current_user)):
        service.message(user["id"], case_id, request.text)
        return service.get(user["id"], case_id)

    @router.post("/{case_id}/demo-review", response_model=SupportCaseRecord)
    def demo_review(case_id: str, user=Depends(current_user)):
        service.demo_review(user["id"], case_id)
        return service.get(user["id"], case_id)

    @router.post("/{case_id}/continue", response_model=PlanSnapshot)
    async def continue_case(case_id: str, user=Depends(current_user)):
        return await service.continue_case(user["id"], case_id)

    @router.post("/{case_id}/close", response_model=SupportCaseRecord)
    def close(case_id: str, user=Depends(current_user)):
        service.close(user["id"], case_id)
        return service.get(user["id"], case_id)

    @router.post("/{case_id}/reopen", response_model=SupportCaseRecord)
    def reopen(case_id: str, user=Depends(current_user)):
        service.reopen(user["id"], case_id)
        return service.get(user["id"], case_id)

    return router
