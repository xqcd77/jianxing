"""Supervisor fan-out/fan-in, source checks, synthesis and independent review.

Feedback belongs to this outer LangGraph, never to the financial business DAG.
The pre-existing services remain untouched for historical evaluation replay.
"""

import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from functools import partial
import re
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .deep_research import DeepResearchService, ResearchAnswer
from .errors import BankError
from .research import ResearchService, ResearchState, merge
from .research_math import model_units
from .supervisor_math import ResearchDetails, checked_details, financial_facts, fact_claims
from .research_tools import READS
from .storage import audit
from .supervisor_agents import SupervisorModel


class SupervisedResearchState(ResearchState, total=False):
    candidate: dict
    semantic_review: dict
    research_details: dict
    role_returns: Annotated[dict, merge]


class ResearchSpecialist:
    def __init__(self, role, service):
        self.role = role
        self._service = service
        self.allowed_reads = frozenset(READS[role])

    async def run(self, state):
        # Neither the other specialist's evidence nor its rationale is an input.
        isolated = {
            **{k: state[k] for k in ('id', 'user_id', 'conversation_id', 'query', 'constraints',
                                    'brief', 'baseline', 'round', 'research_details') if k in state},
            "role": self.role,
            "evidence": {k: v for k, v in state["evidence"].items() if v["role"] == self.role},
            "results": {self.role: state["results"][self.role]} if self.role in state["results"] else {},
        }
        return await self._service._researcher(isolated)


