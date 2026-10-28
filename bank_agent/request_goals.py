"""Track a banking action and its read-only information goal independently."""

from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
import re

from .dialogue import explicit_transfers, transfer_grounding
from .information import information_intent, investment_amounts
from .presentation import yuan
from .storage import Document, Plan, audit, documents, save_document


def persist_goal_agenda(db, user_id, conversation_id, response, extra_goals, updated_goal_ids=None, expected_revision=None, result_only=False):
    """Owned task state, not semantic memory: never expires with a history window."""
    goals = [{"id": n['id'], "kind":"bank_operation", "title":n['operation'],
              "status":n['status'], "node_id":n['id'], 'params':n['params'], 'operation':n['operation']} for n in response.get('nodes', [])]
    with db.write() as session:
        from .errors import BankError
        key = f'goal_agenda_{user_id}_{conversation_id}'
        current = session.get(Document,key)
        if (current and current.owner_id == user_id and current.data.get('plan_id') and not response.get('id')
                and extra_goals and all(g['kind'] in {'clarification', 'capability_gap'} for g in extra_goals)):
            # A side conversation without a banking task must not replace the
            # durable task anchor. Its answer remains in the dialogue history.
            return goals + extra_goals
        if expected_revision is not None and (not current or current.data.get('revision',1) != expected_revision):
            raise BankError('GOAL_VERSION_CONFLICT', '需求已更新，请刷新后重新修改', 409)
        previous = session.get(Document, 'request_goals_'+response.get('id','')) if response.get('id') else current
        previous_data = previous.data if previous and previous.owner_id == user_id else {}
        bank_ids={g.get('node_id'):g['id'] for g in [*previous_data.get('goals',[]),*extra_goals]
                  if g['kind']=='bank_operation'}
        for goal in goals:
            goal['id']=bank_ids.get(goal['node_id'],goal['id'])
        if result_only and previous_data:
            incoming = {g['id']:g for g in extra_goals}
            changes = {g['id']:incoming[g['id']] for g in previous_data['goals'] if g['id'] in incoming
                and g['status']!='CANCELLED' and g.get('revision',1)==incoming[g['id']].get('revision',1)
                and g.get('query')==incoming[g['id']].get('query')}
            extra_goals = [changes.get(g['id'],g) for g in previous_data['goals'] if g['kind']!='bank_operation']
            updated_goal_ids = []  # A read result cannot refresh the plan version used to calculate its budget.
        if updated_goal_ids is not None and previous_data and not result_only:
            changes = {g['id']: g for g in extra_goals if g['id'] in updated_goal_ids}
            extra_goals = [changes.get(g['id'], g) for g in previous_data['goals'] if g['kind'] != 'bank_operation']
        extra_goals=[g for g in extra_goals if g['kind']!='bank_operation']
        goals.extend(extra_goals)
        versions = previous_data.get('goal_plan_versions', {})
        versions = {g['id']: response.get('version',1) if updated_goal_ids is not None and g['id'] in updated_goal_ids
                    else versions.get(g['id'],previous_data.get('plan_version',response.get('version',1)))
                    for g in extra_goals if g['kind'] in {'public_information','bank_task'}}
        for goal in goals:
            goal.setdefault('revision', 1)
        data = {'conversation_id':conversation_id,'plan_id':response.get('id'),
                'revision': (current.data.get('revision',0) if current else 0) + 1,
                'plan_version':response.get('version',1),'goal_plan_versions':versions,'goals':goals}
        if updated_goal_ids is None or not current or current.data.get('plan_id') == response.get('id'):
            save_document(session, key, 'goal_agenda', user_id, data)
        if response.get('nodes'):
            save_document(session, 'request_goals_'+response['id'], 'request_goals', user_id, data)
            plan=session.get(Plan,response['id'])
            if plan and plan.status=='COMPLETED' and any(g['kind']=='bank_task' and g['status']!='CANCELLED' for g in goals):
                plan.status='WAIT_INPUT'
        audit(session,user_id,'REQUEST_GOALS_RECORDED',{'conversation_id':conversation_id,
            'plan_id':response.get('id'),'goal_count':len(goals)})
    return goals


