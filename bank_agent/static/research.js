// @ts-check
import {accountStore,conversationStore,formStore,researchStore,sessionStore,beginIntent,isCurrent,ticket} from "./state.js";
import {$,act,el,modal,toast,yuan} from "./ui.js";
import {api} from "./api.js";
import {renderPlan} from "./plan-view.js";
import {overview} from "./account-view.js";

const researchToolLabels={constraints:'你提供的需求',account:'账户与测评', 'bills.report':'入账账单', 'bills.anomalies':'重复扣款线索','cashflow.project':'已知收支义务',transaction_details:'原始流水核查',stress:'资金压力情景','wealth.compare':'产品目录与适当性',product_terms:'产品条款全文'};

export function resetResearch(){if(researchStore.timer!==null)clearTimeout(researchStore.timer);researchStore.current=null;if($('research-panel'))$('research-panel').hidden=true;}

export function researchEntry(topic){
  const button=el('button',topic==='bills'?'深入核查收支与资金缺口 ↗':'深入比较条款与用款条件 ↗','research-entry');button.type='button';
  button.onclick=()=>openResearchForm(topic);return button;
}

/** @param {string} [topic] @param {any} [previous] */
export function openResearchForm(topic='combined',previous=null){
  beginIntent();
  const root=modal(topic==='bills'?'账单与资金研究':'收支与理财研究'),stamp=formStore.modalVersion, form=el('form',undefined,'research-form');
  root.append(el('p','核查已有流水与收支义务，阅读产品条款。分析完成后，你可以选择方案，再按银行要求确认办理。','muted'));
  const queryLabel=el('label','研究需求'),query=el('textarea');query.required=true;query.rows=3;query.maxLength=4000;
  query.value=previous?.query||(topic==='bills'?'深入分析我的账单，核查异常原因和未来资金缺口。':'深入比较理财产品条款，结合生活费、应急资金和用款日期，说明哪些产品应当排除。');queryLabel.append(query);form.append(queryLabel);
  const inputs={},grid=el('div',undefined,'research-fields'),b=previous?.report?.constraints||{};
  for(const [key,label,type,value] of [
    ['period','账单月份','month',b.period?.length===7?b.period:accountStore.facts?.business_now?.slice(0,7)],
    ['amount_fen','计划申购（元，可不填）','number',b.amount_fen?b.amount_fen/100:''],
    ['reserve_fen','新增生活预留（元）','number',b.reserve_fen?b.reserve_fen/100:''],
    ['emergency_fen','新增应急预留（元）','number',b.emergency_fen?b.emergency_fen/100:''],
    ['needed_on','用款日期（可不填）','date',b.needed_on||'']]){
    const labelNode=el('label',label),input=el('input');input.type=type;input.value=value||'';input.name=key;
    if(type==='number'){input.min=key==='amount_fen'?'0.01':'0';input.max='1000000';input.step='0.01';input.inputMode='decimal';}
    if(type==='date')input.min=accountStore.facts?.business_now?.slice(0,10)||'';
    labelNode.append(input);grid.append(labelNode);inputs[key]=input;
  }
  form.append(grid,el('p','默认测算未来30天，较晚的用款或支出日期会延长测算窗口，最多一年。已有预留不会重复扣减；这里填写新增预留。','research-note'));
  const extra=el('details',undefined,'research-extra');extra.append(el('summary','补充一笔未来支出（可选）'));
  const extraGrid=el('div',undefined,'research-fields'),expense={};
  for(const [key,label,type] of [['label','支出用途','text'],['amount','金额（元）','number'],['date','支出日期','date']]){
    const wrap=el('label',label),input=el('input');input.type=type;input.name='expense_'+key;
    if(type==='number'){input.min='0.01';input.step='0.01';input.max='1000000';}
    if(type==='date')input.min=accountStore.facts?.business_now?.slice(0,10)||'';
    const old=b.expenses?.[0];if(old)input.value=key==='amount'?old.amount_fen/100:old[key];wrap.append(input);extraGrid.append(wrap);expense[key]=input;
  }
  extra.append(extraGrid,el('p','已包含在生活预留中的同一笔支出，请勿重复填写。','research-note'));form.append(extra);
  const onlyLabel=el('label',undefined,'research-check'),only=el('input');only.type='checkbox';only.checked=!!b.analysis_only;onlyLabel.append(only,el('span','只分析，不生成资金办理方案'));form.append(onlyLabel);
  const submit=el('button','开始核查 ↗','primary');submit.type='submit';form.append(submit);root.append(form);
  const fen=input=>{if(!input.value)return null;const raw=input.value;if(!/^\d+(?:\.\d{1,2})?$/.test(raw))throw new Error('金额最多保留两位小数');const [whole,decimal='']=raw.split('.');return Number(whole)*100+Number(decimal.padEnd(2,'0'));};
  form.onsubmit=e=>{e.preventDefault();act(submit,async(epoch)=>{
    const mark=beginIntent();
    const expenses=[];if(Object.values(expense).some(x=>x.value)){if(Object.values(expense).some(x=>!x.value))throw new Error('请补全支出用途、金额和日期');expenses.push({label:expense.label.value,date:expense.date.value,amount_fen:fen(expense.amount)});}
    const constraints={topic:topic==='bills'?'bills':'combined',period:inputs.period.value||null,days:30,amount_fen:fen(inputs.amount_fen),reserve_fen:fen(inputs.reserve_fen)||0,emergency_fen:fen(inputs.emergency_fen)||0,needed_on:inputs.needed_on.value||null,expenses,analysis_only:only.checked};
    const data=await api('/api/research',{query:query.value,constraints,...(conversationStore.id?{conversation_id:conversationStore.id}:{})});
    if(epoch!==sessionStore.epoch||!isCurrent(mark))return;if(stamp===formStore.modalVersion)$('detail-dialog').close();
    data.query=query.value;renderPlan(data,true,mark);
  });};
}

