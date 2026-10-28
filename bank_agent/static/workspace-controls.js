// @ts-check
import {$,act,el,modal,states,toast} from './ui.js';
import {api} from './api.js';
import {beginIntent,conversationStore,isCurrent,planStore,researchStore,sessionStore,ticket} from './state.js';
import {message,detachChatView,restoreChatTask} from './chat.js';
import {renderPlan} from './plan-view.js';
import {renderResearch,resetResearch} from './research.js';

let catalog=[],contextData=null,archived=false,searchTimer=null;
const number=value=>value==null?'—':Number(value).toLocaleString('zh-CN');
const activeStatuses=new Set(['QUEUED','RUNNING']);

function selection(){return catalog.find(m=>m.id===conversationStore.modelId)||catalog[0];}
function paintSelection(){
  const model=selection();if(!model)return;
  $('selected-model-icon').setAttribute('src',model.icon);$('selected-model-name').textContent=model.name;
  $('model-picker').title=model.name+' · '+(model.display_model||model.model);
}

function placePopover(panel,anchor){
  panel.showPopover();
  const rect=anchor.getBoundingClientRect(),width=Math.min(panel.offsetWidth,innerWidth-24);
  panel.style.left=Math.max(12,Math.min(rect.left,innerWidth-width-12))+'px';
  panel.style.top=Math.max(12,rect.top-panel.offsetHeight-12)+'px';
}

function renderModels(){
  const query=$('model-search').value.trim().toLocaleLowerCase(),root=$('model-options');root.replaceChildren();
  for(const model of catalog.filter(m=>(m.name+' '+m.vendor+' '+m.model).toLocaleLowerCase().includes(query))){
    const button=el('button',undefined,'model-option'),icon=el('img'),text=el('span',undefined,'model-option-text');
    button.type='button';icon.src=model.icon;icon.alt='';icon.width=26;icon.height=26;
    text.append(el('b',model.name),el('small',model.display_model||model.model));
    button.append(icon,text,el('span',model.id===conversationStore.modelId?'✓':'›','model-option-status'));
    button.setAttribute('aria-pressed',String(model.id===conversationStore.modelId));
    button.onclick=()=>act(button,epoch=>openProviderConfiguration(model,epoch));root.append(button);
  }
  if(!root.childElementCount)root.append(el('p','没有匹配的模型','workspace-empty'));
}

