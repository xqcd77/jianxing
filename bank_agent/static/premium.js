// @ts-check
import {setupWorkspaceAtmosphere} from './workspace-atmosphere.js';
// Visual responses never alter account values, permissions or request state.
export function setupPremiumMotion(){
  const reduced=matchMedia('(prefers-reduced-motion: reduce)'),fine=matchMedia('(hover: hover) and (pointer: fine)');
  let frame=0,point={x:0,y:0};
  /** @type {HTMLElement|null} */
  let surface=null;
  function reset(node){
    for(const key of ['--tilt-x','--tilt-y','--pointer-x','--pointer-y'])node?.style.removeProperty(key);
  }
  document.querySelectorAll('.balances article').forEach(node=>node.classList.add('tilt-surface'));
  document.addEventListener('pointermove',event=>{
    if(reduced.matches||!fine.matches||!(event.target instanceof Element))return;
    if(document.body.classList.contains('workspace-motion-off')&&!document.getElementById('login-dialog')?.hasAttribute('open'))return;
    const next=event.target.closest('.tilt-surface');if(!(next instanceof HTMLElement))return;
    if(surface!==next){reset(surface);surface=next;}
    point={x:event.clientX,y:event.clientY};if(frame)return;
    frame=requestAnimationFrame(()=>{
      frame=0;if(!surface||!surface.isConnected)return;
      const rect=surface.getBoundingClientRect(),x=Math.max(0,Math.min(1,(point.x-rect.left)/rect.width)),y=Math.max(0,Math.min(1,(point.y-rect.top)/rect.height));
      surface.style.setProperty('--tilt-x',((.5-y)*4).toFixed(2)+'deg');surface.style.setProperty('--tilt-y',((x-.5)*5).toFixed(2)+'deg');
      surface.style.setProperty('--pointer-x',(x*100).toFixed(1)+'%');surface.style.setProperty('--pointer-y',(y*100).toFixed(1)+'%');
    });
  },{passive:true});
  document.addEventListener('pointerout',event=>{
    if(!surface||event.relatedTarget instanceof Node&&surface.contains(event.relatedTarget))return;
    reset(surface);surface=null;
  },{passive:true});
  reduced.addEventListener('change',()=>{if(reduced.matches){cancelAnimationFrame(frame);frame=0;reset(surface);surface=null;}});
  const timers=new Map();
  for(const id of ['balance','available','reserved']){
    const node=document.getElementById(id);if(!node)continue;
    new MutationObserver(()=>{
      if(reduced.matches)return;
      node.classList.remove('amount-updated');void node.offsetWidth;node.classList.add('amount-updated');
      clearTimeout(timers.get(node));timers.set(node,setTimeout(()=>node.classList.remove('amount-updated'),850));
    }).observe(node,{childList:true,characterData:true,subtree:true});
  }
  setupWorkspaceAtmosphere();
}

let entranceTimer;
export function enterWorkspace(){
  clearTimeout(entranceTimer);document.body.classList.remove('workspace-enter');
  requestAnimationFrame(()=>{document.body.classList.add('workspace-enter');entranceTimer=setTimeout(()=>document.body.classList.remove('workspace-enter'),1200);});
}
