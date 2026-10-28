// @ts-check
import {$,act,businessText,categories,doneStates,el,fieldNames,jsonDetails,modal,operations,roles,states,yuan} from "./ui.js";
import {accountStore,conversationStore,formStore,planStore,researchStore,sessionStore,beginIntent,ticket,isCurrent} from "./state.js";
import {incomingCard,informationSources,informationCandidates,message} from "./chat.js";
import {renderResearch} from "./research.js";
import {openSecurity} from "./security.js";
import {api} from "./api.js";
import {overview} from "./account-view.js";
import {renderSupportBanner} from './support-view.js';
import {openSupport} from './support-view.js';
import {openBusinessAction} from './business.js';
import {openRiskConfirmation,productRiskCard,renderPressure,revisionView,riskBanner} from './risk-view.js';

export function summaryField(root, name, value, cls='') {
  if (value === undefined || value === '' || value === null) return;
  const item = el('span',undefined,cls); item.append(el('em',name),document.createTextNode(String(value))); root.append(item);
}

export function nodeSummary(node,nodes=[]) {
  const p = node.params, box = el('div',undefined,'node-summary');
  summaryField(box,'金额',p.amount_fen ? yuan(p.amount_fen) : p.budget_fen ? yuan(p.budget_fen) : null,'amount');
  summaryField(box,'收款人',p.recipient_hint);
  if(node.operation==='cards.apply')summaryField(box,'卡片类型',p.kind==='virtual'?'虚拟卡':'实体卡');
  summaryField(box,'用途',p.purpose); summaryField(box,'备注',p.subject);
  const product = accountStore.facts?.products?.find(item=>item.id === p.product_id);
  summaryField(box,'产品',p.product_id ? product?.name || '所选理财产品' : null);
  summaryField(box,'生日',p.birthday_date); summaryField(box,'地址',p.address);
  if (p.quote?.total_fen) { summaryField(box,'采购总价',yuan(p.quote.total_fen),'amount'); summaryField(box,'配送地址',p.quote.address); }
  summaryField(box,'报告',p.period ? (p.annual ? p.period.slice(0,4)+'年度' : p.period) : null);
  summaryField(box,'单笔上限',p.single_limit_fen ? yuan(p.single_limit_fen) : null);
  summaryField(box,'日上限',p.daily_limit_fen ? yuan(p.daily_limit_fen) : null);
  if(Array.isArray(p.blocked_categories))summaryField(box,'限制类别',p.blocked_categories.length?p.blocked_categories.map(x=>({gaming:'游戏',gambling:'博彩'}[x]||x)).join('、'):'不限制类别');
  if(node.operation==='wealth.purchase'&&product)summaryField(box,'产品条件','风险R'+product.risk_level+' · 起购'+yuan(product.minimum_fen)+' · 锁定'+product.lock_days+'天；不保证本金和收益');
  const queried=nodes.map(n=>n.result?.data?.selected).find(s=>s&&s.subscription_id===p.subscription_id);
  if(node.operation==='wealth.purchase')summaryField(box,'适当性','口头承担风险不能替代有效风险测评');
  const subscriptionName=p.name_hint||node.result?.data?.name||queried?.name||({'S_VIDEO':'视频会员','S_MUSIC':'音乐会员'}[p.subscription_id]);
  summaryField(box,'订阅',subscriptionName||(p.subscription_id?'待查询核对的订阅':null));
  summaryField(box,'取消范围',p.scope ? {bank:'银行代扣',merchant:'商户合同',both:'代扣与商户合同'}[p.scope] : null);
  summaryField(box,'续费提醒',Number.isInteger(p.days_before) ? (p.days_before===0?'扣费当天提醒':'提前'+p.days_before+'天提醒') : null);
  summaryField(box,'参与者',p.participants?.map(id=>id===sessionStore.user?.id?'我':({u_demo:'演示用户',u_wang:'小王',u_li:'小李'}[id]||'已登记参与者')).join('、'));
  if (!box.childNodes.length && node.operation === 'life.purchase') summaryField(box,'采购','到期取得鲜花、蛋糕和配送报价');
  return box;
}

export function resultLine(root, label, value) {
  const line = el('div',undefined,'result-line'); line.append(el('span',label),el('b',String(value))); root.append(line);
}

function aaPending(node) {
  return node.status==='SUCCEEDED'&&['aa.create','aa.status'].includes(node.operation)&&node.result?.data?.shares?.some(s=>s.status==='UNPAID');
}

function nodeStatusLabel(node) {
  if(node.status==='SUCCEEDED'&&node.operation==='aa.create')return '收款请求已发出';
  if(node.status==='SUCCEEDED'&&node.operation==='aa.status')return aaPending(node)?'尚未收齐':'收款进度已查询';
  return states[node.status]||node.status;
}

export function resultTable(root, headings, rows) {
  const wrap = el('div',undefined,'result-table-wrap'), table = el('table',undefined,'result-table');
  const head = el('tr'); headings.forEach(h=>head.append(el('th',h))); const thead = el('thead'); thead.append(head);
  const body = el('tbody'); rows.forEach(values=>{const row=el('tr'); values.forEach(value=>row.append(el('td',String(value ?? '—')))); body.append(row);});
  table.append(thead,body); wrap.append(table); root.append(wrap);
}

