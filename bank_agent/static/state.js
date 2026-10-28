// @ts-check
/** @typedef {import('./api-contracts').PlanSnapshot} PlanSnapshot */
/** @type {{token:string,user:import('./api-contracts').PublicUser|null,epoch:number,busy:number,liveController:AbortController|null,demoVerification:boolean,onExpired:(()=>void)|null,controllers:Set<AbortController>}} */
export const sessionStore={token:sessionStorage.getItem('bank-token')||'',user:null,epoch:0,busy:0,liveController:null,demoVerification:false,onExpired:null,controllers:new Set()};
/** @type {{id:string|null,generation:number,modelId:string|null}} */
export const conversationStore={id:null,generation:0,modelId:null};
/** @type {{current:PlanSnapshot|null,liveSignature:string,cache:Map<string,PlanSnapshot>}} */
export const planStore={current:null,liveSignature:'',cache:new Map()};
/** @type {{modalVersion:number,businessVersion:number,selectedBusiness:string|null}} */
export const formStore={modalVersion:0,businessVersion:0,selectedBusiness:null};
/** @type {{facts:any,capabilities:any[]}} */
export const accountStore={facts:null,capabilities:[]};
/** @type {{current:any,timer:ReturnType<typeof setTimeout>|null}} */
export const researchStore={current:null,timer:null};
/** @type {Map<string,number>} */
const requests=new Map();
/** @param {string} scope */
export function ticket(scope){if(scope==='plan-action')ticket('plan-snapshot');const sequence=(requests.get(scope)||0)+1;requests.set(scope,sequence);return {scope,sequence,epoch:sessionStore.epoch};}
/** @param {{scope:string,sequence:number,epoch:number}} mark */
export function isCurrent(mark){return mark.epoch===sessionStore.epoch&&requests.get(mark.scope)===mark.sequence;}
export function beginIntent(){conversationStore.generation++;ticket('plan-snapshot');return ticket('foreground');}
export function clearReads(){for(const controller of sessionStore.controllers)controller.abort();sessionStore.controllers.clear();requests.clear();}
