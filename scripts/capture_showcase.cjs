// Capture actual application states in an explicitly selected local demo sandbox.
// Never use an existing personal banking database for this script.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const url=process.env.BANK_UI_TEST_URL;
if(!url||!['localhost','127.0.0.1'].includes(new URL(url).hostname))throw new Error('Set BANK_UI_TEST_URL to an isolated localhost demo server.');
const output=path.resolve(process.env.BANK_SCREENSHOT_DIR||'docs/assets/screenshots');
fs.mkdirSync(output,{recursive:true});
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BANK_BROWSER_PATH||path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe')});
  const page=await browser.newPage({viewport:{width:1500,height:1120},deviceScaleFactor:1.5,reducedMotion:'reduce'});
  const errors=[],records=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(url);
    const health=await page.evaluate(()=>fetch('/health').then(r=>r.json()));
    assert.equal(health.execution_mode,'sandbox');assert.equal(health.model_mode,'demo');
    await page.locator('#login button[type=submit]').click();
    await page.waitForFunction(()=>accountUser?.username==='demo'&&document.querySelectorAll('.business-category').length===6);
    await page.evaluate(async()=>{await api('/api/demo/payroll',{event_id:'showcase-initial-salary',amount_fen:2000000});await overview();});
    async function shot(name,selector){
      if(selector)await page.locator(selector).evaluate(n=>window.scrollTo(0,Math.max(0,n.getBoundingClientRect().top+scrollY-35)));else await page.evaluate(()=>window.scrollTo(0,0));
      await page.evaluate(()=>document.fonts.ready);await page.waitForTimeout(400);
      assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2));
      await page.screenshot({path:path.join(output,name+'.png'),animations:'disabled'});
      records.push({file:name+'.png',status:await page.evaluate(()=>plan?.status||null)});
    }
    async function prepare(category,action,params){
      await page.evaluate(async({category,action,params})=>{renderPlan(await api('/api/businesses/'+category+'/'+action+'/prepare',{params}),true);await overview();},{category,action,params});
    }
    async function finish(){
      for(let i=0;i<12;i++){
        if(await page.evaluate(()=>plan?.status==='COMPLETED'))return;
        const node=page.locator('#nodes .node.needs-action').first();assert(await node.count(),'Expected a confirmable node');
        if(await node.locator('.demo-code-button').count()){
          await node.getByRole('button',{name:'获取演示验证码',exact:true}).click();
          await page.waitForFunction(()=>document.querySelector('#nodes .needs-action input[autocomplete="one-time-code"]')?.value.length===6);
        }
        const response=page.waitForResponse(r=>r.url().endsWith('/approve')&&r.request().method()==='POST');
        await node.locator('button.primary').click();assert.equal((await response).status(),200);
        await page.waitForTimeout(250);
      }throw new Error('Demo plan did not complete');
    }
    await shot('01-workspace');
    await prepare('life','allocation',{allocation_kind:'budget_transfer_investment',budget_fen:600000,purpose:'生活备用金',recipient_hint:'妈妈',transfer_fen:80000,product_id:'P_CASH',investment_fen:50000});
    await page.locator('#dag-view').evaluate(n=>{n.open=true;});
    await shot('02-plan-and-funding','.plan-panel');
    await page.locator('.batch-trigger').click();await page.getByRole('button',{name:'确认所列 2 项',exact:true}).click();
    await page.locator('#plan-status').filter({hasText:'等待强验证'}).waitFor();
    await shot('03-verification','#nodes .node.needs-action');
    await finish();assert.equal(await page.evaluate(()=>plan.status),'COMPLETED');
    await prepare('bills','monthly',{period:'2026-09'});
    await page.locator('#dag-view').evaluate(n=>{n.open=false;});
    await shot('04-bill-analysis','.plan-panel');
    await page.evaluate(()=>document.querySelector('#model').click());
    for(const [name,value] of [['amount_fen','3000'],['reserve_fen','1000'],['emergency_fen','1000'],['needed_on','2026-11-20']])await page.locator('.research-form input[name="'+name+'"]').fill(value);
    await page.locator('.research-form button[type=submit]').click();
    await page.locator('#research-status').filter({hasText:'分析完成'}).waitFor({timeout:45000});
    // Collapse the bill detail using the actual disclosure control to show product comparison.
    const bills=page.locator('.research-role').first();if(await bills.getAttribute('open')!==null)await bills.locator('summary').click();
    await shot('05-deep-research','#research-panel');
    // Start a fresh UI session before unrelated examples; do not alter or hide report content.
    await page.locator('#logout').click();
    await page.locator('#login-name').fill('demo');await page.locator('#login-password').fill('DemoBank!2026');
    await page.locator('#login button[type=submit]').click();
    await page.waitForFunction(()=>accountUser?.username==='demo');
    await prepare('subscriptions','cancel',{subscription_id:'S_VIDEO',scope:'bank'});
    await finish();
    await page.locator('#dag-view').evaluate(n=>{n.open=false;});
    await shot('06-subscription-management','.plan-panel');
    await prepare('cards','restrict',{card_id:'C_MAIN',single_limit_fen:50000,daily_limit_fen:100000,blocked_categories:['gaming']});
    await finish();
    await page.locator('#dag-view').evaluate(n=>{n.open=false;});
    await shot('07-card-management','.plan-panel');
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'capture-manifest.json'),JSON.stringify({kind:'live-local-demo-screenshots',model_mode:'demo',banking_api_mocked:false,financial_data:'synthetic',viewport:{width:1500,height:1120},device_scale_factor:1.5,screenshots:records,page_errors:errors},null,2)+'\n');
    console.log(JSON.stringify({captured:records.length,page_errors:errors,output}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
