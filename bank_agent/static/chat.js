// @ts-check
import {$,act,el,jsonDetails,yuan} from "./ui.js";
import {api,watchChatTask} from "./api.js";
import {conversationStore,sessionStore,beginIntent,isCurrent} from "./state.js";
import {overview} from "./account-view.js";
import {renderPlan} from "./plan-view.js";
import {openSecurity} from "./security.js";
import {createActivity} from './activity.js';
import {renderResearch} from './research.js';

let viewController=null;
export function detachChatView(){viewController?.abort();viewController=null;}

export function message(text, role='assistant', loading=false) {
  const row = el('div', undefined, 'message ' + role + (loading ? ' loading' : ''));
  if (role === 'assistant') row.append(el('span','简','assistant-mark'));
  row.append(el('div',text)); $('messages').append(row); $('messages').scrollTop = $('messages').scrollHeight;
  return row;
}

export function incomingCard(data, row) {
  const action=data.ui_action;
  if(action?.kind!=='demo_incoming')return;
  const body=row.lastElementChild,card=el('section',undefined,'incoming-card');
  if(!body)return;
  card.append(el('span','演示账户 · 模拟转入','incoming-caption'),el('strong',yuan(action.amount_fen)),el('p','确认后更新演示余额，不会从外部银行卡扣款。'));
  const actions=el('div',undefined,'incoming-actions'),yes=el('button','确认模拟转入','primary'),no=el('button','暂不转入');
  let decided=false;
  /** @type {Array<[HTMLButtonElement,string]>} */
  const decisions=[[yes,'confirm'],[no,'cancel']];
  for(const [button,decision] of decisions)button.onclick=()=>act(null,async(epoch)=>{
    yes.disabled=no.disabled=true;
    try{
      const result=await api('/api/demo/incoming/'+encodeURIComponent(action.request_id)+'/decision',{decision});
      if(epoch!==sessionStore.epoch)return;
      decided=true;card.classList.add('settled');actions.replaceChildren(el('span',result.status==='COMPLETED'?'✓ 已模拟到账':'已取消','incoming-result'));
      message(result.message);await overview();
      if(result.receipt_id){const details=jsonDetails('查看本次模拟转入凭证',result);body.append(details);}
      const followup=result.continuation;
      if(followup?.can_resume&&conversationStore.id===followup.conversation_id){
        const resume=el('button','继续给'+followup.recipient+'转'+yuan(followup.amount_fen),'primary');
        resume.onclick=()=>act(resume,async(nextEpoch)=>{
          const resumed=await api('/api/plans/'+encodeURIComponent(followup.plan_id)+'/resume',{});
          if(nextEpoch!==sessionStore.epoch||conversationStore.id!==followup.conversation_id)return;
          renderPlan(resumed,true);await overview();resume.remove();
        });
        actions.append(resume);
      }
    }catch(error){
      if(['ACCOUNT_LOCKED','SECURITY_LOCKED'].includes(error.code)&&!actions.querySelector('.security-shortcut')){
        const security=el('button','前往安全中心','security-shortcut');security.onclick=()=>act(security,openSecurity);actions.append(security);
      }
      throw error;
    }finally{yes.disabled=no.disabled=decided;}
  });
  actions.append(yes,no);card.append(actions);body.append(card);$('messages').scrollTop=$('messages').scrollHeight;
}

export function removeWaiting(row) { row.remove();$('messages').scrollTop=$('messages').scrollHeight; }

/** @param {HTMLElement} row @param {any} sources */
export function informationSources(row,sources){
  if(!Array.isArray(sources))return;
  const body=row.lastElementChild;if(!body)return;
  const links=el('div',undefined,'conversation-sources');
  for(const source of sources){
    let url;try{url=new URL(source.url);}catch{continue;}
    if(url.protocol!=='https:'||!['yield.chinabond.com.cn','www.jsfund.cn'].includes(url.hostname))continue;
    const link=el('a',(source.title||'官方来源')+(source.data_date?' · '+source.data_date:''));
    link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';links.append(link);
  }
  if(links.childElementCount)body.append(links);
}

