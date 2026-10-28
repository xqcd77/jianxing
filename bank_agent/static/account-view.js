// @ts-check
import {$,el,yuan} from "./ui.js";
import {accountStore,sessionStore,ticket,isCurrent} from "./state.js";
import {api} from "./api.js";

export function moneyNode(id, fen) {
  const node = $(id), next = yuan(fen);
  if (node.textContent !== next) {
    node.textContent = next; node.classList.remove('money-changed');
    void node.offsetWidth; node.classList.add('money-changed');
  }
}

export function renderOverview(data) {
  moneyNode('balance',data.account.balance_fen); moneyNode('available',data.account.available_fen); moneyNode('reserved',data.account.reserved_fen);
  $('clock').textContent = data.business_now.slice(0,10);
  const holds = data.holds.filter(h => h.status === 'ACTIVE'); $('holds').replaceChildren();
  if (!holds.length) $('holds').textContent = '暂无预留安排';
  for (const hold of holds) {
    const row = el('div',undefined,'hold'); row.append(el('b',yuan(hold.remaining_fen)),el('span',hold.purpose)); $('holds').append(row);
  }
  $('notices').replaceChildren(); $('notice-count').textContent = String(data.notifications.length);
  if (!data.notifications.length) $('notices').textContent = '暂时没有新的提醒';
  for (const notice of data.notifications.slice(-8).reverse()) {
    const row = el('div',undefined,'notice'); row.append(el('b',notice.name || '人工待办'));
    row.append(el('span',notice.message || ('续费日期 ' + notice.due + ' · ' + yuan(notice.amount_fen)))); $('notices').append(row);
  }
}

export async function overview() {
  const mark=ticket('overview');
  const epoch = sessionStore.epoch, result = await api('/api/overview');
  if (epoch !== sessionStore.epoch||!isCurrent(mark)) return;
  accountStore.facts = result; renderOverview(result);
}
