// Use a fresh demo database on port 8001. This test never approves a transfer.
const { chromium } = require('playwright');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.BANK_BROWSER_PATH || path.join(process.env['ProgramFiles(x86)'], 'Microsoft/Edge/Application/msedge.exe'), headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(process.env.BANK_UI_TEST_URL || 'http://127.0.0.1:8001');
    await page.locator('#login .primary').click();
    await page.waitForFunction(() => document.querySelector('#clock').textContent === '2026-10-13');
    async function ask(text) {
      await page.locator('#input').fill(text);
      await page.locator('#send').click();
      await page.waitForFunction(() => !document.querySelector('#send').disabled);
    }
    await ask('向我的账户转入200000元');
    await page.getByRole('button', { name: '确认模拟转入', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#balance').textContent.includes('200,000.00'));
    await ask('给妈妈转100000元');
    assert.equal(await page.locator('#plan-status').innerText(), '等待强验证');
    assert((await page.locator('#messages .assistant').last().innerText()).includes('100,000元'));
    const largeId = await page.evaluate(() => plan.id);
    await ask('100000元啊');
    assert.equal(await page.evaluate(() => plan.id), largeId);
    await ask('改成200元');
    assert.equal(await page.evaluate(() => plan.id), largeId);
    assert.equal(await page.locator('#plan-status').innerText(), '等待确认');
    assert.equal(await page.evaluate(() => plan.nodes[0].params.amount_fen), 20000);
    await ask('给她转100元');
    assert((await page.locator('#messages .assistant').last().innerText()).includes('收款人'));
    const draftId = await page.evaluate(() => conversation);
    await page.evaluate(async id => renderPlan(await api('/api/plans/' + id)), largeId);
    assert.equal(await page.evaluate(() => conversation), draftId, 'An old plan refresh must not change the active conversation');
    await ask('妈妈');
    assert.equal(await page.evaluate(() => plan.nodes[0].params.recipient_hint), '妈妈');
    assert.equal(await page.evaluate(() => plan.nodes[0].params.amount_fen), 10000);
    assert.equal(await page.locator('#balance').innerText(), '¥ 200,000.00');
    const saved = await page.evaluate(() => conversation);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#clock').textContent === '2026-10-13');
    assert.equal(await page.evaluate(() => conversation), saved, 'Conversation identity should survive a page refresh');
    await page.screenshot({ path: '.data/ui-dialogue-desktop.png', fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#dag-view').evaluate(node => node.open = true);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: '.data/ui-dialogue-mobile.png', fullPage: true });
    assert.deepEqual(errors, []);
    console.log('Browser passed: 100,000 yuan, same-plan follow-up, safe amount edit, clarification context protected from background refresh, scoped reload persistence, no debit, mobile layout.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