export function renderResearch(data,autoPoll=true){
  if(researchStore.timer!==null)clearTimeout(researchStore.timer);const previous=researchStore.current;
  researchStore.current={...data,query:data.query||((previous?.id===data.id)?previous.query:'')};
  const panel=$('research-panel'),body=$('research-content');panel.hidden=false;body.replaceChildren();
  if(previous?.id!==data.id)body.hidden=false;
  let collapse=panel.querySelector('button.research-collapse');
  if(!(collapse instanceof HTMLButtonElement)){collapse=el('button','收起报告','research-collapse');panel.querySelector('.section-title')?.append(collapse);}
  collapse.textContent=body.hidden?'展开报告':'收起报告';collapse.setAttribute('aria-expanded',String(!body.hidden));
  const toggle=/** @type {HTMLButtonElement} */(collapse);
  toggle.onclick=()=>{body.hidden=!body.hidden;toggle.textContent=body.hidden?'展开报告':'收起报告';toggle.setAttribute('aria-expanded',String(!body.hidden));};
  $('research-status').textContent=data.stage||'核查资料';$('research-title').textContent=data.report?.title||'收支与理财研究';
  if(data.status==='RESEARCH_RUNNING'){
    panel.classList.add('research-running');body.append(el('p',data.message,'muted'));
    const progress=el('div',undefined,'research-steps');for(const label of ['理解需求','核查资料','回读补查','形成结论'])progress.append(el('span',label));body.append(progress);
    if(data.round)body.append(el('small','第'+data.round+'轮 · '+data.stage,'research-note'));
    const stop=el('button','取消分析');stop.onclick=()=>act(stop,async(epoch)=>{const result=await api('/api/research/'+encodeURIComponent(data.id)+'/cancel',{});if(epoch===sessionStore.epoch&&researchStore.current?.id===data.id)renderResearch(result);});body.append(stop);
    if(autoPoll)pollResearch(data.id,sessionStore.epoch);return;
  }
  panel.classList.remove('research-running');
  if(!data.report){body.append(el('p',data.message,'muted'));const retry=el('button','重新填写需求');retry.onclick=()=>openResearchForm('combined',researchStore.current);body.append(retry);return;}
  const report=data.report;
  const summary=el('section',undefined,'research-overview');summary.append(el('h3','01 · 结论与关键数字'));
  const lead=String(report.summary||'').split(/\n/)[0];summary.append(el('p',lead.length>180?lead.slice(0,180)+'…':lead,'research-summary'));
  if(String(report.summary||'').length>180){const full=el('details');full.append(el('summary','查看完整结论'),el('p',report.summary,'research-note'));summary.append(full);}
  const metrics=el('div',undefined,'research-metrics'),funding=report.funding||{};
  const deficitKey=report.funding.whole_plan_gap_fen!==undefined?'whole_plan_gap_fen':'shortfall_without_income_fen';
  for(const [key,label] of [['available_fen','当前可用'],['contingent_plan_fen','待办资金占用'],['investable_ceiling_fen','测算剩余额度'],[deficitKey,'资金缺口']]){
    if(funding[key]==null)continue;const item=el('div');item.append(el('small',label),el('strong',yuan(funding[key])));metrics.append(item);
  }
  summary.append(metrics);if(data.status!=='RESEARCH_READY')summary.append(el('p',data.message,'research-note'));body.append(summary);
  if(report.quality_note)body.append(el('p',report.quality_note,'research-note'));
  if(report.independent_review){
    const check=report.independent_review;
    body.append(el('p',!check.passed?'独立核验尚有待解决问题，暂不提供办理方案。':check.method==='independent_llm'?'研究结论已完成独立核验。':'已完成规则核对；当前为演示模式。','research-note'));
  }
  const findings=el('section',undefined,'research-findings');findings.append(el('h3','02 · 分析框架与依据'));
  for(const role of report.roles){
    const selected=report.findings.filter(f=>f.id.startsWith(role+':')&&!['funding','coverage'].includes(f.id.split(':')[1])&&f.kind!=='comparison');
    const section=el('details',undefined,'research-role');section.append(el('summary',(role==='bills'?'收支 · 分类、异常与未来支出':'理财 · 适当性、期限与流动性')+' · '+selected.length+'项核查'));
    for(const [index,finding] of selected.entries()){
      const item=el('article',undefined,'research-finding'+(finding.highlight?' highlighted':''));
      const labels={scenario:'情景测算',limitation:'边界与待核实',anomaly:'异常核查',fact:'账本事实',recommendation:'分析建议'};
      item.append(el('h4',String(index+1).padStart(2,'0')+' · '+(labels[finding.kind]||'核查结论')),el('p',finding.text));appendEvidenceButtons(item,data.id,finding.evidence_ids,report);section.append(item);
    }
    findings.append(section);
  }
  body.append(findings);
  if(report.products.length){
    const products=el('section',undefined,'research-products');products.append(el('h3','03 · 产品条件对比'),el('p','模拟产品存在本金损失风险。','risk-caption'));
    for(const product of report.products){
      const card=el('article',undefined,'research-product'+(product.excluded?' excluded':''));
      const head=el('div',undefined,'research-product-head');head.append(el('strong',product.name),el('span',product.excluded?'本次排除':'符合已提供条件','badge '+(product.excluded?'neutral':'GREEN')));card.append(head);
      const specs=el('div',undefined,'research-product-specs');for(const [label,value] of [['风险','R'+product.risk_level],['起购',yuan(product.minimum_fen)],['锁定',product.lock_days+'天'],['赎回','T+'+product.redeem_business_days],['赎回费用',product.fee_bps/100+'%']]){const cell=el('div');cell.append(el('small',label),el('b',value));specs.append(cell);}card.append(specs);
      const sources=product.evidence_ids.map(ref=>report.evidence.find(e=>e.id===ref)).filter(Boolean);
      const detail=el('details',undefined,'research-product-detail');detail.append(el('summary',product.excluded?'查看排除原因与原始依据':'查看条件说明与原始依据'));
      if(sources.length)detail.append(el('p','资料截至 '+sources[0].as_of.slice(0,10),'research-note'));
      for(const reason of product.reasons)detail.append(el('p',reason,'research-exclusion'));
      for(const note of product.notes)detail.append(el('p',note,'research-note'));
      appendEvidenceButtons(detail,data.id,product.evidence_ids,report);card.append(detail);products.append(card);
    }
    body.append(products);
  }
  const coverage=report.findings.find(f=>f.id==='bills:coverage');if(coverage)body.append(el('p',coverage.text,'research-note'));
  for(const gap of report.gaps)body.append(el('p',gap,'error'));
  if(report.next_question)body.append(el('p',report.next_question,'research-note'));
  const actions=el('div',undefined,'research-actions');actions.append(el('h3','下一步'));
  for(const option of report.options){
    const button=el('button','生成办理计划 · '+option.label,'primary');button.dataset.option=option.id;
    button.onclick=()=>act(button,async(epoch)=>{
      const mark=beginIntent();
      const result=await api('/api/research/'+encodeURIComponent(data.id)+'/select',{option_id:option.id,expected_version:report.version});
      if(epoch!==sessionStore.epoch||researchStore.current?.id!==data.id||!isCurrent(mark))return;
      if(!result.id||!result.nodes){renderPlan(result,true,mark);return;}
      for(const child of actions.querySelectorAll('button[data-option]'))if(child instanceof HTMLButtonElement)child.disabled=true;
      researchStore.current.selected_plan_id=result.id;renderPlan(result,true,mark);await overview();$('nodes').scrollIntoView({block:'nearest',behavior:'smooth'});
    }).finally(()=>{if(researchStore.current?.id===data.id&&researchStore.current.selected_plan_id){button.disabled=true;button.textContent='已生成办理计划 · '+option.label;}});if(data.selected_plan_id)button.disabled=true;actions.append(button);
  }
  const again=el('button','补充条件后重新分析');again.onclick=()=>openResearchForm(report.roles.includes('wealth')?'combined':'bills',researchStore.current);actions.append(again);body.append(actions);
}

