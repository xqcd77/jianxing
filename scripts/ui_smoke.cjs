// Run against a fresh, separate demo database on port 8001. Requires Playwright.
// Legacy competition example is prepared by API; normal menus use generic banking workflows.
// TOTP below simulates the second device in this browser integration test only.
const { chromium } = require('playwright');
const crypto = require('node:crypto');
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');

function totp(secret) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  const bits = [...secret].map(c => alphabet.indexOf(c).toString(2).padStart(5, '0')).join('');
  const bytes = [];
  for (let i=0;i+8<=bits.length;i+=8) bytes.push(parseInt(bits.slice(i,i+8),2));
  const counter=Buffer.alloc(8);counter.writeBigUInt64BE(BigInt(Math.floor(Date.now()/30000)));
  const digest=crypto.createHmac('sha1',Buffer.from(bytes)).update(counter).digest();
  const offset=digest[digest.length-1]&15;
  return ((digest.readUInt32BE(offset)&0x7fffffff)%1000000).toString().padStart(6,'0');
}

(async()=>{
  const edge=process.env.BANK_BROWSER_PATH || path.join(process.env['ProgramFiles(x86)']||'', 'Microsoft/Edge/Application/msedge.exe');
  const browser=await chromium.launch({...(fs.existsSync(edge)?{executablePath:edge}:{}),headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8001');
    await page.locator('#login .primary').click();
    await page.waitForFunction(()=>document.querySelector('#clock').textContent==='2026-10-13');
    await page.locator('#demo-panel').evaluate(panel=>panel.open=true);
    await page.locator('#security').click();
    await page.locator('#detail-content details.security-section > summary').click();
    await page.getByRole('button',{name:'显示首次绑定信息',exact:true}).click();
    await page.locator('#detail-content pre').waitFor();
    const seed=await page.locator('#detail-content pre').innerText();
    await page.locator('#detail-close').click();
    await page.locator('#payroll').click();
    await page.waitForFunction(()=>document.querySelector('#balance').textContent.includes('6,000.00'));
    await page.evaluate(async()=>{renderPlan(await api('/api/scenarios/salary',{}),true);await overview();});
    await page.waitForSelector('.node');
    for(let i=0;i<10;i++){
      if((await page.locator('#plan-status').innerText())==='等待预约时间')break;
      const approve=page.locator('.nodefooter button.primary').first();
      await approve.waitFor();
      const card=approve.locator('..');
      const otp=card.locator('input');
      if(await otp.count())await otp.fill(totp(seed));
      const old=await page.locator('#nodes').innerText();
      await approve.click();
      await page.waitForFunction(previous=>document.querySelector('#nodes').innerText!==previous,old);
    }
    assert.equal(await page.locator('#plan-status').innerText(),'等待预约时间');
    assert.match(await page.locator('#balance').innerText(),/4,700\.00/);
    assert.match(await page.locator('#available').innerText(),/700\.00/);
    for(let i=0;i<5;i++){
      const previous=await page.locator('#clock').innerText();
      await page.locator('#advance').click();
      await page.waitForFunction(old=>document.querySelector('#clock').textContent!==old,previous);
    }
    await page.waitForFunction(()=>document.querySelector('#plan-status').textContent==='等待确认');
    await page.locator('.nodefooter button.primary').click();
    await page.waitForFunction(()=>document.querySelector('#plan-status').textContent==='全部完成');
    await page.waitForFunction(()=>document.querySelector('#balance').textContent.includes('4,200.00'));
    assert.match(await page.locator('#balance').innerText(),/4,200\.00/);
    assert.match(await page.locator('#available').innerText(),/1,200\.00/);
    const output=process.env.BANK_UI_OUTPUT||'.data';fs.mkdirSync(output,{recursive:true});
    await page.screenshot({path:path.join(output,'ui-closed-loop.png'),fullPage:true});
    await page.locator('#dag-view').evaluate(panel=>panel.open=true);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.locator('.mobile-nav').isVisible(),true);
    await page.screenshot({path:path.join(output,'ui-mobile-closed-loop.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Browser closed loop passed: payroll, permissions, MFA, due-date purchase, receipts, mobile layout.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
