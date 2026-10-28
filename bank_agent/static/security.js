// @ts-check
import {$,act,el,modal,toast} from "./ui.js";
import {planStore,sessionStore} from "./state.js";
import {api} from "./api.js";
import {overview} from "./account-view.js";
import {renderPlan} from "./plan-view.js";
import {clearSession} from "./app.js";

export function verificationField(input, purpose, binding={}) {
  const wrapper=el('div',undefined,'verification-field');
  input.placeholder=sessionStore.demoVerification?'6位演示验证码':'手机认证器6位验证码';
  input.setAttribute('aria-label',sessionStore.demoVerification?'演示验证码':'手机认证器验证码');
  input.inputMode='numeric';input.maxLength=6;input.autocomplete='one-time-code';
  const row=el('div',undefined,'verification-row');row.append(input);wrapper.append(row);
  const note=el('small',undefined,'verification-note');note.setAttribute('aria-live','polite');wrapper.append(note);
  let timer;
  input.clearVerification=()=>{
    clearInterval(timer);input.value='';delete input.dataset.challengeId;
    note.textContent=sessionStore.demoVerification?'每次操作请获取新的验证码。':'';
  };
  if(sessionStore.demoVerification){
    const get=el('button','获取演示验证码','demo-code-button');get.type='button';row.append(get);
    note.textContent='无需手机短信；2分钟有效，仅限本次操作。';
    get.onclick=()=>act(get,async(epoch)=>{
      const result=await api('/api/security/demo-code',{purpose,...binding});
      if(epoch!==sessionStore.epoch||!wrapper.isConnected)return;
      clearInterval(timer);input.value=result.code;input.dataset.challengeId=result.challenge_id;
      // Start from server-provided lifetime so an incorrect device clock is harmless.
      const expires=performance.now()+result.expires_in*1000;
      const update=()=>{
        if(!wrapper.isConnected){input.clearVerification();return;}
        const seconds=Math.max(0,Math.ceil((expires-performance.now())/1000));
        note.textContent=seconds?'已填入演示验证码 · 剩余 '+seconds+' 秒 · 只能使用一次':'验证码已过期，请重新获取。';
        if(!seconds){clearInterval(timer);input.value='';delete input.dataset.challengeId;}
      };
      update();timer=setInterval(update,1000);
    });
  }else note.textContent='请在已绑定的手机认证器中查看，每枚验证码只能使用一次。';
  return wrapper;
}

export function takeVerification(input) {
  if(!/^\d{6}$/.test(input.value))throw new Error(sessionStore.demoVerification?'请先获取演示验证码，或填写6位手机认证器验证码。':'请填写手机认证器中的6位验证码。');
  const payload={otp:input.value,...(input.dataset.challengeId?{demo_challenge_id:input.dataset.challengeId}:{})};
  input.clearVerification();return payload;
}

export async function openSecurity() {
  const root=modal('安全中心'),stamp=crypto.randomUUID();root.dataset.securityView=stamp;
  root.append(el('p','正在核对账户安全状态…','muted'));
  const epoch=sessionStore.epoch,status=await api('/api/security/status');
  if(epoch!==sessionStore.epoch||root.dataset.securityView!==stamp||!$('detail-dialog').open)return;
  sessionStore.demoVerification=status.demo_verification;root.replaceChildren();
  const summary=el('div',undefined,'security-status'+(status.locked?' is-locked':''));
  const title=el('strong',status.locked?'账户已安全锁定':'账户安全状态正常');summary.append(title);
  const reason=el('p',status.reason);summary.append(reason);root.append(summary);
  root.append(el('p','核验范围：账户锁定与本人的业务事件 · '+status.checked_at.slice(0,16).replace('T',' '),'risk-caption'));
  root.append(el('p',sessionStore.demoVerification?'当前为演示验证，可在本页获取验证码，无需真实短信或手机认证器。':'敏感操作通过登录会话与手机认证器共同验证。','muted'));

  const unlockSection=el('section',undefined,'security-section');unlockSection.dataset.securitySection='unlock';
  unlockSection.append(el('h3','解除安全锁定'),el('p','验证后解除保护，再回到原业务核对并继续办理。','muted'));
  const otp=el('input'),unlock=el('button','验证并解除安全锁定','primary');
  unlockSection.append(verificationField(otp,'unlock'),unlock);unlockSection.hidden=!status.locked;root.append(unlockSection);
  unlock.onclick=()=>act(unlock,async(nextEpoch)=>{
    await api('/api/security/unlock',takeVerification(otp));
    if(nextEpoch!==sessionStore.epoch)return;
    if(root.dataset.securityView===stamp&&$('detail-dialog').open){
      summary.classList.remove('is-locked');title.textContent='账户安全状态正常';
      reason.textContent='安全锁定已解除，可以继续办理业务。';unlockSection.hidden=true;
    }
    if(sessionStore.user)sessionStore.user.locked=false;
    toast('安全锁定已解除，回到原业务确认卡即可继续。');
    await overview();if(planStore.current)renderPlan(await api('/api/plans/'+planStore.current.id));
  });

  const changeSection=el('section',undefined,'security-section');changeSection.dataset.securitySection='password';
  changeSection.append(el('h3','修改登录密码'),el('p','修改后所有登录会话与未使用授权将失效。','muted'));
  const old=el('input'),fresh=el('input'),code=el('input');old.type=fresh.type='password';
  old.placeholder='原密码';old.autocomplete='current-password';fresh.placeholder='新密码（至少10位）';fresh.autocomplete='new-password';
  const change=el('button','验证并修改密码');
  change.onclick=()=>act(change,async()=>{
    if(!old.value||fresh.value.length<10)throw new Error('请填写原密码，新密码至少10位。');
    const payload={old_password:old.value,new_password:fresh.value,...takeVerification(code)};
    old.value=fresh.value='';await api('/api/security/password',payload);clearSession();toast('密码已修改，请重新登录。');
  });
  changeSection.append(old,fresh,verificationField(code,'password'),change);root.append(changeSection);

  const binding=el('details',undefined,'security-section');binding.append(el('summary',sessionStore.demoVerification?'手机认证器（可选绑定）':'绑定手机认证器'));
  binding.append(el('p','选择手机认证器时，在手机中添加绑定密钥。密钥仅显示一次，请完成绑定后关闭窗口。','muted'));
  const enroll=el('button','显示首次绑定信息');
  enroll.onclick=()=>act(enroll,async()=>{
    const result=await api('/api/security/enrollment');if(!binding.isConnected)return;
    binding.append(el('p','添加以下手动密钥；不要发给聊天助手。','muted'),el('pre',result.manual_secret));enroll.remove();
  });binding.append(enroll);root.append(binding);
  const history=el('section',undefined,'security-section security-risk-history');history.append(el('h3','最近的安全与办理事件'));
  if(!status.events.length)history.append(el('p','暂无已记录的相关事件。','muted'));
  for(const event of status.events){const row=el('article',undefined,'security-event');row.append(el('time',new Date(event.at).toLocaleString('zh-CN',{hour12:false})),el('strong',event.label));
    if(event.plan_id){const back=el('button','回到相关业务');back.onclick=()=>act(back,async(nextEpoch)=>{const data=await api('/api/plans/'+encodeURIComponent(event.plan_id));if(nextEpoch!==sessionStore.epoch||root.dataset.securityView!==stamp)return;$('detail-dialog').close();renderPlan(data,true);await overview();});row.append(back);}history.append(row);
  }root.append(history);
}
