// Renders frozen responses in the real UI. Page API access is blocked; no banking actions are replayed.
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const {chromium}=require('playwright');
const root=path.resolve(process.argv[2]||''),url=process.argv[3]||'http://127.0.0.1:8145';
if(!['127.0.0.1','localhost'].includes(new URL(url).hostname))throw new Error('Use an isolated local UI server');
const digest=value=>crypto.createHash('sha256').update(typeof value==='string'?value:JSON.stringify(value)).digest('hex');
const manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json'),'utf8'));
const sourceDir=path.resolve('bank_agent/static');
const uiHash=digest(fs.readdirSync(sourceDir).filter(x=>/\.(js|css|html)$/.test(x)).sort().map(x=>[x,digest(fs.readFileSync(path.join(sourceDir,x),'utf8'))]));
(async()=>{
 const executablePath=process.env.BANK_BROWSER_PATH||path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
 const browser=await chromium.launch({headless:true,executablePath});let count=0,cached=0;
 try{
 const fixtureContext=await browser.newContext();
 const login=await fixtureContext.request.post(url+'/api/auth/login',{data:{username:'demo',password:process.env.BANK_BROWSER_PASSWORD||'DemoBank!2026'}});
 if(!login.ok())throw new Error('Read-only UI fixture login failed');
 const auth=await login.json(),headers={Authorization:'Bearer '+auth.access_token};
 const fixtureFacts=await (await fixtureContext.request.get(url+'/api/overview',{headers})).json();
 const fixtureSecurity=await (await fixtureContext.request.get(url+'/api/security/status',{headers})).json();
 const fallback={user:auth.user,facts:fixtureFacts,demo_verification:fixtureSecurity.demo_verification};
 await fixtureContext.close();
  for(const variant of manifest.variants)for(const id of manifest.cases)for(let trial=1;trial<=manifest.trials;trial++){
   const dir=path.join(root,variant,id,'trial_'+trial),source=path.join(dir,'episode.json');if(!fs.existsSync(source))continue;
   const episodeText=fs.readFileSync(source,'utf8'),episode=JSON.parse(episodeText),frames=[];
   const artifact=path.join(dir,'browser_dom.json');
   if(fs.existsSync(artifact)){
    const previous=JSON.parse(fs.readFileSync(artifact,'utf8'));
    if(previous.manifest_hash!==manifest.manifest_hash||previous.episode_hash!==digest(episodeText)||previous.ui_source_hash!==uiHash)throw new Error('Frozen browser capture differs; keep the original and use a new experiment directory');
    cached++;continue;
   }
   for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:1100},reducedMotion:'reduce'}),page=await context.newPage();
    await page.route('**/api/**',route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:{code:'REPLAY_READ_ONLY',message:'评测界面回放不执行业务'}})}));
    await page.goto(url);await page.waitForFunction(()=>typeof window.renderPlan==='function');
    await page.evaluate(async value=>{const {sessionStore,accountStore}=await import('/static/state.js');sessionStore.user=value.user;sessionStore.demoVerification=value.demo_verification;accountStore.facts=value.facts;document.querySelector('#login-dialog').close();},episode.initial_ui_contexts?.demo||fallback);
    let cursor=0,last='';
    async function render(snapshot,sourceLabel,owner='demo'){
     if(!snapshot||(!snapshot.nodes&&!snapshot.report&&!snapshot.message&&!snapshot.error))return;
     const comparable={...snapshot};delete comparable.by_op;delete comparable.http_status;
     const signature=digest(comparable);if(signature===last)return;last=signature;
     await page.evaluate(async({value,initial})=>{const {renderPlan}=await import('/static/plan-view.js');const {researchStore,planStore,sessionStore,accountStore}=await import('/static/state.js');sessionStore.user=initial.user;sessionStore.demoVerification=initial.demo_verification;accountStore.facts=initial.facts;renderPlan(value,Boolean(value.nodes&&planStore.current?.id!==value.id));if(researchStore.timer!==null){clearTimeout(researchStore.timer);researchStore.timer=null;}},{value:snapshot.error?{message:snapshot.error.message,status:'BLOCKED'}:snapshot,initial:episode.initial_ui_contexts?.[owner]||fallback});
     await page.waitForTimeout(100);
     const visible=await page.locator('#messages').innerText();
     const areas=await page.locator('#funding-preview, #plan-status, #nodes, #research-panel').evaluateAll(nodes=>nodes.filter(n=>n instanceof HTMLElement&&!n.hidden&&n.getClientRects().length).map(n=>n.innerText));
     frames.push({width,source:sourceLabel,visible_text:[visible,...areas].join('\n'),buttons:await page.locator('button:visible').allTextContents()});
    }
    for(const turn of episode.responses||[]){
     if(turn.user_text)await page.evaluate(async text=>{const {message}=await import('/static/chat.js');message(text,'user');},turn.user_text);
     const anchor=turn.trace_event_index??cursor;
     for(const event of (episode.trace||[]).slice(cursor,anchor))if(event.kind==='api'&&(event.response?.nodes||event.response?.report))await render(event.response,'api',event.user);
     cursor=anchor;await render(turn.response,turn.label||'response');
    }
    await context.close();cursor=0;last='';
   }
   const result={version:'browser-dom-replay-v1',manifest_hash:manifest.manifest_hash,episode_hash:digest(episodeText),ui_source_hash:uiHash,
    api_calls_blocked:true,real_interactive_execution:false,ui_context_source:episode.initial_ui_contexts?'frozen_episode':'isolated_seed_labels_legacy_replay',frames};
   fs.writeFileSync(path.join(dir,'browser_dom.json'),JSON.stringify(result,null,2));count++;
  }
 }finally{await browser.close();}
 console.log(JSON.stringify({captured:count,cached,episodes:count+cached,version:'browser-dom-replay-v1',api_calls_blocked:true}));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
