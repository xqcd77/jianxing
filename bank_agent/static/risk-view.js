// @ts-check
import {$,act,el,modal,operations,toast,yuan} from './ui.js';
import {accountStore,formStore,planStore,sessionStore} from './state.js';
import {api} from './api.js';
import {verificationField,takeVerification} from './security.js';

export function riskSeal(sensitive=false){
  const wrap=el('span',undefined,'risk-seal'+(sensitive?' sensitive':''));wrap.setAttribute('aria-hidden','true');
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 56 56');
  const circle=document.createElementNS(svg.namespaceURI,'circle');circle.setAttribute('cx','28');circle.setAttribute('cy','28');circle.setAttribute('r','23');
  svg.append(circle);wrap.append(svg,el('b','!'));return wrap;
}

export function productRiskCard(product){
  const box=el('section',undefined,'product-risk-card');box.setAttribute('aria-label','产品风险与流动性');
  const head=el('div',undefined,'product-risk-head');head.append(el('strong',product.name),el('span','产品风险 R'+product.risk_level,'product-risk-level'));box.append(head);
  const grid=el('div',undefined,'product-risk-grid');
  for(const [label,value] of [['起购',yuan(product.minimum_fen)],['锁定期',product.lock_days?product.lock_days+'个自然日':'无锁定期'],['赎回到账',product.redeem_business_days?'T+'+product.redeem_business_days+'个业务日':'当日模拟结算']]){const cell=el('div');cell.append(el('small',label),el('b',value));grid.append(cell);}box.append(grid);
  box.append(el('p','存在本金损失可能；身份验证不能降低投资风险。','risk-caption'));
  box.append(el('p',product.disclosure_verified?'已核对本地模拟披露 · 第'+product.source_version+'版':'条款尚未核实，暂不能申购','risk-caption'+(product.disclosure_verified?'':' risk-attention')));return box;
}

export function riskBanner(node){
  const risk=node.risk;if(!risk||['FINISHED','READ_ONLY','WAITING'].includes(risk.status))return null;
  const box=el('section',undefined,'risk-banner risk-'+risk.status.toLowerCase());box.dataset.riskStatus=risk.status;
  const head=el('div',undefined,'risk-banner-head');head.append(el('span',risk.status==='VERIFYING'?'◷':risk.status==='VERIFY'?'!':'·','risk-small-symbol'),el('strong',risk.title));box.append(head);
  const reason=risk.reasons.at(-1);if(reason)box.append(el('p',reason.message));
  if(risk.status==='BLOCKED')for(const text of risk.controls)box.append(el('p',text,'risk-caption'));
  return box;
}

export function revisionView(data){
  const revision=data.review?.last_revision;if(!revision)return null;
  const box=el('section',undefined,'revision-card');box.setAttribute('aria-label','本次安排变更');
  box.append(el('strong','安排已更新 · 第'+data.version+'版'),el('p','请核对剩余步骤，原授权已失效；已完成交易保留原凭证。'));
  const labels={amount_fen:'金额',budget_fen:'预算',recipient_hint:'收款人',scope:'取消范围',product_id:'产品',purpose:'预留用途',birthday_date:'安排日期',single_limit_fen:'单笔限额',daily_limit_fen:'日限额',blocked_categories:'限制类别'};
  const display=(key,value)=>value===undefined?'未设置':key.endsWith('_fen')?yuan(value):key==='scope'?({bank:'银行代扣',merchant:'商户合同',both:'代扣与合同'}[value]||'所选范围'):key==='blocked_categories'?(value.length?'已选'+value.length+'类':'不限制'):key==='product_id'?(accountStore.facts?.products?.find(p=>p.id===value)?.name||'待核实产品'):String(value);
  for(const change of revision.changes||[]){
    if(change.change!=='updated'){box.append(el('p',change.change==='added'?'增加待办理步骤':'取消未执行步骤'));continue;}
    for(const [key,label] of Object.entries(labels))if(JSON.stringify(change.before?.[key])!==JSON.stringify(change.after?.[key])){
      const line=el('div',undefined,'revision-line');line.append(el('span',label),el('del',display(key,change.before?.[key])),el('span','→'),el('b',display(key,change.after?.[key])));box.append(line);
    }
    if('before_scheduled_for' in change&&change.before_scheduled_for!==change.after_scheduled_for){const line=el('div',undefined,'revision-line');line.append(el('span','办理时间'),el('del',change.before_scheduled_for?.slice(0,16).replace('T',' ')||'即时办理'),el('span','→'),el('b',change.after_scheduled_for?.slice(0,16).replace('T',' ')||'即时办理'));box.append(line);}
  }return box;
}

