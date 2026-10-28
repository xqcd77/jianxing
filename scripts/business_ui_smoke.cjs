'use strict';
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BANK_BROWSER_PATH||path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  try {
    for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:1100}}),errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8103');
      await page.locator('#login .primary').click();
      await page.waitForFunction(()=>document.querySelectorAll('#scenarios .business-category').length===6&&document.querySelector('#clock').textContent==='2026-10-13');
      assert.deepEqual(await page.locator('#scenarios b').allTextContents(),['智能转账','账单分析','理财操作','卡片管理','订阅代扣','跨场景联动']);
      async function open(category,action){
        const tab=page.locator('#scenarios [data-category="'+category+'"]');
        if(await tab.getAttribute('aria-expanded')!=='true')await tab.click();
        const details=page.waitForResponse(r=>r.url().endsWith('/api/businesses/'+category+'/'+action)&&r.request().method()==='GET');
        await page.locator('#business-options [data-action="'+action+'"]').click();
        const item=await (await details).json();
        if(item.fields.length)await page.waitForFunction(title=>document.querySelector('#detail-dialog').open&&document.querySelector('#detail-title').textContent.endsWith(title),item.label);
      }
      function response(category,action){return page.waitForResponse(r=>r.url().endsWith('/api/businesses/'+category+'/'+action+'/prepare')&&r.request().method()==='POST');}
      async function result(promise){const r=await promise;assert.equal(r.status(),200);const data=await r.json();if(data.id)await page.waitForFunction(id=>plan?.id===id,data.id);return data;}
      async function query(category,action){const ready=response(category,action);await open(category,action);return result(ready);}
      async function send(category,action){const ready=response(category,action);await page.locator('#detail-content button[type="submit"]').click();const data=await result(ready);await page.locator('#detail-dialog').waitFor({state:'hidden'});return data;}
      assert.equal((await query('funds','balance')).status,'COMPLETED');
      const before=await page.evaluate(()=>facts.account.balance_fen);
      await open('funds','incoming');
      await page.locator('#detail-content [name="amount_fen"]').fill('500');
      const funding=await send('funds','incoming');assert.equal(funding.status,'WAIT_DEMO_CONFIRM');
      await page.getByRole('button',{name:'确认模拟转入',exact:true}).last().click();
      await page.waitForFunction(old=>facts.account.balance_fen===old+50000,before);
      await open('funds','transfer');
      await page.locator('#detail-content [name="recipient_hint"]').fill('妈妈');
      await page.locator('#detail-content [name="amount_fen"]').fill('500');
      const transfer=await send('funds','transfer');assert.equal(transfer.status,'WAIT_CONFIRM');assert.equal(transfer.nodes[0].params.amount_fen,50000);
      await open('funds','scheduled_transfer');
      await page.locator('#detail-content [name="recipient_hint"]').fill('妈妈');
      await page.locator('#detail-content [name="amount_fen"]').fill('100');
      await page.locator('#detail-content [name="scheduled_for"]').fill('2026-10-15T09:00');
      assert.equal((await send('funds','scheduled_transfer')).status,'WAIT_TIME');
      await open('bills','monthly');await page.locator('#detail-content [name="period"]').fill('2026-09');
      const month=await send('bills','monthly');assert.equal(month.status,'COMPLETED');assert.equal(month.nodes[0].params.annual,false);
      await open('bills','annual');await page.locator('#detail-content [name="period"]').fill('2026');
      assert.equal((await send('bills','annual')).nodes[0].params.annual,true);
      await query('wealth','compare');
      await open('wealth','assess');
      assert.deepEqual(await page.locator('#detail-content .risk-question select').evaluateAll(nodes=>nodes.map(n=>n.value)),['','','','','']);
      for(const input of await page.locator('#detail-content .risk-question select').all())await input.selectOption('2');
      assert.equal((await send('wealth','assess')).status,'WAIT_CONFIRM');
      await open('wealth','purchase');await page.locator('#detail-content [name="product_id"]').selectOption('P_CASH');await page.locator('#detail-content [name="amount_fen"]').fill('100');
      assert.equal((await send('wealth','purchase')).status,'WAIT_MFA');
      await query('cards','query');await open('cards','restore');
      assert.equal(await page.locator('#detail-content button[type="submit"]').isDisabled(),true);await page.locator('#detail-close').click();
      await open('cards','restrict');await page.locator('#detail-content [name="card_id"]').selectOption('C_MAIN');
      assert.equal((await send('cards','restrict')).status,'WAIT_CONFIRM');
      await query('subscriptions','detect');const contracts=await query('subscriptions','query');assert.equal(contracts.status,'WAIT_CONFIRM');
      await open('subscriptions','cancel');await page.locator('#detail-content [name="subscription_id"]').selectOption('S_VIDEO');
      assert.equal(await page.locator('#detail-content [name="scope"]').inputValue(),'');await page.locator('#detail-content [name="scope"]').selectOption('bank');
      assert.equal((await send('subscriptions','cancel')).nodes[0].params.scope,'bank');
      await open('life','budget_cashflow');await page.locator('#detail-content [name="amount_fen"]').fill('100');await page.locator('#detail-content [name="purpose"]').fill('备用金');
      const budget=await send('life','budget_cashflow');assert.equal(budget.nodes.length,2);assert.equal(budget.status,'WAIT_CONFIRM');
      const all=await query('life','overview');assert.equal(all.status,'COMPLETED');assert.equal(new Set(all.nodes.map(n=>n.agent)).size,6);
      assert.equal(await page.evaluate(old=>facts.account.balance_fen===old+50000,before),true);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
      await page.screenshot({path:'.data/business-ui-'+width+'.png',fullPage:true});
      await open('life','allocation');
      assert.equal(await page.evaluate(()=>document.querySelector('#detail-dialog').scrollWidth<=document.querySelector('#detail-dialog').clientWidth),true);
      await page.screenshot({path:'.data/business-form-'+width+'.png',fullPage:true});
      await page.close();
    }
    console.log('Six-category UI passed on desktop and mobile: real forms, query/confirmation/MFA, scheduling, account scopes and no overflow.');
  }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
