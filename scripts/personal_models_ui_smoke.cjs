/* Isolated model-configuration fixture only. Never use real credentials here. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const KEY='synthetic-personal-mimo-browser-key';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
 const errors=[],checks=[],responses=[];
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1050},reducedMotion:'reduce'});
  page.on('pageerror',error=>errors.push(error.message));
  page.on('response',response=>{if(response.url().includes('/api/models'))responses.push(response.text());});
  await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8140');await page.locator('#login button[type=submit]').click();
  await page.waitForFunction(()=>window.accountUser?.username==='demo'&&document.querySelector('#selected-model-name').textContent==='DeepSeek');
  await page.locator('#model-picker').click();assert.equal(await page.locator('.model-option:disabled').count(),0);
  assert(!(await page.locator('#model-menu').innerText()).includes('未接入'));
  for(const provider of ['DeepSeek','GLM','MiMo','Kimi','Qwen','MiniMax']){
   await page.locator('.model-option').filter({hasText:provider}).click();await page.locator('#provider-api-key').waitFor();
   assert.equal(await page.locator('#provider-api-key').getAttribute('type'),'password');
   assert.equal(await page.locator('#provider-api-key').inputValue(),'');
   assert((await page.locator('#provider-concrete-model option').count())>=2);
   await page.getByRole('button',{name:'返回厂商列表',exact:true}).click();
  }
  checks.push('六家均可点开，包含隐藏Key输入与具体模型选择，不显示未接入');
  await page.locator('.model-option').filter({hasText:'MiMo'}).click();await page.locator('#provider-api-key').fill(KEY);
  await page.getByRole('button',{name:'显示或隐藏API Key',exact:true}).click();assert.equal(await page.locator('#provider-api-key').getAttribute('type'),'text');
  await page.getByRole('button',{name:'显示或隐藏API Key',exact:true}).click();
  await page.getByRole('button',{name:'获取可用模型',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.provider-config-note')?.textContent.includes('已读取'));
  await page.locator('#provider-concrete-model').selectOption('mimo-v2.6-flash');
  await page.screenshot({path:'.data/personal-model-config-desktop.png'});
  await page.getByRole('button',{name:'保存并使用',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#selected-model-name').textContent==='MiMo');
  const saved=await page.evaluate(()=>api('/api/models/mimo/config'));
  assert(saved.has_personal_key&&saved.model==='mimo-v2.6-flash');
  assert(!await page.evaluate(key=>JSON.stringify({...localStorage,...sessionStorage}).includes(key),KEY));
  await page.reload();await page.waitForFunction(()=>window.accountUser?.username==='demo'&&document.querySelector('#selected-model-name').textContent==='MiMo');
  await page.locator('#model-picker').click();await page.locator('.model-option').filter({hasText:'MiMo'}).click();
  assert.equal(await page.locator('#provider-api-key').inputValue(),'');assert((await page.locator('#provider-api-key').getAttribute('placeholder')).includes('已保存'));
  assert.equal(await page.locator('#provider-concrete-model').inputValue(),'mimo-v2.6-flash');
  checks.push('可获取模型列表、保存并使用，刷新恢复型号且Key不回填或进入浏览器存储');
  await page.locator('#provider-concrete-model').selectOption('__custom__');await page.locator('#provider-custom-model').fill('mimo-v2.6-pro');
  await page.getByRole('button',{name:'保存并使用',exact:true}).click();
  await page.waitForFunction(async()=> (await api('/api/models/mimo/config')).model==='mimo-v2.6-pro');
  checks.push('可手动填写模型ID并沿用已保存的Key');
  await page.setViewportSize({width:390,height:844});await page.locator('#model-picker').click();await page.locator('.model-option').filter({hasText:'MiMo'}).click();
  const box=await page.locator('#model-menu').boundingBox();assert(box&&box.x>=0&&box.x+box.width<=390&&box.y>=0);
  assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth));
  await page.screenshot({path:'.data/personal-model-config-mobile.png'});await page.keyboard.press('Escape');
  await page.setViewportSize({width:1440,height:1050});await page.locator('#logout').click();
  await page.locator('#login-name').fill('wang');await page.locator('#login-password').fill('DemoBank!2026');await page.locator('#login button[type=submit]').click();
  await page.waitForFunction(()=>window.accountUser?.username==='wang'&&document.querySelector('#selected-model-name').textContent==='DeepSeek');
  await page.locator('#model-picker').click();await page.locator('.model-option').filter({hasText:'MiMo'}).click();
  assert.equal(await page.locator('#provider-api-key').inputValue(),'');assert(await page.locator('#provider-api-key').getAttribute('required')!==null);
  assert.equal(await page.locator('#provider-concrete-model').inputValue(),'mimo-v2.6-pro');
  checks.push('390px无溢出；另一个账户无法使用或看到前一账户的Key');
  for(const response of responses){const text=await response;assert(!text.includes(KEY));}
  assert.deepEqual(errors,[]);
  fs.writeFileSync('.data/personal-models-browser.json',JSON.stringify({passed:checks.length,checks,page_errors:errors,synthetic_demo:true},null,2));
  console.log(JSON.stringify({passed:checks.length,checks,page_errors:errors},null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