export function resultView(node,assistedDone=false) {
  const receipt = node.result, data = receipt.data, box = el('div',undefined,'result-panel');
  if (node.operation === 'bills.report') {
    if(!data.transaction_ids?.length){box.append(el('p','该期间暂无入账流水，不能据此认定没有消费。请调整查询期间，或补充该期账单后再核对。'));return box;}
    const stats = el('div',undefined,'mini-stats');
    for (const [label,number] of [['工资收入',data.income_fen],['毛消费',data.gross_consumption_fen],['退款',data.refund_fen],['净消费',data.consumption_fen],['转账支出',data.transfer_fen]]) {
      const part=el('div',undefined,'mini-stat'); part.append(el('b',yuan(number)),el('small',label)); stats.append(part);
    }
    box.append(stats);
    if(data.incoming_fen) resultLine(box,'本人账户资金转入（不计为工资收入）',yuan(data.incoming_fen));
    const max = Math.max(1,...Object.values(data.categories).map(Math.abs));
    for (const [category,amount] of Object.entries(data.categories)) {
      const row=el('div',undefined,'category-row'), track=el('div',undefined,'category-track'), bar=el('i');
      bar.style.width=Math.abs(amount)/max*100+'%'; track.append(bar); row.append(el('span',categories[category]||category),track,el('span',yuan(amount))); box.append(row);
    }
    box.append(el('p','净消费＝毛消费－退款；转账及AA支出另列，不计消费。已有记录月份 '+data.covered_months.join('、')+'；截至 '+data.as_of.slice(0,10)+'。分类和月合计已扣除退款。','result-note'));
  } else if (node.operation === 'wealth.compare') {
    resultTable(box,['产品','产品风险','起购','锁定期','赎回','当前申购条件'],data.products.map(p=>[p.name,'R'+p.risk_level,yuan(p.minimum_fen),p.lock_days?p.lock_days+'天':'无锁定期',p.redeem_business_days?'T+'+p.redeem_business_days:'当日模拟到账',p.eligible?'满足当前已知条件':p.eligibility_reasons?.join('；')||'需核对测评与资金条件']));
    box.append(el('p','产品存在本金损失可能；验证身份不能降低投资风险。比较结果不等于申购授权，条款与资金条件会在办理时再次核验。','risk-caption'));
    box.append(el('p',data.recommendation_reason,'result-note'));
  } else if (node.operation === 'wealth.holdings') {
    if (!data.holdings.length) box.append(el('p','当前没有理财持仓。'));
    for (const h of data.holdings) resultLine(box,accountStore.facts?.products?.find(p=>p.id===h.product_id)?.name||'理财持仓','市值 '+yuan(h.market_value_fen)+' · 未实现收益 '+yuan(h.unrealized_gain_fen)+(h.redeemable_value_fen!==undefined?' · 当前可赎 '+yuan(h.redeemable_value_fen):'')+(h.next_unlock_date?' · 下批解锁 '+h.next_unlock_date:''));
    for (const r of data.redemptions) resultLine(box,'赎回 '+yuan(r.amount_fen),r.status==='SETTLED'?'已到账':'等待 '+r.settles_on+' 结算');
  } else if (node.operation === 'wealth.redeem') {
    resultLine(box,data.status==='SETTLED'?'已到账金额':'待结算金额',yuan(data.amount_fen)); resultLine(box,'结算状态',data.status==='SETTLED'?'已到账':'预计 '+data.settles_on+' 到账');
  } else if (node.operation === 'wealth.purchase') {
    resultLine(box,'本次申购本金',yuan(data.amount_fen));resultLine(box,'申购后账户余额',yuan(data.balance_fen));
    box.append(el('p','已按本次金额核验测评、风险等级、起购金额与可用资金。演示产品不承诺本金和收益；赎回受锁定期及到账条件限制。','result-note'));
  } else if (node.operation === 'subscriptions.cancel') {
    resultLine(box,'银行代扣',data.bank_mandate==='REVOKED'?'已撤销':'仍有效'); resultLine(box,'商户合同',data.merchant_contract==='CANCELLED'?'已取消':'仍有效');
    box.append(el('p',(data.message||'')+' 本次取消不产生退款。'+(data.merchant_contract==='ACTIVE'?'撤销银行代扣不等于解除商户合同，可能仍有合同义务。如需关闭商户续费，请前往商户App的会员/自动续费管理核查，保留结果后再核对合同状态；本次未解除商户合同。':'商户自动续费已取消；后续扣费以这里列出的授权和合同状态为准。'),'result-note'));
    box.append(el('p','本次只处理所选订阅的授权或合同，没有冻结整张卡，也没有修改卡片普通支付限制；其他支付仍按卡片当前状态与银行规则核验。','result-note'));
  } else if (node.operation === 'subscriptions.query') {
    resultTable(box,['订阅','金额','下次扣费','银行代扣','商户合同'],data.subscriptions.map(s=>[s.name,yuan(s.amount_fen),s.next_debit,s.bank_mandate==='ACTIVE'?'有效':'已撤销',s.merchant_contract==='ACTIVE'?'有效':'已取消']));
  } else if (node.operation === 'subscriptions.detect') {
    box.append(el('p','发现 '+data.candidates.length+' 个周期扣费候选，需要核实合同。'));
    if(data.candidates.length)resultTable(box,['扣费说明','参考扣费','已观察月数'],data.candidates.map(c=>[c.description||'待核实商户',yuan(c.amount_fen),c.observed_months]));
  } else if (node.operation === 'subscriptions.remind') {
    resultLine(box,'续费提醒',data.days_before===0?'扣费当天提醒':'提前'+data.days_before+'天提醒');
    if(data.name)resultLine(box,data.name,yuan(data.amount_fen)+' · 下次扣费 '+data.next_debit);
    box.append(el('p','已设置应用内提醒；没有取消合同或停止续费。','result-note'));
  } else if (node.operation === 'bills.anomalies') {
    box.append(el('p',data.alerts.length ? data.alerts.map(a=>a.reason).join('；') : '未发现规则定义的疑似重复扣款。'));
    const records=data.alerts.flatMap(a=>a.transactions||[]);
    if(records.length)resultTable(box,['日期','交易说明','扣款金额','流水引用'],records.map(t=>[t.date,t.description,yuan(t.amount_fen),t.transaction_id]));
    if(data.alerts.length)box.append(el('p','请先核对这些消费是否分别发生；若仍有异议，可保留交易记录联系银行核查。当前未退款，也未认定欺诈。','result-note'));
  } else if (node.operation === 'cashflow.project') {
    resultLine(box,'压力情景最低余额',yuan(data.minimum_projected_fen)); resultLine(box,'已知未来扣费',yuan(data.known_future_debits_fen));
    box.append(el('p','仅覆盖登记订阅及已确认预计收入；预计收入不进入账户可用余额。','result-note'));
  } else if (node.operation === 'life.purchase') {
    for (const order of data.orders) resultLine(box,order.item_id==='FLOWERS'?'鲜花订单':'蛋糕订单',yuan(order.amount_fen)+' · 已确认');
    resultLine(box,'剩余预算已释放',yuan(data.released_fen));
  } else if (node.operation === 'aa.create' || node.operation === 'aa.status') {
    resultLine(box,'收款事项',data.subject || node.params.subject || 'AA收款');
    const paid=data.shares.filter(s=>s.status==='PAID').reduce((sum,s)=>sum+s.amount_fen,0),pending=data.shares.filter(s=>s.status==='UNPAID');
    resultLine(box,node.operation==='aa.create'?'发起时已收':'本次查询已收',yuan(paid));
    resultLine(box,'尚未支付',pending.length+'人 · '+yuan(pending.reduce((sum,s)=>sum+s.amount_fen,0)));
    box.append(el('p',pending.length?'请求已发出，款项尚未收齐；发起收款不会再次扣除垫付消费。':'其他参与者的应付份额已收齐，发起人自担不计入收款。','result-note'));
    for (const share of data.shares) resultLine(box,share.user_id===sessionStore.user?.id?'我':({u_demo:'演示用户',u_wang:'小王',u_li:'小李'}[share.user_id]||'已登记参与者'),yuan(share.amount_fen)+' · '+({SELF_SHARE:'发起人自担',PAID:'已支付',UNPAID:'等待本人支付'}[share.status]||share.status));
    const requestId=data.request_id||data.id;
    if(requestId){const query=el('button','查询最新收款进度','secondary');query.onclick=()=>act(query,async()=>{
      const mark=ticket('aa-status'),fresh=await api('/api/businesses/funds/aa_status/prepare',{params:{request_id:requestId}});renderPlan(fresh,true,mark);
    });box.append(query);}
  } else if (node.operation === 'cards.query') {
    data.cards.forEach(card=>{resultLine(box,card.name+' · '+card.last4,({ACTIVE:'正常',LOST:'已挂失',APPROVED_PENDING_DELIVERY:'待寄送'}[card.status]||'待核实'));cardConditions(box,card);});
  } else if (node.operation.startsWith('cards.')) {
    resultLine(box,'卡片状态',data.status==='LOST'?'已挂失':data.status==='APPROVED_PENDING_DELIVERY'?'审批通过，等待模拟寄送':'可用');
    cardConditions(box,data);
    if(node.operation==='cards.apply')box.append(el('p','本次以存量客户身份资料有效作为合成演示前提，Agent未完成真实身份核验；真实开卡仍需银行审核申请资料并按要求认证。','result-note'));
  } else if (node.operation === 'balance.query') {
    resultLine(box,'账户余额',yuan(data.balance_fen)); resultLine(box,'可用资金',yuan(data.available_fen));
  } else if (node.operation === 'life.orders') {
    resultLine(box,'生活目标',data.goals.length+' 项'); resultLine(box,'商户订单',data.orders.length+' 笔');
  } else if (node.operation === 'wealth.assess') {
    resultLine(box,'模拟测评等级','C'+data.risk_level); box.append(el('p','五题模拟测评，仅供本原型演示。','result-note'));
  } else {
    if (data.transaction_id) box.append(el('p','银行已确认本次操作。'));
    else box.append(el('p','此步骤已完成。'));
  }
  if (data.requires_human) box.append(el('p',assistedDone?'当时未完成的商户部分已在关联计划办结，详情见协助记录；本凭证保留原始处理结果。':'商户合同尚未取消，已登记协助工单；请查看协助进度后继续。','notice-warning'));
  box.append(el('p','回执 '+receipt.receipt_id+' · '+receipt.business_time.slice(0,16).replace('T',' '),'receipt-caption'));
  return box;
}

