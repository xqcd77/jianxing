// Run on a fresh demo database at port 8001; never touches the main account.
const { chromium } = require('playwright');
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const edge = process.env.BANK_BROWSER_PATH || path.join(process.env['ProgramFiles(x86)'] || '', 'Microsoft/Edge/Application/msedge.exe');
  const browser = await chromium.launch({ ...(fs.existsSync(edge) ? { executablePath: edge } : {}), headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, reducedMotion: 'no-preference' });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(process.env.BANK_UI_TEST_URL || 'http://127.0.0.1:8001');
    await page.locator('#login .primary').click();
    await page.waitForFunction(() => document.querySelector('#clock').textContent === '2026-10-13');
    const request = async text => {
      await page.locator('#input').fill(text);
      await page.locator('#send').click();
      await page.waitForFunction(() => !document.querySelector('#send').disabled);
    };
    await request('现在我要向我的账户转入20000元');
    const card = page.locator('.incoming-card');
    await card.waitFor();
    const reply = await page.locator('#messages .assistant').last().innerText();
    assert(reply.includes('20,000元'));
    assert(!/cashflow|funds\.|2000000|工具目录|接口/.test(reply));
    assert.equal(await page.locator('#balance').innerText(), '¥ 0.00');
    await page.screenshot({ path: '.data/ui-product-confirm.png', fullPage: true });
    await page.getByRole('button', { name: '确认模拟转入', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#balance').textContent.includes('20,000.00'));
    assert((await page.locator('#messages .assistant').last().innerText()).includes('已模拟转入20,000元'));
    await request('给妈妈转800元');
    await page.getByRole('button', { name: '确认执行', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#balance').textContent.includes('19,200.00'));
    assert((await page.locator('#messages .assistant').last().innerText()).includes('转账800元'));
    await request('我要向我的账户转入100元');
    await page.getByRole('button', { name: '暂不转入', exact: true }).click();
    assert.equal(await page.locator('#balance').innerText(), '¥ 19,200.00');
    await request('购买理财500元');
    assert((await page.locator('#messages .assistant').last().innerText()).includes('哪款理财产品'));
    const before = await page.locator('.landscape').screenshot();
    const transformBefore = await page.locator('.mist-one').evaluate(node => getComputedStyle(node).transform);
    await page.waitForTimeout(900);
    const after = await page.locator('.landscape').screenshot();
    const transformAfter = await page.locator('.mist-one').evaluate(node => getComputedStyle(node).transform);
    assert.notEqual(transformBefore, transformAfter, 'Mist must continue moving after page load');
    assert(!before.equals(after), 'Landscape should visibly change between frames');
    await page.screenshot({ path: '.data/ui-product-desktop.png', fullPage: true });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    assert.equal(await page.locator('.mist-one').evaluate(node => getComputedStyle(node).animationName), 'none');
    assert.equal(await page.locator('.wash-jade').evaluate(node => getComputedStyle(node).animationName), 'none');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#dag-view').evaluate(node => node.open = true);
    await page.screenshot({ path: '.data/ui-product-mobile.png', fullPage: true });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile must not overflow');
    assert.deepEqual(errors, []);
    console.log('Browser passed: plain-language funding confirmation, incoming ledger, transfer receipt, cancellation, product clarification, continuous motion, reduced motion, mobile layout.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