/** @param {HTMLElement} row @param {any[]} [candidates] @param {any[]} [excluded] */
export function informationCandidates(row,candidates=[],excluded=[]){
  if(!candidates.length&&!excluded.length)return;
  const body=row.lastElementChild;if(!body)return;
  const section=el('section',undefined,'etf-candidates');section.append(el('h3','产品资料与条件核对'));
  for(const product of candidates){
    const card=el('article',undefined,'etf-candidate'),p=product.profile||{},check=product.condition_check||{};
    card.append(el('strong',product.name+' · '+product.code));
    card.append(el('p','风险等级：'+(p.risk_level?'R'+p.risk_level:'待核实')+'；管理费率：'+(p.management_fee_pct!=null?p.management_fee_pct+'%':'待核实')+'；托管费率：'+(p.custody_fee_pct!=null?p.custody_fee_pct+'%':'待核实')));
    card.append(el('p','最低投入：'+(p.minimum_fen!=null?yuan(p.minimum_fen):'待核实')+'；按金额定投：'+(p.supports_amount_dca===true?'资料注明支持，仍需核对渠道':p.supports_amount_dca===false?'当前资料不支持':'待核实')));
    if(check.passed_checks?.length)card.append(el('p','已核对：'+check.passed_checks.join('、')));
    if(check.missing?.length)card.append(el('p','待核实：'+check.missing.join('、'),'risk-caption'));
    if(p.fetched_at)card.append(el('small','资料读取时间：'+new Date(p.fetched_at).toLocaleString('zh-CN')));
    card.append(el('p','资料条件核对不代表完成适合性判断，本次没有购买。','risk-caption'));
    section.append(card);
  }
  if(excluded.length){
    const details=el('details');details.append(el('summary','查看未列入候选的产品'));
    for(const product of excluded)details.append(el('p',product.name+'：'+(product.condition_check?.exclusion_reasons||[]).join('；')));
    section.append(details);
  }
  body.append(section);
}

export function setupChat(){
  $('chat').onsubmit=e=>{e.preventDefault();const text=$('input').value.trim();if(!text)return;const mark=beginIntent();act($('send'),async(epoch)=>{
  const safe=text.replace(/sk-[\w-]+/g,'[密钥已隐藏]').replace(/((?:验证码|密码)\s*[:：=]\s*)\S+|((?:验证码|密码)\s*(?:是|为)?\s*)[A-Za-z0-9][A-Za-z0-9!@#$%^&*._-]*/g,(_value,prefix,plainPrefix)=>(prefix||plainPrefix)+'[已隐藏]');message(safe,'user');$('input').value='';const wait=message('正在处理…','assistant',true);
  try{const task=await api('/api/chat/tasks',{message:text,client_request_id:crypto.randomUUID(),...(conversationStore.modelId?{model_id:conversationStore.modelId}:{}),...(conversationStore.id?{conversation_id:conversationStore.id}:{})});if(epoch!==sessionStore.epoch)return;
    conversationStore.id=task.conversation_id;if(sessionStore.user)sessionStorage.setItem('bank-chat-task-'+sessionStore.user.id,task.id);
    if(sessionStore.user)sessionStorage.setItem('bank-conversation-'+sessionStore.user.id,task.conversation_id);
    document.dispatchEvent(new CustomEvent('workspace:changed'));
    await followChatTask(task.id,wait,mark,epoch);}
  catch(error){if(epoch===sessionStore.epoch&&isCurrent(mark))message('这次回复未能完整显示。'+(error.message||'请稍后重试；涉及办理时，请先核实当前计划状态。'));throw error;}
  finally{if(!wait.classList.contains('activity-message'))removeWaiting(wait);}
});};
$('input').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))$('chat').requestSubmit();};
}