class SupervisorResearchService(DeepResearchService):
    def __init__(self, *args):
        super().__init__(*args)
        self.model = SupervisorModel(self.settings)
        self.specialists = {role: ResearchSpecialist(role, self) for role in ("bills", "wealth")}
        self.verifier = self.banking.semantic_verifier
        builder = StateGraph(SupervisedResearchState)
        builder.add_node("supervisor", self._supervisor)
        builder.add_node("bills_research", self.specialists["bills"].run)
        builder.add_node("wealth_research", self.specialists["wealth"].run)
        builder.add_node("verify_sources", self._verify)
        builder.add_node("supervisor_followup", self._followup)
        builder.add_node("synthesize", self._synthesize)
        builder.add_node("independent_review", self._independent_review)
        builder.add_node("publish", self._publish)
        builder.add_edge(START, "supervisor")
        builder.add_conditional_edges("supervisor", self._dispatch_specialists, ["bills_research", "wealth_research"])
        builder.add_edge("bills_research", "verify_sources")
        builder.add_edge("wealth_research", "verify_sources")
        builder.add_conditional_edges("verify_sources", self._source_route, ["supervisor_followup", "synthesize"])
        builder.add_conditional_edges(
            "supervisor_followup", self._dispatch_specialists, ["bills_research", "wealth_research"]
        )
        builder.add_edge("synthesize", "independent_review")
        builder.add_conditional_edges("independent_review", self._semantic_route, ["supervisor_followup", "publish"])
        builder.add_edge("publish", END)
        self.graph = builder.compile(checkpointer=self.banking.checkpointer)

    async def _supervisor(self, state):
        result = await super()._supervisor(state)
        query = result.get("query", state["query"])
        details = {}
        if self.settings.model_mode == "llm" and re.search(r"互斥|方案[甲乙AB]|[甲乙]留|\d\s*(?:点|时|:|：)", query):
            payload = {
                "request": query,
                "needed_on": result["brief"]["needed_on"],
                "instruction": "只提取用户明确说明的互斥现金方案及用款时刻，不设计新方案。金额为整数分；"
                "alternatives.name取原文标签如甲、乙；retained_cash是保留现金而非实际预留指令。"
                "needed_at为含+08:00的ISO日期时间，必须对应needed_on和原文时分；无明确时刻则null。"
                "income_delay_days仅取明确延期天数。",
            }
            for attempt in range(2):
                try:
                    extracted = await self.model.astructured("details", payload, ResearchDetails)
                    details = checked_details(extracted, query, result["brief"]["needed_on"])
                    break
                except (BankError, ValueError) as error:
                    payload = {**payload, "correction": str(error)[:120]}
                    await self._log(state, "RESEARCH_DETAILS_RETRY", {"attempt": attempt + 1})
        updated = {**state, **result, 'research_details': details}
        handoff = await self._handoff(updated, 'supervisor', 'research_constraints', {
            'kind': 'research_brief', 'brief': result['brief'],
            'active_roles': result['active_roles'], 'research_details': details})
        return {**result, 'research_details': details, **handoff}

    async def _handoff(self, state, role, stage, content, *, status='complete', open_items=(), next_reads=(), evidence=None):
        store = self.model.harness.role_returns
        if store is None:
            return {}
        return_id = await self.banking._atomic(partial(store.record,
            owner_id=state['user_id'], conversation_id=state['conversation_id'], task_id=state['id'],
            role=role, stage=stage, revision=state['round'], source_fingerprint=state['baseline'],
            status=status, content=content, evidence=list(state['evidence'].values()) if evidence is None else evidence,
            open_items=open_items, next_reads=next_reads))
        return {'role_returns': {role + ':' + stage: return_id}}

    async def _verify(self, state):
        checked = await super()._verify(state)
        packets = {}
        verification = checked['verification']
        for role in state['active_roles']:
            result = state['results'][role]
            missing = [*result['missing'], *result.get('review_feedback', [])]
            if result.get('invalid_claim_ids') or result.get('rejected_reads'):
                missing.append('角色输出含无效引用或越界读取，待修正')
            if result.get('model_error'):
                missing.append('角色模型暂不可用：' + result['model_error'])
            if not verification['fresh']:
                missing.append('银行状态已变化，须重新读取')
            returned = await self._handoff(state, role + '_research', 'specialist_research', {
                'kind': 'specialist', 'claims': verification['verified'][role]['claims'],
                'focus_claim_ids': result.get('focus_claim_ids', [])},
                status='partial' if missing else 'complete', open_items=missing,
                next_reads=result.get('requested_reads', []),
                evidence=[e for e in state['evidence'].values() if e['role'] == role])
            packets.update(returned.get('role_returns', {}))
        return {**checked, 'role_returns': packets}

    @staticmethod
    def _dispatch_specialists(state):
        return [Send(role + "_research", {**state, "role": role}) for role in state["active_roles"]]

    def _source_route(self, state):
        v = state["verification"]
        return (
            "supervisor_followup" if v["fresh"] and v["repairs"] and state["round"] < self.max_rounds else "synthesize"
        )

    async def _followup(self, state):
        # Only unresolved roles are rescheduled; already verified peer results remain.
        result = await self._targeted(state)
        await self._log(
            state,
            "SUPERVISOR_RESEARCH_FOLLOWUP",
            {
                "roles": result["active_roles"],
                "round": result["round"],
            },
        )
        return {**result, "candidate": {}, "semantic_review": {}}

    async def _log(self, state, event, details):
        def write():
            with self.db.write() as session:
                audit(session, state["user_id"], event, {"research_id": state["id"], **details})

        await self.banking._atomic(write)

    async def _synthesize(self, state):
        await asyncio.to_thread(self._update, state["user_id"], state["id"], stage="汇总研究结论", round=state["round"])
        if (
            self.settings.model_mode != "llm"
            or not state["verification"]["fresh"]
            or any(r.get("invalid_claim_ids") or r.get("rejected_reads") for r in state["results"].values())
        ):
            return await self._candidate(state, {'method': 'verified_rules'})
        evidence, sources = self._answer_evidence(state)
        program_facts = financial_facts(state)
        # Synthesis consumes facts and citations, not researchers' private rationale.
        payload = {
            "user_request": state["query"],
            "constraints": model_units(state["brief"]),
            "evidence": model_units(evidence),
            "source_documents": model_units(sources),
            "program_verified_financial_facts": program_facts,
            "no_financial_write_executed": True,
        }
        store = self.model.harness.role_returns
        if store is not None:
            payload['role_returns'] = {}
            for role in state['results']:
                key = role + '_research:specialist_research'
                return_id = state.get('role_returns', {}).get(key)
                if not return_id:
                    raise BankError('ROLE_RETURN_MISSING', '角色交接尚未完成，请重新研究', 409)
                packet = await asyncio.to_thread(store.model_input, return_id,
                    owner_id=state['user_id'], conversation_id=state['conversation_id'],
                    task_id=state['id'], reader='supervisor')
                if packet['source_fingerprint'] != state['baseline']:
                    raise BankError('ROLE_RETURN_STALE', '角色交接依据已变化，请重新研究', 409)
                payload['role_returns'][role] = model_units(packet)
        else:
            payload['specialist_findings'] = {role: {'claims': model_units(result['claims']),
                'missing': result['missing']} for role, result in state['results'].items()}
        payload["review_feedback"] = [
            text for result in state["results"].values() for text in result.get("review_feedback", [])
        ]
        error = None
        for attempt in range(2):
            try:
                raw = await self.model.astructured("analyst", payload, ResearchAnswer)
                draft = ResearchAnswer.model_validate(raw.model_dump())
                findings, computations = self._checked_answer(draft, state["evidence"], state["query"], program_facts)
                return await self._candidate(state, {
                        "method": "supervisor_synthesis",
                        "findings": findings,
                        "calculations": computations,
                        "financial_facts": program_facts,
                    })
            except (BankError, ValueError, InvalidOperation, ZeroDivisionError, SyntaxError) as exc:
                error = exc.code if isinstance(exc, BankError) else str(exc)[:120]
                await self._log(state, "RESEARCH_ANSWER_RETRY", {"attempt": attempt + 1, "error": error})
                payload = {**payload, "output_correction": "修正结构、引用或计算错误：" + error}
        await self._log(state, "RESEARCH_ANSWER_FALLBACK", {"error": error})
        return await self._candidate(state, {'method': 'verified_rules', 'model_error': error})

    async def _candidate(self, state, candidate):
        missing = [*state['verification']['gaps']]
        if candidate.get('model_error'):
            missing.append('汇总模型暂不可用：' + candidate['model_error'])
        if not state['verification']['fresh']:
            missing.append('银行状态已变化，须重新读取')
        handoff = await self._handoff(state, 'supervisor', 'research_synthesis', {
            'kind': 'synthesis', 'method': candidate['method'],
            'findings': candidate.get('findings', []), 'calculations': candidate.get('calculations', [])},
            status='partial' if missing else 'complete', open_items=missing)
        return {'candidate': candidate, **handoff}

    async def _independent_review(self, state):
        evidence, sources = self._answer_evidence(state)
        refs = {e["id"] for e in state["evidence"].values()}
        refs.update(source for e in state["evidence"].values() for source in e.get("source_ids", []))
        refs.update(s["source_id"] for s in sources)
        # Raw source documents are explicitly untrusted evidence, not instructions.
        shown = state["candidate"].get("findings")
        payload = {
            "kind": "research",
            "original_request": state["query"],
            "protected_constraints": model_units(state["brief"]),
            "candidate_findings": shown + fact_claims(state)
            if shown
            else fact_claims(state) or [c for r in state["verification"]["verified"].values() for c in r["claims"]],
            "candidate_products": []
            if state["candidate"].get("findings")
            else state["verification"]["verified"].get("wealth", {}).get("products", []),
            "evidence": model_units(evidence),
            "source_documents": model_units(sources),
            "evidence_ids": sorted(refs),
            "program_financial_facts": financial_facts(state),
            "financial_write_executed": False,
        }
        await asyncio.to_thread(
            self._update, state["user_id"], state["id"], stage="独立核验结论与需求", round=state["round"]
        )
        review = await self.verifier.review(payload)
        await self._log(state, "INDEPENDENT_RESEARCH_REVIEW", {"round": state["round"], **review})
        v = deepcopy(state["verification"])
        results = deepcopy(state["results"])
        for issue in review["issues"]:
            if not issue["blocking"]:
                continue
            roles = [issue["target"]] if issue["target"] in {"bills", "wealth"} else list(results)
            for role in roles:
                if role not in results:
                    continue
                if role not in v["repairs"]:
                    v["repairs"].append(role)
                allowed_reads = [name for name in issue["reads"] if name in READS[role]]
                results[role]["requested_reads"] = list(
                    dict.fromkeys([*results[role].get("requested_reads", []), *allowed_reads])
                )
                results[role]["review_feedback"] = [issue["detail"]]
        v["fresh"] = (
            v["fresh"] and await asyncio.to_thread(self.tools.fingerprint, state["user_id"]) == state["baseline"]
        )
        handoff = await self._handoff(state, 'verifier', 'independent_review', {
            'kind': 'review', **{k: review[k] for k in ('passed', 'method', 'issues', 'input_digest')}},
            status='complete' if review['passed'] and v['fresh'] else 'blocked',
            open_items=[i['detail'] for i in review['issues']] + v['gaps'] +
                       (['银行状态已变化，须重新读取'] if not v['fresh'] else []) +
                       ([review['error_code']] if review.get('error_code') else []))
        return {'semantic_review': review, 'verification': v, 'results': results, **handoff}

    def _semantic_route(self, state):
        review, v = state["semantic_review"], state["verification"]
        return (
            "supervisor_followup"
            if not review["passed"]
            and review["method"] == "independent_llm"
            and v["fresh"]
            and v["repairs"]
            and state["round"] < self.max_rounds
            else "publish"
        )

    async def _researcher(self, state):
        # DeepResearchService handles source reads and deterministic claims. Feed
        # semantic repair goals to the same role without sharing its peer's context.
        feedback = state.get("results", {}).get(state["role"], {}).get("review_feedback", [])
        if feedback:
            state = {**state, "query": state["query"] + "\n核验待解决问题（不是新的用户授权）：" + "；".join(feedback)}
        result = await super()._researcher(state)
        if feedback:
            result["results"][state["role"]]["review_feedback"] = feedback
        return result

    async def _publish(self, state):
        state = deepcopy(state)
        await self.banking._atomic(partial(self._update, state['user_id'], state['id'],
            role_return_ids=state.get('role_returns', {})))
        review = state["semantic_review"]
        v = state["verification"]
        v["fresh"] = (
            v["fresh"] and await asyncio.to_thread(self.tools.fingerprint, state["user_id"]) == state["baseline"]
        )
        if not review["passed"]:
            v["gaps"] = [*v["gaps"], "部分研究结论尚未通过独立核验，请补充资料或重新核查。"]
            # Publish only reproducible, source-bound facts on degradation. A
            # failed semantic review still blocks all actionable options.
            safe = fact_claims(state)
            if safe and v["fresh"]:
                v["verified"]["bills"]["claims"] = [*safe, *v["verified"]["bills"]["claims"]]
                if "wealth" in v["verified"]:
                    v["verified"]["wealth"]["products"] = []
        elif state["candidate"].get("findings") and v["fresh"]:
            v["verified"]["bills"]["claims"] = state["candidate"]["findings"] + fact_claims(state)
            if "wealth" in v["verified"]:
                v["verified"]["wealth"]["claims"] = []
                v["verified"]["wealth"]["products"] = []
            await self._log(
                state,
                "RESEARCH_ANSWER_VERIFIED",
                {
                    "calculations": state["candidate"]["calculations"],
                    "program_financial_facts": state["candidate"]["financial_facts"],
                    "sources": [e["id"] for e in state["evidence"].values()],
                },
            )
        return await ResearchService._report(self, state)

    def _decorate_report(self, state, report):
        review = state["semantic_review"]
        # Both ordinary and degraded reports must use the same budget envelope.
        # The earlier per-role funding card omitted separately proposed transfers.
        budget = next((f for f in financial_facts(state) if f['kind']=='unified_budget'),None)
        if budget:
            def fen(key):
                return int(Decimal(budget[key])*100)
            report['funding'].update(
                new_reserve_fen=fen('new_reserve_yuan'),
                proposed_transfer_fen=fen('new_proposed_transfer_yuan'),
                investable_ceiling_fen=fen('maximum_new_investment_yuan'),
                whole_plan_gap_fen=fen('whole_plan_gap_yuan'))
            account = next(e for e in state['evidence'].values() if e['tool'] == 'account')
            text=(f"当前银行快照：总余额{Decimal(account['data']['balance_fen'])/100}元，"
                  f"已有预留占用{budget['existing_hold_already_excluded_yuan']}元，可用{budget['available_yuan']}元。"
                  f"新增生活费预留{Decimal(state['brief']['reserve_fen'])/100}元、"
                  f"应急预留{Decimal(state['brief']['emergency_fen'])/100}元，"
                  f"转账计划合计{Decimal(budget['new_proposed_transfer_yuan']) + Decimal(budget['existing_pending_transfer_yuan'])}元"
                  f"（新增{budget['new_proposed_transfer_yuan']}元，已有未执行{budget['existing_pending_transfer_yuan']}元），"
                  f"已登记扣费{budget['registered_future_dues_yuan']}元，其他支出{budget['additional_user_declared_expenses_yuan']}元，"
                  f"已有未执行计划{budget['existing_pending_plan_yuan']}元。依次扣除后最多可投{budget['maximum_new_investment_yuan']}元；"
                  f"原拟投{budget['requested_investment_yuan']}元，整份需求缺口{budget['whole_plan_gap_yuan']}元。"
                  '已有预留已从可用额扣除，不重复扣减；仅测算，不擅自调整目标或办理资金操作。')
            if re.search(r'昨天|昨日|旧报告|之前.{0,8}报告|上次.{0,8}报告', state['query']):
                text += '旧报告属于历史快照，余额、已登记扣费和待执行计划变化后，旧上限失效；本次以上述银行现状重新计算。'
            for claim in report['findings']:
                if claim['id'].endswith(':funding'):
                    claim.update(text=text,values=dict(report['funding']))
            if report['summary'].startswith(('整份安排：','当前可用')):
                report['summary']=text
        report = {
            **report,
            "architecture": "supervisor_four_specialists",
            "independent_review": {k: review[k] for k in ("passed", "method")},
            "synthesis_method": state["candidate"]["method"],
            "orchestration_roles": [
                "supervisor",
                *[r + "_research" for r in state["verification"]["verified"]],
                "verifier",
            ],
        }
        suspicious = [
            e["id"]
            for e in state["evidence"].values()
            if e["tool"] in {"product_terms", "wealth.compare"}
            and re.search(
                r"忽略.{0,12}(?:权限|规则|指令|安全)|跳过.{0,8}(?:确认|验证)|申购已完成|购买已完成|ignore previous",
                str(e["data"]),
                re.I,
            )
        ]
        if suspicious:
            report["findings"].append(
                {
                    "id": "wealth:source_instruction_boundary",
                    "kind": "limitation",
                    "values": {},
                    "evidence_ids": suspicious,
                    "text": "已识别产品资料中夹带的异常操作指令或完成声明，已隔离为待核查资料，不作为执行指令或银行回执。"
                    "资料声称交易完成不代表实际成交；办理状态只能依据银行回执。其他可核查的账单和产品字段仍分别分析，本次未据此办理资金操作。",
                }
            )
        if "wealth" in state["verification"]["verified"]:
            excluded = state['brief'].get('excluded_product_ids', [])
            if excluded:
                account = next(e for e in state['evidence'].values() if e['tool'] == 'account')
                report['findings'].append({'id': 'wealth:preference_boundary', 'kind': 'limitation',
                    'values': {'excluded_product_ids': excluded}, 'evidence_ids': [account['id']],
                    'text': '已按本次有效要求与保存的偏好排除' + '、'.join(excluded)
                        + f"；当前银行测评为R{account['data']['risk_level']}。偏好筛选与银行风险适当性分别核对，"
                          '排除项不作为可选方案；偏好不等于本金或收益保证。本轮尚未申购，未来办理仍须本人另行选择、核对并授权。'})
            refs = [e["id"] for e in state["evidence"].values() if e["tool"] in {"product_terms", "wealth.compare"}]
            report["findings"].append(
                {
                    "id": "wealth:risk_boundary",
                    "kind": "limitation",
                    "values": {},
                    "evidence_ids": refs,
                    "text": "满足风险测评、起购或赎回条件仅代表候选资格，不代表本金或收益保证；"
                    "产品可能发生本金损失，当前净值测算也不保证未来赎回足额，办理前须重新核对。",
                }
            )
        if re.search(r"预授权|重复.{0,5}扣|扣款|争议", state["query"]):
            refs = [e["id"] for e in state["evidence"].values() if e["tool"] == "transaction_details"]
            if refs:
                report["findings"].append(
                    {
                        "id": "bills:dispute_next_step",
                        "kind": "limitation",
                        "values": {},
                        "evidence_ids": refs,
                        "text": "如你仍不认可核查结果，请凭本次展示的订单号、商户订单和银行入账回执逐项核对；"
                        "仍有差异时可通过银行交易争议入口或客服申请核查。当前未发起退款，也未取消合同或代扣。",
                    }
                )
        if not review["passed"]:
            report["quality"] = "review_incomplete"
            report["quality_note"] = (
                "核验服务暂时不可用；以下保留已核查的事实，暂不提供办理方案，请稍后重新分析。"
                if review["method"] == "unavailable"
                else "以下保留已核查的原始事实；候选结论未通过独立核验，暂不提供可办理方案。"
            )
        return report