async function openProviderConfiguration(model,epoch){
  const data=await api('/api/models/'+encodeURIComponent(model.id)+'/config');
  if(epoch!==sessionStore.epoch||!$('model-menu').matches(':popover-open'))return;
  const panel=$('model-menu'),root=$('model-options');panel.classList.add('configuring');root.replaceChildren();
  const form=el('form',undefined,'provider-configuration'),header=el('div',undefined,'provider-config-head'),back=el('button','‹','provider-config-back'),logo=el('img'),heading=el('div');
  back.type='button';back.setAttribute('aria-label','返回厂商列表');back.onclick=()=>{key.value='';panel.classList.remove('configuring');renderModels();placePopover(panel,$('model-picker'));};
  logo.src=model.icon;logo.alt='';logo.width=30;logo.height=30;heading.append(el('h3',model.name),el('small',model.vendor));header.append(back,logo,heading);form.append(header);
  const keyLabel=el('label',undefined,'provider-field'),keyRow=el('div',undefined,'provider-key-row'),key=el('input'),show=el('button','显示','provider-key-toggle');
  key.id='provider-api-key';key.type='password';key.autocomplete='new-password';key.maxLength=4096;key.spellcheck=false;key.setAttribute('aria-label','API Key');
  key.placeholder=data.has_personal_key?'已保存，留空保持原Key':data.has_saved_key?'使用默认Key，或填写你的Key':'填写该厂商的API Key';
  key.required=!data.has_saved_key;show.type='button';show.setAttribute('aria-label','显示或隐藏API Key');show.setAttribute('aria-pressed','false');
  show.onclick=()=>{const visible=key.type==='password';key.type=visible?'text':'password';show.textContent=visible?'隐藏':'显示';show.setAttribute('aria-pressed',String(visible));};
  keyRow.append(key,show);keyLabel.append(el('span','API Key'),keyRow);form.append(keyLabel);
  const modelLabel=el('div',undefined,'provider-field'),caption=el('div',undefined,'provider-model-caption'),fetchModels=el('button','获取可用模型','provider-fetch'),select=el('select'),custom=el('input');
  select.id='provider-concrete-model';select.setAttribute('aria-label','具体模型');custom.id='provider-custom-model';custom.placeholder='填写厂商提供的模型ID';custom.setAttribute('aria-label','自定义模型ID');custom.maxLength=150;custom.hidden=true;
  fetchModels.type='button';caption.append(el('span','具体模型'),fetchModels);modelLabel.append(caption,select,custom);form.append(modelLabel);
  function options(ids,selected){select.replaceChildren();for(const id of [...new Set(ids)]){const option=el('option',id);option.value=id;select.append(option);}const manual=el('option','自定义模型ID…');manual.value='__custom__';select.append(manual);select.value=ids.includes(selected)?selected:ids[0];}
  options(data.options,data.model);select.onchange=()=>{custom.hidden=select.value!=='__custom__';custom.required=!custom.hidden;if(!custom.hidden)custom.focus();};
  const note=el('p',data.has_personal_key?'已保存此账户的Key，填写新Key可替换。':'Key加密保存在当前账户，不进入聊天记录。','provider-config-note'),error=el('p',undefined,'provider-config-error'),save=el('button','保存并使用','primary provider-save');error.setAttribute('role','alert');save.type='submit';
  fetchModels.onclick=()=>act(fetchModels,async nextEpoch=>{error.textContent='';try{const result=await api('/api/models/'+encodeURIComponent(model.id)+'/options',{...(key.value.trim()?{api_key:key.value.trim()}:{})});if(nextEpoch!==sessionStore.epoch||!form.isConnected)return;const current=select.value;options(result.models,current);custom.hidden=true;custom.required=false;note.textContent='已读取该Key可用的 '+result.models.length+' 个模型。';placePopover(panel,$('model-picker'));}catch(failure){if(nextEpoch===sessionStore.epoch&&form.isConnected)error.textContent=failure.message;}});
  form.onsubmit=event=>{event.preventDefault();act(save,async nextEpoch=>{
    error.textContent='';const id=conversationStore.id,chosen=select.value==='__custom__'?custom.value.trim():select.value;
    try{
      await api('/api/models/'+encodeURIComponent(model.id)+'/config',{model:chosen,...(key.value.trim()?{api_key:key.value.trim()}:{})});
      key.value='';if(nextEpoch!==sessionStore.epoch||conversationStore.id!==id)return;
      const latest=await api('/api/models');if(nextEpoch!==sessionStore.epoch)return;catalog=latest.models;
      if(!id){const fresh=await api('/api/conversations',{model_id:model.id});if(nextEpoch!==sessionStore.epoch)return;conversationStore.id=fresh.conversation_id;persistConversation();}
      else {await api('/api/conversations/'+encodeURIComponent(id),{model_id:model.id});if(nextEpoch!==sessionStore.epoch||conversationStore.id!==id)return;}
      conversationStore.modelId=model.id;paintSelection();panel.hidePopover();await refreshContext();toast('已保存，后续请求使用 '+chosen);
    }catch(failure){if(nextEpoch===sessionStore.epoch&&form.isConnected)error.textContent=failure.message;}
  });};
  form.append(note,error,save);root.append(form);placePopover(panel,$('model-picker'));key.focus();
}

function persistConversation(){
  if(sessionStore.user&&conversationStore.id)sessionStorage.setItem('bank-conversation-'+sessionStore.user.id,conversationStore.id);
}

function clearConversationView(){
  detachChatView();beginIntent();resetResearch();researchStore.current=null;planStore.current=null;planStore.liveSignature='';
  contextData=null;$('context-meter-label').textContent='上下文';$('context-meter').style.setProperty('--context-used','0%');
  $('context-meter').classList.remove('context-warning');$('context-popover').hidePopover();$('model-menu').hidePopover();
  $('messages').replaceChildren();$('nodes').replaceChildren();$('plan-summary').textContent='计划、授权和结果，都会在这里清晰呈现。';
  $('plan-status').textContent='等待安排';$('plan-status').className='badge neutral';
  for(const id of ['plan-progress','plan-actions','dag-view','funding-preview','support-summary'])$(id).hidden=true;
  $('research-panel').hidden=true;$('conversation-title').textContent='说说你的安排';
}