async def complete_goal_agenda(banking, user_id, conversation_id, response, intent):
    import asyncio
    if not intent:
        return response
    if intent.get('information_query') and not intent.get('additional_goals'):
        query = intent['information_query']
        extras = [{'id': __import__('uuid').uuid4().hex, 'kind':'public_information',
            'title':'ETF资料与投资条件' if query.get('asset_type')=='etf' else '公开资料查询',
            'source_text': intent.get('summary',''), 'query':query, 'result':deepcopy(response),
            'status':'UNAVAILABLE' if response.get('source_status')=='unavailable' else
                'NEEDS_INPUT' if response.get('suitability')=='needs_verification' else 'ANSWERED'}]
        response['request_goals'] = await banking._atomic(persist_goal_agenda,banking.planner.db,user_id,
            conversation_id,response,extras)
        return response
    if not intent.get('additional_goals'):
        if response.get('nodes'):
            await banking._atomic(persist_goal_agenda,banking.planner.db,user_id,conversation_id,response,[])
            return await asyncio.to_thread(banking.plans.snapshot,user_id,response['id'])
        return response
    from .schemas import AdditionalGoal
    notes = [AdditionalGoal.model_validate(g) for g in intent['additional_goals']]
    extras = [{'id':__import__('uuid').uuid4().hex, 'kind':g.kind, 'title':g.title, 'source_text':g.source_text,
               'status':'NEEDS_RETRY' if g.information_query else 'UNSUPPORTED' if g.kind=='capability_gap' else 'NEEDS_INPUT',
               'notice':g.message, 'query':g.information_query.model_dump(mode='json') if g.information_query else None,
               'pending_task':g.pending_task.model_dump(mode='json') if g.pending_task else None,
               'operation':g.pending_task.action if g.pending_task else None,
               'params':g.pending_task.params if g.pending_task else None,
               'research_query':g.research_query}
              for i,g in enumerate(notes)]
    from .advisory_budget import bind_budget_rule, derive_budget
    facts = await asyncio.to_thread(banking.planner.bank.facts, user_id)
    for index, goal in enumerate(extras):
        if goal['kind'] == 'public_information' and goal.get('query'):
            rule = bind_budget_rule(goal)
            if rule:
                from .schemas import PublicInformationQuery
                amount, notice = derive_budget(rule, facts, response.get('nodes', []))
                query = PublicInformationQuery.model_validate({**goal['query'], 'budget_fen': amount})
                goal.update(budget_rule=rule, query=query.model_dump(mode='json'), notice=notice)
                notes[index] = notes[index].model_copy(update={'information_query': query})
    await banking._atomic(persist_goal_agenda,banking.planner.db,user_id,conversation_id,response,extras)
    from .progress import publish_progress
    partial = await asyncio.to_thread(banking.plans.snapshot,user_id,response['id']) if response.get('nodes') else {
        **response, 'conversation_id':conversation_id, 'request_goals':extras,
        'message':'已保留本次各项目标，正在核查公开资料；尚未执行资金操作。'}
    publish_progress('goals_ready',f'已识别{len(partial.get("request_goals",[]))}项目标，正在核查资料',partial)
    async def read(index, note):
        if note.information_query:
            result = await banking.information.answer(note.information_query)
            extras[index].update(result=result,status='UNAVAILABLE' if result.get('source_status')=='unavailable' else
                                 'NEEDS_INPUT' if result.get('suitability')=='needs_verification' else 'ANSWERED')
        elif note.kind=='research' and getattr(banking,'research_service',None):
            from .research_schemas import ResearchRequest
            result=await banking.research_service.astart(user_id,ResearchRequest(
                query='只分析，不生成办理方案。'+note.research_query,conversation_id=conversation_id))
            extras[index].update(result=result,status=result['status'])
    await asyncio.gather(*(read(i,g) for i,g in enumerate(notes)))
    goals = await banking._atomic(persist_goal_agenda,banking.planner.db,user_id,conversation_id,response,extras,None,None,True)
    if response.get('nodes'):
        return await asyncio.to_thread(banking.plans.snapshot,user_id,response['id'])
    messages = [response.get('message','')]
    for g in extras:
        messages.append(g['title']+'：'+g.get('notice',''))
        if g.get('result'):
            messages.append(g['result']['message'])
    return {**response, 'request_goals':goals,'message':'\n'.join(messages)}


PROGRESS_QUESTION = re.compile(
    r"\s*(?:(?:现在|刚才|那个|这次|你)?\s*)?(?:好了吗|好了没|好了没有|完成了吗|完成了没|"
    r"(?:研究|分析|核查)(?:完了吗|完成了吗|好了没|好了吗|结果呢|的结果呢)|"
    r"进度(?:怎么样|如何|呢)|(?:研究|分析)进度(?:怎么样|如何|呢)|还要多久)\s*[？?。！!]*\s*"
)


