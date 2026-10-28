// @ts-check
import {accountStore,conversationStore,formStore,planStore,researchStore,sessionStore} from "./state.js";
import {api} from "./api.js";
import {$,act,businessText,el,jsonDetails,modal,states,toast,yuan} from "./ui.js";
import {closeBusinessPanel,renderBusinessCategories} from "./business.js";
import {overview,renderOverview} from "./account-view.js";
import {renderPlan} from "./plan-view.js";
import {openResearchForm,renderResearch,resetResearch} from "./research.js";
import {openSecurity} from "./security.js";
import {setupChat} from './chat.js';
import {clearReads,beginIntent,isCurrent,ticket} from './state.js';
import {openSupport} from './support-view.js';
import {enterWorkspace,setupPremiumMotion} from './premium.js';
import {initializeWorkspaceControls,resetWorkspaceControls,setupWorkspaceControls} from './workspace-controls.js';

export async function initialize() {
  const mark=ticket('session-init');
  const [user,security,caps,catalog,data]=await Promise.all([api('/api/auth/me'),api('/api/security/status'),api('/api/capabilities'),api('/api/businesses'),api('/api/overview')]);
  if(!isCurrent(mark))return;
  sessionStore.user=user;$('username').textContent=user.name;$('demo-panel').hidden=!user.demo_admin;sessionStore.demoVerification=security.demo_verification;
  conversationStore.id=sessionStorage.getItem('bank-conversation-'+user.id)||null;accountStore.capabilities=caps.operations;accountStore.facts=data;renderBusinessCategories(catalog);renderOverview(data);
  startLive();await initializeWorkspaceControls();enterWorkspace();
}

export function clearSession() {
  resetWorkspaceControls();
  clearReads();planStore.cache.clear();
  resetResearch();
  $('support-summary').hidden=true;$('support-summary').replaceChildren();
  closeBusinessPanel();$('scenarios').replaceChildren();
  if(sessionStore.user)sessionStorage.removeItem('bank-conversation-'+sessionStore.user.id);
  if(sessionStore.user)sessionStorage.removeItem('bank-prerequisite-'+sessionStore.user.id);
  sessionStore.epoch++;sessionStore.token='';sessionStorage.removeItem('bank-token');sessionStore.liveController?.abort();planStore.current=null;conversationStore.id=null;sessionStore.user=null;accountStore.facts=null;planStore.liveSignature='';sessionStore.demoVerification=false;
  $('nodes').replaceChildren();$('detail-content').replaceChildren();$('holds').textContent='暂无预留安排';$('notices').textContent='暂时没有新的提醒';$('messages').replaceChildren();$('plan-summary').textContent='请登录后查看自己的业务安排。';
  $('plan-status').textContent='等待安排';$('plan-status').className='badge neutral';$('plan-progress').hidden=true;$('plan-actions').hidden=true;$('dag-view').hidden=true;
  for(const key of ['balance','available','reserved','clock'])$(key).textContent='—';$('username').textContent='尚未登录';$('detail-dialog').close();
  $('login-password').value='';if(!$('login-dialog').open)$('login-dialog').showModal();
}

export function startLive() {
  sessionStore.liveController?.abort();sessionStore.liveController=new AbortController();const controller=sessionStore.liveController,epoch=sessionStore.epoch;
  (async()=>{while(sessionStore.token&&!controller.signal.aborted&&epoch===sessionStore.epoch){try{
    const response=await fetch('/api/live',{headers:{Authorization:'Bearer '+sessionStore.token},signal:controller.signal});
    if(!response.ok||!response.body)throw new Error('同步连接不可用');const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
    $('sync-status').textContent='账户同步正常';document.body.classList.remove('offline');
    while(!controller.signal.aborted){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let end;
      while((end=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,end);buffer=buffer.slice(end+1);if(!line)continue;const update=JSON.parse(line);
        if(epoch!==sessionStore.epoch)return;if(update.type==='session_expired'){clearSession();return;}
        if(update.type==='snapshot'){ticket('overview');accountStore.facts={...accountStore.facts,account:{...accountStore.facts?.account,...update.account},business_now:update.business_now,holds:update.holds,notifications:update.notifications};renderOverview(update);const mark=update.plans.find(p=>p.id===planStore.current?.id);
          const signature=mark?[mark.version,mark.status,mark.signature,update.account.balance_fen,update.account.reserved_fen].join(':'):'';
          if(mark&&planStore.current&&!sessionStore.busy&&signature!==planStore.liveSignature){const id=planStore.current.id,mark=ticket('plan-snapshot');const fresh=await api('/api/plans/'+id);if(epoch===sessionStore.epoch&&planStore.current?.id===id&&isCurrent(mark)){renderPlan(fresh);planStore.liveSignature=signature;}}
        }
      }
    }
  }catch(error){if(controller.signal.aborted||epoch!==sessionStore.epoch)return;$('sync-status').textContent='连接恢复中';document.body.classList.add('offline');}
    await new Promise(resolve=>setTimeout(resolve,3000));
  }})();
}

$('login').onsubmit=e=>{e.preventDefault();$('login-error').textContent='';act(e.submitter,async()=>{try{const result=await api('/api/auth/login',{username:$('login-name').value,password:$('login-password').value});sessionStore.epoch++;sessionStore.token=result.access_token;sessionStorage.setItem('bank-token',sessionStore.token);$('login-password').value='';$('login-dialog').close();await initialize();}catch(error){$('login-error').textContent=error.message;}});};