async function followChatTask(taskId,wait,mark,epoch){
  detachChatView();const controller=new AbortController();viewController=controller;
  const current=()=>epoch===sessionStore.epoch&&isCurrent(mark)&&wait.isConnected;
  const activity=createActivity(wait,current),{stop,retry}=activity;
  let settled=false,finalTask=null,displayed=false,researchIds=[],lastRevision=-1;
  stop.onclick=()=>act(stop,async()=>{
    if(!current())return;
    if(researchIds.length){
      await Promise.all(researchIds.map(id=>api('/api/research/'+encodeURIComponent(id)+'/cancel',{})));
    }else await update(await api('/api/chat/tasks/'+encodeURIComponent(taskId)+'/cancel',{}));
  });
  async function update(task){
    if(!current()||task.revision<lastRevision)return;
    lastRevision=task.revision;
    activity.update(task);
    if(task.partial?.nodes)renderPlan(task.partial,false,mark);
    const terminal=['SUCCEEDED','FAILED','CANCELLED','INTERRUPTED'].includes(task.status);
    if(terminal){settled=true;finalTask=task;stop.hidden=true;}
  }
  async function finish(){
    if(!finalTask||displayed||!current())return;
    let result=finalTask.result||finalTask.partial;
    if(result?.nodes)result=await api('/api/plans/'+encodeURIComponent(result.id));
    const studies=result?.status?.startsWith('RESEARCH_')?[result]:(result?.request_goals||[]).filter(g=>g.kind==='research'&&g.status!=='CANCELLED'&&g.result?.id).map(g=>g.result);
    researchIds=studies.filter(s=>s.status==='RESEARCH_RUNNING').map(s=>s.id);
    if(researchIds.length){stop.hidden=false;stop.textContent='停止分析';stop.setAttribute('aria-label','停止本次分析');}
    const outcomes=await Promise.all(studies.map(async study=>{
      let latest=await api('/api/research/'+encodeURIComponent(study.id));
      while(current()){
        activity.add('research:'+study.id,latest.stage||'核查研究资料');
        renderResearch(latest,false);
        if(latest.status!=='RESEARCH_RUNNING')return latest;
        await new Promise(resolve=>setTimeout(resolve,1200));
        if(!current())return latest;
        latest=await api('/api/research/'+encodeURIComponent(study.id));
      }
      return latest;
    }));
    if(!current()){activity.dispose();return;}
    if(result?.nodes)result=await api('/api/plans/'+encodeURIComponent(result.id));
    else if(outcomes.length)result=outcomes[0];
    const interrupted=outcomes.find(s=>s.status!=='RESEARCH_READY');
    const state=interrupted?(interrupted.status==='RESEARCH_CANCELLED'?'CANCELLED':'INTERRUPTED'):finalTask.status;
    let answer='';
    if(result?.error)answer=result.error.message;
    else if(result)renderPlan(result,true,mark,text=>{answer=text;return wait;});
    if(['CANCELLED','INTERRUPTED'].includes(finalTask.status))answer=[answer,finalTask.label].filter(Boolean).join('\n');
    if(!answer)answer=finalTask.label;
    activity.complete(state,outcomes.length?null:finalTask.updated_at);
    displayed=true;
    await activity.type(answer);
    if(epoch===sessionStore.epoch&&sessionStore.user){const key='bank-chat-task-'+sessionStore.user.id;if(sessionStorage.getItem(key)===taskId)sessionStorage.removeItem(key);}
    if(current()){await overview();document.dispatchEvent(new CustomEvent('workspace:changed'));}
  }
  async function watch(){
    retry.hidden=true;
    try{if(!settled){await watchChatTask(taskId,update,controller.signal);if(!settled&&current())await update(await api('/api/chat/tasks/'+encodeURIComponent(taskId)));}await finish();}
    catch(error){if(current())activity.disconnected();else activity.dispose();}
  }
  retry.onclick=()=>act(retry,watch);await watch();
}

export function restoreChatTask(){
  const taskId=sessionStore.user&&sessionStorage.getItem('bank-chat-task-'+sessionStore.user.id);
  if(!taskId)return;
  const mark=beginIntent(),wait=message('正在恢复任务进度…','assistant',true);
  act($('send'),async(epoch)=>{await followChatTask(taskId,wait,mark,epoch);});
}
