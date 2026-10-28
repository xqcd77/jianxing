/* Run against an isolated demo instance; never the user's banking account. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  const checks=[],errors=[];
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1050},reducedMotion:'reduce'});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8140');
    await page.locator('#login button[type=submit]').click();
    await page.waitForFunction(()=>window.accountUser?.username==='demo'&&document.querySelector('#selected-model-name')?.textContent==='DeepSeek');
    await page.locator('#model-picker').click();
    assert.equal(await page.locator('.model-option').count(),6);
    assert(await page.locator('.model-option img').evaluateAll(nodes=>nodes.every(n=>n.complete&&n.naturalWidth>0)));
    await page.screenshot({path:'.data/workspace-model-picker-desktop.png'});
    await page.locator('.model-option').filter({hasText:'GLM'}).click();
    await page.locator('#provider-api-key').fill('synthetic-browser-glm-key');
    await page.locator('#provider-concrete-model').selectOption('glm-5.3');
    await page.getByRole('button',{name:'保存并使用',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#selected-model-name').textContent==='GLM');
    assert.equal(await page.evaluate(async()=>{const detail=await api('/api/conversations/'+window.conversation);return detail.model_id;}),'glm');
    checks.push('六个本地厂标、选择写入会话、菜单自动关闭');

    await page.locator('#conversation-history').click();
    await page.locator('.conversation-open').filter({hasText:'下一步先核对'}).first().click();
    await page.waitForFunction(()=>document.querySelectorAll('#messages .message.user').length===2);
    assert.equal(await page.locator('#selected-model-name').innerText(),'DeepSeek');
    await page.locator('#context-meter').click();
    await page.locator('#context-popover:popover-open').waitFor();
    assert.equal(await page.locator('.context-headline strong').innerText(),'12,480');
    assert((await page.locator('.context-footnote').innerText()).includes('提供商返回用量'));
    await page.screenshot({path:'.data/workspace-context-desktop.png'});
    await page.keyboard.press('Escape');
    checks.push('历史消息与原会话模型恢复、最近一次容量明细、Escape关闭');

    await page.locator('#new-conversation').click();
    await page.waitForFunction(()=>document.querySelectorAll('#messages .message.user').length===0);
    assert.equal(await page.locator('#context-meter-label').innerText(),'上下文');
    await page.locator('#input').fill('查询账户余额');await page.locator('#send').click();
    await page.waitForFunction(()=>document.querySelector('.activity-message')&&!document.querySelector('#send').disabled&&!document.querySelector('.activity-message.loading'));
    const saved=await page.evaluate(()=>window.conversation);
    await page.reload();
    await page.waitForFunction(()=>window.accountUser?.username==='demo'&&document.querySelectorAll('#messages .message.user').length===1);
    assert.equal(await page.evaluate(()=>window.conversation),saved);
    assert((await page.locator('#messages').innerText()).includes('查询账户余额'));
    checks.push('新会话隔离、真实规则请求、刷新后消息恢复');

    await page.locator('#conversation-history').click();
    await page.locator('#conversation-search').fill('查询账户余额');
    await page.waitForFunction(()=>document.querySelector('.conversation-row.selected'));
    const row=page.locator('.conversation-row.selected');
    await row.locator('summary').click();await row.getByRole('button',{name:'重命名',exact:true}).click();
    const title='日常收支 · '+String(Date.now()).slice(-6);
    await page.getByRole('textbox',{name:'会话名称',exact:true}).fill(title);
    await page.locator('#detail-dialog').getByRole('button',{name:'保存',exact:true}).click();
    await page.locator('#conversation-search').fill(title);
    await page.waitForFunction(title=>document.querySelector('.conversation-open b')?.textContent===title,title);
    await page.screenshot({path:'.data/workspace-history-desktop.png'});
    await page.locator('.conversation-row summary').click();await page.locator('.conversation-row').getByRole('button',{name:'归档',exact:true}).click();
    await page.locator('.conversation-empty').waitFor();
    await page.locator('#show-archived').click();await page.locator('.conversation-row').waitFor();
    await page.locator('.conversation-row summary').click();await page.locator('.conversation-row').getByRole('button',{name:'恢复会话',exact:true}).click();
    await page.locator('#show-archived').click();await page.locator('#conversation-search').fill('');
    await page.locator('.conversation-row').first().waitFor();
    checks.push('历史搜索、重命名、归档与恢复');
    await page.locator('#conversation-close').click();

    await page.setViewportSize({width:390,height:844});
    await page.locator('#model-picker').click();
    assert.equal(await page.locator('.model-option').count(),6);
    assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth));
    const box=await page.locator('#model-menu').boundingBox();assert(box&&box.x>=0&&box.x+box.width<=390&&box.y>=0);
    await page.screenshot({path:'.data/workspace-model-picker-mobile.png'});
    await page.keyboard.press('Escape');
    await page.getByRole('button',{name:'历史会话',exact:true}).click();
    const drawer=await page.locator('#conversation-drawer').boundingBox();assert(drawer&&drawer.x>=0&&drawer.x+drawer.width<=390);
    await page.screenshot({path:'.data/workspace-history-mobile.png'});
    checks.push('390px移动端无横向溢出、菜单与历史抽屉可用');
    assert.deepEqual(errors,[]);
    fs.writeFileSync('.data/workspace-controls-browser.json',JSON.stringify({passed:checks.length,checks,page_errors:errors,synthetic_demo:true},null,2));
    console.log(JSON.stringify({passed:checks.length,checks,page_errors:errors},null,2));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
