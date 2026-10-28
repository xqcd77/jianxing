'use strict';
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.BANK_BROWSER_PATH || path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  try {
    for (const width of [1440,390]) {
      const page = await browser.newPage({viewport:{width,height:1100}});
      const errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.goto(process.env.BANK_UI_TEST_URL || 'http://127.0.0.1:8102');
      await page.locator('#login .primary').click();
      await page.waitForFunction(()=>document.querySelector('#balance').textContent.includes('0.00'));
      async function ask(text) {
        await page.locator('#input').fill(text);
        await page.locator('#send').click();
        await page.waitForFunction(()=>!document.querySelector('#send').disabled, null, {timeout:150000});
      }
      await ask('给妈妈转500元');
      assert.match(await page.locator('#messages').innerText(),/先向自己的账户转入资金/);
      await ask('向里面转账500元');
      assert.match(await page.locator('#messages .assistant').last().innerText(),/给妈妈转账500元的安排会保留/);
      await page.getByRole('button', {name:'确认模拟转入',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#balance').textContent.includes('500.00'));
      const continuation=page.getByRole('button', {name:/继续给妈妈/});
      await continuation.click();
      await page.getByRole('button', {name:'确认执行',exact:true}).waitFor();
      assert.match(await page.locator('#balance').innerText(),/500\.00/);
      await page.getByRole('button', {name:'确认执行',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#plan-status').textContent==='全部完成');
      assert.match(await page.locator('#balance').innerText(),/0\.00/);
      assert.match(await page.locator('#messages').innerText(),/已在演示账户向妈妈转账500元/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
      assert.deepEqual(errors,[]);
      await page.screenshot({path:'.data/funding-context-ui-'+width+'.png',fullPage:true});
      await page.close();
    }
    console.log('Funding context closed loop passed: insufficient funds -> contextual deposit -> separate transfer approval, desktop and mobile.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error.message);process.exitCode=1;});
