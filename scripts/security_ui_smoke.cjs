'use strict';
const {chromium}=require('playwright');
const path=require('node:path');
const assert=require('node:assert/strict');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BANK_BROWSER_PATH||path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  try{
    for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8103');
      await page.locator('#login .primary').click();
      await page.waitForFunction(()=>accountUser&&document.querySelectorAll('#scenarios .business-category').length===6);
      async function open(category,action){
        const tab=page.locator('#scenarios [data-category="'+category+'"]');
        if(await tab.getAttribute('aria-expanded')!=='true')await tab.click();
        await page.locator('#business-options [data-action="'+action+'"]').click();
        await page.waitForFunction(()=>document.querySelector('#detail-dialog').open&&document.querySelector('#detail-content [name]'));
      }
      async function submit(category,action){
        const ready=page.waitForResponse(r=>r.url().endsWith('/api/businesses/'+category+'/'+action+'/prepare')&&r.request().method()==='POST');
        await page.locator('#detail-content button[type="submit"]').click();
        const result=await ready;assert.equal(result.status(),200);
        await page.locator('#detail-dialog').waitFor({state:'hidden'});return result.json();
      }
      await open('cards','lost');
      await page.locator('#detail-content [name="card_id"]').selectOption('C_MAIN');
      const lockedPlan=await submit('cards','lost');assert.equal(lockedPlan.status,'WAIT_MFA');
      await page.waitForFunction(id=>plan?.id===id,lockedPlan.id);
      for(let i=0;i<3;i++){
        const node=page.locator('#nodes .node.needs-action').first();
        await node.getByRole('button',{name:'获取演示验证码',exact:true}).click();
        const input=node.getByRole('textbox',{name:'演示验证码',exact:true});
        await page.waitForFunction(()=>{const input=document.querySelector('#nodes .needs-action .verification-field input');return input?.dataset.challengeId&&/^\d{6}$/.test(input.value);});
        await input.fill(String((Number(await input.inputValue())+1)%1000000).padStart(6,'0'));
        const ready=page.waitForResponse(r=>r.url().endsWith('/approve')&&r.request().method()==='POST');
        await node.getByRole('button',{name:'验证并执行',exact:true}).click();
        assert.equal((await ready).status(),401);
      }
      await open('funds','incoming');
      await page.locator('#detail-content [name="amount_fen"]').fill('10000');
      const incoming=await submit('funds','incoming');assert.equal(incoming.status,'WAIT_DEMO_CONFIRM');
      const confirmation=page.getByRole('button',{name:'确认模拟转入',exact:true}).last();
      const blocked=page.waitForResponse(r=>r.url().includes('/incoming/')&&r.url().endsWith('/decision'));
      await confirmation.click();assert.equal((await blocked).status(),423);
      await page.getByRole('button',{name:'前往安全中心',exact:true}).click();
      const unlock=page.locator('[data-security-section="unlock"]');
      await unlock.getByRole('button',{name:'获取演示验证码',exact:true}).click();
      await page.waitForFunction(()=>{const input=document.querySelector('[data-security-section="unlock"] input');return input?.dataset.challengeId&&/^\d{6}$/.test(input.value);});
      assert.match(await unlock.getByRole('textbox',{name:'演示验证码',exact:true}).inputValue(),/^\d{6}$/);
      await page.screenshot({path:'.data/security-unlock-'+width+'.png',fullPage:true});
      const unlocked=page.waitForResponse(r=>r.url().endsWith('/api/security/unlock'));
      await unlock.getByRole('button',{name:'验证并解除安全锁定',exact:true}).click();assert.equal((await unlocked).status(),200);
      await page.waitForFunction(()=>document.querySelector('.security-status strong').textContent==='账户安全状态正常');
      assert.equal(await unlock.isVisible(),false);
      await page.locator('#detail-close').click();
      const before=await page.evaluate(()=>facts.account.balance_fen);
      await confirmation.click();await page.waitForFunction(old=>facts.account.balance_fen===old+1000000,before);
      await open('funds','transfer');
      await page.locator('#detail-content [name="recipient_hint"]').fill('妈妈');
      await page.locator('#detail-content [name="amount_fen"]').fill('2000');
      const transfer=await submit('funds','transfer');assert.equal(transfer.status,'WAIT_MFA');
      await page.waitForFunction(id=>plan?.id===id,transfer.id);
      const node=page.locator('#nodes .node.needs-action').first();
      await node.getByRole('button',{name:'获取演示验证码',exact:true}).click();
      await page.waitForFunction(()=>{const input=document.querySelector('#nodes .needs-action .verification-field input');return input?.dataset.challengeId&&/^\d{6}$/.test(input.value);});
      const done=page.waitForResponse(r=>r.url().endsWith('/approve')&&r.request().method()==='POST');
      await node.getByRole('button',{name:'验证并执行',exact:true}).click();
      const result=await done;assert.equal(result.status(),200);assert.equal((await result.json()).status,'COMPLETED');
      await page.waitForFunction(old=>facts.account.balance_fen===old+800000,before);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.deepEqual(errors,[]);await page.screenshot({path:'.data/security-transfer-'+width+'.png',fullPage:true});await page.close();
    }
    console.log('Demo verification passed on desktop and mobile: lock, blocked incoming, code delivery, unlock, 10000 yuan deposit, 2000 yuan transfer and receipts.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