function cardConditions(box,card){
  if(card.kind)resultLine(box,'卡片类型',card.kind==='virtual'?'虚拟借记卡，用于关联账户付款':'实体借记卡');
  if(card.name)resultLine(box,'卡片名称',card.name);
  if(card.single_limit_fen!==undefined)resultLine(box,'单笔交易上限',yuan(card.single_limit_fen));
  if(card.daily_limit_fen!==undefined)resultLine(box,'日累计交易上限',yuan(card.daily_limit_fen));
  if(Array.isArray(card.blocked_categories))resultLine(box,'限制类别',card.blocked_categories.length?card.blocked_categories.map(x=>({gaming:'游戏',gambling:'博彩'}[x]||x)).join('、'):'无额外类别限制');
  box.append(el('p','以上为银行规则核准的支付限制，不代表新增信用授信。','result-note'));
}

export function drawDag(nodes) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  const positions = new Map(), layers = new Map();
  for (const node of nodes) {
    const dependencies=[...node.depends_on,...(node.after||[])];
    const level=Math.max(0,...dependencies.map(key=>(positions.get(key)?.level??-1)+1));
    const index=layers.get(level)||0;layers.set(level,index+1);positions.set(node.local_id,{level,x:10+level*166,y:14+index*66});
  }
  const width=Math.max(310,...[...positions.values()].map(p=>p.x+150));
  const height=Math.max(85,...[...positions.values()].map(p=>p.y+56));
  svg.setAttribute('width',String(width));svg.setAttribute('height',String(height));svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
  const add=(tag,attrs,text)=>{const item=document.createElementNS(svg.namespaceURI,tag);Object.entries(attrs).forEach(([k,v])=>item.setAttribute(k,String(v)));if(text)item.textContent=text;svg.append(item);return item;};
  for (const node of nodes) {
    const target=positions.get(node.local_id);
    for (const [dependencies,soft] of [[node.depends_on,false],[node.after||[],true]]) for (const key of dependencies) {
      const source=positions.get(key);if(!source)continue;
      add('path',{d:`M${source.x+142} ${source.y+22} C${source.x+155} ${source.y+22},${target.x-15} ${target.y+22},${target.x} ${target.y+22}`,fill:'none',stroke:soft?'#b5b8a2':'#7e9c71','stroke-width':1,'stroke-dasharray':soft?'3 3':'none'});
    }
  }
  for (const node of nodes) {
    const p=positions.get(node.local_id),fill=node.status==='SUCCEEDED'?'#e9f0e2':node.status.startsWith('WAIT')?'#f4edda':'#f2f4ea';
    add('rect',{x:p.x,y:p.y,width:142,height:44,rx:5,fill,stroke:'#d4ddc9'});
    add('text',{x:p.x+10,y:p.y+17,'font-size':9,fill:'#677f5a'},roles[node.agent]+' · '+nodeStatusLabel(node));
    add('text',{x:p.x+10,y:p.y+32,'font-size':9,fill:'#7b916b'},operations[node.operation]||node.operation);
  }
  $('dag-canvas').replaceChildren(svg);
}

