// @ts-check
import {$,act,el,modal,toast} from './ui.js';
import {api} from './api.js';
import {conversationStore,formStore,sessionStore,beginIntent,isCurrent,ticket} from './state.js';
import {renderPlan} from './plan-view.js';
import {overview} from './account-view.js';

const labels={PENDING:'等待核查',ACTION_REQUIRED:'可继续办理',FOLLOW_UP:'等待业务办理',RESOLVED:'已办结',CLOSED:'已关闭'};

/** @param {string|null} [caseId] */
export async function openSupport(caseId=null){
  const mark=ticket('support-view'),stamp=formStore.modalVersion,epoch=sessionStore.epoch;
  const response=await api('/api/support/cases'+(caseId?'/'+encodeURIComponent(caseId):''));
  if(epoch!==sessionStore.epoch||!isCurrent(mark)||stamp!==formStore.modalVersion)return;
  const root=modal('业务协助工单'),version=formStore.modalVersion;
  root.append(el('p','协助会保留原业务和处理记录。核查结果只提供下一步，资金与合同变更仍需你确认。','muted'));
  const cases=caseId?[response]:response;
  if(!cases.length)root.append(el('p','暂无协助工单。余额不足、风险测评过期或安全锁定，可先按办理卡片提示自助处理。','muted'));
  for(const item of cases){
    const card=el('section',undefined,'support-card'),head=el('div',undefined,'support-head');
    head.append(el('h3',item.title),el('span',labels[item.status]||'处理中','badge '+(item.status==='RESOLVED'?'GREEN':'neutral')));card.append(head,el('p',item.message,'muted'),el('p',item.next_step,'support-next'));
    const history=el('ol',undefined,'support-timeline');for(const event of item.timeline){const row=el('li');row.append(el('small',new Date(event.at).toLocaleString('zh-CN',{hour12:false})),el('p',event.text));history.append(row);}card.append(history);
    const buttons=el('div',undefined,'support-actions');
    const current=()=>sessionStore.epoch===epoch&&formStore.modalVersion===version&&$('detail-dialog').open;
    if(item.status==='CLOSED'){
      const reopen=el('button','重新申请核查');reopen.onclick=()=>act(reopen,async()=>{await api('/api/support/cases/'+item.id+'/reopen',{});if(current())await openSupport(caseId);});buttons.append(reopen);
    }
    if(['PENDING','ACTION_REQUIRED','FOLLOW_UP'].includes(item.status)){
      if(sessionStore.user?.demo_admin){
        const review=el('button',item.status==='PENDING'?'模拟客服核查':'更新演示核查');
        review.onclick=()=>act(review,async()=>{const result=await api('/api/support/cases/'+item.id+'/demo-review',{});if(current()){if(result.status==='FOLLOW_UP')toast('已有待办计划，请先查看并确认；条件变化时可以更新核查。');await openSupport(caseId);}});buttons.append(review);
      }
      if(item.status==='ACTION_REQUIRED'&&item.reason_code!=='EVIDENCE_UNAVAILABLE'){
        const resume=el('button','继续核对办理','primary');resume.onclick=()=>act(resume,async()=>{
          const intent=beginIntent(),result=await api('/api/support/cases/'+item.id+'/continue',{});
          if(epoch!==sessionStore.epoch||!isCurrent(intent))return;
          if(current())$('detail-dialog').close();renderPlan(result,true,intent);await overview();
        });buttons.append(resume);
      }
      const label=el('label','补充处理信息'),text=el('textarea');text.maxLength=500;text.rows=2;label.append(text);card.append(label);
      const submit=el('button','提交补充');submit.onclick=()=>act(submit,async()=>{if(!text.value.trim()){toast('请先填写需要补充的信息。');return;}await api('/api/support/cases/'+item.id+'/messages',{text:text.value.trim()});if(current())await openSupport(caseId);});buttons.append(submit);
      const close=el('button','关闭协助');close.onclick=()=>act(close,async()=>{await api('/api/support/cases/'+item.id+'/close',{});if(current())await openSupport(caseId);});buttons.append(close);
    }
    if(item.reason_code==='EVIDENCE_UNAVAILABLE'&&item.status!=='RESOLVED'){
      const again=el('button','重新描述需求');again.onclick=()=>{beginIntent();conversationStore.id=null;if(sessionStore.user)sessionStorage.removeItem('bank-conversation-'+sessionStore.user.id);$('detail-dialog').close();$('input').focus();toast('请重新描述本次需求，将按当前资料核查；原计划仍保留在任务记录中。');};buttons.append(again);
    }
    const planId=item.followup_plan_id||item.plan_id,view=el('button',item.status==='RESOLVED'?'查看办理结果':item.followup_plan_id?'查看待办计划':'查看原业务');
    view.onclick=()=>act(view,async()=>{const intent=beginIntent(),result=await api('/api/plans/'+planId);if(!current()||!isCurrent(intent))return;$('detail-dialog').close();renderPlan(result,false,intent);});buttons.append(view);card.append(buttons);root.append(card);
  }
  if(sessionStore.user?.demo_admin)root.append(el('p','模拟客服仅用于本项目演示，不会联系真实银行或商户，也不会直接执行转账、退款或取消合同。','research-note'));
}

/** @param {import('./api-contracts').SupportCaseRecord[]} cases */
export function renderSupportBanner(cases){
  const root=$('support-summary');root.replaceChildren();root.hidden=!cases.length;
  for(const item of cases){
    const row=el('div',undefined,'support-summary-row'),text=el('div');text.append(el('strong',item.title+' · '+labels[item.status]),el('p',item.next_step,'muted'));
    const button=el('button','查看协助进度');button.onclick=()=>act(button,()=>openSupport(item.id));row.append(text,button);root.append(row);
  }
}
