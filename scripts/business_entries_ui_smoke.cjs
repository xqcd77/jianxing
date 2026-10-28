'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BANK_BROWSER_PATH||path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe')});
  const report=[];
  try{
    for(const width of [1440,390]){
      const page=await browser.newPage({viewport:{width,height:1100}}),errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/api/security/demo-code',async route=>{await new Promise(resolve=>setTimeout(resolve,150));await route.continue();});
      await page.goto(process.env.BANK_UI_TEST_URL||'http://127.0.0.1:8103');
      async function signIn(name='demo'){
        await page.locator('#login-name').fill(name);await page.locator('#login-password').fill('DemoBank!2026');
        await page.locator('#login .primary').click();
        await page.waitForFunction(name=>accountUser?.username===name&&document.querySelectorAll('#scenarios .business-category').length===6,name);
      }
      async function switchUser(name){
        if(await page.locator('#logout').isVisible())await page.locator('#logout').click();
        else await page.evaluate(()=>document.querySelector('#logout').click());
        await page.locator('#login-dialog').waitFor({state:'visible'});await signIn(name);
      }
      await signIn();
      await page.evaluate(()=>api('/api/demo/payroll',{event_id:'catalog-ui-'+crypto.randomUUID(),amount_fen:3_000_000}));
      await page.evaluate(()=>overview());
      const categories=await page.evaluate(()=>api('/api/businesses'));
      const leaves=categories.flatMap(c=>c.actions.flatMap(a=>a.mode==='group'?a.children.map(child=>({category:c.id,item:child,group:a.id})):[{category:c.id,item:a}]));
      assert.equal(leaves.length,33);
      async function choose(category,action,group){
        const tab=page.locator('#scenarios [data-category="'+category+'"]');
        if(await tab.getAttribute('aria-expanded')!=='true')await tab.click();
        if(!await page.locator('#business-options [data-action="'+action+'"]').count()){
          if(await page.locator('.business-back').count())await page.locator('.business-back').click();
          if(group)await page.locator('#business-options [data-action="'+group+'"]').click();
        }
        await page.locator('#business-options [data-action="'+action+'"]').waitFor();
        const metadata=page.waitForResponse(r=>r.url().endsWith('/api/businesses/'+category+'/'+action)&&r.request().method()==='GET');
        await page.locator('#business-options [data-action="'+action+'"]').click();
        return (await metadata).json();
      }
      async function authorize(){
        for(let i=0;i<20;i++){
          const pending=page.locator('#nodes .node.needs-action').first();
          if(!await pending.count())return;
          if(await pending.locator('.demo-code-button').count()){
            const issued=page.waitForResponse(r=>r.url().endsWith('/api/security/demo-code')&&r.request().method()==='POST');
            await pending.getByRole('button',{name:'获取演示验证码',exact:true}).click();
            await page.evaluate(()=>renderPlan(structuredClone(plan)));
            const issuedResponse=await issued;assert.equal(issuedResponse.status(),200,'Demo code delivery: '+await page.locator('#toast').innerText());
            try{await page.waitForFunction(()=>{const input=document.querySelector('#nodes .needs-action .verification-field input');return input?.dataset.challengeId&&/^\d{6}$/.test(input.value);});}
            catch(error){await page.screenshot({path:'.data/business-failed-verification.png',fullPage:true});throw new Error('Code display failed: '+JSON.stringify(await page.evaluate(()=>({planId:plan?.id,summary:plan?.summary,status:plan?.status,busy,toast:document.querySelector('#toast').textContent,inputs:[...document.querySelectorAll('#nodes .nodefooter input')].map(i=>({length:i.value.length,challenge:Boolean(i.dataset.challengeId),connected:i.isConnected})),scripts:[...document.scripts].map(s=>s.src)})))+' '+error.message);}
            const preserved=await pending.locator('.verification-field input').inputValue();
            await page.evaluate(()=>renderPlan(structuredClone(plan)));
            assert.equal(await pending.locator('.verification-field input').inputValue(),preserved);
          }
          const ready=page.waitForResponse(r=>r.url().endsWith('/approve')&&r.request().method()==='POST');
          await pending.locator('button.primary').click();const response=await ready;assert.equal(response.status(),200);
          const result=await response.json();await page.waitForFunction(data=>plan?.id===data.id&&plan.status===data.status&&plan.nodes.filter(n=>n.status==='SUCCEEDED').length===data.nodes.filter(n=>n.status==='SUCCEEDED').length,result);
        }throw new Error('Authorization did not finish');
      }
      async function run(category,action,values={},group){
        const prepared=page.waitForResponse(r=>r.url().endsWith('/api/businesses/'+category+'/'+action+'/prepare')&&r.request().method()==='POST');
        const item=await choose(category,action,group);
        if(item.fields.length){
          await page.waitForFunction(title=>document.querySelector('#detail-dialog').open&&document.querySelector('#detail-title').textContent.endsWith(title),item.label);
          if(values.allocation_kind)await page.locator('#detail-content [name="allocation_kind"]').selectOption(values.allocation_kind);
          for(const spec of item.fields){
            const label=page.locator('#detail-content [data-field="'+spec.name+'"]');
            if(!await label.isVisible())continue;
            if(spec.kind==='risk'){for(const input of await label.locator('select').all())await input.selectOption('2');continue;}
            if(spec.kind==='multi'){
              for(const input of await label.locator('input').all())if((values[spec.name]||[]).includes(await input.getAttribute('value')))await input.check();
              continue;
            }
            const input=label.locator('input,select').first();
            if(spec.kind==='select'){
              assert.equal(await input.isDisabled(),false,'No available object for '+category+'/'+action+'/'+spec.name);
              await input.selectOption(values[spec.name]===undefined?spec.choices[0].value:String(values[spec.name]));
            }else if(values[spec.name]!==undefined)await input.fill(String(values[spec.name]));
          }
          await page.locator('#detail-content button[type="submit"]').click();
        }
        const response=await prepared;assert.equal(response.status(),200,category+'/'+action);const result=await response.json();
        if(result.nodes){await page.waitForFunction(id=>plan?.id===id,result.id);await authorize();}
        if(result.status==='WAIT_DEMO_CONFIRM'){
          const done=page.waitForResponse(r=>r.url().includes('/incoming/')&&r.url().endsWith('/decision'));
          await page.getByRole('button',{name:'确认模拟转入',exact:true}).last().click();assert.equal((await done).status(),200);
        }
        return result;
      }
      let aaRequest;
      for(const {category,item,group} of leaves){
        const action=item.id,values={};
        if(['transfer','scheduled_transfer'].includes(action))Object.assign(values,{recipient_hint:'妈妈',amount_fen:'100'});
        if(action==='scheduled_transfer'){
          const day=await page.evaluate(()=>facts.business_now.slice(0,10));const date=new Date(day+'T09:00:00Z');date.setUTCDate(date.getUTCDate()+1);values.scheduled_for=date.toISOString().slice(0,10)+'T09:00';
        }
        if(action==='incoming')values.amount_fen='10000';
        if(action==='aa_create')Object.assign(values,{amount_fen:'300.01',participants:['u_wang','u_li'],subject:'共同用餐-'+width});
        if(action==='reserve')Object.assign(values,{amount_fen:'1000',purpose:'生活备用金'});
        if(action==='monthly')values.period='2026-09';if(action==='annual')values.period='2026';
        if(action==='income_plan'){
          const date=new Date((await page.evaluate(()=>facts.business_now.slice(0,10)))+'T09:00:00Z');date.setUTCDate(date.getUTCDate()+20);
          Object.assign(values,{amount_fen:'6000',date:date.toISOString().slice(0,10),label:'预计工资'});
        }
        if(action==='purchase')Object.assign(values,{product_id:'P_CASH',amount_fen:'500'});
        if(action==='redeem')Object.assign(values,{product_id:'P_CASH',amount_fen:'100'});
        if(category==='cards'){
          if(action==='apply')Object.assign(values,{kind:'virtual',name:'日常付款卡'});
          if(action!=='apply'&&action!=='query')values.card_id='C_MAIN';
          if(action==='limit')values.amount_fen='12000';
        }
        if(action==='cancel'&&category==='subscriptions')values.scope='bank';
        if(action==='allocation')Object.assign(values,{allocation_kind:'budget_transfer_investment',budget_fen:'3000',purpose:'生活备用金',recipient_hint:'妈妈',transfer_fen:'800',product_id:'P_CASH',investment_fen:'500'});
        if(action==='budget_cashflow')Object.assign(values,{amount_fen:'1000',purpose:'出行备用金'});
        const result=await run(category,action,values,group);
        if(action==='scheduled_transfer'){
          await page.evaluate(()=>api('/api/demo/clock',{days:1}));
          await page.evaluate(async id=>{renderPlan(await api('/api/plans/'+id));await overview();},result.id);await authorize();
        }
        if(action==='aa_create')aaRequest=await page.evaluate(()=>plan.nodes[0].result.data.request_id);
        if(action==='aa_manage'){
          await page.waitForSelector('#detail-content .business-record');assert.equal(await page.getByRole('button',{name:'支付我的份额',exact:true}).count(),0);
          await page.locator('#detail-content .business-record-actions').first().getByRole('button',{name:'查看进度',exact:true}).click();
          await page.locator('#detail-dialog').waitFor({state:'hidden'});
          await switchUser('wang');await run('funds','aa_manage',{},'aa');
          const payment=page.waitForResponse(r=>r.url().endsWith('/funds/aa_pay/prepare'));
          await page.getByRole('button',{name:'支付我的份额',exact:true}).first().click();assert.equal((await payment).status(),200);
          await page.locator('#detail-dialog').waitFor({state:'hidden'});await authorize();
          await switchUser('demo');
          const progress=await page.evaluate(id=>api('/api/businesses/funds/aa_status/prepare',{params:{request_id:id}}),aaRequest);
          assert.equal(progress.nodes[0].result.data.shares[1].status,'PAID');
        }
        if(action==='plans'){
          await page.waitForSelector('#detail-content .business-record');
          await page.getByRole('button',{name:'查看详情',exact:true}).first().click();await page.locator('#detail-dialog').waitFor({state:'hidden'});
        }
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,category+'/'+action+' overflow');
        report.push({width,category,action,result:'passed'});
      }
      // Exercise every allocation form variant, including hidden-field validation.
      for(const kind of ['budget','budget_transfer','budget_investment']){
        await run('life','allocation',{allocation_kind:kind,budget_fen:'100',purpose:'备用金',recipient_hint:'妈妈',transfer_fen:'10',product_id:'P_CASH',investment_fen:'100'});
        const operations=await page.evaluate(()=>plan.nodes.map(n=>n.operation));
        assert.equal(operations.includes('funds.transfer'),kind.includes('transfer'));
        assert.equal(operations.includes('wealth.purchase'),kind.includes('investment'));
      }
      await run('cards','apply',{kind:'physical',name:'备用实体卡-'+width});
      assert.match(await page.locator('#nodes').innerText(),/等待模拟寄送/);
      await run('cards','query');assert.match(await page.locator('#nodes').innerText(),/待寄送/);
      // A delayed read-only result must not replace a newly opened security dialog.
      await page.route('**/api/businesses/life/plans/prepare',async route=>{await new Promise(resolve=>setTimeout(resolve,800));await route.continue();});
      const oldRead=page.waitForResponse(r=>r.url().endsWith('/life/plans/prepare'));
      await choose('life','plans');await page.evaluate(()=>document.querySelector('#security').click());
      await page.waitForFunction(()=>document.querySelector('#detail-title').textContent==='安全中心');
      assert.equal((await oldRead).status(),200);await page.waitForTimeout(200);
      assert.equal(await page.locator('#detail-title').innerText(),'安全中心');await page.locator('#detail-close').click();
      await page.unroute('**/api/businesses/life/plans/prepare');
      await choose('life','allocation');await page.locator('#detail-dialog').waitFor({state:'visible'});
      await page.screenshot({path:'.data/business-allocation-'+width+'.png',fullPage:true});await page.locator('#detail-close').click();
      await page.locator('#scenarios [data-category="life"]').click();await page.locator('#scenarios [data-category="life"]').click();
      assert.equal(await page.locator('#business-options .business-option').count(),4);
      assert.doesNotMatch(await page.locator('#business-options').innerText(),/生日|鲜花|蛋糕/);
      await page.screenshot({path:'.data/business-simplified-'+width+'.png',fullPage:true});assert.deepEqual(errors,[]);await page.close();
    }
    fs.writeFileSync('.data/business-entry-ui-results.json',JSON.stringify(report,null,2));
    console.log('All 33 visible business entries passed on desktop and mobile, including participant AA payment and allocation variants.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