/** @param {any} data @param {boolean} [speak] @param {ReturnType<typeof ticket>|null} [mark] @param {((text:string)=>HTMLElement)|null} [reply] */
export function renderPlan(data, speak=false,mark=null,reply=null) {
  const say=reply||message;
  if(data.nodes)planStore.cache.set(data.id,data);
  if(mark&&!isCurrent(mark))return;
  if(returnFromAssessment(data))return;
  if(data.nodes&&!speak&&!mark&&planStore.current&&planStore.current.id!==data.id)return;
  if(data.nodes&&planStore.current&&planStore.current.id===data.id&&data.version<planStore.current.version)return;
  // Silent refreshes of an old plan must never steal the current dialogue.
  if(!data.nodes||speak||!conversationStore.id)conversationStore.id = data.conversation_id || conversationStore.id;
  if(conversationStore.id&&sessionStore.user)sessionStorage.setItem('bank-conversation-'+sessionStore.user.id,conversationStore.id);
  if(data.status?.startsWith('RESEARCH_')){if(speak)say(data.report?.summary||data.message);renderResearch(data);if(speak)$('research-panel').scrollIntoView({block:'nearest',behavior:'smooth'});return;}
  if (!data.nodes) { const row=say(data.message || '请补充办理信息。');incomingCard(data,row);informationSources(row,data.sources);informationCandidates(row,data.candidates,data.excluded_candidates);
    if(data.status==='ANSWERED'&&!planStore.current){$('plan-status').textContent='咨询已答复';const empty=$('nodes').querySelector('.plan-empty');if(empty)empty.textContent='本次咨询不产生办理计划。需要办理时，这里会显示核对详情与操作进度。';}
    return; }
  if(speak&&data.assistant_message)say(data.assistant_message);
  if(speak)ticket('plan-snapshot');
  renderFundingPreview(data);
  renderSupportBanner(data.support_cases||[]);
  const openDetails = new Set([...$('nodes').querySelectorAll('details[open]')].map(node=>node.getAttribute('data-key')));
  const current=planStore.current;
  const unchanged=current&&JSON.stringify(current.request_goals||[])===JSON.stringify(data.request_goals||[])&&current.funding_preview.fingerprint===data.funding_preview.fingerprint&&current.id===data.id&&current.version===data.version&&current.status===data.status&&current.security_locked===data.security_locked&&JSON.stringify(current.support_cases||[])===JSON.stringify(data.support_cases||[])&&current.summary===data.summary&&current.nodes.length===data.nodes.length&&data.nodes.every(node=>{
    const previous=current.nodes.find(old=>old.id===node.id);return previous&&previous.status===node.status&&previous.fingerprint===node.fingerprint&&previous.result?.receipt_id===node.result?.receipt_id&&JSON.stringify(previous.error)===JSON.stringify(node.error);
  });
  // An identical silent snapshot must preserve controls, including code delivery in flight.
  // A changed plan, status or fingerprint rebuilds the controls and discards stale codes.
  if(!speak&&unchanged){planStore.current=data;return;}
  const assisted=new Set((data.support_cases||[]).filter(c=>c.plan_id===data.id&&c.status==='RESOLVED'&&c.followup_plan_id&&c.node_id).map(c=>c.node_id));
  const assistedComplete=assisted.size>0&&data.nodes.every(n=>n.status==='SUCCEEDED'||assisted.has(n.id));
  const planLabel=data.status==='COMPLETED'&&data.nodes.some(aaPending)?'请求已发出 · AA尚未收齐':states[data.status]||data.status;
  planStore.current=data; planStore.liveSignature=''; $('plan-summary').textContent=businessText(data.summary); $('plan-status').textContent=assistedComplete?'原记录 · 后续已办结':planLabel;
  $('plan-status').className='badge '+data.status; $('plan-actions').hidden=false; $('plan-progress').hidden=false; $('dag-view').hidden=false;
  const completed=data.nodes.filter(node=>doneStates.has(node.status)||assisted.has(node.id)).length;
  $('progress-count').textContent=completed+' / '+data.nodes.length+' 步';$('progress-label').textContent=assistedComplete?'关联办理完成':planLabel;
  $('progress-fill').style.width=completed/data.nodes.length*100+'%';$('active-agents').replaceChildren();
  $('active-agents').hidden=false;
  if(data.review.architecture==='supervisor_four_specialists'){
    $('active-agents').append(el('span','统一办理','active-agent'));
    if(data.review.semantic_review?.plan_version===data.version) $('active-agents').append(el('span',data.review.semantic_review.method==='independent_llm'?'已独立核验':'规则核对 · 演示','active-agent'));
  }
  for(const role of new Set(data.nodes.map(n=>n.agent)))$('active-agents').append(el('span',roles[role]+'服务','active-agent'));
  $('resume').hidden=assistedComplete||['COMPLETED','CANCELLED','UNKNOWN'].includes(data.status);
  $('handoff').hidden=assistedComplete||['COMPLETED','CANCELLED','PARTIAL'].includes(data.status);
  $('cancel').hidden=assistedComplete||data.status==='CANCELLED'||data.nodes.some(n=>n.status==='UNKNOWN');
  $('nodes').replaceChildren();drawDag(data.nodes);
  for(const history of data.completed_history||[]){
    const card=el('section',undefined,'node');card.append(el('p',history.message));
    const view=el('button','查看原办理凭证','secondary');view.type='button';
    view.onclick=async()=>{try{const original=await api('/api/plans/'+encodeURIComponent(history.plan_id));renderPlan(original,true);}catch(error){message(error.message);}};
    card.append(view);$('nodes').append(card);
  }
  const revision=revisionView(data);if(revision)$('nodes').append(revision);
  if(data.request_goals?.length){
    const goals=el('section',undefined,'node request-goals');goals.append(el('h3','这次请求的需求进度'));
    for(const goal of data.request_goals){
      const row=el('div',undefined,'goal-row');row.dataset.status=goal.status;
      const bound=data.nodes.find(n=>n.id===goal.node_id);
      const heading=el('div',undefined,'goal-heading'),status=bound?nodeStatusLabel(bound):states[goal.status]||({NEEDS_INPUT:'待补充条件',UNAVAILABLE:'资料暂不可用',UNSUPPORTED:'当前能力未接入',ANSWERED:'已查询',NEEDS_RETRY:'待重试查询',NEEDS_RECHECK:'需重新核查',RESEARCH_RUNNING:'分析中',RESEARCH_READY:'分析完成'})[goal.status]||goal.status;
      heading.append(el('strong',goal.title),el('span',status,'goal-status'));row.append(heading);
      if(goal.kind==='bank_operation'){goals.append(row);continue;}
      const budget=goal.result?.investment_budget;
      if(budget){
        const parts=[];
        if(budget.budget_fen!=null)parts.push('资料筛选预算 '+yuan(budget.budget_fen));
        if(budget.periodic_amount_fen!=null)parts.push('每次 '+yuan(budget.periodic_amount_fen));
        if(budget.full_contributions!=null)parts.push('可覆盖 '+budget.full_contributions+' 次完整投入');
        if(parts.length)row.append(el('p',parts.join(' · ')+'；未购买或开通定投。','result-note'));
      }
      const detail=el('details',undefined,'goal-detail');detail.append(el('summary',goal.kind==='research'?'查看研究进度与结论':'查看条件与下一步'));
      const body=el('div');if(goal.notice)body.append(el('p',goal.notice,'risk-caption'));if(goal.result?.message)body.append(el('p',goal.result.message));detail.append(body);row.append(detail);
      if(goal.kind==='research'&&goal.result?.id){const view=el('button','打开研究报告');view.onclick=()=>{renderResearch(goal.result);$('research-panel').scrollIntoView({block:'start',behavior:'smooth'});};body.append(view);}
      if(goal.kind==='bank_task'&&goal.pending_task?.action==='wealth.purchase'){
        const choice=el('div',undefined,'goal-products');
        for(const product of accountStore.facts?.products||[]){const button=el('button','选择'+product.name);button.onclick=()=>{ $('input').value='我选择'+product.name;$('chat').requestSubmit();};choice.append(button);}body.append(choice);detail.open=true;
      }
      if(goal.result?.sources)informationSources(row,goal.result.sources);
      if(goal.result)informationCandidates(row,goal.result.candidates,goal.result.excluded_candidates);goals.append(row);
    }
    $('nodes').append(goals);
  }
  for (const [index,node] of data.nodes.entries()) {
    const awaiting=['WAIT_CONFIRM','WAIT_MFA'].includes(node.status);
    const card=el('article',undefined,'node'+(awaiting?' needs-action':node.error?(node.error.category==='business'?' condition-node':' error-node'):node.status==='SUCCEEDED'?' done-node':''));
    card.dataset.nodeId=node.id;card.tabIndex=-1;
    const head=el('div',undefined,'nodehead');head.append(el('span',String(index+1).padStart(2,'0'),'node-index'),el('strong',operations[node.operation]||node.operation),el('span',assisted.has(node.id)?'关联计划已办结':nodeStatusLabel(node),'badge '+node.status));card.append(head);
    const permission=node.status==='UNKNOWN'?'已提交，先核实原交易':node.risk?.status==='VERIFYING'?'等待原交易核实':doneStates.has(node.status)?'以实际办理状态和凭证为准':{GREEN:'直接办理',YELLOW:'需明确确认',RED:'需验证后办理',DYNAMIC:'到期评估风险'}[node.tier]||node.tier;
    const metadata=[node.depends_on.length?'依赖前置结果':'',node.scheduled_for?'预约 '+node.scheduled_for.slice(0,16).replace('T',' '):'',node.status==='UNKNOWN'||node.risk?.status==='VERIFYING'?permission:''].filter(Boolean);
    if(metadata.length)card.append(el('p',metadata.join(' · '),'node-meta'));
    card.append(nodeSummary(node,data.nodes));
    const risk=riskBanner(node);if(risk&&!assisted.has(node.id))card.append(risk);
    if(node.risk?.product&&!doneStates.has(node.status))card.append(productRiskCard(node.risk.product));
    if (node.result) {
      const result=el('details',undefined,'node-result-detail');
      result.open=aaPending(node)||(node.operation==='wealth.compare'&&(data.request_goals||[]).some(g=>g.kind==='bank_task'&&g.pending_task?.action==='wealth.purchase'));
      result.append(el('summary','查看本步骤结果与凭证'),resultView(node,assisted.has(node.id)));card.append(result);
    }
    if (assisted.has(node.id)&&node.error)card.append(el('p','本步骤的剩余业务已由关联计划办结，原始受阻记录保留在凭证与审计中。','result-note'));
    if (node.error&&!assisted.has(node.id)) {
      const unlocked=node.error.code==='SECURITY_LOCKED'&&!data.security_locked;
      if(!risk||unlocked){card.append(el('p',unlocked?'安全锁定已解除，这项业务仍待你重新核对。':node.error.message,node.error.category==='business'?'risk-caption':'error'));
      if(unlocked||node.error.next_step)card.append(el('p',unlocked?'点击“解锁后继续核对”，核对完成后再确认办理。':node.error.next_step,'node-next-step'));}
    }
    const controls=el('div',undefined,'nodefooter');
    if(node.error?.code==='SECURITY_LOCKED'){
      const unlock=el('button',data.security_locked?'前往安全中心解锁':'解锁后继续核对','primary');
      unlock.onclick=()=>act(unlock,async(epoch)=>{
        if(data.security_locked){await openSecurity();return;}
        const mark=ticket('plan-action'),result=await api('/api/plans/'+data.id+'/resume',{});
        if(epoch===sessionStore.epoch&&planStore.current?.id===data.id){renderPlan(result,true,mark);await overview();}
      });controls.append(unlock);
    }
    if (awaiting) {
      const approve=el('button',node.tier==='RED'?'核对并验证':'核对并确认','primary');
      if(data.funding_preview.shortfall_fen>0||!['CONFIRM','VERIFY'].includes(node.risk?.status))approve.disabled=true;
      approve.onclick=()=>act(approve,()=>openRiskConfirmation(data,node,{render:renderPlan,summary:nodeSummary,approve:async(fresh,current,verification)=>{
        const epoch=sessionStore.epoch;
        const mark=ticket('plan-action');
        try{const result=await api('/api/plans/'+fresh.id+'/approve',{node_id:current.id,fingerprint:current.fingerprint,decision:'approve',...verification});if(epoch!==sessionStore.epoch||planStore.current?.id!==fresh.id||!isCurrent(mark))return;await overview();renderPlan(result,true,mark);}
        catch(error){if(error.code==='STALE_APPROVAL')renderPlan(await api('/api/plans/'+data.id));if(error.code==='SECURITY_LOCKED')await openSecurity();throw error;}
      }}));
      const reject=el('button','不办理此步');reject.onclick=()=>act(reject,async(epoch)=>{const mark=ticket('plan-action');const result=await api('/api/plans/'+data.id+'/approve',{node_id:node.id,fingerprint:node.fingerprint,decision:'reject'});if(epoch!==sessionStore.epoch||planStore.current?.id!==data.id)return;renderPlan(result,true,mark);await overview();});controls.append(approve,reject);
    }
    if(node.status==='UNKNOWN'){const check=el('button','核实银行结果','primary');check.onclick=()=>act(check,async(epoch)=>{const mark=ticket('plan-action');const result=await api('/api/plans/'+data.id+'/reconcile',{});if(epoch!==sessionStore.epoch||planStore.current?.id!==data.id)return;renderPlan(result,true,mark);await overview();});controls.append(check);}
    for(const action of node.risk?.permitted_next_actions||[]){
      if(['edit','reconcile'].includes(action)||action==='security'&&node.error?.code==='SECURITY_LOCKED')continue;
      const label={fund:'补充账户资金',assess:'重新进行风险测评',security:'前往安全中心',refresh:'条件处理后继续',support:'查看业务协助'}[action];if(!label)continue;
      const button=el('button',label);button.onclick=()=>act(button,async(epoch)=>{
        if(action==='security')return openSecurity();
        if(action==='support')return openSupport();
        if(action==='fund'||action==='assess'){const category=action==='fund'?'funds':'wealth',item=await api('/api/businesses/'+category+'/'+(action==='fund'?'incoming':'assess'));if(epoch===sessionStore.epoch)await openBusinessAction({label:action==='fund'?'智能转账':'理财操作',id:category},item,{resumePlanId:action==='assess'?data.id:null});return;}
        const mark=ticket('plan-action'),result=await api('/api/plans/'+data.id+'/resume',{});if(epoch===sessionStore.epoch&&planStore.current?.id===data.id){renderPlan(result,true,mark);await overview();}
      });controls.append(button);
    }
    if (!doneStates.has(node.status) && !assisted.has(node.id)&&node.status!=='UNKNOWN'&&node.risk?.status!=='VERIFYING'&&data.status!=='CANCELLED') {const edit=el('button','调整安排');edit.onclick=()=>editNode(data,node);controls.append(edit);}
    if(controls.childNodes.length)card.append(controls);
    const detail=jsonDetails('查看办理凭证',{业务:operations[node.operation]||'银行业务',状态:nodeStatusLabel(node),
      ...(node.params.amount_fen?{金额:yuan(node.params.amount_fen)}:{}),
      ...(node.result?{凭证编号:node.result.receipt_id,办理时间:node.result.business_time,来源:'银行沙箱；仅供演示'}:{说明:'当前步骤尚未产生办理凭证'})});
    detail.dataset.key=node.id;if(openDetails.has(node.id))detail.open=true;card.append(detail);$('nodes').append(card);
  }
  const research=(data.request_goals||[]).find(g=>g.kind==='research'&&g.result?.id&&g.status!=='CANCELLED');
  if(research)renderResearch(research.result);
  else if(speak&&researchStore.current){if(researchStore.timer!==null)clearTimeout(researchStore.timer);researchStore.current=null;$('research-panel').hidden=true;}
  if(speak)focusNextStep(data);
}

