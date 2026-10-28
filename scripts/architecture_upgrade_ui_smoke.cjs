const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');

(async()=>{
  const edge=path.join(process.env['ProgramFiles(x86)']||'','Microsoft/Edge/Application/msedge.exe');
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  const records=[];
  try{
    for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:1050}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8124');
      await page.locator('#login button[type=submit]').click();
      await page.waitForFunction(()=>window.accountUser?.username==='demo');
      await page.evaluate(async()=>{await api('/api/demo/payroll',{event_id:'upgrade-ui-'+crypto.randomUUID(),amount_fen:600000});await overview();});
      const before=await page.evaluate(()=>facts.account.balance_fen);
      const ask=async text=>{await page.locator('#input').fill(text);await page.locator('#send').click();};
      await ask('给妈妈转500元，拿1000元看看ETF，再开个视频会员');
      await page.waitForFunction(()=>plan?.request_goals?.length===3&&plan.nodes[0].status==='WAIT_CONFIRM');
      assert(await page.getByRole('button',{name:'停止本次任务',exact:true}).isVisible());
      await page.waitForFunction(()=>!document.querySelector('#send').disabled);
      const productDetails=page.locator('.goal-detail').filter({has:page.locator('.etf-candidates')}).first();
      if(!await productDetails.evaluate(node=>node.open))await productDetails.locator('summary').first().click();
      assert((await page.locator('.etf-candidates').first().innerText()).includes('管理费率'));
      assert.equal(await page.evaluate(()=>facts.account.balance_fen),before);
      await ask('第二个少花两百');
      await page.waitForFunction(()=>!document.querySelector('#send').disabled&&plan?.request_goals?.[1]?.query?.budget_fen===80000);
      const saved=await page.evaluate(()=>plan.id);
      await ask('给妈妈转400元，拿900元看看ETF');
      await page.waitForFunction(id=>plan?.id!==id&&plan?.request_goals?.length===2,saved);
      await page.getByRole('button',{name:'停止本次任务',exact:true}).click();
      await page.waitForFunction(()=>!document.querySelector('#send').disabled&&plan?.nodes?.[0]?.status==='CANCELLED');
      assert.equal(await page.evaluate(async()=>(await api('/api/overview')).account.balance_fen),before);
      await ask('给妈妈转300元，拿700元看看ETF');
      await page.waitForFunction(()=>sessionStorage.getItem('bank-chat-task-u_demo'));
      await page.reload();
      await page.waitForFunction(()=>window.accountUser?.username==='demo'&&!document.querySelector('#send').disabled&&plan?.request_goals?.[1]?.query?.budget_fen===70000);
      assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth));
      assert.deepEqual(errors,[]);
      await page.screenshot({path:'.data/architecture-upgrade-ui-'+width+'.png',fullPage:true});
      records.push({width,partialProgress:true,goalAmendment:true,cancellation:true,reloadRecovery:true,noUnauthorizedDebit:true,noOverflow:true});
      await page.close();
    }
    fs.writeFileSync('.data/architecture-upgrade-ui-verification.json',JSON.stringify({
      at:new Date().toISOString(),source:'isolated_public_source_fixture',records
    },null,2)+'\n');
    console.log(JSON.stringify(records,null,2));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
