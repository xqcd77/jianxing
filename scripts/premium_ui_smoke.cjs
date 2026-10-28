const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  const records=[],errors=[];
  try{
    for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:width===1440?960:844},reducedMotion:'no-preference'});
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8137');
      await page.locator('#login-dialog[open]').waitFor();
      await page.waitForTimeout(950);
      const beam=()=>page.locator('.login-submit').evaluate(node=>{const style=getComputedStyle(node,'::before');return {name:style.animationName,duration:style.animationDuration,timing:style.animationTimingFunction,transform:style.transform,gradient:style.backgroundImage};});
      const first=await beam();
      assert.equal(first.name,'login-border-spin');assert.equal(first.duration,'2s');assert.equal(first.timing,'linear');
      assert(first.gradient.includes('conic-gradient'));
      await page.waitForTimeout(180);assert.notEqual((await beam()).transform,first.transform);
      const sculpture=()=>page.locator('.sculpture-float').evaluate(node=>getComputedStyle(node).transform);
      const firstPose=await sculpture();await page.waitForTimeout(180);assert.notEqual(await sculpture(),firstPose);
      assert(!await page.locator('#login-dialog').evaluate(node=>node.scrollWidth>node.clientWidth),'login has horizontal overflow');
      await page.locator('.login-submit').hover();await page.waitForTimeout(300);
      assert((await page.locator('.login-submit-face').evaluate(node=>getComputedStyle(node).backgroundColor)).includes('0.8'));
      await page.locator('#login-password').focus();await page.keyboard.press('Tab');
      assert.equal(await page.locator('.login-submit').evaluate(node=>getComputedStyle(node).outlineWidth),'3px');
      await page.screenshot({path:'.data/premium-login-focus-'+width+'.png'});
      await page.locator('.login-submit').evaluate(node=>node.blur());await page.mouse.move(10,10);await page.waitForTimeout(300);
      await page.screenshot({path:'.data/premium-login-'+width+'.png'});
      if(width===1440){
        await page.locator('#login-password').fill('incorrect-demo-password');await page.locator('.login-submit').click();
        await page.locator('#login-error').filter({hasText:/密码|登录|凭证/}).waitFor();
        assert(await page.locator('#login-dialog').evaluate(node=>node.open));
        await page.locator('#login-password').fill('DemoBank!2026');
      }
      await page.locator('.login-submit').click();
      await page.waitForFunction(()=>window.accountUser?.username==='demo'&&!document.querySelector('#login-dialog').open);
      await page.waitForTimeout(1250);
      assert.equal(await page.locator('#scenarios .scenario small').count(),6);
      assert.equal(await page.locator('.balance-caption').count(),3);
      assert(await page.locator('.brand-type small').innerText());
      assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth));
      const balance=page.locator('.balance-main'),money=await page.locator('#balance').innerText();
      await balance.hover({position:{x:25,y:25}});await page.waitForTimeout(150);
      assert(await balance.evaluate(node=>node.style.getPropertyValue('--tilt-x')));
      assert.equal(await page.locator('#balance').innerText(),money);
      await page.mouse.move(5,5);await page.screenshot({path:'.data/premium-workspace-'+width+'.png',fullPage:true});
      await page.locator('[data-category="funds"]').click();
      assert(await page.locator('#business-description').innerText());
      await page.locator('#business-close').click();
      await page.locator('#logout').click();await page.locator('#login-dialog[open]').waitFor();
      assert.equal(await page.locator('#login-password').inputValue(),'');
      records.push({width,animatedLogin:true,spin2s:true,hoverAlpha80:true,keyboardFocus:true,restoredInformation:true,cardTilt:true,noHorizontalOverflow:true,logout:true});
      await page.emulateMedia({reducedMotion:'reduce'});
      assert.equal((await beam()).name,'none');
      assert.equal(await page.locator('.sculpture-float').evaluate(node=>getComputedStyle(node).animationName),'none');
      await page.close();
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync('.data/premium-ui-results.json',JSON.stringify({at:new Date().toISOString(),records,reducedMotion:true,javascriptErrors:errors},null,2));
    console.log(JSON.stringify(records));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
