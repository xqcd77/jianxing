// @ts-check
/** @typedef {import('./api-contracts').PlanSnapshot} PlanSnapshot */
/** @param {unknown} value @returns {asserts value is PlanSnapshot} */
export function assertPlan(value){
  const p=/** @type {any} */(value);
  if(!p||typeof p.id!=='string'||!Number.isInteger(p.version)||!Array.isArray(p.nodes)||typeof p.status!=='string')throw new Error('办理计划响应不完整，请刷新核实');
  for(const n of p.nodes){
    if(typeof n.id!=='string'||typeof n.operation!=='string'||!Array.isArray(n.depends_on)||typeof n.status!=='string')throw new Error('办理步骤响应不完整，请刷新核实');
    if(!n.risk||!['READ_ONLY','CONFIRM','VERIFY','BLOCKED','VERIFYING','WAITING','FINISHED'].includes(n.risk.status)||!Array.isArray(n.risk.reasons)||!Array.isArray(n.risk.permitted_next_actions)||n.risk.plan_version!==p.version||n.risk.fingerprint!==n.fingerprint)throw new Error('风险核验信息不完整，请刷新后再办理');
  }
  const preview=p.funding_preview;
  if(!preview||!Number.isSafeInteger(preview.required_fen)||!Number.isSafeInteger(preview.before?.available_fen)||!Number.isSafeInteger(preview.after?.balance_fen))throw new Error('资金影响响应格式有误，请刷新核实');
  if(!preview.pressure||!Array.isArray(preview.pressure.events)||!Number.isSafeInteger(preview.pressure.minimum_available_fen))throw new Error('资金压力信息不完整，请刷新核实');
  if(!p.confirmation_batch||!Array.isArray(p.confirmation_batch.items)||typeof p.confirmation_batch.fingerprint!=='string')throw new Error('确认清单响应格式有误，请刷新核实');
}
/** @param {string} path @param {any} value */
export function assertResponse(path,value){
  if(value?.nodes)assertPlan(value);
  if(path==='/api/plans'&&Array.isArray(value))for(const p of value)assertPlan(p);
  if(path==='/api/overview'&&(!Number.isSafeInteger(value?.account?.balance_fen)||!Array.isArray(value?.holds)))throw new Error('账户响应格式有误，请刷新核实');
  if(path.startsWith('/api/businesses')&&value?.fields)assertAction(value);
  if(path==='/api/businesses'){if(!Array.isArray(value))throw new Error('业务目录格式有误');for(const c of value){if(typeof c.id!=='string'||!Array.isArray(c.actions))throw new Error('业务目录不完整');for(const a of c.actions)assertAction(a);}}
}
/** @param {any} action */
function assertAction(action){if(typeof action.id!=='string'||typeof action.label!=='string'||!Array.isArray(action.fields))throw new Error('业务事项响应不完整');for(const f of action.fields)if(typeof f.name!=='string'||typeof f.kind!=='string'||typeof f.required!=='boolean')throw new Error('业务表单响应格式有误');}