const guidedSteps=new Set();
const returningAssessments=new Set();
function returnFromAssessment(data){
  if(!sessionStore.user||!data.nodes?.some(n=>n.operation==='wealth.assess')||!data.nodes.every(n=>n.status==='SUCCEEDED'))return false;
  const key='bank-prerequisite-'+sessionStore.user.id;let link;
  try{link=JSON.parse(sessionStorage.getItem(key)||'null');}catch{return false;}
  if(link?.fromPlanId!==data.id||!link.toPlanId||returningAssessments.has(data.id))return false;
  returningAssessments.add(data.id);const epoch=sessionStore.epoch,mark=ticket('plan-action');
  act(null,async()=>{try{const result=await api('/api/plans/'+encodeURIComponent(link.toPlanId)+'/resume',{});
    if(epoch!==sessionStore.epoch||!isCurrent(mark))return;
    sessionStorage.removeItem(key);renderPlan(result,true,mark);await overview();message('测评已更新，已返回原安排重新核对；已完成的转账不会重复办理。');
  }finally{returningAssessments.delete(data.id);}});
  return true;
}
function focusNextStep(data){
  const node=data.nodes.find(n=>n.risk?.permitted_next_actions?.includes('assess'))||data.nodes.find(n=>['WAIT_CONFIRM','WAIT_MFA'].includes(n.status));
  if(!node){if((data.request_goals||[]).some(g=>g.kind==='research'))$('research-panel').scrollIntoView({block:'start',behavior:'smooth'});return;}
  const key=[sessionStore.epoch,data.id,node.id,node.error?.code||node.status].join(':');
  if(guidedSteps.has(key))return;guidedSteps.add(key);
  const card=[...$('nodes').querySelectorAll('article[data-node-id]')].find(el=>el instanceof HTMLElement&&el.dataset.nodeId===node.id);
  if(card instanceof HTMLElement){card.scrollIntoView({block:'center',behavior:'smooth'});card.focus({preventScroll:true});}
  if(node.risk?.permitted_next_actions?.includes('assess')&&!$('detail-dialog').open){
    const epoch=sessionStore.epoch,version=data.version;
    act(null,async()=>{const item=await api('/api/businesses/wealth/assess');if(epoch===sessionStore.epoch&&planStore.current?.id===data.id&&planStore.current?.version===version&&!$('detail-dialog').open)await openBusinessAction({id:'wealth',label:'理财操作'},item,{resumePlanId:data.id});});
  }
}