export async function openConversation(id){
  const mark=ticket('conversation-open'),epoch=sessionStore.epoch;
  const data=await api('/api/conversations/'+encodeURIComponent(id));
  if(epoch!==sessionStore.epoch||!isCurrent(mark))return;
  clearConversationView();conversationStore.id=id;conversationStore.modelId=data.model_id;persistConversation();paintSelection();
  $('conversation-title').textContent=data.title==='新会话'?'说说你的安排':data.title;
  const renderTurns=turns=>{for(const turn of turns){message(turn.message,'user');if(!activeStatuses.has(turn.status))message(turn.answer||'已保存本次处理记录。');}};
  renderTurns(data.turns);
  for(const turn of data.legacy_turns||[])message(turn.content,turn.role);
  if(data.next_before){
    let before=data.next_before;const more=el('button','加载更早记录','load-earlier');
    more.onclick=()=>act(more,async()=>{
      const past=await api('/api/conversations/'+encodeURIComponent(id)+'?before='+encodeURIComponent(before));
      if(conversationStore.id!==id)return;const fragment=document.createDocumentFragment();
      for(const turn of past.turns){const user=el('div',undefined,'message user'),answer=el('div',undefined,'message assistant');user.append(el('div',turn.message));answer.append(el('span','简','assistant-mark'),el('div',turn.answer));fragment.append(user,answer);}
      more.after(fragment);before=past.next_before;if(!before)more.remove();
    });$('messages').prepend(more);
  }
  if(!$('messages').childElementCount)message('新的安排，从这里开始。');
  $('conversation-drawer').close();const foreground=ticket('foreground');
  if(data.plan_id){const plan=await api('/api/plans/'+encodeURIComponent(data.plan_id));if(isCurrent(foreground)&&conversationStore.id===id)renderPlan(plan);}
  if(data.research_id){try{const study=await api('/api/research/'+encodeURIComponent(data.research_id));if(isCurrent(foreground)&&conversationStore.id===id)renderResearch(study);}catch{/* The retained plan remains available independently. */}}
  if(data.active_task_id&&conversationStore.id===id&&sessionStore.user){sessionStorage.setItem('bank-chat-task-'+sessionStore.user.id,data.active_task_id);restoreChatTask();}
  await refreshContext();$('input').focus();
}

export async function newConversation(){
  const epoch=sessionStore.epoch,data=await api('/api/conversations',{...(conversationStore.modelId?{model_id:conversationStore.modelId}:{})});
  if(epoch!==sessionStore.epoch)return;clearConversationView();conversationStore.id=data.conversation_id;conversationStore.modelId=data.model_id;
  if(sessionStore.user)sessionStorage.removeItem('bank-chat-task-'+sessionStore.user.id);
  persistConversation();paintSelection();$('conversation-drawer').close();message('新的安排，从这里开始。');await refreshContext();$('input').focus();
}

async function loadHistory(){
  const mark=ticket('conversation-list'),query=$('conversation-search').value;
  const result=await api('/api/conversations?q='+encodeURIComponent(query)+'&archived='+archived);
  if(!isCurrent(mark))return;const root=$('conversation-list');root.replaceChildren();
  const today=new Date().toLocaleDateString('en-CA'),yesterday=new Date(Date.now()-86400000).toLocaleDateString('en-CA');let group='';
  for(const item of result.items){
    const date=new Date(item.updated_at).toLocaleDateString('en-CA'),label=date===today?'今天':date===yesterday?'昨天':'更早';
    if(label!==group){root.append(el('h3',label,'conversation-date'));group=label;}
    const row=el('article',undefined,'conversation-row'),open=el('button',undefined,'conversation-open'),text=el('span');
    row.dataset.conversationId=item.conversation_id;
    if(item.conversation_id===conversationStore.id)row.classList.add('selected');
    const icon=el('span',item.active_task_id?'◌':'◷','conversation-row-icon');if(item.active_task_id)icon.classList.add('is-running');
    text.append(el('b',item.title),el('small',item.preview||'尚未开始新的安排'));open.append(icon,text);
    open.onclick=()=>act(open,()=>openConversation(item.conversation_id));
    const actions=el('details',undefined,'conversation-row-actions'),summary=el('summary','⋯');summary.setAttribute('aria-label','会话选项');actions.append(summary);
    const rename=el('button','重命名'),archive=el('button',archived?'恢复会话':'归档');
    rename.onclick=()=>{
      const root=modal('重命名会话'),input=el('input'),save=el('button','保存','primary');input.value=item.title;input.maxLength=100;input.setAttribute('aria-label','会话名称');
      save.onclick=()=>act(save,async epoch=>{await api('/api/conversations/'+encodeURIComponent(item.conversation_id),{title:input.value});if(epoch!==sessionStore.epoch)return;$('detail-dialog').close();if(conversationStore.id===item.conversation_id)$('conversation-title').textContent=input.value;await loadHistory();});root.append(input,save);input.focus();
    };
    archive.onclick=()=>act(archive,async epoch=>{await api('/api/conversations/'+encodeURIComponent(item.conversation_id),{archived:!archived});if(epoch===sessionStore.epoch)await loadHistory();});
    actions.append(rename,archive);row.append(open,actions);root.append(row);
  }
  if(!result.items.length){const empty=el('div',undefined,'conversation-empty');empty.append(el('span','◷'),el('h3',query?'没有找到相关会话':archived?'还没有归档会话':'从第一份安排开始'),el('p','会话会自动保留，可随时回来继续。'));root.append(empty);}
}