export function renderPressure(root,preview){
  const p=preview.pressure;if(!p)return;
  const box=el('section',undefined,'pressure-card'+(p.shortfall_fen?' pressure-warning':''));box.setAttribute('aria-label','计划资金压力');
  const title=el('div',undefined,'pressure-head');title.append(el('strong','未来30天 · 资金压力'),el('span',!p.complete?'部分条件待核实':p.shortfall_fen?'存在资金缺口':'已知安排内未见缺口'));box.append(title);
  const track=el('div',undefined,'funding-flow');track.setAttribute('aria-label','当前可用 '+yuan(preview.before.available_fen)+'；预计占用 '+yuan(preview.required_fen)+'；即时办理后可用 '+yuan(preview.immediate_after.available_fen));
  const total=Math.max(1,preview.before.available_fen,preview.required_fen);const occupied=el('i',undefined,'funding-occupied');occupied.style.width=Math.min(100,Math.max(0,preview.required_fen/total*100))+'%';track.append(occupied);box.append(track);
  const caption=el('div',undefined,'funding-flow-labels');caption.append(el('span','当前可用 '+yuan(preview.before.available_fen)),el('span','预计占用 '+yuan(preview.required_fen)));box.append(caption);
  if(p.shortfall_fen)box.append(el('p',(p.complete?'按已登记安排测算，':'按已核验部分测算，')+p.shortfall_on+' 可能缺少 '+yuan(p.shortfall_fen)+'。可调整金额或安排预算；当前没有因此自动扣款。','pressure-summary'));
  else box.append(el('p','已知安排下最低可用 '+yuan(p.minimum_available_fen)+'；尚未覆盖其他账外支出。','pressure-summary'));
  const details=el('details',undefined,'pressure-timeline');details.append(el('summary','查看日期、收支与测算假设'));
  for(const event of p.events){const row=el('div',undefined,'pressure-event');row.append(el('time',event.date),el('span',event.source?.includes('_')&&event.label==='待办理资金安排'?'本计划资金变化':event.label),el('b',(event.delta_fen>0?'+':'−')+yuan(Math.abs(event.delta_fen))),el('small','测算可用 '+yuan(event.projected_available_fen)));details.append(row);}
  for(const assumption of p.assumptions)details.append(el('p',assumption,'risk-caption'));details.append(el('p','数据截至 '+preview.business_time.slice(0,16).replace('T',' ')+' · 演示业务时间','risk-caption'));box.append(details);root.append(box);
}