export function toFen(value) {
  if(!/^\d+(\.\d{1,2})?$/.test(String(value)))throw new Error('金额需大于零，最多两位小数');
  const [whole,decimal='']=String(value).split('.');const result=BigInt(whole)*100n+BigInt(decimal.padEnd(2,'0'));
  if(result<=0n||result>100000000n)throw new Error('金额超出演示支持范围');return Number(result);
}

export function editNode(current,node) {
  const mark=beginIntent();
  const root=modal('调整 · '+(operations[node.operation]||node.operation));
  const stamp=formStore.modalVersion;
  root.append(el('p','修改后需要重新核对，旧授权将失效。生日日期变动会同步调整预算与采购日期。','muted'));
  const form=el('form'),fields=el('div',undefined,'form-grid'),inputs=[];
  const protectedFields=new Set(['quote','goal_id','hold_id','order_id','request_id','contract_version','subscription_id','assistance_case_id','assistance_version']);
  if(node.params.subscription_id)root.append(el('p','如需更换订阅，请重新选择业务并核对合同。','muted'));
  for(const [key,value] of Object.entries(node.params)){
    if(protectedFields.has(key))continue;
    const label=el('label',fieldNames[key]||key,'form-field');let input;
    const options=key==='blocked_categories'?[['','不限制'],['gifts','礼品'],['gaming','游戏'],['gifts,gaming','礼品与游戏']]:key==='product_id'?accountStore.facts.products.map(p=>[p.id,p.name]):key==='card_id'?accountStore.facts.cards.map(c=>[c.id,c.name]):key==='recipient_hint'?accountStore.facts.contacts.map(c=>[c.name,c.name]):key==='subscription_id'?[['S_VIDEO','视频会员'],['S_MUSIC','音乐会员']]:key==='scope'?[['both','代扣与商户合同'],['bank','银行代扣'],['merchant','商户合同']]:key==='kind'?[['virtual','虚拟卡'],['physical','实体卡']]:null;
    if(options){input=el('select');for(const [id,name] of options){const option=el('option',name);option.value=id;input.append(option);}input.value=Array.isArray(value)?value.join(','):value;}
    else{input=el('input');if(typeof value==='boolean'){input.type='checkbox';input.checked=value;}else{input.type=/date/.test(key)?'date':'text';input.value=key.endsWith('_fen')?(value/100).toFixed(2):Array.isArray(value)?value.join(','):String(value);}}
    if(key.endsWith('_fen')){input.inputMode='decimal';label.append(el('small','单位：元'));}
    if(key==='answers')label.append(el('small','模拟问卷，逗号分隔五个真实回答；不替你作答'));
    label.append(input);fields.append(label);inputs.push([key,input,value]);
  }
  const save=el('button','保存并重新核对','primary');save.type='submit';const actions=el('div',undefined,'modal-actions');actions.append(save);form.append(fields,actions);root.append(form);
  let scheduled,scheduledEnabled;
  if(node.operation==='funds.transfer'){
    const label=el('label','定时转账','form-field');scheduledEnabled=el('input');scheduledEnabled.type='checkbox';scheduledEnabled.checked=Boolean(node.scheduled_for);label.append(scheduledEnabled);
    scheduled=el('input');scheduled.type='datetime-local';scheduled.value=node.scheduled_for?.slice(0,16)||'';scheduled.disabled=!scheduledEnabled.checked;scheduledEnabled.onchange=()=>scheduled.disabled=!scheduledEnabled.checked;
    label.append(scheduled,el('small','使用演示业务日期与北京时间；取消勾选后重新确认即时转账'));fields.append(label);
  }
  form.onsubmit=e=>{e.preventDefault();act(save,async()=>{const params={...node.params};if(node.operation==='life.purchase')params.quote={};
    for(const [key,input,original] of inputs){params[key]=key.endsWith('_fen')?toFen(input.value):typeof original==='boolean'?input.checked:typeof original==='number'?Number(input.value):Array.isArray(original)?input.value.split(/[,，\s]+/).filter(Boolean).map(v=>key==='answers'?Number(v):v):input.value;}
    if(scheduledEnabled?.checked&&!scheduled.value)throw new Error('请填写预约时间');
    const result=await api('/api/plans/'+current.id+'/edit',{node_id:node.id,params,expected_version:current.version,...(scheduledEnabled?{scheduled_for:scheduledEnabled.checked?scheduled.value+':00+08:00':null}:{})});if(!isCurrent(mark))return;if(stamp===formStore.modalVersion)$('detail-dialog').close();renderPlan(result,false,mark);await overview();});};
}