export async function refreshContext(){
  const id=conversationStore.id,mark=ticket('context-meter');
  const data=id?await api('/api/conversations/'+encodeURIComponent(id)+'/context'):null;
  if(!isCurrent(mark)||conversationStore.id!==id)return;contextData=data;
  const pct=data?.utilization_pct??0;$('context-meter').style.setProperty('--context-used',pct+'%');
  $('context-meter').classList.toggle('context-warning',pct>=75);
  $('context-meter-label').textContent=data?.input_tokens!=null?Math.round(pct)+'%':'上下文';
  $('context-meter').title=data?.input_tokens!=null?'最近一次模型输入 · '+number(data.input_tokens)+' / '+number(data.context_limit)+' Token':'查看已配置窗口与任务记忆';
  if($('context-popover').matches(':popover-open'))renderContext();
}

function renderContext(){
  const data=contextData,model=selection(),root=$('context-details');root.replaceChildren();$('context-model').textContent=model?.name||'';
  const limit=data?.context_limit??model?.context_tokens??0,input=data?.input_tokens;
  const headline=el('div',undefined,'context-headline');headline.append(el('strong',input!=null?number(input):'等待首次调用'),el('span',input!=null?' / '+number(limit)+' Token':number(limit)+' Token 窗口'));root.append(headline);
  const bar=el('div',undefined,'context-capacity-bar'),fill=el('i');fill.style.width=(data?.utilization_pct??0)+'%';bar.append(fill);root.append(bar);
  const rows=el('dl',undefined,'context-metrics');
  for(const [label,value] of [['输入占用',input!=null?number(input):'尚无记录'],['预算估算',number(data?.estimated_input_tokens)],['输出预留',number(data?.output_reserved??0)],['安全余量',number(data?.safety_margin??0)],['可用预算',number(data?.budget_remaining_tokens)]])rows.append(el('dt',label),el('dd',value));root.append(rows);
  const method=data?.measurement==='provider_usage'?'提供商返回用量':data?.measurement==='conservative_estimate'?'UTF-8 保守估算':'发送需求后更新';
  root.append(el('p',method+' · 最近一次模型调用，非会话累计用量','context-footnote'));
  if(data?.removed_items||data?.removed_turns)root.append(el('p','已按预算省略 '+data.removed_items+' 条辅助记忆、'+data.removed_turns+' 条非关键历史。','context-footnote'));
}

async function showMemory(epoch){
  const conversation=conversationStore.id;$('context-popover').hidePopover();await refreshContext();
  if(epoch!==sessionStore.epoch||conversationStore.id!==conversation)return;
  const data=contextData,root=modal('任务与记忆');
  const summary=data?.task_summary;root.append(el('p','会话历史保存在账户下；模型每次只接收当前任务所需的上下文。','muted'));
  root.append(el('h3','当前任务'));
  if(!summary?.goals.length)root.append(el('p','暂无待接续的任务。','muted'));
  for(const goal of summary?.goals||[]){const row=el('div',undefined,'memory-overview-row');row.append(el('b',goal.title),el('span',states[goal.status]||({'NEEDS_INPUT':'待补信息','NEEDS_RECHECK':'需重新核对','RESEARCH_READY':'分析完成'}[goal.status])||goal.status));root.append(row);}
  if(summary?.negative_constraints.length){root.append(el('h3','保留的要求'));for(const constraint of summary.negative_constraints)root.append(el('p',constraint));}
  root.append(el('h3','可用记忆与来源'));
  const memories=data?.memories||[];if(!memories.length)root.append(el('p','暂无适用于当前会话的历史记忆。','muted'));
  const labels={preference:'个人偏好',product:'产品资料',conversation:'对话摘要',research_report:'研究记录',bank_receipt:'办理回执'};
  for(const memory of memories.slice(0,20)){const row=el('div',undefined,'memory-overview-row');row.append(el('b',memory.key||labels[memory.kind]||'历史依据'),el('span','版本 '+memory.version));const read=el('button','查看来源');read.onclick=()=>act(read,async nextEpoch=>{const id=conversationStore.id,evidence=await api('/api/memory/evidence/'+encodeURIComponent(memory.id)+(id?'?conversation_id='+encodeURIComponent(id):''));if(nextEpoch!==sessionStore.epoch||id!==conversationStore.id)return;const source=modal('原始依据');source.append(el('p',evidence.text,'memory-source-text'));});row.append(read);root.append(row);}
}

