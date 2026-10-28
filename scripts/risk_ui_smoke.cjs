'use strict';
// Use an isolated local sandbox: this acceptance run changes synthetic balances.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const base=process.env.BANK_UI_TEST_URL;
if(!base||!['127.0.0.1','localhost'].includes(new URL(base).hostname))throw new Error('Set BANK_UI_TEST_URL to an isolated local sandbox.');
const output='.data/risk-ui';fs.mkdirSync(output,{recursive:true});

(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.BANK_BROWSER_PATH||path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
 const records=[];
 async function login(page,url){await page.goto(url);await page.locator('#login button[type=submit]').click();await page.waitForFunction(()=>window.accountUser?.username==='demo');assert.equal((await page.evaluate(()=>fetch('/health').then(r=>r.json()))).model_mode,'demo');}
 async function prepare(page,category,action,params){return page.evaluate(async({category,action,params})=>{const result=await window.api('/api/businesses/'+category+'/'+action+'/prepare',{params});window.renderPlan(result,true);return result;},{category,action,params});}
 async function open(page){await page.locator('#nodes .node.needs-action').getByRole('button',{name:/核对并验证|核对并确认/}).first().click();await page.locator('[data-risk-confirmation]').waitFor();}
 async function approve(page){const box=page.locator('[data-risk-confirmation]');if(await box.locator('.demo-code-button').count()){await box.getByRole('button',{name:'获取演示验证码',exact:true}).click();await page.waitForFunction(()=>/^\d{6}$/.test(document.querySelector('[data-risk-confirmation] input')?.value||''));}
  const response=page.waitForResponse(r=>r.url().endsWith('/approve')&&r.request().method()==='POST');await box.getByRole('button',{name:/^(验证|确认)并?办理本步骤$|^验证并办理本步骤$|^确认办理本步骤$/}).click();assert.equal((await response).status(),200);await page.locator('[data-risk-confirmation]').waitFor({state:'hidden'});}
 try{
  for(const width of [1440,390]){
   const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));await login(page,base);
   await page.evaluate(()=>window.api('/api/demo/payroll',{event_id:'risk-ui-'+crypto.randomUUID(),amount_fen:2000000}));
   await prepare(page,'funds','reserve',{amount_fen:10000,purpose:'风险体验生活预留'});
   await open(page);assert.equal(await page.locator('#detail-dialog').getAttribute('role'),'alertdialog');assert.equal(await page.locator('[data-risk-confirmation] .demo-code-button').count(),0);
   assert.equal(await page.evaluate(()=>document.activeElement.textContent),'返回修改');await page.keyboard.press('Escape');assert.equal(await page.locator('#detail-dialog').isVisible(),false);
   await open(page);await approve(page);assert.equal(await page.evaluate(()=>window.plan.status),'COMPLETED');
   const target=await prepare(page,'funds','transfer',{recipient_hint:'妈妈',amount_fen:1000000});assert.equal(target.status,'WAIT_MFA');await open(page);
   assert.equal(await page.locator('.risk-seal.sensitive').count(),1);assert.match(await page.locator('[data-risk-confirmation]').innerText(),/不会撤回已完成的转账/);
   await page.screenshot({path:output+'/sensitive-'+width+'.png',animations:'disabled'});
   // A concurrent balance change must invalidate this presentation, not submit.
   let submits=0;const listener=r=>{if(r.url().endsWith('/approve')&&r.method()==='POST')submits++;};page.on('request',listener);
   await page.evaluate(()=>window.api('/api/demo/payroll',{event_id:'risk-concurrent-'+crypto.randomUUID(),amount_fen:100}));
   await page.locator('[data-risk-confirmation]').getByRole('button',{name:'验证并办理本步骤',exact:true}).click();
   await page.locator('[data-risk-confirmation]').waitFor({state:'hidden'});assert.equal(submits,0);page.off('request',listener);
   await open(page);await approve(page);assert.equal(await page.evaluate(()=>window.plan.status),'COMPLETED');
   await prepare(page,'funds','transfer',{recipient_hint:'妈妈',amount_fen:90000000});
   assert.equal(await page.locator('[data-risk-status="BLOCKED"]').count(),1);assert.equal(await page.locator('#nodes').getByRole('button',{name:'补充账户资金',exact:true}).count(),1);
   await page.locator('#nodes').getByRole('button',{name:'补充账户资金',exact:true}).click();await page.locator('#detail-content input[name="amount_fen"]').waitFor();await page.locator('#detail-close').click();
   await page.locator('#nodes').getByRole('button',{name:'调整安排',exact:true}).click();await page.locator('#detail-content input').first().waitFor();
   // Native edit fields are labelled by business names, not raw parameter names.
   await page.locator('#detail-content label').filter({hasText:'金额'}).locator('input').fill('500');
   await page.locator('#detail-content button[type=submit]').click();await page.locator('.revision-card').waitFor();assert.match(await page.locator('.revision-card').innerText(),/900,000.00/);
   await page.locator('.revision-card').scrollIntoViewIfNeeded();await page.screenshot({path:output+'/revision-'+width+'.png',animations:'disabled'});
   await prepare(page,'subscriptions','cancel',{subscription_id:'S_VIDEO',scope:'bank'});
   for(let i=0;i<4&&await page.evaluate(()=>window.plan.status!=='COMPLETED');i++){await open(page);await approve(page);}
   assert.match(await page.locator('#nodes').innerText(),/商户合同/);
   await page.locator(width<700?'[data-nav="security"]':'#security').click();await page.locator('.security-risk-history').waitFor();assert.match(await page.locator('.security-risk-history').innerText(),/身份验证通过/);await page.locator('#detail-close').click();
   await prepare(page,'wealth','purchase',{product_id:'P_CASH',amount_fen:50000});assert.match(await page.locator('.product-risk-card').innerText(),/产品风险 R1/);
   await open(page);await page.screenshot({path:output+'/product-'+width+'.png',animations:'disabled'});await page.keyboard.press('Escape');
   await page.emulateMedia({reducedMotion:'reduce'});await open(page);
   assert.equal(await page.locator('.risk-seal circle').evaluate(e=>getComputedStyle(e).animationName),'none');await page.keyboard.press('Escape');
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);records.push({width,passed:true,checks:['explicit confirmation','MFA','concurrent balance invalidation','recovery entry','change diff','subscription scope','owned event history','product risk','reduced motion','responsive layout']});await page.close();
  }
  if(process.env.BANK_UNKNOWN_TEST_URL){
   const page=await browser.newPage({viewport:{width:1440,height:1000}});await login(page,process.env.BANK_UNKNOWN_TEST_URL);
   await page.evaluate(()=>window.api('/api/demo/payroll',{event_id:'risk-unknown-'+crypto.randomUUID(),amount_fen:600000}));
   await prepare(page,'funds','transfer',{recipient_hint:'妈妈',amount_fen:50000});await open(page);await approve(page);
   assert.equal(await page.evaluate(()=>window.plan.status),'UNKNOWN');assert.equal(await page.locator('[data-risk-status="VERIFYING"]').count(),1);
   assert.equal(await page.locator('#nodes').getByRole('button',{name:/核对并验证|核对并确认/}).count(),0);
   await page.locator('[data-risk-status="VERIFYING"]').scrollIntoViewIfNeeded();await page.screenshot({path:output+'/unknown-result.png',animations:'disabled'});
   await page.getByRole('button',{name:'核实银行结果',exact:true}).click();await page.waitForFunction(()=>window.plan.status==='COMPLETED');assert.equal(await page.evaluate(()=>window.plan.available_fen),550000);await page.close();records.push({unknown_result_reconciliation:true,no_duplicate_payment:true});
  }
  fs.writeFileSync(output+'/verification.json',JSON.stringify(records,null,2));console.log(JSON.stringify(records));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