export function renderFundingPreview(data){
  const root=$('funding-preview'),preview=data.funding_preview;if(!root||!preview)return;
  const monetary=new Set(['funds.reserve','funds.release','funds.transfer','aa.pay','wealth.purchase','wealth.redeem','life.purchase','life.cancel_order']);
  const hasMoney=data.nodes.some(n=>monetary.has(n.operation)),batch=data.confirmation_batch;
  if(!hasMoney&&!batch?.available){root.hidden=true;return;}
  root.hidden=false;root.replaceChildren();const done=data.nodes.every(n=>doneStates.has(n.status));
  root.append(el('h3',hasMoney?(done?'当前资金与办理凭证':data.nodes.some(n=>doneStates.has(n.status))?'剩余安排的资金影响':'整份安排的资金影响'):'核对普通操作'));
  if(hasMoney){
  const grid=el('div',undefined,'funding-grid');
  for(const [label,value] of [['需占用可用资金',preview.required_fen],['预计余额',preview.after.balance_fen],['仍预留',preview.after.reserved_fen],['预计可用',preview.after.available_fen]]){const item=el('div');item.append(el('small',label),el('strong',yuan(value)));grid.append(item);}root.append(grid);
  if(!preview.complete_projection)root.append(el('p','部分条件尚未满足，余额展示为已核验部分的估算。请先处理下方条件。','funding-note'));
  if(preview.shortfall_fen)root.append(el('p','当前资金缺口 '+yuan(preview.shortfall_fen)+'，可先补款或调整安排。','error'));
  const details=el('details');details.append(el('summary','预留、待到账与已知支出'));
  for(const h of preview.holds)details.append(el('p',h.purpose+' · '+yuan(h.amount_fen)));
  details.append(el('p','未来30天已知代扣 '+yuan(preview.known_future_debits_fen)+'（尚未扣款）'));
  if(preview.simulated_recurring_debits_fen)details.append(el('p','等待预约期间已模拟代扣 '+yuan(preview.simulated_recurring_debits_fen)+'，已计入预计余额。'));
  for(const warning of preview.event_warnings||[])details.append(el('p',warning,'error'));
  for(const incoming of preview.deferred_inflows)details.append(el('p','赎回预计 '+incoming.settles_on+' 到账 '+yuan(incoming.amount_fen)+'，当前不算可用资金。'));
  details.append(el('p',preview.note,'funding-note'));root.append(details);
  if(!done)renderPressure(root,preview);
  }
  if(batch?.available){const button=el('button','集中核对并确认 '+batch.items.length+' 项普通操作','primary batch-trigger');button.onclick=()=>openBatchConfirmation(data);root.append(button);}
}