export async function initializeWorkspaceControls(){
  const epoch=sessionStore.epoch,data=await api('/api/models');if(epoch!==sessionStore.epoch)return;
  catalog=data.models;conversationStore.modelId=data.default_id;paintSelection();
  if(conversationStore.id){try{await openConversation(conversationStore.id);}catch(error){if(error.code!=='CONVERSATION_NOT_FOUND')throw error;conversationStore.id=null;await refreshContext();}}
  else await refreshContext();
}

export function resetWorkspaceControls(){
  detachChatView();catalog=[];contextData=null;conversationStore.modelId=null;archived=false;
  $('model-menu').hidePopover();$('context-popover').hidePopover();$('conversation-drawer').close();
  $('model-menu').classList.remove('configuring');
  $('selected-model-name').textContent='选择模型';$('context-meter-label').textContent='上下文';$('context-meter').style.setProperty('--context-used','0%');
  $('conversation-search').value='';$('model-search').value='';$('show-archived').textContent='查看已归档';$('show-archived').setAttribute('aria-pressed','false');
  $('conversation-list').replaceChildren();$('context-details').replaceChildren();$('model-options').replaceChildren();$('conversation-title').textContent='说说你的安排';
}

export function setupWorkspaceControls(){
  $('model-picker').onclick=()=>{$('model-menu').classList.remove('configuring');renderModels();if($('model-menu').matches(':popover-open'))$('model-menu').hidePopover();else{placePopover($('model-menu'),$('model-picker'));$('model-search').focus();}};
  $('model-search').oninput=renderModels;
  $('model-menu').addEventListener('toggle',()=>{const opened=$('model-menu').matches(':popover-open');$('model-picker').setAttribute('aria-expanded',String(opened));if(!opened){const key=document.getElementById('provider-api-key');if(key instanceof HTMLInputElement)key.value='';}});
  $('context-meter').onclick=()=>act(null,async()=>{if($('context-popover').matches(':popover-open')){$('context-popover').hidePopover();return;}await refreshContext();renderContext();placePopover($('context-popover'),$('context-meter'));});
  $('context-popover').addEventListener('toggle',()=>{$('context-meter').setAttribute('aria-expanded',String($('context-popover').matches(':popover-open')));});
  $('context-memory').onclick=()=>act($('context-memory'),showMemory);
  $('conversation-history').onclick=()=>act(null,async()=>{$('conversation-drawer').showModal();await loadHistory();$('conversation-search').focus();});
  $('conversation-close').onclick=()=>$('conversation-drawer').close();
  $('conversation-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>act(null,loadHistory),180);};
  $('show-archived').onclick=()=>act(null,async()=>{archived=!archived;$('show-archived').setAttribute('aria-pressed',String(archived));$('show-archived').textContent=archived?'返回会话':'查看已归档';await loadHistory();});
  for(const id of ['new-conversation','drawer-new'])$(id).onclick=()=>act(null,newConversation);
  $('conversation-drawer').addEventListener('click',event=>{if(event.target===$('conversation-drawer')){const rect=$('conversation-drawer').getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$('conversation-drawer').close();}});
  document.addEventListener('workspace:changed',()=>{if(sessionStore.user)act(null,refreshContext);});
  document.addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.altKey&&event.key.toLowerCase()==='n'&&sessionStore.user){event.preventDefault();act(null,newConversation);}});
  window.addEventListener('resize',()=>{for(const id of ['model-menu','context-popover'])$(id).hidePopover();});
}