/** @param {any} original @param {any} node @param {{render:Function,summary:Function,approve:Function}} handlers */
export async function openRiskConfirmation(original,node,handlers){
  const epoch=sessionStore.epoch,opening=formStore.modalVersion;
  const fresh=await api('/api/plans/'+encodeURIComponent(original.id));
  if(epoch!==sessionStore.epoch||formStore.modalVersion!==opening||planStore.current?.id!==original.id)return;
  const current=fresh.nodes.find(n=>n.id===node.id);
  if(!current||fresh.version!==original.version||current.fingerprint!==node.fingerprint||!['WAIT_CONFIRM','WAIT_MFA'].includes(current.status)){
    handlers.render(fresh);toast('安排已变化，请核对最新详情。');return;
  }
  if(!['CONFIRM','VERIFY'].includes(current.risk.status)||fresh.funding_preview.shortfall_fen){handlers.render(fresh);toast('请先处理当前办理条件，再重新核对。');return;}
  const sensitive=current.risk.authorization_tier==='RED';
  const root=modal(sensitive?'核对并验证 · '+(operations[current.operation]||'敏感业务'):'核对 · '+(operations[current.operation]||'本次操作'));
  const stamp=formStore.modalVersion,dialog=$('detail-dialog');dialog.setAttribute('role','alertdialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-labelledby','detail-title');dialog.setAttribute('aria-describedby','risk-dialog-explanation');
  root.classList.add('risk-dialog-content');root.dataset.riskConfirmation=current.id;
  const hero=el('div',undefined,'risk-dialog-hero'+(sensitive?' is-sensitive':''));hero.append(riskSeal(sensitive));const text=el('div');text.append(el('strong',sensitive?'敏感操作，再核对一步':'本次变更，由你确认'),el('p',current.risk.reasons.map(r=>r.message).join(' ')));text.id='risk-dialog-explanation';hero.append(text);root.append(hero,handlers.summary(current,fresh.nodes));
  if(current.risk.product)root.append(productRiskCard(current.risk.product));
  const funds=el('div',undefined,'risk-confirm-funds');for(const [label,amount] of [['整份安排预计余额',fresh.funding_preview.after.balance_fen],['仍预留',fresh.funding_preview.after.reserved_fen],['预计可用',fresh.funding_preview.after.available_fen]]){const cell=el('div');cell.append(el('small',label),el('b',yuan(amount)));funds.append(cell);}root.append(funds);
  if(!fresh.funding_preview.complete_projection)root.append(el('p','部分步骤条件待满足，以上为已核验部分的估算。','risk-caption'));
  for(const consequence of current.risk.consequences)root.append(el('p',consequence,'risk-consequence'));
  for(const control of current.risk.controls)root.append(el('p',control,'risk-caption'));
  root.append(el('p','请确认业务信息与金额，本次验证仅用于办理该步骤。','risk-caption'));
  let otp;if(sensitive){otp=el('input');root.append(verificationField(otp,'approve',{plan_id:fresh.id,node_id:current.id,fingerprint:current.fingerprint}));}
  const actions=el('div',undefined,'modal-actions'),back=el('button','返回修改'),yes=el('button',sensitive?'验证并办理本步骤':'确认办理本步骤',sensitive?'risk-submit':'primary');back.type=yes.type='button';back.onclick=()=>dialog.close();
  const error=el('p',undefined,'risk-dialog-error');error.setAttribute('role','alert');error.tabIndex=-1;root.append(error);
  const valid=()=>epoch===sessionStore.epoch&&stamp===formStore.modalVersion&&dialog.open&&planStore.current?.id===fresh.id&&planStore.current?.version===fresh.version;
  yes.onclick=()=>act(yes,async()=>{
    if(!valid())return;
    try{
      const latest=await api('/api/plans/'+encodeURIComponent(fresh.id));
      if(!valid())return;
      const live=latest.nodes.find(n=>n.id===current.id);
      if(latest.version!==fresh.version||live?.fingerprint!==current.fingerprint||!['WAIT_CONFIRM','WAIT_MFA'].includes(live?.status)||latest.funding_preview.fingerprint!==fresh.funding_preview.fingerprint||!['CONFIRM','VERIFY'].includes(live?.risk.status)){
        dialog.close();handlers.render(latest);toast('资金或办理条件已变化，请重新核对；本次尚未提交。');return;
      }
      await handlers.approve(fresh,current,otp?takeVerification(otp):{});
      if(stamp===formStore.modalVersion&&dialog.open)dialog.close();
    }catch(e){if(valid()){error.textContent=e.message;error.focus();}throw e;}
  });actions.append(back,yes);root.append(actions);back.focus();
}
