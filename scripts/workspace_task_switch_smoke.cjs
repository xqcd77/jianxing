const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  const checks=[],errors=[],cancelCalls=[];
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1050},reducedMotion:'reduce'});
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',request=>{if(request.method()==='POST'&&/\/cancel$/.test(request.url()))cancelCalls.push(request.url());});
    await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8140');
    await page.locator('#login button[type=submit]').click();
    await page.waitForFunction(()=>window.accountUser?.username==='demo'&&document.querySelector('#selected-model-name')?.textContent==='DeepSeek');
    await page.locator('#new-conversation').click();await page.waitForFunction(()=>window.conversation);
    const accepted=page.waitForResponse(r=>r.url().endsWith('/api/chat/tasks')&&r.request().method()==='POST');
    await page.locator('#input').fill('查询账户余额');await page.locator('#send').click();
    const task=await(await accepted).json(),previous=task.conversation_id;
    await page.waitForFunction(()=>document.querySelector('.activity-message'));
    await page.locator('#model-picker').click();await page.locator('.model-option').filter({hasText:'Kimi'}).click();
    await page.locator('#provider-api-key').fill('synthetic-browser-kimi-key');
    await page.getByRole('button',{name:'保存并使用',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#selected-model-name').textContent==='Kimi');
    await page.locator('#new-conversation').click();
    await page.waitForFunction(previous=>window.conversation&&window.conversation!==previous,previous);
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);
    await page.waitForFunction(async id=>(await api('/api/chat/tasks/'+id)).status==='SUCCEEDED',task.id,{polling:400});
    const snapshot=await page.evaluate(id=>api('/api/chat/tasks/'+id),task.id);
    assert.equal(snapshot.cancel_requested,false);assert.deepEqual(cancelCalls,[]);
    assert.equal(await page.locator('#messages .message.user').count(),0);
    assert.equal(await page.locator('#selected-model-name').innerText(),'Kimi');
    checks.push('运行中切换模型与新会话：旧任务完成且未被取消，新界面不被旧结果覆盖');

    await page.locator('#conversation-history').click();
    await page.locator(`[data-conversation-id="${previous}"] .conversation-open`).click();
    await page.waitForFunction(()=>document.querySelectorAll('#messages .message.user').length===1);
    const history=await page.evaluate(id=>api('/api/conversations/'+id),previous);
    assert.equal(history.turns[0].model_id,'deepseek');assert.equal(history.model_id,'kimi');
    checks.push('原请求保留DeepSeek模型快照，会话后续选择Kimi单独保存');

    await page.locator('#context-meter').click();await page.locator('#context-memory').click();
    await page.getByRole('heading',{name:'任务与记忆',exact:true}).waitFor();
    assert((await page.locator('#detail-content').innerText()).includes('当前任务'));
    await page.locator('#detail-close').click();
    await page.locator('#logout').click();await page.locator('#login-dialog[open]').waitFor();
    await page.locator('#login-name').fill('wang');await page.locator('#login-password').fill('DemoBank!2026');
    await page.locator('#login button[type=submit]').click();
    await page.waitForFunction(()=>window.accountUser?.username==='wang'&&document.querySelector('#selected-model-name').textContent==='DeepSeek');
    await page.locator('#conversation-history').click();await page.locator('.conversation-empty').waitFor();
    assert.equal(await page.locator('.conversation-row').count(),0);
    assert.equal(await page.locator('#context-meter-label').innerText(),'上下文');
    assert.equal(await page.locator('#conversation-search').inputValue(),'');
    checks.push('任务与记忆面板可用；退出后历史、搜索、模型与容量按新账户隔离');
    assert.deepEqual(errors,[]);
    fs.writeFileSync('.data/workspace-task-switch-browser.json',JSON.stringify({passed:checks.length,checks,page_errors:errors,synthetic_demo:true},null,2));
    console.log(JSON.stringify({passed:checks.length,checks,page_errors:errors},null,2));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