export function pollResearch(id,epoch){
  researchStore.timer=setTimeout(async()=>{
    if(epoch!==sessionStore.epoch||researchStore.current?.id!==id)return;
    try{const next=await api('/api/research/'+encodeURIComponent(id));if(epoch!==sessionStore.epoch||researchStore.current?.id!==id)return;renderResearch(next);}
    catch(error){if(epoch===sessionStore.epoch&&researchStore.current?.id===id){$('research-status').textContent='连接暂缓';toast(error.message);pollResearch(id,epoch);}}
  },1500);
}

export function appendEvidenceButtons(root,id,refs,report){
  const strip=el('div',undefined,'research-evidence-links');
  for(const ref of refs){const ev=report.evidence.find(e=>e.id===ref);if(!ev)continue;const button=el('button',researchToolLabels[ev.tool]||'查看依据');button.type='button';
    button.onclick=()=>act(button,async(epoch)=>{const data=await api('/api/research/'+encodeURIComponent(id)+'/evidence/'+encodeURIComponent(ref));if(epoch!==sessionStore.epoch||researchStore.current?.id!==id)return;showResearchEvidence(data);});strip.append(button);
  }root.append(strip);
}

export function showResearchEvidence(ev){
  const root=modal(researchToolLabels[ev.tool]||'核查依据'),d=ev.data;
  root.append(el('p','资料时间 '+ev.as_of.slice(0,16).replace('T',' ')+' · '+(ev.currently_fresh?'与当前资料一致':'历史快照，当前条件可能已变化'),'muted'));
  if(ev.tool==='product_terms'){for(const p of d.products){root.append(el('h3',p.catalog.name));for(const c of p.disclosure?.clauses||[])root.append(el('p',c.text));}}
  else if(ev.tool==='transaction_details'){for(const row of d.rows){const item=el('article',undefined,'research-source-row');item.append(el('strong',row.subject),el('span',row.posted_at.slice(0,10)+' · '+yuan(row.amount_fen)));root.append(item);}}
  else if(['cashflow.project','stress'].includes(ev.tool)){
    root.append(el('p','当前可用 '+yuan(d.available_fen)+' · 未来已知扣费 '+yuan(d.known_future_debits_fen)));
    for(const x of d.obligations)root.append(el('p',x.date+' · 已登记代扣 '+yuan(x.amount_fen)));
    for(const x of d.indicative_income)root.append(el('p',x.date+' · 预计收入 '+yuan(x.amount_fen)+'（尚未入账）'));
    for(const x of d.pending_plans||[])root.append(el('p','未执行办理计划 · 潜在支出 '+yuan(x.amount_fen)));
    if(ev.tool==='stress')root.append(el('p','收入推迟7天，已知扣费增加20%，当天先扣支出再计预计收入。'));
  }else if(ev.tool==='account')root.append(el('p','余额 '+yuan(d.balance_fen)+'，可用 '+yuan(d.available_fen)+'；风险等级R'+d.risk_level+'，测评有效至 '+d.risk_valid_until));
  else if(ev.tool==='constraints'){root.append(el('p','计划申购 '+(d.amount_fen?yuan(d.amount_fen):'未指定')+'；生活预留 '+yuan(d.reserve_fen)+'；应急预留 '+yuan(d.emergency_fen)),el('p','用款日期 '+(d.needed_on||'未指定')));for(const x of d.expenses)root.append(el('p',x.date+' · '+x.label+' '+yuan(x.amount_fen)));for(const x of d.negative_constraints)root.append(el('p',x));}
  else if(ev.tool==='bills.report')root.append(el('p',d.period+' · 工资 '+yuan(d.income_fen)+'，转入 '+yuan(d.incoming_fen)+'，消费净额 '+yuan(d.consumption_fen)+'，转账及AA '+yuan(d.transfer_fen)));
  else if(ev.tool==='wealth.compare'){for(const p of d.products)root.append(el('p',p.name+' · R'+p.risk_level+' · 起购 '+yuan(p.minimum_fen)));}
  else root.append(el('p','疑似重复扣款分组 '+(d.alerts?.length||0)+' 组，需要结合原始流水和商户订单核实。'));
  const details=el('details',undefined,'research-source-meta');details.append(el('summary','查看来源与完整性标识'),el('p','来源记录 '+ev.source_ids.length+' 条 · '+ev.source_ids.slice(0,8).join('、')),el('p','SHA-256 '+ev.sha256));root.append(details);
}
