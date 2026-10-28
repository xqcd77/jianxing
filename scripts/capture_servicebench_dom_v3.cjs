// Read-only DOM replay. User text is never inserted into the judge's candidate channel.
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const {chromium} = require('playwright');
const root = path.resolve(process.argv[2] || ''), url = process.argv[3];
if (!['127.0.0.1','localhost'].includes(new URL(url).hostname)) throw new Error('Local static UI required');
const digest = value => crypto.createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const manifest = JSON.parse(fs.readFileSync(path.join(root,'manifest.json'),'utf8'));
const taskRows = fs.readFileSync(path.join(root,'frozen_scenarios.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
const replayHash = digest(fs.readFileSync(__filename,'utf8'));
const replaySource = path.join(root,'capture_source_v3.cjs');
if(fs.existsSync(replaySource) && digest(fs.readFileSync(replaySource,'utf8'))!==replayHash) throw new Error('Capture source changed; preserve its prior revision');
if(!fs.existsSync(replaySource)) fs.copyFileSync(__filename,replaySource);
const staticDir = path.resolve(process.argv[4] || 'bank_agent/static');
const uiHash = digest(fs.readdirSync(staticDir).filter(n => /\.(js|css|html)$/.test(n)).sort().map(n => [n,digest(fs.readFileSync(path.join(staticDir,n),'utf8'))]));
(async () => {
  const executablePath = process.env.BANK_BROWSER_PATH || path.join(process.env['ProgramFiles(x86)'] || 'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
  const browser = await chromium.launch({headless:true,executablePath});
  let captured = 0, cached = 0;
  try {
    for (const variant of manifest.variants) for (const id of manifest.cases) for (let trial=1; trial<=manifest.trials; trial++) {
      const dir = path.join(root,variant,id,'trial_'+trial), episodePath = path.join(dir,'episode.json');
      if (!fs.existsSync(episodePath)) continue;
      const episodeText = fs.readFileSync(episodePath,'utf8'), episode = JSON.parse(episodeText), frames = [];
      const target = path.join(dir,'browser_dom_v3.json');
      if (fs.existsSync(target)) {
        const old = JSON.parse(fs.readFileSync(target,'utf8'));
        if (old.manifest_hash !== manifest.manifest_hash || old.episode_hash !== digest(episodeText) || old.ui_source_hash !== uiHash || old.assistant_only !== true || old.capture_script_hash!==replayHash) throw new Error('Existing DOM capture differs; preserve its old capture revision');
        cached++; continue;
      }
      for (const width of [1440,390]) {
        const context = await browser.newContext({viewport:{width,height:1100},reducedMotion:'reduce'});
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/api/**', route => route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:{code:'READ_ONLY_REPLAY',message:'只读界面回放'}})}));
        await page.goto(url);
        await page.waitForFunction(() => typeof window.renderPlan === 'function');
        if (episode.initial_ui_contexts?.demo) await page.evaluate(async initial => {
          const {sessionStore,accountStore} = await import('/static/state.js');
          sessionStore.user=initial.user; sessionStore.demoVerification=initial.demo_verification; accountStore.facts=initial.facts;
          document.querySelector('#login-dialog').close();
          document.querySelector('#messages').replaceChildren();
        },episode.initial_ui_contexts.demo);
        let cursor=0, previous='', assistantCount=0, previousAreas='';
        async function render(snapshot, phase, owner='demo', chatError=false) {
          if (!snapshot || (!snapshot.nodes && !snapshot.report && !snapshot.message && !snapshot.error)) return;
          // Checkpoint is an evaluator's private bank-state projection, not a
          // response the application ever sends to its plan renderer.
          if(snapshot.nodes && !snapshot.id) return;
          const clean={...snapshot}; delete clean.by_op; delete clean.http_status;
          const signature=digest(clean); const changed=signature!==previous; previous=signature;
          // A chat follow-up may update the same plan and must still be spoken.
          if(!changed && phase.endsWith(':api')) return;
          await page.evaluate(async ({value,initial,chatError,phase}) => {
            const {renderPlan} = await import('/static/plan-view.js');
            const {planStore,researchStore,sessionStore,accountStore} = await import('/static/state.js');
            if(initial){sessionStore.user=initial.user;sessionStore.demoVerification=initial.demo_verification;accountStore.facts=initial.facts;}
            if(value.error){
              const {toast}=await import('/static/ui.js');
              toast(value.error.message);
              if(chatError){const {message}=await import('/static/chat.js');message('这次回复未能完整显示。'+(value.error.message||'请稍后重试；涉及办理时，请先核实当前计划状态。'));}
            }else {
              // Render without navigation or prerequisite API side effects.
              // An explicit user response may activate a new read-only plan.
              // Silent background API refreshes must retain the original guard.
              if(!phase.endsWith(':api') && value.nodes && planStore.current?.id!==value.id)planStore.current=null;
              renderPlan(value,false);
              if(!phase.endsWith(':api') && (value.nodes || value.report)){
                const text=value.nodes?value.assistant_message:(value.report?.summary||value.message);
                if(text){const {message}=await import('/static/chat.js');message(text);}
              }
            }
            if(researchStore.timer!==null){clearTimeout(researchStore.timer);researchStore.timer=null;}
          },{value:snapshot,initial:episode.initial_ui_contexts?.[owner],chatError,phase});
          await page.waitForTimeout(60);
          const messages=await page.locator('#messages .message.assistant').allInnerTexts();
          const fresh=messages.slice(assistantCount); assistantCount=messages.length;
          const regions=await page.evaluate(async()=>{
            const {planStore}=await import('/static/state.js');
            return [...document.querySelectorAll('#funding-preview, #plan-status, #nodes, #research-panel, #toast')]
              .filter(n=>n instanceof HTMLElement&&!n.hidden&&n.getClientRects().length&&(planStore.current||!['funding-preview','plan-status','nodes'].includes(n.id)))
              .map(n=>({kind:n.id==='research-panel'?'research_report':n.id==='toast'?'error_notice':'business_card',text:n.innerText}));
          });
          const areas=JSON.stringify(regions);
          const parts=[...(fresh.length?[{kind:'assistant_reply',text:fresh.join('\n')}]:[]),...(areas!==previousAreas?regions:[])].filter(x=>x.text.trim());
          const shown=parts.map(x=>'['+x.kind+']\n'+x.text).join('\n');previousAreas=areas;
          frames.push({width,source:phase,regions:parts,assistant_text:shown,visible_text:shown,static_chrome_excluded:true,buttons:await page.locator('button:visible').allTextContents(),horizontal_overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2)});
          // Native disclosure controls only: no financial, selection, retry,
          // navigation or API button is activated. Preserve the initial view.
          const interactions=[];
          for(let pass=0;pass<8;pass++){
            const summaries=page.locator('#nodes details:not([open]) > summary:visible, #research-panel details:not([open]) > summary:visible');
            const count=await summaries.count(); if(!count)break;
            for(let i=count-1;i>=0;i--){const control=summaries.nth(i);interactions.push(await control.innerText());await control.click();}
          }
          if(interactions.length){
            const expanded=await page.evaluate(()=>[...document.querySelectorAll('#nodes, #research-panel')]
              .filter(n=>n instanceof HTMLElement&&!n.hidden&&n.getClientRects().length)
              .map(n=>({kind:n.id==='research-panel'?'research_report':'business_card',text:n.innerText})));
            frames.push({width,source:phase,view:'read_only_disclosures',interactions,regions:expanded,
              assistant_text:expanded.map(x=>'['+x.kind+']\n'+x.text).join('\n'),static_chrome_excluded:true});
          }

        }
        const kinds=new Map(taskRows.find(row=>row.execution.id===id).execution.steps.map(step=>[step.label,step.kind]));
        for(const turn of episode.responses||[]){
          if(turn.user_text) await page.evaluate(async text=>{const {message}=await import('/static/chat.js');message(text,'user');},turn.user_text);
          const anchor=turn.trace_event_index??cursor;
          for(const event of (episode.trace||[]).slice(cursor,anchor)) if(event.kind==='api' && (event.response?.nodes||event.response?.report)) await render(event.response,(turn.label||'turn')+':api',event.user);
          cursor=anchor; await render(turn.response,turn.label||'turn','demo',kinds.get(turn.label)==='chat');
        }
        if(errors.length) throw new Error('UI script error: '+errors[0]);
        await context.close();
      }
      fs.writeFileSync(target,JSON.stringify({version:'browser-dom-regions-v3',manifest_hash:manifest.manifest_hash,episode_hash:digest(episodeText),ui_source_hash:uiHash,capture_script_hash:replayHash,api_calls_blocked:true,real_interactive_execution:false,assistant_only:true,disclosure_policy:'native_summary_only_no_buttons',frames},null,2));
      captured++;
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify({captured,cached,assistant_only:true,bank_api_calls_blocked:true,real_interactive_execution:false}));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
