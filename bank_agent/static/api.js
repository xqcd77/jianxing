// @ts-check
import {sessionStore} from './state.js';
import {assertResponse} from './contracts.js';
/** @param {string} path @param {any} [body] @returns {Promise<any>} */
export async function api(path,body){
  const epoch=sessionStore.epoch,token=sessionStore.token;
  const controller=body===undefined?new AbortController():null;
  if(controller)sessionStore.controllers.add(controller);
  try{
    const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},...(controller?{signal:controller.signal}:{}),...(body===undefined?{}:{body:JSON.stringify(body)})});
    let data;try{data=await response.json();}catch{throw new Error('服务响应异常，请刷新核实结果');}
    if(!response.ok){
      if(response.status===401&&data.error?.code==='UNAUTHENTICATED'&&epoch===sessionStore.epoch&&token===sessionStore.token)sessionStore.onExpired?.();
      const error=Object.assign(new Error(data.error?.message||'请求失败，请核对输入'),{code:data.error?.code});throw error;
    }
    assertResponse(path,data);return data;
  }finally{if(controller)sessionStore.controllers.delete(controller);}
}
/** @param {string} id @returns {Promise<import('./api-contracts').PlanSnapshot>} */
export function getPlan(id){return api('/api/plans/'+encodeURIComponent(id));}

/** @param {string} taskId @param {(task:any)=>Promise<void>} update @param {AbortSignal} [signal] */
export async function watchChatTask(taskId,update,signal){
  const controller=new AbortController(),epoch=sessionStore.epoch;
  const detach=()=>controller.abort();
  if(signal?.aborted)controller.abort();else signal?.addEventListener('abort',detach,{once:true});
  sessionStore.controllers.add(controller);
  try{
    const response=await fetch('/api/chat/tasks/'+encodeURIComponent(taskId)+'/events',{
      headers:{Authorization:'Bearer '+sessionStore.token},signal:controller.signal});
    if(!response.ok||!response.body)throw new Error('任务进度连接暂不可用，可以恢复查看');
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
    while(epoch===sessionStore.epoch){
      const {value,done}=await reader.read();if(done)return;
      buffer+=decoder.decode(value,{stream:true});let end;
      while((end=buffer.indexOf('\n'))>=0){
        const line=buffer.slice(0,end);buffer=buffer.slice(end+1);if(!line)continue;
        const event=JSON.parse(line);
        if(event.type==='session_expired'){sessionStore.onExpired?.();return;}
        if(event.type==='progress')await update(event.task);
      }
    }
  }finally{controller.abort();sessionStore.controllers.delete(controller);signal?.removeEventListener('abort',detach);}
}
