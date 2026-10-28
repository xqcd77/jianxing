'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BANK_BROWSER_PATH||path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  const records=[];
  try{
    for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:1050}}),errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8103');
      await page.locator('#login button[type=submit]').click();
      await page.waitForFunction(()=>accountUser?.username==='demo'&&document.querySelectorAll('#scenarios .business-category').length===6);
      await page.evaluate(()=>api('/api/demo/payroll',{event_id:'research-ui-'+crypto.randomUUID(),amount_fen:2000000}));
      await page.evaluate(()=>overview());
      const before=await page.evaluate(()=>facts.account.balance_fen),plansBefore=await page.evaluate(async()=>(await api('/api/plans')).length);
      await page.locator('[data-category="wealth"]').click();await page.locator('.research-entry').click();
      for(const [name,value] of [['amount_fen','5000'],['reserve_fen','6000'],['emergency_fen','3000'],['needed_on','2026-11-20']])await page.locator('input[name="'+name+'"]').fill(value);
      await page.locator('.research-form button[type=submit]').click();
      await page.locator('#research-status').filter({hasText:'分析完成'}).waitFor({timeout:30000});
      assert.equal(await page.locator('.research-product').count(),3);
      assert.equal(await page.evaluate(async()=>(await api('/api/overview')).account.balance_fen),before);
      assert.equal(await page.evaluate(async()=>(await api('/api/plans')).length),plansBefore);
      assert(await page.locator('.research-product.excluded').innerText().then(t=>t.includes('测评')));
      await page.locator('.research-product').first().locator('.research-evidence-links button').first().click();
      await page.locator('#detail-dialog[open]').waitFor();assert((await page.locator('#detail-content').innerText()).includes('不承诺本金和收益'));await page.locator('#detail-close').click();
      await page.screenshot({path:'.data/research-report-'+width+'.png',fullPage:true});
      await page.locator('button[data-option="buy_P_CASH"]').click();await page.locator('#plan-status').filter({hasText:'等待确认'}).waitFor();
      await page.locator('#nodes .node').filter({hasText:'为计划预留预算'}).locator('.primary').click();
      await page.locator('#plan-status').filter({hasText:'等待强验证'}).waitFor();
      const purchase=page.locator('#nodes .node').filter({hasText:'申购理财产品'});
      await purchase.getByRole('button',{name:'获取演示验证码',exact:true}).click();
      await purchase.locator('input[autocomplete="one-time-code"]').waitFor();await page.waitForFunction(()=>document.querySelector('#nodes input[autocomplete="one-time-code"]')?.value.length===6);
      await purchase.getByRole('button',{name:'验证并执行',exact:true}).click();
      await page.locator('#plan-status').filter({hasText:'全部完成'}).waitFor();
      assert.equal(await page.evaluate(async()=>(await api('/api/overview')).account.balance_fen),before-500000);
      assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth));
      const id=await page.evaluate(()=>activeResearch.id);
      await page.reload();await page.waitForFunction(id=>activeResearch?.id===id,id);
      assert.equal(await page.locator('.research-product').count(),3);
      assert(await page.locator('button[data-option="buy_P_CASH"]').isDisabled());
      assert.deepEqual(errors,[]);records.push({width,report:true,noAnalysisWrites:true,terms:true,bankConfirmation:true,mfa:true,receipt:true,reload:true});
      await page.screenshot({path:'.data/research-completed-'+width+'.png',fullPage:true});await page.close();
    }
    fs.writeFileSync('.data/research-ui-results.json',JSON.stringify(records,null,2));console.log(JSON.stringify(records));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
