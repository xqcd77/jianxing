// @ts-check
import {$,el} from './ui.js';

/** Public workflow milestones only; model reasoning is never requested or displayed.
 * @param {HTMLElement} row @param {()=>boolean} current
 */
export function createActivity(row,current){
  const body=/** @type {HTMLElement} */(row.lastElementChild);
  body.classList.add('activity-body');row.classList.add('activity-message');
  const trace=el('details',undefined,'activity-trace'),summary=el('summary'),duration=el('span','已运行 0秒');
  const steps=el('ol',undefined,'activity-steps'),answer=el('div',undefined,'activity-answer');
  const actions=el('div',undefined,'activity-actions'),stop=el('button','停止'),retry=el('button','重新连接'),skip=el('button','显示全文');
  const announcement=el('span',undefined,'sr-only');announcement.setAttribute('role','status');announcement.setAttribute('aria-live','polite');
  stop.setAttribute('aria-label','停止本次任务');retry.setAttribute('aria-label','恢复查看进度');
  for(const button of [stop,retry,skip])button.type='button';
  retry.hidden=skip.hidden=true;trace.open=true;summary.append(duration);trace.append(summary,steps);
  answer.setAttribute('aria-live','polite');answer.setAttribute('aria-atomic','true');answer.hidden=true;
  actions.append(stop,retry,skip);body.replaceChildren(trace,answer,actions,announcement);
  let started=Date.now(),ended=false,disposed=false,connectionLost=false,timer,frame=0,release=()=>{},typingText='';
  /** @type {Array<{key:string,label:string}>} */
  const milestones=[];
  const scroll=()=>{const root=$('messages');if(root.scrollHeight-root.scrollTop-root.clientHeight<150)root.scrollTop=root.scrollHeight;};
  const clock=()=>{if(!connectionLost)duration.textContent=(ended?'耗时 ':'已运行 ')+Math.max(0,Math.round((Date.now()-started)/1000))+'秒';};
  function dispose(){disposed=true;clearInterval(timer);cancelAnimationFrame(frame);release();stop.hidden=retry.hidden=skip.hidden=true;row.classList.remove('loading','typing');}
  timer=setInterval(()=>{if(!row.isConnected||!current()){dispose();return;}clock();},500);
  function paint(active=true){
    steps.replaceChildren();
    for(const [index,step] of milestones.entries()){
      const item=el('li',undefined,'activity-step'),icon=el('span',active&&index===milestones.length-1?'◌':'✓','step-icon');
      icon.setAttribute('aria-hidden','true');item.classList.toggle('is-active',active&&index===milestones.length-1);
      const label=el('span',undefined,'step-label'),accessible=el('span',step.label,'sr-only'),visual=el('span',undefined,'step-wave');
      visual.setAttribute('aria-hidden','true');
      Array.from(step.label).forEach((character,i)=>{const part=el('span',character);part.style.setProperty('--char',String(i));visual.append(part);});
      label.append(accessible,visual);item.append(icon,label);steps.append(item);
    }
    if(active&&milestones.length)announcement.textContent=milestones.at(-1)?.label||'';
    scroll();
  }
  function add(stage,label){
    if(ended||disposed||!current()||!label||stage==='ready')return;
    const captions={understanding:'理解你的安排',planning:'拆分任务与核对条件',verifying:'核对资金与办理条件',banking:'生成办理卡片',information:'查询产品资料与条件'};
    label=captions[stage]||(stage==='goals_ready'?String(label).split('，')[0]:String(label).replace(/^正在/,''));
    const key=stage+':'+label;
    if(milestones.at(-1)?.key===key)return;
    milestones.push({key,label:String(label)});if(milestones.length>10)milestones.shift();paint();
  }
  function complete(state='SUCCEEDED',at=null){
    if(disposed)return;
    ended=true;connectionLost=false;clearInterval(timer);row.classList.remove('loading');stop.hidden=retry.hidden=true;
    paint(false);trace.open=false;
    const finish=at?Date.parse(at):Date.now(),seconds=Math.max(0,Math.round(((Number.isFinite(finish)?finish:Date.now())-started)/1000));
    duration.textContent=(state==='SUCCEEDED'?'耗时 ':state==='CANCELLED'?'已停止 · ':state==='INTERRUPTED'?'已中断 · ':'未完成 · ')+seconds+'秒';
    if(state!=='SUCCEEDED'){
      steps.lastElementChild?.classList.add('step-incomplete');
      const icon=steps.lastElementChild?.querySelector('.step-icon');if(icon)icon.textContent='–';
    }
  }
  async function type(text){
    if(disposed||!current())return;
    typingText=String(text||'');answer.hidden=!typingText;answer.setAttribute('aria-busy','true');answer.textContent='';
    const characters=Array.from(typingText),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
    if(!characters.length||reduced){answer.textContent=typingText;answer.setAttribute('aria-busy','false');return;}
    row.classList.add('typing');skip.hidden=false;
    await new Promise(resolve=>{
      let finished=false;const start=performance.now(),length=Math.min(2400,Math.max(400,characters.length*12));
      release=()=>{if(finished)return;finished=true;cancelAnimationFrame(frame);resolve(undefined);};
      const tick=now=>{
        if(!current()||!row.isConnected){dispose();return;}
        const count=Math.min(characters.length,Math.max(1,Math.floor((now-start)/length*characters.length)));
        answer.textContent=characters.slice(0,count).join('');scroll();
        if(count===characters.length){release();return;}frame=requestAnimationFrame(tick);
      };
      skip.onclick=()=>{answer.textContent=typingText;release();};frame=requestAnimationFrame(tick);
    });
    row.classList.remove('typing');skip.hidden=true;answer.setAttribute('aria-busy','false');scroll();
  }
  add('queued','等待处理');
  return {row,stop,retry,add,complete,type,dispose,
    update(task){
      connectionLost=false;
      const created=Date.parse(task.created_at);if(Number.isFinite(created))started=created;
      // Events are durable and may arrive in a batch after reconnecting.
      const events=Array.isArray(task.events)?task.events:[];
      if(events.length){
        milestones.splice(0);for(const event of events)add(event.stage,event.label);
      }else add(task.stage||'queued',task.label||'等待处理');
      if(!ended)clock();
    },
    disconnected(){connectionLost=true;retry.hidden=false;announcement.textContent='连接已断开，可以重新连接';duration.textContent='连接已断开';},
  };
}