export function openBatchConfirmation(data){
  const batch=data.confirmation_batch,root=modal('核对本次普通操作'),stamp=formStore.modalVersion;
  root.append(el('p','一次确认下列清单。申购、大额转账等敏感操作仍需独立验证。','muted'));
  for(const item of batch.items){const row=el('article',undefined,'batch-row');row.append(el('strong',operations[item.operation]||'银行业务'),nodeSummary({...item,params:{...item.params,...(item.display_amount_fen?{amount_fen:item.display_amount_fen}:{})}},data.nodes));root.append(row);}
  root.append(el('p','按整份安排估算：余额 '+yuan(data.funding_preview.after.balance_fen)+'，预留 '+yuan(data.funding_preview.after.reserved_fen)+'，可用 '+yuan(data.funding_preview.after.available_fen)+'。','funding-note'));
  const yes=el('button','确认所列 '+batch.items.length+' 项','primary');yes.onclick=()=>act(yes,async(epoch)=>{
    const mark=ticket('plan-action');const result=await api('/api/plans/'+data.id+'/approve-batch',{expected_version:data.version,manifest_fingerprint:batch.fingerprint});
    if(epoch!==sessionStore.epoch||planStore.current?.id!==data.id)return;
    if(stamp===formStore.modalVersion)$('detail-dialog').close();renderPlan(result,true,mark);await overview();
  });root.append(yes);
}