def split_transfer_information(message, facts):
    """Only split an unambiguous verified transfer plus a public read request.

    Other bank operations, conditions and scheduling stay with the full planner.
    The original request still enters the banking graph and its audit trail.
    """
    if re.search(r"如果|假如|是否|明天|后天|预约|每月|\d+[日号点时]|\d{4}-\d{2}-\d{2}", message):
        return None
    bindings = explicit_transfers(message, facts)
    if len(bindings) != 1:
        return None
    transfer = bindings[0]
    remainder = message[:transfer["start"]] + " " + message[transfer["end"]:]
    # The legacy two-goal path must not consume a third action as ETF keywords.
    if re.search(r'会员|订阅|卡片|新卡|银行卡|取消|撤销|预留|赎回|申购|余额|账单|AA|收款|冻结|解锁|挂失',remainder,re.I):
        return None
    information = information_intent(remainder)
    if not information or not information.information_query:
        return None
    intent = transfer_grounding(transfer["text"], facts, {})
    if not intent or intent.clarification:
        return None
    query = information.information_query
    total, _ = investment_amounts(message) if query.currency == "CNY" else (None, None)
    calculation = ""
    if re.search(r"剩下|剩余|其余|余下", remainder) and total is not None:
        remaining = max(0, total - transfer["amount_fen"])
        query.budget_fen = remaining
        calculation = (f"按你说明的总资金{yuan(total)}，计划给{transfer['recipient_hint']}转{yuan(transfer['amount_fen'])}后，"
                       f"用于推荐研究的预算是{yuan(remaining)}；这项预算测算不表示转账已经完成。\n")
    return intent, query, calculation


def save_request_goals(db, user_id, plan, query, result=None):
    # Older two-goal parsing shares the same durable agenda and follow-up path.
    goals = [
            {"id": "information", "kind": "public_information", "title": "ETF推荐与条件核查" if query.asset_type == "etf" else "公开资料查询",
             "status": "NEEDS_INPUT" if result and result.get("suitability") == "needs_verification" else
                       "UNAVAILABLE" if result and result.get("source_status") == "unavailable" else
                       "ANSWERED" if result else "NEEDS_RETRY",
             "query": query.model_dump(mode="json"), "result": result},
        ]
    persist_goal_agenda(db,user_id,plan['conversation_id'],plan,goals)


def attach_request_goals(session, user_id, snapshot):
    doc = session.get(Document, "request_goals_" + snapshot["id"])
    if not doc or doc.kind != "request_goals" or doc.owner_id != user_id:
        return
    goals = deepcopy(doc.data["goals"])
    for goal in goals:
        if goal["kind"] == "bank_operation":
            node = next((n for n in snapshot["nodes"] if n["id"] == goal["node_id"]), None)
            goal["status"] = node["status"] if node else "NEEDS_RECHECK"
            if node:
                goal['params'] = node['params']
                goal['operation'] = node['operation']
                if node['operation']=='funds.transfer':
                    goal["title"] = "向" + node["params"]["recipient_hint"] + "转账" + yuan(node["params"]["amount_fen"])
                else:
                    goal['title'] = {'cards.apply':'申请' + ('虚拟卡' if node['params'].get('kind') == 'virtual' else '实体卡'),
                        'cards.restrict':'设置新卡消费限额','balance.query':'查询余额'}.get(node['operation'],node.get('reason') or '银行办理')
        elif goal['kind']=='public_information' and goal['status']!='CANCELLED' and snapshot["version"] != doc.data.get('goal_plan_versions',{}).get(goal['id'],doc.data["plan_version"]):
            goal["status"] = "NEEDS_RECHECK"
            goal["notice"] = "办理计划已修改，请重新核查推荐预算；此前资料保留为历史参考。"
        elif goal['kind']=='bank_task' and goal['status']!='CANCELLED' and snapshot['version']!=doc.data.get('goal_plan_versions',{}).get(goal['id'],doc.data['plan_version']):
            goal['status']='NEEDS_RECHECK'
            goal['notice']='资金安排已修改，请明确新的理财预算后继续，旧比例金额不直接用于申购。'
        elif goal['kind']=='research' and goal.get('result') and goal['status']!='CANCELLED':
            research=session.get(Document,goal['result']['id'])
            if research and research.owner_id==user_id and research.kind=='research':
                goal['result']={k:v for k,v in research.data.items() if k not in {'query','constraints'}}
                goal['status']=research.data['status']
    snapshot["request_goals"] = goals
    if doc.data.get('completed_history'):
        snapshot['completed_history'] = deepcopy(doc.data['completed_history'])
        snapshot['assistant_message'] += '\n' + '\n'.join(h['message'] for h in snapshot['completed_history'])
    snapshot['goal_revision'] = doc.data.get('revision',1)
    if len(goals)>1:
        labels={'WAIT_CONFIRM':'待确认','WAIT_MFA':'待验证','READY':'待前序办理','SUCCEEDED':'已完成',
                'NEEDS_INPUT':'待补充','NEEDS_RECHECK':'需重新核对','RESEARCH_RUNNING':'研究中','RESEARCH_READY':'研究完成'}
        def progress_label(goal):
            if goal['status']=='SUCCEEDED' and goal.get('operation')=='aa.create':
                return '收款请求已发出'
            return labels.get(goal['status'],'已保留')
        snapshot['assistant_message']='本次各项进度：'+ '；'.join(g['title']+'（'+progress_label(g)+'）' for g in goals)+'。\n'+snapshot['assistant_message']
    for goal in goals:
        if goal['kind']!='bank_operation':
            snapshot['assistant_message'] += '\n'+goal['title']+'：'+(goal.get('notice') or '已单独保留，请查看下方资料与条件核查。')


