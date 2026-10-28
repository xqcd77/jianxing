"""Unified business agent, deterministic domain services and gated semantic review."""

import asyncio
from functools import partial
from typing import Annotated

from langgraph.graph import END, START, StateGraph

from .deep_banking import DeepBankingGraph
from .graph import State
from .memory import protected_context
from .operations import DEFINITIONS
from .schemas import Intent, NodeSpec
from .presentation import yuan
from .storage import audit
from .research import merge
from .research_tools import digest


class BusinessState(State, total=False):
    # No parallel domain LLMs: service compilation replaces proposals each revision.
    proposals: list[dict]
    revision: int
    supplied_intent: bool
    semantic_review: dict
    role_returns: Annotated[dict, merge]


class SupervisorBankingGraph(DeepBankingGraph):
    async def _initialize(self, data_dir, _legacy_builder):
        builder = StateGraph(BusinessState)
        builder.add_node("supervisor", self._delegate_business)
        builder.add_node("business", self._aplan)
        builder.add_node("business_services", self._business_services)
        builder.add_node("validate_dag", self._areview)
        builder.add_node("independent_review", self._independent_review)
        builder.add_node("revise_business", self._revise_business)
        builder.add_node("compile_plan", self._acompile)
        builder.add_node("verify", self._verify)
        builder.add_node("backtrack", self._backtrack)
        builder.add_node("replan", self._replan)
        builder.add_node("execute", self._aexecute)
        builder.add_node("await_input", self._await_input)
        builder.add_node("finish", self._finish)
        builder.add_edge(START, "supervisor")
        builder.add_edge("supervisor", "business")
        builder.add_conditional_edges("business", self._business_route, ["business_services", END])
        builder.add_edge("business_services", "validate_dag")
        builder.add_conditional_edges("validate_dag", self._review_route, ["independent_review", "compile_plan"])
        builder.add_conditional_edges(
            "independent_review", self._semantic_route, ["compile_plan", "revise_business", END]
        )
        builder.add_conditional_edges("revise_business", self._business_route, ["business_services", END])
        builder.add_edge("compile_plan", "verify")
        builder.add_conditional_edges("verify", self._verify_route, ["backtrack", "execute", "await_input"])
        builder.add_conditional_edges("backtrack", self._backtrack_route, ["replan", "verify", "await_input"])
        builder.add_edge("replan", "verify")
        builder.add_conditional_edges("execute", self._execution_route, ["await_input", "finish"])
        builder.add_edge("await_input", "verify")
        builder.add_edge("finish", END)
        await super()._initialize(data_dir, builder)

    async def _delegate_business(self, state):
        await self._atomic(
            self._audit_role,
            state["user_id"],
            "SUPERVISOR_DELEGATED",
            {
                "conversation_id": state["conversation_id"],
                "role": "business",
                "scope": "explicit_business_request",
            },
        )
        handoff = await self._handoff(state, 'supervisor', 'business_dispatch',
            {'kind': 'dispatch', 'targets': ['business']})
        return {'revision': 0, 'supplied_intent': bool(state.get('intent')), **handoff}

    async def _handoff(self, state, role, stage, content, *, status='complete', open_items=()):
        store = self.planner.model.harness.role_returns
        if store is None:
            return {}
        return_id = await self._atomic(partial(store.record,
            owner_id=state['user_id'], conversation_id=state['conversation_id'],
            task_id=state['thread_id'], role=role, stage=stage, revision=state.get('revision', 0),
            source_fingerprint=digest(state.get('specs', [])),
            status=status, content=content, open_items=open_items))
        return {'role_returns': {role + ':' + stage: return_id}}

    async def _planned_return(self, state, result):
        if not result.get('intent'):
            return result
        intent = result['intent']
        missing = [text for text in (intent.get('clarification'), intent.get('reject_reason')) if text]
        missing.extend(g.get('message') or g['title'] for g in intent.get('additional_goals', []))
        handoff = await self._handoff({**state, **result, 'specs': result.get('specs', [])}, 'business', 'business_planning', {
            'kind': 'business', 'intent': intent, 'nodes': result.get('specs', [])},
            status='partial' if missing else 'complete', open_items=missing)
        return {**result, **handoff}

    async def _aplan(self, state):
        return await self._planned_return(state, await super()._aplan(state))

    async def _areview(self, state):
        # Domain services bind recipients/products and enrich nodes. Hand off
        # their validated revision, not the earlier unbound planning draft.
        result = await super()._areview(state)
        return await self._planned_return(state, {'intent': state['intent'], **result})

    def _audit_role(self, user_id, event, details):
        with self.planner.db.write() as session:
            audit(session, user_id, event, details)

    @staticmethod
    def _business_route(state):
        return END if state.get("response") else "business_services"

    async def _business_services(self, state):
        proposals = await asyncio.to_thread(self.planner.compile_services, state["specs"], state["user_id"])
        return {"proposals": proposals}

    def _review(self, state):
        result = super()._review(state)
        review = result["review"]
        review["business_service_count"] = review.pop("agent_count")
        review["orchestration_roles"] = ["supervisor", "business"]
        review["agent_count"] = 2
        review["architecture"] = "supervisor_four_specialists"
        return result

    @staticmethod
    def _review_route(state):
        # One explicit operation stays on the short path. Complex compositions
        # receive a separate semantic check in addition to mandatory rule checks.
        tasks = state.get("intent", {}).get("tasks", [])
        changes = any(DEFINITIONS[t["action"]][2] != "GREEN" for t in tasks)
        return "independent_review" if len(tasks) > 1 and changes else "compile_plan"

    async def _independent_review(self, state):
        preview = await asyncio.to_thread(
            self.plans.simulator.for_specs,
            state["user_id"],
            [NodeSpec.model_validate(n) for n in state["specs"]],
            state["review"],
        )
        pending_budget=sum(g.get('pending_task',{}).get('params',{}).get('amount_fen',0)
            for g in state['intent'].get('additional_goals',[]) if g.get('kind')=='bank_task'
            and (g.get('pending_task') or {}).get('action') in {'funds.transfer','funds.reserve','wealth.purchase','aa.pay'})
        preview['required_fen']+=pending_budget
        preview['shortfall_fen']=max(preview['shortfall_fen'],preview['required_fen']-preview['before']['available_fen'],0)
        global_blockers=[b for b in preview['blockers'] if b.get('category')!='business']
        if preview['shortfall_fen'] or global_blockers:
            parts = []
            for task in state["intent"]["tasks"]:
                amount = task["params"].get("amount_fen")
                if amount and task["action"] in {"funds.reserve", "funds.transfer", "wealth.purchase"}:
                    label = {"funds.reserve": "预留", "funds.transfer": "转账", "wealth.purchase": "申购"}[
                        task["action"]
                    ]
                    parts.append(label + yuan(amount))
            message = (
                "；".join(parts)
                + f"。整份计划需要{yuan(preview['required_fen'])}，当前可用{yuan(preview['before']['available_fen'])}。"
            )
            if preview["shortfall_fen"]:
                message += f"资金缺口{yuan(preview['shortfall_fen'])}。三项全部可行并完成授权后才能执行，本次一项都未办理。请补足缺口或重新选择各项金额后再核对。".replace(
                    "三项", "各项"
                )
            else:
                message += "整份计划尚未满足办理条件，本次未执行。" + preview["blockers"][0]["message"]
            draft = Intent.model_validate(state["intent"]).model_copy(update={"clarification": message})
            return await self._atomic(self._plan, {**state, "intent": draft.model_dump(mode="json")})
        facts = await asyncio.to_thread(self.planner.bank.facts, state["user_id"])
        packets = {}
        store = self.planner.model.harness.role_returns
        if store is not None:
            return_id = state.get('role_returns', {}).get('business:business_planning')
            if not return_id:
                from .errors import BankError
                raise BankError('ROLE_RETURN_MISSING', '业务交接尚未完成', 409)
            packet = await asyncio.to_thread(store.model_input, return_id, owner_id=state['user_id'],
                conversation_id=state['conversation_id'], task_id=state['thread_id'], reader='verifier')
            if packet['source_fingerprint'] != digest(state['specs']):
                from .errors import BankError
                raise BankError('ROLE_RETURN_STALE', '业务草案已变化，请重新核对', 409)
            packets['business'] = packet
        review = await self.semantic_verifier.review(
            {
                "kind": "business",
                "original_request": state["message"],
                "additional_goals": state['intent'].get('additional_goals', []),
                "protected_constraints": state["review"]["protected"],
                "candidate_nodes": state["specs"],
                "current_facts": facts,
                "whole_plan_preflight": preview,
                "evidence_ids": [],
                "financial_write_executed": False,
                'authorization_workflow': {
                    'candidate_stage': 'uncommitted_business_draft',
                    'details_presented_by': 'ui_plan_cards',
                    'confirmation_and_mfa_enforced_by': 'independent_bank_gateway',
                    'confirmation_is_dag_operation': False,
                    'model_can_authorize': False,
                },
                "role_returns": packets,
            }
        )
        review["plan_version"] = 1
        result = {
            **state["review"],
            "semantic_review": review,
            "orchestration_roles": ["supervisor", "business", "verifier"],
            "agent_count": 3,
        }
        await self._atomic(
            self._audit_role,
            state["user_id"],
            "INDEPENDENT_BUSINESS_REVIEW",
            {
                "conversation_id": state["conversation_id"],
                "revision": state["revision"],
                **review,
            },
        )
        handoff = await self._handoff(state, 'verifier', 'independent_review', {
            'kind': 'review', **{k: review[k] for k in ('passed', 'method', 'issues', 'input_digest')}},
            status='complete' if review['passed'] else 'blocked',
            open_items=[i['detail'] for i in review['issues']] +
                       ([review['error_code']] if review.get('error_code') else []))
        output = {'review': result, 'semantic_review': review, **handoff}
        can_revise = (
            not state["supplied_intent"]
            and review["method"] == "independent_llm"
            and state["revision"] < max(0, min(self.planner.settings.max_replans, 3))
        )
        if not review["passed"] and not can_revise:
            message = (
                "核验服务暂时不可用，尚未提交这份办理计划。原需求已保留，请稍后重试。"
                if review["method"] == "unavailable"
                else "这份组合办理计划仍有条件需要核对，尚未执行。请核对各项金额、对象和办理范围后重新提交。"
            )
            draft = Intent.model_validate(state["intent"]).model_copy(update={"clarification": message})
            blocked = await self._atomic(self._plan, {**state, "intent": draft.model_dump(mode="json")})
            output.update(blocked)
        return output

    @staticmethod
    def _semantic_route(state):
        if state.get("response"):
            return END
        return "compile_plan" if state["semantic_review"]["passed"] else "revise_business"

    async def _revise_business(self, state):
        intent = await self.planner.arevise(
            state["user_id"],
            state["message"],
            state["conversation_id"],
            state["intent"],
            state["semantic_review"]["issues"],
        )
        result = await self._atomic(
            self._plan, {**state, "intent": Intent.model_validate(intent).model_dump(mode="json")}
        )
        updated = {**result, 'revision': state['revision'] + 1}
        return await self._planned_return(state, updated)

    async def _validate_append(self, user_id, message, conversation_id, plan, existing, added, context, facts):
        await asyncio.to_thread(self.reviewer.review, existing + added)
        review = await self.semantic_verifier.review(
            {
                "kind": "business",
                "original_request": message,
                "protected_constraints": protected_context(message, context, facts).model_dump(mode="json"),
                "existing_plan": {"id": plan["id"], "version": plan["version"], "nodes": plan["nodes"]},
                "candidate_nodes": [n.model_dump(mode="json") for n in existing + added],
                "new_nodes": [n.id for n in added],
                "current_facts": facts,
                "evidence_ids": [],
                "instruction": "只核对本次追加与既有安排的冲突，已完成步骤不是本次新增操作。",
                "financial_write_executed": False,
            }
        )
        await self._atomic(
            self._audit_role,
            user_id,
            "INDEPENDENT_APPEND_REVIEW",
            {
                "plan_id": plan["id"],
                "expected_version": plan["version"],
                **review,
            },
        )
        if not review["passed"]:
            return {
                "status": "NEEDS_CLARIFICATION",
                "conversation_id": conversation_id,
                "execution_mode": "sandbox",
                "message": "追加安排尚未通过核对，原计划与已完成回执已保留。请核对新增业务的金额、对象和范围后再提交。",
            }
        return None