$('payroll').onclick=()=>act($('payroll'),async()=>{const result=await api('/api/demo/payroll',{event_id:'ui-'+crypto.randomUUID(),amount_fen:600000});toast('模拟工资 '+yuan(result.amount_fen)+' 已入账');await overview();});

$('advance').onclick=()=>act($('advance'),async()=>{const result=await api('/api/demo/clock',{days:1});toast('业务日期已推进至 '+result.business_now.slice(0,10));await overview();if(planStore.current)renderPlan(await api('/api/plans/'+planStore.current.id));});

for(const id of ['refresh-plan','resume','handoff'])$(id).onclick=()=>act($(id),async(epoch)=>{if(!planStore.current)return;const target=planStore.current.id,mark=ticket(id==='refresh-plan'?'plan-snapshot':'plan-action');const result=await api('/api/plans/'+target+(id==='refresh-plan'?'':'/'+id),id==='refresh-plan'?undefined:{});if(epoch!==sessionStore.epoch||planStore.current?.id!==target)return;renderPlan(result,false,mark);await overview();});

$('cancel').onclick=()=>{if(!planStore.current)return;const intent=beginIntent(),target=planStore.current,root=modal('取消剩余安排');root.append(el('p','未执行的步骤将停止，未使用的预留预算将释放。已经完成的转账和申购会保留，不能直接撤回。','muted'));const stamp=formStore.modalVersion,yes=el('button','确认取消剩余安排','primary');yes.onclick=()=>act(yes,async(epoch)=>{const result=await api('/api/plans/'+target.id+'/cancel',{});if(epoch!==sessionStore.epoch||!isCurrent(intent)||planStore.current?.id!==target.id)return;if(stamp===formStore.modalVersion)$('detail-dialog').close();renderPlan(result,false,intent);await overview();});const actions=el('div',undefined,'modal-actions');actions.append(yes);root.append(actions);};

$('history').onclick=()=>act($('history'),async(epoch)=>{const mark=ticket('history-view'),stamp=formStore.modalVersion;const [results,studies]=await Promise.all([api('/api/plans'),api('/api/research')]);if(epoch!==sessionStore.epoch||!isCurrent(mark)||stamp!==formStore.modalVersion)return;const root=modal('任务记录');if(!results.length&&!studies.length)root.append(el('p','暂无办理或分析记录。','muted'));for(const item of results){const button=el('button',undefined,'history-item');button.append(el('b',businessText(item.summary)),el('small',(states[item.status]||item.status)+' · '+item.nodes.length+' 步'));button.onclick=()=>{$('detail-dialog').close();conversationStore.id=item.conversation_id;renderPlan(item,false,beginIntent());};root.append(button);}if(studies.length)root.append(el('h3','研究记录'));for(const item of studies){const button=el('button',undefined,'history-item');button.append(el('b',item.report?.title||'收支与理财研究'),el('small',item.stage+' · 资金操作另行授权'));button.onclick=()=>{$('detail-dialog').close();renderPlan(item,false,beginIntent());};root.append(button);}});

$('audit').onclick=()=>act($('audit'),async(epoch)=>{const mark=ticket('audit-view'),stamp=formStore.modalVersion,result=await api('/api/audit');if(epoch!==sessionStore.epoch||!isCurrent(mark)||stamp!==formStore.modalVersion)return;const root=modal('操作凭证');root.append(el('p',result.chain_verified?'本地凭证哈希链校验通过；记录包含计划、授权和银行结果。':'凭证校验未通过，请暂停办理并核实。','muted'));for(const item of result.events.slice(-60).reverse())root.append(jsonDetails(item.kind+' · '+item.created_at.slice(0,19).replace('T',' '),item));});

$('security').onclick=()=>act($('security'),openSecurity);
$('support').onclick=()=>act($('support'),()=>openSupport());

$('logout').onclick=()=>act($('logout'),async()=>{await api('/api/auth/logout',{});clearSession();});

$('detail-close').onclick=()=>$('detail-dialog').close();

$('workspace').onclick=()=>window.scrollTo({top:0,behavior:'smooth'});

$('detail-dialog').addEventListener('close',()=>{formStore.modalVersion++;$('detail-content').replaceChildren();});

document.querySelectorAll('[data-nav]').forEach(button=>{if(button instanceof HTMLElement&&button.dataset.nav)button.onclick=()=>$(button.dataset.nav||'').click();});

document.addEventListener('visibilitychange',()=>document.body.classList.toggle('effects-paused',document.hidden));

$('model').onclick=()=>{
  if(!sessionStore.user){toast('请先登录，再填写研究需求。');if(!$('login-dialog').open)$('login-dialog').showModal();return;}
  openResearchForm('combined');
};
api('/health').then(data=>{
  const mode=data.model_mode==='llm'?(data.model_thinking?'已启用深度推理':'已启用智能规划'):'本地演示';
  $('model').title=mode+' · 点击填写收支与理财研究需求';
}).catch(()=>{$('model').title='服务暂未连接，研究需求可在连接恢复后提交';});

setupChat();setupWorkspaceControls();setupPremiumMotion();sessionStore.onExpired=clearSession;
// Read-only migration facade for the existing browser acceptance scripts.
Object.assign(window,{api,overview,renderPlan:(data,speak=false)=>renderPlan(data,speak,beginIntent()),renderOverview});
for(const [name,read] of Object.entries({accountUser:()=>sessionStore.user,facts:()=>accountStore.facts,plan:()=>planStore.current,conversation:()=>conversationStore.id,activeResearch:()=>researchStore.current}))Object.defineProperty(window,name,{get:read,configurable:true});
if(sessionStore.token)act(null,initialize);else $('login-dialog').showModal();