def activate_plan_agenda(db,user_id,response):
    """Returning from a prerequisite restores the owned original task, not a new plan."""
    with db.write() as session:
        stored=session.get(Document,'request_goals_'+response['id'])
        if stored and stored.owner_id==user_id:
            key=f"goal_agenda_{user_id}_{response['conversation_id']}"
            current=session.get(Document,key)
            if not current or current.data.get('plan_id')!=response['id']:
                value={**stored.data,'revision':max(stored.data.get('revision',1),current.data.get('revision',1) if current else 0)+1}
                save_document(session,key,'goal_agenda',user_id,value)
                stored.data=value
        dialogue=session.get(Document,f"dialogue_{user_id}_{response['conversation_id']}")
        if dialogue and dialogue.owner_id==user_id:
            dialogue.data={**dialogue.data,'active_plan_id':response['id'],'active_request_kind':'plan'}


async def continue_goal_agenda(banking, user_id, conversation_id, message):
    """Resolve one versioned goal patch; never replay previous bank tasks."""
    if not conversation_id:
        return None
    from .goal_state import read_agenda, parse_goal_patch, apply_goal_patch
    from .errors import ClarificationNeeded
    agenda = await banking._atomic(read_agenda,banking,user_id,conversation_id)
    if not agenda:
        return None
    from .pending_goals import continue_pending_goal
    pending=await continue_pending_goal(banking,user_id,conversation_id,message,agenda)
    if pending:
        await banking._atomic(banking._record_turn,user_id,conversation_id,message,pending)
        return pending
    if len(agenda['goals'])==1 and (agenda['goals'][0].get('query') or {}).get('topic')=='risk_free_rate':
        return None  # Currency/tenor follow-ups use the existing dated source contract.
    try:
        facts = await asyncio.to_thread(banking.planner.bank.facts, user_id)
        patch = parse_goal_patch(message,agenda,facts)
    except ClarificationNeeded as error:
        return {'status':'NEEDS_CLARIFICATION','message':error.message,'conversation_id':conversation_id,
                'request_goals':agenda['goals'],'goal_revision':agenda.get('revision',1),'execution_mode':'sandbox'}
    if not patch:
        return None
    response = await apply_goal_patch(banking,user_id,conversation_id,patch)
    await banking._atomic(banking._record_turn,user_id,conversation_id,message,response)
    return response


def conversation_task_status(db, user_id, conversation_id, message, banking, research):
    """A progress question reads the selected owned task, never asks an LLM."""
    if not conversation_id or not PROGRESS_QUESTION.fullmatch(message):
        return None
    with db.read() as session:
        dialogue = session.get(Document, f"dialogue_{user_id}_{conversation_id}")
        context = dict(dialogue.data) if dialogue and dialogue.owner_id == user_id and dialogue.kind == "dialogue" else {}
        kind = context.get("active_request_kind")
        if kind == "public_information" and context.get("last_information_response"):
            response = deepcopy(context["last_information_response"])
            try:
                fresh = datetime.fromisoformat(context["last_information_query_at"]) > datetime.now(timezone.utc) - timedelta(minutes=30)
            except (KeyError, ValueError, TypeError):
                fresh = False
            if not fresh:
                return {"status": "ANSWERED", "conversation_id": conversation_id, "execution_mode": "sandbox",
                        "message": "刚才的公开资料已超过本次会话的有效期，请重新查询后再核对产品条件。"}
            return {**response, "conversation_id": conversation_id}
        if kind == "plan" and context.get("active_plan_id"):
            return banking.plans.snapshot(user_id, context["active_plan_id"])
        records = [d for d in documents(session, "research", user_id) if d.data.get("conversation_id") == conversation_id]
        if not records:
            return None
        selected = next((d for d in records if d.id == context.get("active_research_id")), None)
        selected = selected or max(records, key=lambda d: (d.data.get("created_at", ""), d.id))
        research_id = selected.id
    response = research.get(user_id, research_id)
    if response["status"] == "RESEARCH_RUNNING":
        response["message"] = f"还在处理你刚才的分析。当前阶段：{response.get('stage', '核查资料')}；尚未办理任何资金操作。"
    elif response.get("report"):
        response["message"] += "\n" + response["report"].get("summary", "")
    return response
