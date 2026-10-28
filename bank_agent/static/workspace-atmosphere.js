// @ts-check
// Decorative canvas rendering: bounded density, no account data and no network.
export function setupWorkspaceAtmosphere(){
  const dustCanvas=/** @type {HTMLCanvasElement|null} */(document.getElementById('workspace-dust'));
  const edgeCanvas=/** @type {HTMLCanvasElement|null} */(document.getElementById('workspace-edges'));
  const toggle=/** @type {HTMLButtonElement|null} */(document.getElementById('ambient-toggle'));
  if(!dustCanvas||!edgeCanvas||!toggle)return;
  const dust=dustCanvas.getContext('2d'),edge=edgeCanvas.getContext('2d');if(!dust||!edge)return;
  startWorkspaceAtmosphere(dustCanvas,edgeCanvas,toggle,dust,edge);
}

/** @param {HTMLCanvasElement} dustCanvas @param {HTMLCanvasElement} edgeCanvas
 * @param {HTMLButtonElement} toggle @param {CanvasRenderingContext2D} dust @param {CanvasRenderingContext2D} edge */
function startWorkspaceAtmosphere(dustCanvas,edgeCanvas,toggle,dust,edge){
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  let enabled=true;try{enabled=localStorage.getItem('bank-workspace-motion')!=='off';}catch{}
  let width=0,height=0,frame=0,last=0,elapsed=0,refreshFrame=0,sequence=0;
  /** @type {Array<{x:number,y:number,r:number,speed:number,phase:number}>} */
  let motes=[];
  /** @type {Array<{x:number,y:number,width:number,height:number,radius:number,perimeter:number}>} */
  let panels=[];
  /** @type {Array<{x:number,y:number,vx:number,vy:number,life:number,age:number,color:string}>} */
  let sparks=[];
  let emitted=0,active=false;
  const colors=['186,169,122','123,175,163','169,150,191','132,174,193'];
  const random=seed=>{const value=Math.sin(seed*127.1+311.7)*43758.5453;return value-Math.floor(value);};
  function resize(){
    width=innerWidth;height=innerHeight;const ratio=Math.min(devicePixelRatio||1,1.5);
    for(const canvas of [dustCanvas,edgeCanvas]){canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);canvas.style.width=width+'px';canvas.style.height=height+'px';}
    dust.setTransform(ratio,0,0,ratio,0,0);edge.setTransform(ratio,0,0,ratio,0,0);
    const count=width<700?24:Math.min(68,Math.round(width*height/23000));
    motes=Array.from({length:count},(_,i)=>({x:random(i+1)*width,y:random(i+91)*height,r:.55+random(i+133)*1.1,speed:.008+random(i+181)*.012,phase:random(i+222)*Math.PI*2}));
    edgeCanvas.dataset.particleLimit=String(count+72);refreshPanels();
  }
  function refreshPanels(){
    sparks=[]; // Scroll/layout changes must not leave particles over another card's text.
    panels=[...document.querySelectorAll('.hero,.chatbox,.research-panel:not([hidden]),.request-goals,.node.needs-action')]
      .filter(node=>node instanceof HTMLElement&&node.offsetWidth>0).map(node=>{
        const box=node.getBoundingClientRect(),radius=Math.min(parseFloat(getComputedStyle(node).borderRadius)||18,box.width/2,box.height/2);
        return {x:box.left,y:box.top,width:box.width,height:box.height,radius,perimeter:2*(box.width+box.height-4*radius)+Math.PI*2*radius};
      }).filter(box=>box.y<height-10&&box.y+box.height>15&&box.width>180).slice(0,width<700?3:4);
  }
  function scheduleRefresh(){if(refreshFrame)return;refreshFrame=requestAnimationFrame(()=>{refreshFrame=0;refreshPanels();});}
  function point(panel,distance){
    const {width:w,height:h,radius:r}=panel;let position=((distance%panel.perimeter)+panel.perimeter)%panel.perimeter;
    const straight=[w-2*r,h-2*r,w-2*r,h-2*r],centers=[[w-r,r],[w-r,h-r],[r,h-r],[r,r]];
    for(let side=0;side<4;side++){
      if(position<=straight[side]){
        const local=side===0?{x:r+position,y:0,nx:0,ny:-1}:side===1?{x:w,y:r+position,nx:1,ny:0}:side===2?{x:w-r-position,y:h,nx:0,ny:1}:{x:0,y:h-r-position,nx:-1,ny:0};
        return {...local,x:panel.x+local.x,y:panel.y+local.y};
      }
      position-=straight[side];const arc=Math.PI*r/2;
      if(position<=arc){const angle=-Math.PI/2+side*Math.PI/2+position/Math.max(1,r);return {x:panel.x+centers[side][0]+Math.cos(angle)*r,y:panel.y+centers[side][1]+Math.sin(angle)*r,nx:Math.cos(angle),ny:Math.sin(angle)};}
      position-=arc;
    }
    return {x:panel.x+r,y:panel.y,nx:0,ny:-1};
  }
  function glow(context,x,y,radius,color,alpha){
    const light=context.createRadialGradient(x,y,0,x,y,radius);light.addColorStop(0,`rgba(${color},${alpha})`);light.addColorStop(.25,`rgba(${color},${alpha*.35})`);light.addColorStop(1,`rgba(${color},0)`);
    context.fillStyle=light;context.fillRect(x-radius,y-radius,radius*2,radius*2);
  }
  function paint(now){
    frame=0;if(!active)return;
    const interval=width<700?1000/24:1000/30;
    if(last&&now-last<interval){frame=requestAnimationFrame(paint);return;}
    const delta=last?Math.min(65,now-last):interval;last=now;elapsed+=delta;
    dust.clearRect(0,0,width,height);edge.clearRect(0,0,width,height);
    for(const mote of motes){
      const x=(mote.x+Math.sin(elapsed/9000+mote.phase)*16+width)%width,y=(mote.y-elapsed*mote.speed+height*1000)%height;
      const alpha=.15+.2*(.5+.5*Math.sin(elapsed/1800+mote.phase));
      glow(dust,x,y,mote.r*4,'136,163,138',alpha*.35);dust.fillStyle=`rgba(119,151,126,${alpha})`;dust.beginPath();dust.arc(x,y,mote.r,0,Math.PI*2);dust.fill();
    }
    for(const [index,panel] of panels.entries()){
      const distance=elapsed*.055+panel.perimeter*.19*index;
      for(let head=0;head<2;head++){
        const offset=distance+head*panel.perimeter*.48,color=colors[(index+head)%colors.length];
        for(let trail=0;trail<7;trail++){
          const p=point(panel,offset-trail*5),alpha=(1-trail/7)*.68;
          glow(edge,p.x+p.nx*1.4,p.y+p.ny*1.4,trail===0?7:3.5,color,alpha);
          edge.fillStyle=`rgba(${color},${alpha})`;edge.beginPath();edge.arc(p.x+p.nx*1.4,p.y+p.ny*1.4,trail===0?1.25:.7,0,Math.PI*2);edge.fill();
        }
        if(elapsed-emitted>150&&sparks.length<72){
          const p=point(panel,offset),seed=++sequence;
          sparks.push({x:p.x+p.nx*3,y:p.y+p.ny*3,vx:p.nx*(.006+random(seed)*.012),vy:p.ny*(.006+random(seed+100)*.012)-.004,age:0,life:650+random(seed+51)*650,color});
        }
      }
    }
    if(elapsed-emitted>150)emitted=elapsed;
    sparks=sparks.filter(spark=>spark.age<spark.life);
    for(const spark of sparks){spark.age+=delta;spark.x+=spark.vx*delta;spark.y+=spark.vy*delta;glow(edge,spark.x,spark.y,3.4,spark.color,(1-spark.age/spark.life)*.65);}
    edgeCanvas.dataset.frame=String((Number(edgeCanvas.dataset.frame)||0)+1);edgeCanvas.dataset.particles=String(motes.length+sparks.length);
    frame=requestAnimationFrame(paint);
  }
  function refreshMode(){
    const off=!enabled||reduced.matches;
    document.body.classList.toggle('workspace-motion-off',off);
    toggle.disabled=reduced.matches;toggle.setAttribute('aria-pressed',String(!off));
    const label=reduced.matches?'系统已减少动态效果':enabled?'关闭工作台动态效果':'开启工作台动态效果';toggle.setAttribute('aria-label',label);toggle.title=label;
    const canRun=!off&&!document.hidden&&!document.getElementById('login-dialog')?.hasAttribute('open')&&!document.getElementById('detail-dialog')?.hasAttribute('open');
    active=canRun;edgeCanvas.dataset.running=String(canRun);
    if(frame)cancelAnimationFrame(frame);frame=0;last=0;
    if(canRun){resize();frame=requestAnimationFrame(paint);}else{dust.clearRect(0,0,width,height);edge.clearRect(0,0,width,height);sparks=[];}
  }
  toggle.onclick=()=>{enabled=!enabled;try{localStorage.setItem('bank-workspace-motion',enabled?'on':'off');}catch{}refreshMode();};
  addEventListener('resize',()=>{resize();},{passive:true});
  addEventListener('scroll',scheduleRefresh,{passive:true});
  document.addEventListener('visibilitychange',refreshMode);reduced.addEventListener('change',refreshMode);
  const observer=new MutationObserver(records=>{if(records.some(record=>[...record.addedNodes].some(node=>node instanceof Element)))scheduleRefresh();});
  const shell=document.querySelector('.shell');if(shell)observer.observe(shell,{childList:true,subtree:true});
  for(const id of ['login-dialog','detail-dialog']){const modal=document.getElementById(id);if(modal)new MutationObserver(refreshMode).observe(modal,{attributes:true,attributeFilter:['open']});}
  resize();refreshMode();
}
