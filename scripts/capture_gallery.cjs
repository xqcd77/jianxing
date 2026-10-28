// Capture the current product using real UI/API states in a fresh local sandbox.
// Public ETF sources are fixed by serve_upgrade_fixture.py; no external model is used.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const url=process.env.BANK_UI_TEST_URL;
if(!url||!['localhost','127.0.0.1'].includes(new URL(url).hostname))throw new Error('An isolated BANK_UI_TEST_URL is required.');
const output=path.resolve(process.env.BANK_SCREENSHOT_DIR||'.data/gallery-capture');
fs.mkdirSync(output,{recursive:true});

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BANK_BROWSER_PATH||path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  const page=await browser.newPage({viewport:{width:1440,height:1040},deviceScaleFactor:1,reducedMotion:'no-preference'});
  const errors=[],records=[];
  page.on('pageerror',error=>errors.push(error.message));
  async function shot(name,selector,caption){
    await page.evaluate(()=>document.fonts.ready);
    const target=selector?page.locator(selector):page;
    if(selector)await target.scrollIntoViewIfNeeded();else await page.evaluate(()=>window.scrollTo(0,0));
    await page.waitForTimeout(200);
    assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2));
    if(name==='12-allocation'){
      const funding=await page.locator('#funding-preview').boundingBox(),dag=await page.locator('#dag-view').boundingBox();
      await page.screenshot({path:path.join(output,name+'.png'),clip:{x:funding.x,y:funding.y,width:funding.width,height:dag.y+dag.height-funding.y},animations:'allow'});
    }else await target.screenshot({path:path.join(output,name+'.png'),animations:'allow',...(!selector&&name!=='01-login'?{fullPage:true}:{})});
    records.push({file:name+'.png',caption,viewport:page.viewportSize(),status:await page.evaluate(()=>plan?.status||null)});
    console.log(name);
  }
  async function prepare(category,action,params={}){
    await page.evaluate(async({category,action,params})=>{renderPlan(await api('/api/businesses/'+category+'/'+action+'/prepare',{params}),true);await overview();},{category,action,params});
  }
  async function finish(){
    for(let i=0;i<12;i++){
      if(await page.evaluate(()=>plan?.status==='COMPLETED'))return;
      const card=page.locator('#nodes .node.needs-action').first();
      const confirm=card.getByRole('button',{name:/^核对并(确认|验证)$/});
      assert(await confirm.count(),'Expected a confirmable step');
      await confirm.click();await page.locator('#detail-dialog[open]').waitFor();
      if(await page.locator('#detail-dialog .demo-code-button').count()){
        await page.locator('#detail-dialog').getByRole('button',{name:'获取演示验证码',exact:true}).click();
        await page.waitForFunction(()=>document.querySelector('#detail-dialog input[autocomplete="one-time-code"]')?.value.length===6);
      }
      await page.locator('#detail-dialog').getByRole('button',{name:/^(确认|验证并)办理本步骤$/}).click();
      await page.waitForFunction(()=>!document.querySelector('#detail-dialog').open);
    }
    throw new Error('Plan did not finish');
  }
  async function showResult(){
    for(const detail of await page.locator('.node-result-detail').all())if(!await detail.evaluate(n=>n.open))await detail.locator('summary').first().click();
    await page.locator('#dag-view').evaluate(n=>{n.open=false;});
  }
  async function send(text){await page.locator('#input').fill(text);await page.locator('#send').click();}
  async function answer(){await page.waitForFunction(()=>!document.querySelector('#send').disabled,undefined,{timeout:45000});}
  try{
    await page.goto(url);
    const health=await page.evaluate(()=>fetch('/health').then(r=>r.json()));
    assert.equal(health.execution_mode,'sandbox');assert.equal(health.model_mode,'demo');
    await page.locator('#login-dialog[open]').waitFor();
    await shot('01-login',null,'动态登录：东方绿金、悬浮卡片与流光按钮');
    await page.locator('#login button[type=submit]').click();await page.waitForFunction(()=>window.accountUser?.username==='demo');
    const initial=await page.evaluate(()=>facts.account.balance_fen);
    assert.equal(initial,0,'Use a fresh demo account, not an existing account.');
    await page.evaluate(async()=>{await api('/api/demo/payroll',{event_id:'gallery-'+crypto.randomUUID(),amount_fen:2000000});await overview();});
    await page.waitForTimeout(900);await shot('02-workspace',null,'工作台：余额、六类业务、对话与任务面板');
    await page.setViewportSize({width:390,height:1000});
    await shot('14-mobile',null,'手机端：工作台布局、余额与六类业务');
    await page.setViewportSize({width:1440,height:1040});
    await send('推荐每日定投200元的ETF，风险等级R3，投资3年，通过基金平台购买');
    await page.waitForFunction(()=>document.querySelectorAll('.activity-message:last-child .activity-step').length>=2);
    await shot('03-chat-progress','.chatbox','公开执行步骤：实时进度、流动波浪与停止入口');
    await answer();
    assert(await page.locator('.etf-candidates').count());
    await page.locator('.etf-candidates').last().scrollIntoViewIfNeeded();
    await shot('13-etf-information','.chatbox','ETF公开资料：来源、候选与未接入交易通道的说明');
    await send('给妈妈转400元，给小王转1500元，剩下的钱20%用于理财，并给出推荐');await answer();
    assert.equal(await page.evaluate(()=>plan.nodes.filter(n=>n.operation==='funds.transfer').length),2);
    assert.equal(await page.evaluate(()=>facts.account.balance_fen),2000000);
    await shot('04-multiple-goals','.request-goals','复合需求：两笔转账与剩余资金理财分别保留');
    const first=page.locator('article[data-node-id]').filter({hasText:'妈妈'});
    await first.getByRole('button',{name:'核对并确认',exact:true}).click();
    await page.getByRole('button',{name:'确认办理本步骤',exact:true}).click();
    await page.waitForFunction(()=>plan.nodes.some(n=>n.operation==='funds.transfer'&&n.status==='SUCCEEDED')&&!document.querySelector('#detail-dialog').open);
    const second=page.locator('article[data-node-id]').filter({hasText:'小王'});
    await second.getByRole('button',{name:'核对并验证',exact:true}).click();
    await shot('05-verification','#detail-dialog','大额转账：核对金额、资金影响与一次性验证码');
    await page.locator('#detail-dialog').getByRole('button',{name:'获取演示验证码',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#detail-dialog input[autocomplete="one-time-code"]')?.value.length===6);
    await page.getByRole('button',{name:'验证并办理本步骤',exact:true}).click();
    await page.waitForFunction(()=>plan.nodes.filter(n=>n.operation==='funds.transfer'&&n.status==='SUCCEEDED').length===2&&!document.querySelector('#detail-dialog').open);
    await page.getByRole('button',{name:'选择模拟30天产品',exact:true}).click();
    await page.locator('#detail-dialog[open] .risk-question select').first().waitFor();
    await shot('06-risk-assessment','#detail-dialog','适当性前置条件：直接进入测评，完成后返回原计划');
    await page.locator('#detail-dialog').evaluate(n=>n.close());
    await page.locator('#model').click();
    await page.locator('.research-form textarea').fill('核查本月收支、未来现金流与理财风险和流动性，只分析不办理。');
    await page.locator('.research-form input[name="amount_fen"]').fill('3000');
    await page.locator('.research-form input[name="reserve_fen"]').fill('1000');
    await page.locator('.research-form input[name="emergency_fen"]').fill('1000');
    await page.locator('.research-check input').check();
    await page.locator('.research-form button[type=submit]').click();
    await page.locator('#research-status').filter({hasText:'分析完成'}).waitFor({timeout:45000});
    await shot('07-research','#research-panel','研究报告：结论、现金流、产品条件与证据入口');
    await page.locator('#research-hide').count().then(async count=>{if(count)await page.locator('#research-hide').click();});
    // A new login clears the visible research context without hiding or rewriting data.
    await page.locator('#logout').click();
    await page.locator('#login-name').fill('demo');await page.locator('#login-password').fill('DemoBank!2026');
    await page.locator('#login button[type=submit]').click();
    await page.waitForFunction(()=>window.accountUser?.username==='demo');
    await prepare('bills','monthly',{period:'2026-09'});await showResult();
    await shot('08-bills','#nodes article.node','账单分析：收入、消费、退款与转账独立统计');
    await prepare('wealth','compare');await showResult();
    await shot('09-wealth','#nodes article.node','理财比较：风险、起购、锁定期与当前申购条件');
    await prepare('cards','restrict',{card_id:'C_MAIN',single_limit_fen:50000,daily_limit_fen:100000,blocked_categories:['gaming']});
    await finish();await showResult();await shot('10-cards','#nodes article.node','卡片管理：支付限额、类别限制与银行回执');
    await prepare('subscriptions','cancel',{subscription_id:'S_VIDEO',scope:'bank'});
    await finish();await showResult();await shot('11-subscriptions','#nodes article.node','订阅管理：银行停扣与商户合同分别展示');
    await prepare('life','allocation',{allocation_kind:'budget_transfer_investment',budget_fen:600000,purpose:'生活备用金',recipient_hint:'妈妈',transfer_fen:80000,product_id:'P_CASH',investment_fen:50000});
    await page.locator('#dag-view').evaluate(n=>{n.open=true;});
    await shot('12-allocation','#funding-preview','跨场景资金安排：预算预留、转账、申购与任务依赖');
    assert.equal(records.length,14);assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'capture-manifest.json'),JSON.stringify({captured_at:new Date().toISOString(),kind:'actual-local-demo-ui',model_mode:'demo',banking_api_mocked:false,external_public_sources:'fixed ETF fixtures in serve_upgrade_fixture.py',financial_data:'synthetic, isolated fresh account',device_scale_factor:1,screenshots:records,page_errors:errors},null,2)+'\n');
    console.log(JSON.stringify({captured:records.length,page_errors:errors,output}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
