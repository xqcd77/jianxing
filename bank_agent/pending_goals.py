"""Fill a deferred task on its original plan; never reconstruct old transfers."""
import asyncio
import re

from .schemas import DomainTask, Intent, NodeSpec, ReplanRequest, PlanEdit
from .storage import audit


async def continue_pending_goal(banking,user_id,conversation_id,message,agenda):
    pending=[g for g in agenda['goals'] if g['kind']=='bank_task' and g['status'] in {'NEEDS_INPUT','NEEDS_RECHECK'}]
    facts=await asyncio.to_thread(banking.planner.bank.facts,user_id)
    products=[p for p in facts['products'] if re.fullmatch(
        r'\s*(?:我)?\s*(?:选择|选定|选|就选|改选|要)?\s*'+re.escape(p['name'])+r'[。！!\s]*',message)]
    contacts=[c for c in facts['contacts'] if c.get('verified') and any(re.fullmatch(
        r'\s*(?:收款人\s*(?:是|选|选择|改为)\s*)?'+re.escape(label)+r'[。\s]*',message)
        for label in [c['name'],c.get('phone',''),*c.get('aliases',[])] if label)]
    if len(products)==1:
        pending=[g for g in pending if g['pending_task']['action']=='wealth.purchase']
        fields={'product_id':products[0]['id']}
    elif len(contacts)==1:
        pending=[g for g in pending if g['pending_task']['action']=='funds.transfer' and not g['pending_task']['params'].get('recipient_hint')]
        fields={'recipient_hint':contacts[0]['name']}
    else:
        return None
    if not pending:
        editable=[g for g in agenda['goals'] if g['kind']=='bank_operation' and g.get('operation')=='wealth.purchase'
            and g['status'] in {'FAILED','WAIT_INPUT','WAIT_MFA','READY','WAIT_CONFIRM'}]
        if products and len(editable)==1:
            plan=await asyncio.to_thread(banking.plans.snapshot,user_id,agenda['plan_id'])
            node=next(n for n in plan['nodes'] if n['id']==editable[0]['node_id'])
            if node['params'].get('product_id')!=products[0]['id']:
                return await banking.amutate(user_id,plan['id'],'edit',PlanEdit(node_id=node['id'],
                    expected_version=plan['version'],params={**node['params'],'product_id':products[0]['id']}))
        same=next((g for g in agenda['goals'] if g['kind']=='bank_operation' and g.get('operation')=='wealth.purchase'
            and products and (g.get('params') or {}).get('product_id')==products[0]['id']),None)
        return await asyncio.to_thread(banking.plans.snapshot,user_id,agenda['plan_id']) if same else None
    if len(pending)!=1:
        return {'status':'NEEDS_CLARIFICATION','conversation_id':conversation_id,
                'message':'请明确为第几项理财需求选择产品，其他目标已保留。','request_goals':agenda['goals']}
    async with banking.process_locks.ahold(f'goal:{user_id}:{conversation_id}'):
        from .goal_state import read_agenda
        from .request_goals import persist_goal_agenda
        latest=await asyncio.to_thread(read_agenda,banking,user_id,conversation_id)
        goal=next((g for g in latest['goals'] if g['id']==pending[0]['id'] and g['status'] in {'NEEDS_INPUT','NEEDS_RECHECK'}),None)
        if not goal:
            return await asyncio.to_thread(banking.plans.snapshot,user_id,latest['plan_id'])
        task=DomainTask.model_validate(goal['pending_task'])
        task.params={**task.params,**fields}
        plan=await asyncio.to_thread(banking.plans.snapshot,user_id,latest['plan_id']) if latest.get('plan_id') else None
        if goal['status']=='NEEDS_RECHECK' and task.action=='wealth.purchase':
            from .request_understanding import remaining_wealth_budget
            unfinished=[DomainTask(domain=n['agent'],action=n['operation'],params=n['params']) for n in plan['nodes']
                        if n['status'] not in {'SUCCEEDED','CANCELLED','SKIPPED','PARTIAL'}]
            fresh=(remaining_wealth_budget(goal.get('source_text',''),facts,unfinished)
                   if (goal.get('slots') or {}).get('amount_source')!='explicit_update' else None)
            if fresh is None:
                plan['assistant_message']+='\n资金安排已调整，请先明确新的理财预算，旧金额不会直接申购。'
                return plan
            task.params['amount_fen']=fresh
        # Keep the source-computed amount and transfer dependencies unchanged.
        banking.planner.bank.validate_params(task.action,task.params)
        if plan and set(task.depends_on)-{n['local_id'] for n in plan['nodes']}:
            goal.update(pending_task=task.model_dump(mode='json'),status='NEEDS_INPUT',revision=goal.get('revision',1)+1,
                notice='已保留本次选择；请先补齐前序转账的收款人或金额，其他已明确步骤可以继续。')
            await banking._atomic(persist_goal_agenda,banking.planner.db,user_id,conversation_id,plan,
                [g for g in latest['goals'] if g['kind']!='bank_operation'],[goal['id']],latest['revision'])
            return await asyncio.to_thread(banking.plans.snapshot,user_id,plan['id'])
        if plan:
            existing=[NodeSpec(id=n['local_id'],agent=n['agent'],operation=n['operation'],params=n['params'],
                depends_on=n['depends_on'],after=n['after'],scheduled_for=n['scheduled_for'],reason=n['reason']) for n in plan['nodes']]
            task.id=task.id or 'goal_'+goal['id'][:20]
            added=NodeSpec(id=task.id,agent=task.domain,operation=task.action,params=task.params,depends_on=task.depends_on,
                          reason='用户为已保留的目标明确补充必填字段')
            result=await banking.amutate(user_id,plan['id'],'replan',ReplanRequest(expected_version=plan['version'],
                reason=f'用户原话“{message}”；为原理财目标补充产品，不重做已完成转账',nodes=[*existing,added]))
        else:
            result=await banking.astart(user_id,message,conversation_id,intent=Intent(summary='补充理财产品',tasks=[task]))
        node=next(n for n in result['nodes'] if n['local_id']==task.id)
        goal.update(kind='bank_operation',node_id=node['id'],operation=node['operation'],params=node['params'],status=node['status'])
        goal.pop('pending_task',None)
        extras=[g for g in latest['goals'] if g['kind']!='bank_operation' or g['id']==goal['id']]
        await banking._atomic(persist_goal_agenda,banking.planner.db,user_id,conversation_id,result,extras)
        with banking.planner.db.write() as session:
            audit(session,user_id,'DEFERRED_GOAL_FILLED',{'goal_id':goal['id'],'fields':list(fields)},result['id'])
        return await asyncio.to_thread(banking.plans.snapshot,user_id,result['id'])
