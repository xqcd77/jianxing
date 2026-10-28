from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet
import pyotp
from sqlalchemy import delete, select

from .errors import BankError
from .storage import AuditEvent, Document, Grant, LoginSession, Node, Plan, User, audit, canonical, now_iso, save_document, uid


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240_000).hex()
    return f"{salt}:{digest}"


def password_matches(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(":")[0]), stored)


def action_fingerprint(user_id, plan_id, version, node_id, operation, params):
    return hashlib.sha256(
        canonical(
            {
                "user": user_id,
                "plan": plan_id,
                "version": version,
                "node": node_id,
                "operation": operation,
                "params": params,
            }
        ).encode()
    ).hexdigest()


class AuthService:
    def __init__(self, db, data_dir, settings=None):
        self.db = db
        self.settings = settings
        self.clock = lambda: datetime.now(timezone.utc)
        key_path = data_dir / "auth.key"
        from .locking import ProcessLocks
        with ProcessLocks(data_dir).hold("bootstrap:auth_key"):
            if not key_path.exists():
                with key_path.open("xb") as output:
                    output.write(Fernet.generate_key())
            self.fernet = Fernet(key_path.read_bytes())

    def new_totp(self):
        return self.fernet.encrypt(pyotp.random_base32().encode()).decode()

    def login(self, username, password):
        with self.db.write() as session:
            user = session.scalar(select(User).where(User.username == username))
            if user is None or not password_matches(password, user.password_hash):
                raise BankError("INVALID_CREDENTIALS", "用户名或密码错误", 401)
            token = secrets.token_urlsafe(40)
            expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            session.add(
                LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, expires_at=expires)
            )
            audit(session, user.id, "LOGIN", {"username": username})
            return {"access_token": token, "token_type": "bearer", "user": self.public_user(user)}

    def authenticate(self, token):
        with self.db.read() as session:
            login = session.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
            if login is None or login.expires_at <= now_iso():
                raise BankError("UNAUTHENTICATED", "请先登录或重新登录", 401)
            user = session.get(User, login.user_id)
            return self.public_user(user)

    def logout(self, token):
        with self.db.write() as session:
            login = session.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
            if login:
                audit(session, login.user_id, "LOGOUT", {"session_revoked": True})
                session.delete(login)
        return {"logged_out": True}

    @staticmethod
    def public_user(user):
        return {
            "id": user.id,
            "username": user.username,
            "name": user.name,
            "locked": user.locked,
            "risk_level": user.risk_level,
            "demo_admin": user.is_demo_admin,
        }

    def enrollment(self, user_id):
        with self.db.write() as session:
            enrollment_id = f"mfa_enrollment_{user_id}"
            if session.get(Document, enrollment_id):
                raise BankError(
                    "MFA_ALREADY_ENROLLED",
                    "绑定密钥只显示一次，请使用已绑定的手机认证器。演示遗失密钥时请切换新数据目录。",
                    409,
                )
            user = session.get(User, user_id)
            secret = self.fernet.decrypt(user.totp_encrypted.encode()).decode()
            save_document(session, enrollment_id, "mfa_enrollment", user_id, {"delivered_at": now_iso()})
            audit(session, user_id, "MFA_ENROLLMENT_DELIVERED", {"factor": "phone_totp", "one_time_delivery": True})
            # Enrollment is delivered only to the authenticated owner, never to an agent.
            return {
                "otpauth_uri": pyotp.TOTP(secret).provisioning_uri(user.username, issuer_name="简行演示银行"),
                "manual_secret": secret,
                "message": "在手机认证器中添加；验证码不应发送给聊天模型。",
            }

    def demo_allowed(self, user):
        return bool(
            self.settings
            and self.settings.demo_enabled
            and self.settings.demo_verification_enabled
            and (user.id, user.username) in {("u_demo", "demo"), ("u_wang", "wang"), ("u_li", "li")}
        )

    def security_status(self, user_id):
        with self.db.read() as session:
            user = session.get(User, user_id)
            circuit = session.get(Document, f"circuit_{user_id}")
            suspicion = session.get(Document, f"suspicion_{user_id}")
            reasons = []
            if user.locked:
                if user.failed_verifications >= 3:
                    reasons.append("连续验证码验证失败，已触发安全锁定。")
                if circuit and circuit.data.get("failures", 0) >= 3:
                    reasons.append("多次业务办理失败，已触发安全保护。")
                if suspicion and suspicion.data.get("attempts", 0) >= 3:
                    reasons.append("检测到多次可疑请求，已触发安全保护。")
            labels = {"MFA_VERIFIED": "身份验证通过", "MFA_FAILED": "身份验证未通过",
                "SECURITY_UNLOCKED": "安全锁定已解除", "SECURITY_LOCKED": "敏感操作安全暂停",
                "CIRCUIT_OPEN": "业务通道保护已开启", "PLAN_EDITED": "办理安排已修改，原授权失效",
                "PLAN_REPLANNED": "办理计划已更新，剩余步骤重新核对",
                "BANK_SUBMISSION_UNKNOWN": "已提交交易正在核实", "BANK_RESULT_STILL_UNKNOWN": "继续核实原交易结果",
                "YELLOW_BATCH_APPROVED": "普通操作清单已确认", "OPERATION_REJECTED": "本人选择不办理此步"}
            events = list(session.scalars(select(AuditEvent).where(AuditEvent.user_id == user_id,
                AuditEvent.kind.in_(labels)).order_by(AuditEvent.seq.desc()).limit(12)))
            return {
                "locked": user.locked,
                "reason": " ".join(reasons) or ("敏感操作已暂停，请验证后返回原业务重新核对。" if user.locked
                    else "当前未触发账户安全锁定；每笔业务仍会单独核验授权与办理条件。"),
                "checked_at": self.clock().isoformat(),
                "events": [{"label": labels[e.kind], "at": e.created_at, "plan_id": e.plan_id} for e in events],
                "demo_verification": self.demo_allowed(user),
                "demo_code_ttl_seconds": 120,
            }

    @staticmethod
    def _demo_digest(challenge_id, code):
        return hashlib.sha256((challenge_id + ":" + code).encode()).hexdigest()

    def issue_demo_code(self, user_id, purpose, plan_id=None, node_id=None, fingerprint=None):
        # A simulated delivery channel, never an agent tool or a real second factor.
        with self.db.write() as session:
            user = session.get(User, user_id)
            if not self.demo_allowed(user):
                raise BankError("DEMO_VERIFICATION_DISABLED", "当前环境未开启演示验证码，请使用手机认证器", 403)
            if purpose not in {"unlock", "approve", "password"}:
                raise BankError("INVALID_VERIFICATION_PURPOSE", "验证码用途无效")
            if user.locked and purpose != "unlock":
                raise BankError("SECURITY_LOCKED", "账户已安全锁定，请先在安全中心解除锁定", 423)
            binding = {}
            if purpose == "approve":
                plan = session.get(Plan, plan_id) if plan_id else None
                node = session.get(Node, node_id) if node_id else None
                if not plan or plan.user_id != user_id or not node or node.plan_id != plan.id:
                    raise BankError("PLAN_NOT_FOUND", "计划不存在或无权访问", 404)
                if plan.status in {"CANCELLED", "COMPLETED"} or node.status != "WAIT_MFA" or node.tier != "RED":
                    raise BankError("NOT_AWAITING_APPROVAL", "此步骤当前不需要验证码", 409)
                if not fingerprint or fingerprint != node.fingerprint:
                    raise BankError("STALE_APPROVAL", "业务详情已变化，请刷新安排后重新获取验证码", 409)
                binding = {"plan_id": plan.id, "plan_version": plan.version, "node_id": node.id, "fingerprint": fingerprint}
            elif any([plan_id, node_id, fingerprint]):
                raise BankError("INVALID_VERIFICATION_PURPOSE", "解锁和改密验证码不接受交易参数")
            # One current challenge per purpose and operation; replacements revoke the old one.
            challenge_id = uid("demo_code")
            document_id = f"demo_verification_{user_id}_{purpose}_{node_id or 'account'}"
            code = f"{secrets.randbelow(1_000_000):06d}"
            expires = (self.clock() + timedelta(seconds=120)).isoformat()
            save_document(session, document_id, "demo_verification", user_id, {
                "challenge_id": challenge_id, "purpose": purpose, "binding": binding,
                "code_hash": self._demo_digest(challenge_id, code), "expires_at": expires, "used": False,
            })
            audit(session, user_id, "DEMO_CODE_ISSUED", {"purpose": purpose, "simulated": True, "expires_at": expires}, plan_id or "")
            return {"challenge_id": challenge_id, "code": code, "expires_at": expires,
                    "expires_in": 120, "verification_mode": "demo", "message": "演示验证码已生成，2分钟内有效，仅限本次用途。"}

    @staticmethod
    def _failed_verification(session, user, factor):
        user.failed_verifications += 1
        user.locked = user.failed_verifications >= 3 or user.locked
        audit(session, user.id, "MFA_FAILED", {"attempts": user.failed_verifications, "locked": user.locked, "factor": factor})
        return False

    def verify_factor(self, session, user, otp, *, purpose, challenge_id=None, binding=None, allow_locked=False):
        if not challenge_id:
            return self.verify_totp(session, user, otp, allow_locked=allow_locked)
        if user.locked and not allow_locked:
            raise BankError("SECURITY_LOCKED", "账户已安全锁定，请先在安全中心解除锁定", 423)
        if not self.demo_allowed(user):
            raise BankError("DEMO_VERIFICATION_DISABLED", "当前环境未开启演示验证码，请使用手机认证器", 403)
        doc = session.get(Document, f"demo_verification_{user.id}_{purpose}_{(binding or {}).get('node_id', 'account')}")
        data = doc.data if doc and doc.owner_id == user.id else {}
        valid = bool(
            data.get("challenge_id") == challenge_id
            and not data.get("used", True)
            and data.get("purpose") == purpose
            and data.get("binding") == (binding or {})
            and data.get("expires_at", "") > self.clock().isoformat()
            and hmac.compare_digest(data.get("code_hash", ""), self._demo_digest(challenge_id, otp))
        )
        if not valid:
            return self._failed_verification(session, user, "demo_code")
        doc.data = {**data, "used": True, "used_at": self.clock().isoformat()}
        user.failed_verifications = 0
        audit(session, user.id, "MFA_VERIFIED", {"factor": "demo_code", "purpose": purpose, "simulated": True}, (binding or {}).get("plan_id", ""))
        return True

    def verify_totp(self, session, user, otp, allow_locked=False):
        if user.locked and not allow_locked:
            raise BankError("SECURITY_LOCKED", "敏感操作已锁定，请在安全中心通过第二因子解锁", 423)
        secret = self.fernet.decrypt(user.totp_encrypted.encode()).decode()
        totp = pyotp.TOTP(secret)
        counter = int(self.clock().timestamp()) // 30
        matched = next((value for value in [counter, counter - 1] if totp.at(value * 30) == otp), None)
        if matched is None or matched <= user.last_totp_counter:
            return self._failed_verification(session, user, "phone_totp")
        user.last_totp_counter = matched
        user.failed_verifications = 0
        audit(session, user.id, "MFA_VERIFIED", {"factor": "phone_totp"})
        return True

    def unlock(self, user_id, otp, challenge_id=None):
        valid = False
        with self.db.write() as session:
            user = session.get(User, user_id)
            valid = self.verify_factor(session, user, otp, purpose="unlock", challenge_id=challenge_id, allow_locked=True)
            if valid:
                user.locked = False
                save_document(session, f"circuit_{user_id}", "circuit", user_id, {"failures": 0})
                save_document(session, f"suspicion_{user_id}", "suspicion", user_id, {"attempts": 0})
                audit(session, user.id, "SECURITY_UNLOCKED", {"actor": user_id})
        if not valid:
            raise BankError("INVALID_OTP", "验证码错误、过期或已使用", 401)
        return {"unlocked": True}

    def change_password(self, user_id, old_password, new_password, otp, challenge_id=None):
        if len(new_password) < 10:
            raise BankError("WEAK_PASSWORD", "新密码至少10位")
        valid = False
        with self.db.write() as session:
            user = session.get(User, user_id)
            if not password_matches(old_password, user.password_hash):
                raise BankError("INVALID_CREDENTIALS", "原密码错误", 401)
            valid = self.verify_factor(session, user, otp, purpose="password", challenge_id=challenge_id)
            if valid:
                user.password_hash = password_hash(new_password)
                session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
                for grant in session.scalars(select(Grant).where(Grant.user_id == user_id)):
                    grant.consumed = True
                audit(session, user_id, "PASSWORD_CHANGED", {"sessions_revoked": True})
        if not valid:
            raise BankError("INVALID_OTP", "验证码错误、过期或已使用", 401)
        return {"changed": True, "message": "密码已修改，旧会话与授权已撤销，请重新登录"}

    @staticmethod
    def create_grant(session, user_id, node_id, fingerprint, tier):
        grant = Grant(
            id=uid("grant"),
            user_id=user_id,
            node_id=node_id,
            fingerprint=fingerprint,
            tier=tier,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            consumed=False,
        )
        session.add(grant)
        audit(session, user_id, "OPERATION_AUTHORIZED", {"node_id": node_id, "fingerprint": fingerprint, "tier": tier})
        session.flush()
        return grant
