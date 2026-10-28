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
      await page.goto(process.env.BANK_UI_TEST_URL || 'http://127.0.0.1:8001');
      await page.locator('#login .primary').click();
      await page.waitForFunction(()=>document.querySelector('#clock').textContent==='2026-10-13');
      await page.locator('#demo-panel').evaluate(node=>node.open=true);
      await page.locator('#payroll').click();
      await page.waitForFunction(()=>!document.querySelector('#payroll').disabled);
      async function ask(text) {
        await page.locator('#input').fill(text);
        await page.locator('#send').click();
        await page.waitForFunction(()=>!document.querySelector('#send').disabled);
      }
      await ask('给妈妈转500元');
      await page.waitForFunction(()=>document.querySelectorAll('#nodes .node').length===1);
      await ask('再给妈妈转100元');
      await page.waitForFunction(()=>document.querySelectorAll('#nodes .node').length===2);
      assert.match(await page.locator('#nodes').innerText(),/第2版/);
      assert.match(await page.locator('#plan-status').innerText(),/确认/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
      assert.deepEqual(errors,[]);
      await page.screenshot({path:'.data/dynamic-ui-'+width+'.png',fullPage:true});
      await page.close();
    }
    console.log('Dynamic plan append: desktop and mobile passed.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error.message);process.exitCode=1;});
