const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  const records=[];
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1050},reducedMotion:'no-preference'}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8132');
    await page.locator('#login button[type=submit]').click();
    await page.waitForFunction(()=>window.accountUser?.username==='demo');
    await page.evaluate(async()=>{await api('/api/demo/payroll',{event_id:'conversation-'+crypto.randomUUID(),amount_fen:600000});await overview();});
    const send=async text=>{await page.locator('#input').fill(text);await page.locator('#send').click();};
    const latest=()=>page.locator('.activity-message').last();
    await send('给妈妈转400元，1000元看看ETF');
    await page.waitForFunction(()=>document.querySelectorAll('.activity-message:last-child .activity-step').length>=2);
    const running=latest();
    assert(await running.locator('.activity-trace').evaluate(node=>node.open));
    assert(await running.locator('.activity-answer').isHidden());
    assert.notEqual(await running.locator('.is-active .step-wave>span').first().evaluate(node=>getComputedStyle(node).animationName),'none');
    await running.scrollIntoViewIfNeeded();
    await page.locator('.chatbox').screenshot({path:'.data/conversation-running-1440.png'});
    await page.waitForFunction(()=>document.querySelector('.activity-message:last-child')?.classList.contains('typing'));
    assert.equal(await latest().locator('.activity-trace').evaluate(node=>node.open),false);
    assert((await latest().locator('.activity-answer').innerText()).length>0);
    await latest().getByRole('button',{name:'显示全文',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);
    assert(await latest().locator('.activity-answer').innerText());
    assert.equal(await page.locator('.activity-message').count(),1);
    assert.equal(await latest().locator('.is-active').count(),0);
    await latest().locator('summary').first().click();
    assert(await latest().locator('.activity-steps').isVisible());
    await latest().locator('summary').first().click();
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1050});
      assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth));
      await page.locator('.chatbox').screenshot({path:'.data/conversation-complete-'+width+'.png'});
    }
    records.push({scenario:'实际阶段、逐字显示、自动收起、展开记录与跳过动画',passed:true,widths:[1440,390]});

    await send('给妈妈转300元，800元看看ETF');
    await latest().getByRole('button',{name:'停止本次任务',exact:true}).waitFor();
    await latest().getByRole('button',{name:'停止本次任务',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);
    assert((await latest().locator('.activity-trace>summary').innerText()).includes('已停止'));
    assert.equal(await latest().locator('.activity-trace').evaluate(node=>node.open),false);
    records.push({scenario:'停止任务后收起记录并展示实际状态',passed:true});

    let disconnected=false;
    await page.route('**/api/chat/tasks/*/events',route=>{if(!disconnected){disconnected=true;return route.abort('failed');}return route.continue();});
    await send('给妈妈转200元，700元看看ETF');
    await latest().getByRole('button',{name:'恢复查看进度',exact:true}).waitFor();
    await latest().getByRole('button',{name:'恢复查看进度',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#send').disabled&&!document.querySelector('.activity-message:last-child')?.classList.contains('loading'));
    assert.equal(await latest().locator('.activity-trace').evaluate(node=>node.open),false);
    await page.unroute('**/api/chat/tasks/*/events');
    records.push({scenario:'断线重连后恢复真实步骤与答案',passed:true});

    await send('给妈妈转100元，600元看看ETF');
    await latest().locator('.activity-step').first().waitFor();
    await page.reload();
    await page.waitForFunction(()=>window.accountUser?.username==='demo'&&document.querySelector('.activity-message'));
    await page.waitForFunction(()=>!document.querySelector('#send').disabled&&!document.querySelector('.activity-message:last-child')?.classList.contains('loading'));
    assert(await latest().locator('.activity-answer').innerText());
    records.push({scenario:'刷新后恢复后台请求和步骤记录',passed:true});

    await send('深入研究我的收支与理财，只分析不办理');
    await latest().getByRole('button',{name:'停止本次分析',exact:true}).waitFor();
    assert(await latest().locator('.activity-answer').isHidden());
    await latest().getByRole('button',{name:'停止本次分析',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);
    assert((await latest().locator('.activity-trace>summary').innerText()).includes('已停止'));
    records.push({scenario:'研究仍在运行时保持步骤，并可单独停止分析',passed:true});

    await send('深入研究本月收支与未来资金缺口，只分析不办理');
    await latest().getByRole('button',{name:'停止本次分析',exact:true}).waitFor();
    await page.waitForFunction(()=>!document.querySelector('#send').disabled&&!document.querySelector('.activity-message:last-child')?.classList.contains('loading'),undefined,{timeout:30000});
    assert(await latest().locator('.activity-answer').innerText());
    assert.equal(await latest().locator('.activity-trace').evaluate(node=>node.open),false);
    assert(await page.locator('#research-panel').isVisible());
    records.push({scenario:'研究任务结束后才收起并输出结论',passed:true});

    await send('给妈妈转50元，同时研究本月收支与现金流');
    await page.waitForFunction(()=>plan?.nodes?.some(n=>n.operation==='funds.transfer'&&n.status==='WAIT_CONFIRM'));
    await latest().getByRole('button',{name:'停止本次分析',exact:true}).waitFor();
    assert(await latest().locator('.activity-answer').isHidden());
    await page.waitForFunction(()=>!document.querySelector('#send').disabled,undefined,{timeout:30000});
    assert(await latest().locator('.activity-answer').innerText());
    records.push({scenario:'办理卡片先可用，混合研究继续更新步骤',passed:true});

    await send('查余额');
    await page.waitForFunction(()=>document.querySelector('.activity-message:last-child')?.classList.contains('typing'));
    await page.reload();
    await page.waitForFunction(()=>window.accountUser?.username==='demo'&&document.querySelector('.activity-message')&&!document.querySelector('#send').disabled);
    assert(await latest().locator('.activity-answer').innerText());
    records.push({scenario:'逐字显示期间刷新仍可恢复完整答案',passed:true});

    await page.emulateMedia({reducedMotion:'reduce'});
    await send('查余额');
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);
    assert.equal(await latest().locator('.activity-actions button').filter({hasText:'显示全文'}).isVisible(),false);
    assert.equal(await latest().locator('.activity-answer').getAttribute('aria-busy'),'false');
    records.push({scenario:'减少动态效果时直接展示完整答案',passed:true});
    assert.deepEqual(errors,[]);
    fs.writeFileSync('.data/conversation-ui-results.json',JSON.stringify({at:new Date().toISOString(),records,javascriptErrors:errors},null,2));
    console.log(JSON.stringify(records));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
