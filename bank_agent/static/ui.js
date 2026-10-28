// @ts-check
import {accountStore,formStore,sessionStore} from "./state.js";

let toastTimer;

/** @template {keyof import('./dom-contracts').Elements} K @overload @param {K} id @returns {import('./dom-contracts').Elements[K]} */
/** @overload @param {string} id @returns {HTMLElement} */
/** @param {string} id @returns {HTMLElement} */
export function $(id) {
  const node=document.getElementById(id);
  if(!node)throw new Error('界面缺少必要元素：'+id);
  return node;
}

/** @template {keyof HTMLElementTagNameMap} K @param {K} tag @param {string|number} [text] @param {string} [cls] @returns {HTMLElementTagNameMap[K]} */
export const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
};

export const yuan = fen => '¥ ' + (Number(fen || 0) / 100).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});

/** @type {Record<string,string>} */
export const roles = {funds:'资金',bills:'账单',wealth:'理财',cards:'卡片',subscriptions:'订阅',life:'生活安排'};

export function businessText(text) {
  let result=String(text);
  for(const product of accountStore.facts?.products||[])result=result.replaceAll(product.id,product.name);
  return result;
}


/** @type {Record<string,string>} */
export const states = {WAIT_CONFIRM:'等待确认',WAIT_MFA:'等待强验证',WAIT_TIME:'等待预约时间',SUCCEEDED:'已完成',COMPLETED:'全部完成',FAILED:'条件暂不满足',UNKNOWN:'结果核实中',WAIT_INPUT:'需要补充信息',WAIT_HUMAN:'等待人工',CANCELLED:'已取消',SKIPPED:'已跳过',READY:'等待前置步骤',PARTIAL:'部分完成'};

/** @type {Record<string,string>} */
export const operations = {
  'balance.query':'查看账户资金','funds.reserve':'为计划预留预算','funds.release':'释放预留资金','funds.transfer':'办理转账',
  'aa.create':'发起AA收款','aa.pay':'支付本人AA份额','aa.status':'查看AA收款进度',
  'bills.report':'分析消费账单','bills.anomalies':'检查疑似异常扣款','cashflow.project':'测算未来现金流','cashflow.income_plan':'登记预计收入',
  'wealth.compare':'比较可选理财产品','wealth.assess':'更新模拟风险测评','wealth.purchase':'申购理财产品','wealth.redeem':'赎回理财产品','wealth.holdings':'查看持仓与净值',
  'cards.query':'查看卡片状态','cards.apply':'申请新卡','cards.limit':'申请调整卡片额度','cards.restrict':'设置交易限制','cards.unlock':'解除交易限制','cards.lost':'办理卡片挂失','cards.restore':'办理卡片解挂',
  'subscriptions.detect':'识别周期扣费候选','subscriptions.query':'查看订阅合同','subscriptions.cancel':'处理订阅与代扣取消','subscriptions.remind':'设置续费提醒',
  'life.catalog':'查看模拟商品','life.goal':'确认生日安排','life.purchase':'采购生日鲜花与蛋糕','life.orders':'查看目标与订单','life.cancel_order':'取消订单并退款'
};

/** @type {Record<string,string>} */
export const categories = {housing:'居住',groceries:'日常采购',food:'餐饮',transport:'交通',utilities:'水电',subscription:'订阅',gifts:'礼品',refund:'退款'};

/** @type {Record<string,string>} */
export const fieldNames = {amount_fen:'金额',budget_fen:'预算金额',recipient_hint:'收款人',purpose:'预留用途',subject:'备注',product_id:'理财产品',birthday_date:'生日日期',address:'配送地址',card_id:'付款卡片',kind:'卡片类型',name:'名称',single_limit_fen:'单笔限额',daily_limit_fen:'日限额',blocked_categories:'限制类别',subscription_id:'订阅',scope:'取消范围',days_before:'提前提醒天数',answers:'五题测评答案（1—5分）',period:'报告期间',annual:'年度统计',days:'预测天数',income_delay_days:'收入推迟天数',expense_increase_pct:'支出增加百分比',date:'预计收入日期',label:'收入说明',participants:'参与者用户编号'};

fieldNames.name_hint='订阅名称';
export const doneStates = new Set(['SUCCEEDED','SKIPPED','CANCELLED','PARTIAL']);

export function toast(text) {
  $('toast').textContent = text; $('toast').style.display = 'block';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').style.display = 'none', 6000);
}

export async function act(button, fn) {
  const epoch = sessionStore.epoch; sessionStore.busy++;
  if (button) button.disabled = true;
  try { return await fn(epoch); } catch (error) { toast(error.message); }
  finally { sessionStore.busy--; if (button) button.disabled = false; }
}

export function modal(title) {
  formStore.modalVersion++;
  $('detail-dialog').removeAttribute('role');$('detail-dialog').removeAttribute('aria-describedby');
  $('detail-dialog').setAttribute('aria-labelledby','detail-title');
  $('detail-content').classList.remove('risk-dialog-content');delete $('detail-content').dataset.riskConfirmation;
  $('detail-title').textContent = title; $('detail-content').replaceChildren();
  delete $('detail-content').dataset.businessForm;
  delete $('detail-content').dataset.securityView;
  if (!$('detail-dialog').open) $('detail-dialog').showModal();
  return $('detail-content');
}

export function jsonDetails(title,data) {
  const node = el('details', undefined, 'node-details');
  node.append(el('summary',title),el('pre',JSON.stringify(data,null,2))); return node;
}
