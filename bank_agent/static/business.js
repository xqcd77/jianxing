// @ts-check
import {conversationStore,formStore,sessionStore,beginIntent,isCurrent} from "./state.js";
import {$,act,businessText,categories,doneStates,el,modal,states,yuan} from "./ui.js";
import {api} from "./api.js";
import {researchEntry} from "./research.js";
import {renderPlan,toFen} from "./plan-view.js";
import {overview} from "./account-view.js";
import {message,removeWaiting} from "./chat.js";

const businessPermission = {GREEN:'直接查询',YELLOW:'需本人确认',RED:'需强验证',DYNAMIC:'办理时核验权限'};

export function closeBusinessPanel() {
  formStore.businessVersion++;
  formStore.selectedBusiness=null;$('business-panel').hidden=true;
  for(const button of $('scenarios').children){button.classList.remove('selected');button.setAttribute('aria-expanded','false');}
}

export function renderBusinessCategories(categories) {
  $('business-close').onclick=closeBusinessPanel;
  closeBusinessPanel();$('scenarios').replaceChildren();
  for(const category of categories){
    const button=el('button',undefined,'scenario business-category');button.type='button';button.dataset.category=category.id;
    button.setAttribute('aria-expanded','false');button.setAttribute('aria-controls','business-panel');
    button.append(el('b',category.label),el('small',category.description),el('span',category.icon,'scenario-icon'));
    button.classList.add('tilt-surface');
    button.onclick=()=>{
      if(formStore.selectedBusiness===category.id){closeBusinessPanel();return;}
      formStore.selectedBusiness=category.id;
      for(const peer of $('scenarios').children){const active=peer===button;peer.classList.toggle('selected',active);peer.setAttribute('aria-expanded',String(active));}
      showBusinessChoices(category,category.actions);
      $('business-panel').hidden=false;
      $('business-panel').scrollIntoView({block:'nearest',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
    };
    $('scenarios').append(button);
  }
}

/** @param {any} category @param {any[]} items @param {any} [group] */
export function showBusinessChoices(category,items,group=null) {
  const view=++formStore.businessVersion;
  $('business-title').textContent=category.label+(group?' · '+group.label:'');
  $('business-description').textContent=group?.description||category.description;$('business-options').replaceChildren();
  if(group){const back=el('button','← 返回'+category.label,'business-back');back.type='button';back.onclick=()=>showBusinessChoices(category,category.actions);$('business-options').append(back);}
  for(const item of items){
    const choice=el('button',undefined,'business-option');choice.type='button';choice.dataset.action=item.id;
    const head=el('span',undefined,'business-option-head');head.append(el('strong',item.label),el('span','↗','business-arrow'));
    choice.append(head,el('small',item.description));
    if(item.mode!=='group')choice.append(el('span',businessPermission[item.tier],'business-permission '+item.tier));
    choice.onclick=()=>act(choice,async(epoch)=>{
      const fresh=await api('/api/businesses/'+encodeURIComponent(category.id)+'/'+encodeURIComponent(item.id));
      if(epoch!==sessionStore.epoch||formStore.selectedBusiness!==category.id||view!==formStore.businessVersion)return;
      if(fresh.mode==='group')showBusinessChoices(category,fresh.children,fresh);
      else await openBusinessAction(category,fresh);
    });$('business-options').append(choice);
  }
  if(!group&&['bills','wealth'].includes(category.id))$('business-options').append(researchEntry(category.id));
}

export function showBusinessRecords(category,item,result) {
  const root=modal(item.label);root.dataset.businessForm=crypto.randomUUID();
  const recordStamp=root.dataset.businessForm;
  root.append(el('p',result.message,'business-intro'));
  if(!result.items.length){root.append(el('p',result.status==='AA_RECORDS'?'暂无本人参与的AA收款。':'暂无已有办理计划。','muted'));return;}
  for(const record of result.items){
    const row=el('article',undefined,'business-record'),actions=el('div',undefined,'business-record-actions');
    if(result.status==='AA_RECORDS'){
      row.append(el('strong',record.subject));
      const status={SELF_SHARE:'本人分摊，无需向自己付款',PAID:'本人份额已支付',UNPAID:'本人份额待支付'}[record.my_status];
      row.append(el('p',status+' · '+yuan(record.my_amount_fen),'muted'),el('p','已收款 '+yuan(record.collected_fen)+' / 应收款 '+yuan(record.receivable_fen),'muted'));
      for(const [action,label] of [['aa_status','查看进度'],...(record.my_status==='UNPAID'?[['aa_pay','支付我的份额']]:[])]){
        const button=el('button',label,action==='aa_pay'?'primary':'');button.onclick=()=>act(button,async(epoch)=>{
          const details=await api('/api/businesses/funds/'+action);
          if(epoch!==sessionStore.epoch||root.dataset.businessForm!==recordStamp||!row.isConnected)return;
          await submitBusiness(category,details,{request_id:record.request_id},null,root);
        });actions.append(button);
      }
    }else{
      row.append(el('strong',businessText(record.summary)),el('p',(states[record.status]||record.status)+' · '+record.nodes.filter(n=>doneStates.has(n.status)).length+' / '+record.nodes.length+'步','muted'));
      const view=el('button','查看详情');view.onclick=()=>{$('detail-dialog').close();renderPlan(record,false,beginIntent());};actions.append(view);
      if(['WAIT_CONFIRM','WAIT_MFA','WAIT_INPUT','WAIT_TIME','FAILED'].includes(record.status)){
        const resume=el('button','继续办理');resume.onclick=()=>act(resume,async(epoch)=>{
          const updated=await api('/api/plans/'+record.id+'/resume',{});if(epoch!==sessionStore.epoch)return;
          if(root.dataset.businessForm===recordStamp)$('detail-dialog').close();renderPlan(updated,true);await overview();
        });actions.append(resume);
      }
      if(!['COMPLETED','CANCELLED'].includes(record.status)){
        const cancel=el('button','取消剩余');cancel.onclick=()=>{renderPlan(record,false,beginIntent());$('cancel').click();};actions.append(cancel);
      }
    }
    row.append(actions);root.append(row);
  }
}

export async function submitBusiness(category,item,params,button,formRoot,options={resumePlanId:null}) {
  const mark=beginIntent();
  const formStamp=formRoot?.dataset.businessForm;
  const requestView=formStore.businessVersion,requestModal=formStore.modalVersion;
  return act(button,async(epoch)=>{
    const title=item.label;message(title,'user');
    const wait=message(item.tier==='GREEN'?'正在查询并核对结果…':'正在核对办理条件与确认详情…','assistant',true);
    try{
      const result=await api('/api/businesses/'+encodeURIComponent(category.id)+'/'+encodeURIComponent(item.id)+'/prepare',{params,...(conversationStore.id?{conversation_id:conversationStore.id}:{})});
      if(epoch!==sessionStore.epoch||!isCurrent(mark))return;
      if(['AA_RECORDS','PLAN_RECORDS'].includes(result.status)&&(requestView!==formStore.businessVersion||requestModal!==formStore.modalVersion))return;
      if(formRoot&&formRoot.isConnected&&formRoot.dataset.businessForm===formStamp)$('detail-dialog').close();
      if(options.resumePlanId&&item.operation==='wealth.assess'&&result.id&&sessionStore.user){
        sessionStorage.setItem('bank-prerequisite-'+sessionStore.user.id,JSON.stringify({fromPlanId:result.id,toPlanId:options.resumePlanId}));
      }
      if(['AA_RECORDS','PLAN_RECORDS'].includes(result.status))showBusinessRecords(category,item,result);
      else if(options.resumePlanId&&item.operation==='wealth.assess'&&result.nodes?.every(n=>n.status==='SUCCEEDED')){
        const resumed=await api('/api/plans/'+encodeURIComponent(options.resumePlanId)+'/resume',{});
        if(epoch!==sessionStore.epoch||!isCurrent(mark))return;
        if(sessionStore.user)sessionStorage.removeItem('bank-prerequisite-'+sessionStore.user.id);
        renderPlan(resumed,true,mark);message('测评已更新，已返回原安排重新核对；已完成的转账不会重复办理。');
      }else renderPlan(result,true,mark);
      await overview();
    }catch(error){if(formRoot?.isConnected&&formRoot.dataset.businessForm===formStamp){const notice=formRoot.querySelector('.business-error');if(notice)notice.textContent=error.message;}throw error;}
    finally{removeWaiting(wait);}
  });
}

export async function openBusinessAction(category,item,options={resumePlanId:null}) {
  beginIntent();
  if(!item.fields.length)return submitBusiness(category,item,{},null,null,options);
  const root=modal(category.label+' · '+item.label);
  root.dataset.businessForm=crypto.randomUUID();
  root.append(el('p',item.description,'business-intro'));
  root.append(el('p',item.tier==='GREEN'?'直接查询，结果以当前账本和所选统计期间为准。':'先生成可核对的安排，再在办理计划中按权限确认或验证。','business-form-note'));
  const form=el('form'),grid=el('div',undefined,'form-grid business-fields');
  /** @type {Array<[string,()=>unknown]>} */ const readers=[];
  const visible=spec=>Object.entries(spec.show_when||{}).every(([name,values])=>{const input=grid.querySelector('[name="'+name+'"]');return values.includes(input instanceof HTMLInputElement||input instanceof HTMLSelectElement?input.value:undefined);});
  let unavailable=false;
  for(const spec of item.fields){
    const grouped=['risk','multi'].includes(spec.kind),label=el(grouped?'fieldset':'label',undefined,'form-field');label.dataset.field=spec.name;
    label.append(el(grouped?'legend':'span',spec.label+(spec.required?' *':'')));
    if(spec.kind==='risk'){
      label.classList.add('full-width');
      /** @type {Array<[string,string[]]>} */
      const questions=[
        ['投资经验',['尚无经验','少量低风险经验','有基金等经验','多类产品经验','长期主动投资经验']],
        ['可接受的本金波动',['不能接受损失','可接受很小波动','可接受一定波动','可接受较大波动','可承担明显损失']],
        ['资金使用期限',['随时可能使用','三个月以内','三个月至一年','一年至三年','三年以上']],
        ['收益与风险偏好',['优先本金稳定','低风险和流动性','兼顾收益与波动','愿承担风险争取收益','能承担高风险追求收益']],
        ['出现亏损时的应对',['立即停止投资','倾向尽快退出','核实原因后决定','可承受较长波动','理解风险并有长期安排']],
      ];
      const inputs=[];
      for(const [index,[question,answers]] of questions.entries()){
        const row=el('label',String(index+1)+'. '+question,'risk-question');const input=el('select');input.required=true;input.setAttribute('aria-label',question);
        const blank=el('option','请选择你的真实情况');blank.value='';input.append(blank);
        for(const [score,answer] of answers.entries()){const option=el('option',answer);option.value=String(score+1);input.append(option);}
        row.append(input);label.append(row);inputs.push(input);
      }
      label.append(el('small','竞赛模拟问卷，五题均需本人回答；不代表银行正式风险测评。'));
      readers.push([spec.name,()=>inputs.map(input=>Number(input.value))]);
    }else if(spec.kind==='multi'){
      const choices=el('div',undefined,'business-checkboxes'),inputs=[];
      for(const option of spec.choices||[]){const row=el('label',undefined,'business-check');const input=el('input');input.type='checkbox';input.value=option.value;row.append(input,el('span',option.label));choices.append(row);inputs.push(input);}
      label.append(choices);if(spec.required&&!inputs.length){unavailable=true;label.append(el('small','当前没有可选择的业务对象。'));}
      readers.push([spec.name,()=>{const values=inputs.filter(input=>input.checked).map(input=>input.value);if(spec.required&&!values.length)throw new Error('请至少选择一项：'+spec.label);return values;}]);
    }else{
      const input=el(spec.kind==='select'?'select':'input');input.name=spec.name;input.required=spec.required;input.setAttribute('aria-label',spec.label);
      if(input instanceof HTMLSelectElement){
        const blank=el('option',spec.choices?.length?'请选择':spec.empty_message||'当前没有可办理的对象');blank.value='';input.append(blank);
        for(const choice of spec.choices||[]){const option=el('option',choice.label);option.value=choice.value;input.append(option);}
        if(spec.default!==undefined)input.value=String(spec.default);
        if(spec.required&&!spec.choices?.length){input.disabled=true;unavailable=true;}
        const detail=el('small',undefined,'business-product-detail');
        input.onchange=()=>{detail.textContent=(spec.choices||[]).find(choice=>choice.value===input.value)?.detail||'';};
        input.dispatchEvent(new Event('change'));label.append(input,detail);
      }else{
        input.type=({date:'date',month:'month',datetime:'datetime-local',integer:'number',year:'number'})[spec.kind]||'text';
        if(spec.kind==='money'){input.inputMode='decimal';input.pattern='[0-9]+(\\.[0-9]{1,2})?';input.placeholder=spec.placeholder||'请输入元，最多两位小数';}
        if(['integer','year'].includes(spec.kind)){input.step='1';input.min=String(spec.minimum??1900);input.max=String(spec.maximum??2100);}
        if(spec.kind==='datetime')input.step='60';
        if(spec.default!==undefined)input.value=spec.kind==='money'?(spec.default/100).toFixed(2):String(spec.default);
        if(spec.placeholder)input.placeholder=spec.placeholder;
        if(spec.kind==='recipient'){
          const list=el('datalist');list.id='business-recipient-options';
          for(const choice of spec.choices||[]){const option=el('option',choice.label);option.value=choice.value;list.append(option);}
          input.setAttribute('list',list.id);input.placeholder='姓名、手机号或唯一备注';label.append(list);
        }
        label.append(input);
      }
      readers.push([spec.name,()=>{
        if(spec.kind==='money')return toFen(input.value.trim());
        if(['integer','year'].includes(spec.kind)){if(!/^\d+$/.test(input.value))throw new Error('请填写整数：'+spec.label);return spec.kind==='year'?input.value:Number(input.value);}
        if(spec.kind==='datetime')return input.value+':00+08:00';
        return input.value.trim();
      }]);
    }
    if(spec.help)label.append(el('small',spec.help));grid.append(label);
  }
  const error=el('p',undefined,'error business-error');error.setAttribute('role','alert');
  const actions=el('div',undefined,'modal-actions'),cancel=el('button','暂不办理'),submit=el('button',item.tier==='GREEN'?'查询结果':'生成办理计划','primary');
  cancel.type='button';cancel.onclick=()=>$('detail-dialog').close();submit.type='submit';submit.disabled=unavailable;
  if(unavailable){const reasons=item.fields.filter(spec=>spec.required&&['select','multi'].includes(spec.kind)&&!spec.choices?.length).map(spec=>spec.empty_message||'暂无'+spec.label);root.append(el('p',reasons.join('；')+'。查询当前状态或完成对应安排后，可以再次选择。','notice-warning'));}
  actions.append(cancel,submit);form.append(grid,error,actions);root.append(form);
  function updateVisibility(){
    unavailable=false;
    for(const spec of item.fields){
      const active=visible(spec),label=grid.querySelector('[data-field="'+spec.name+'"]');if(!(label instanceof HTMLElement))continue;label.hidden=!active;
      const blocked=active&&spec.required&&['select','multi'].includes(spec.kind)&&!spec.choices?.length;
      for(const input of label.querySelectorAll('input,select')){if(!(input instanceof HTMLInputElement||input instanceof HTMLSelectElement))continue;input.disabled=!active||blocked;input.required=active&&spec.required&&spec.kind!=='multi';}
      if(blocked)unavailable=true;
    }submit.disabled=unavailable;
  }
  grid.addEventListener('change',updateVisibility);updateVisibility();
  form.onsubmit=event=>{event.preventDefault();error.textContent='';let params;
    try{params=Object.fromEntries(readers.filter(([key])=>visible(item.fields.find(spec=>spec.name===key))).map(([key,read])=>[key,read()]));}catch(problem){error.textContent=problem.message;return;}
    submitBusiness(category,item,params,submit,root,options);
  };
}
