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
      const page=await browser.newPage({viewport:{width,height:1100}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8103');
      assert((await page.title()).includes('简行'));
      await page.locator('#login button[type=submit]').click();
      await page.waitForFunction(()=>accountUser?.username==='demo'&&document.querySelectorAll('#scenarios .business-category').length===6);
      // Normal insufficient-balance attempts must not lock the user.
      for(let i=0;i<4;i++){
        await page.locator('#input').fill('给妈妈转100000元');await page.locator('#send').click();
        await page.waitForFunction(()=>plan?.status==='FAILED'&&!document.querySelector('#send').disabled);
      }
      assert.equal(await page.evaluate(async()=>(await api('/api/auth/me')).locked),false);
      await page.evaluate(async()=>{await api('/api/demo/clock',{days:1});await api('/api/demo/payroll',{event_id:'jianxing-ui-'+crypto.randomUUID(),amount_fen:600000});await overview();});
      const before=await page.evaluate(()=>facts.account.balance_fen);
      await page.evaluate(async()=>{const p=await api('/api/businesses/life/allocation/prepare',{params:{allocation_kind:'budget_transfer_investment',budget_fen:300000,purpose:'生活费',recipient_hint:'妈妈',transfer_fen:80000,product_id:'P_CASH',investment_fen:50000}});renderPlan(p,true);});
      assert.equal(await page.evaluate(()=>plan.funding_preview.required_fen),430000);
      assert((await page.locator('#funding-preview').innerText()).includes('4,300.00'));
      await page.screenshot({path:'.data/jianxing-preview-'+width+'.png',fullPage:true});
      await page.locator('.batch-trigger').click();await page.getByRole('button',{name:'确认所列 2 项',exact:true}).click();
      await page.locator('#plan-status').filter({hasText:'等待强验证'}).waitFor();
      const purchase=page.locator('#nodes .node').filter({hasText:'申购理财产品'});
      await purchase.getByRole('button',{name:'获取演示验证码',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#nodes input[autocomplete="one-time-code"]')?.value.length===6);
      await purchase.getByRole('button',{name:'验证并执行',exact:true}).click();
      await page.locator('#plan-status').filter({hasText:'全部完成'}).waitFor();
      assert.equal(await page.evaluate(async()=>(await api('/api/overview')).account.balance_fen),before-130000);
      // A delayed history response must not replace a newly opened security dialog.
      let release;const barrier=new Promise(resolve=>release=resolve);let captured=false;
      await page.route('**/api/plans',async route=>{if(!captured){captured=true;await barrier;}await route.continue();});
      await page.locator('#history').evaluate(button=>button.click());
      await page.waitForTimeout(150);await page.locator('#security').evaluate(button=>button.click());
      await page.locator('#detail-title').filter({hasText:'安全中心'}).waitFor();release();await page.waitForTimeout(500);
      assert((await page.locator('#detail-title').innerText()).includes('安全中心'));await page.locator('#detail-close').click();await page.unroute('**/api/plans');
      assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth));
      await page.screenshot({path:'.data/jianxing-completed-'+width+'.png',fullPage:true});
      assert.deepEqual(errors,[]);records.push({width,brand:true,normalFailureNoLock:true,wholePlanPreview:true,yellowBatch:true,redMfa:true,staleDialogProtected:true});
      await page.close();
    }
    fs.writeFileSync('.data/jianxing-ui-results.json',JSON.stringify(records,null,2));console.log(JSON.stringify(records));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
