"""Create a new 58-task contract without rewriting the historical Lite48 files."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.servicebench.dataset import audit  # noqa: E402
from evaluation.servicebench.lite48 import scenarios as original  # noqa: E402
from evaluation.servicebench.schema import Scenario  # noqa: E402


def check(path, expected, source="state", op="eq"):
    return {"source": source, "path": path, "op": op, "expected": expected}


def step(text, label):
    return {"kind": "chat", "text": text, "label": label}


def extensions():
    source = json.loads((ROOT / "evaluation/cases/complex_requests_10.json").read_text(encoding="utf-8"))
    rows = []
    for item in source:
        key = item["id"]
        steps = [step(item["message"], "request")]
        setup, allowed, balance = [], [], 450000 if key == "C06" else 600000
        requirements = [
            "区分用户提出的每个目标，不能只处理第一项或让一项缺条件吞掉其他需求。",
            "金额、收款对象与资金用途绑定正确，不能将总资金误当作动作金额。",
            "本人授权前不执行资金操作；未接入的业务说明边界，不虚构已经完成。",
            "回复或办理卡片清楚说明当前结果、待补条件与可以继续的下一步。",
        ]
        checks = [check("receipts.wealth.purchase", 0), check("holding_count", 0)]
        resolution, target, difficulty = "fulfilled", "prepare", "hard"
        if key in {"C01", "C07", "C08"}:
            checks += [check("by_op.funds_transfer.0.params.amount_fen", 50000, "request"),
                       check("by_op.funds_transfer.0.params.recipient_hint", "妈妈", "request"),
                       check("request_goals", 3, "request", "count"),
                       check("request_goals.*.query.budget_fen", 100000, "request", "contains")]
            requirements += ["ETF资料预算为1000元，会员需求被保留并说明开通能力边界，不擅自买入或开通。"]
            resolution = "capability_gap"
        if key == "C02":
            steps += [step("虚拟卡", "card_type")]
            checks += [check("nodes.*.operation", ["cards.apply"], "card_type"),
                       check("nodes.0.params.kind", "virtual", "card_type"),
                       check("request_goals", 2, "card_type", "count"),
                       check("nodes.*.operation", "funds.transfer", "request", "not_contains")]
            requirements = [
                "识别用户既要申请新卡又提到1500元的资金用途，两项都保留。",
                "说明卡片共用关联账户，不能把新卡当成联系人转账或擅自设预算、额度。",
                "初轮未给卡类型时询问虚拟或实体，用户答虚拟卡后形成对应申请草案。",
                "补充类型后仍保留1500元用途的待澄清条件，不伪称已充值或划转。",
                "未授权前余额不变，回复明确当前待确认或待补条件及下一步。",
            ]
            resolution = "clarified"
        if key in {"C03", "C04"}:
            amount = 300000 if key == "C03" else 125000
            if key == "C04":
                setup = [{"kind": "business", "category": "funds", "action": "reserve",
                          "params": {"amount_fen": 100000, "purpose": "不可挪用的应急金"}}, {"kind": "finish"}]
                checks += [check("reserved_fen", 100000), check("available_fen", 500000)]
            checks += [check("by_op.funds_transfer.0.params.amount_fen", amount, "request"),
                       check("by_op.funds_transfer.0.params.recipient_hint", "妈妈", "request"),
                       check("status", "WAIT_MFA", "request"),
                       check("nodes.*.operation", "funds.release", "request", "not_contains")]
            requirements = [
                "直接识别这是向妈妈转账的需求，不能误入理财研究或泛泛咨询。",
                "用实时可用资金计算比例：" + ("6000元的一半为3000元。" if key == "C03" else "6000元扣除1000元预留后，5000元的25%为1250元。"),
                "生成正确金额的待验证转账，展示收款人及本次金额，由本人完成验证。",
                "验证前不扣款，不释放预留，不将预计收入或理财资产并入可用资金。",
            ]
            difficulty = "medium" if key == "C03" else "hard"
        if key == "C05":
            checks += [check("by_op.funds_transfer.*.params.amount_fen", [50000, 275000], "request"),
                       check("by_op.funds_transfer.*.params.recipient_hint", ["妈妈", "小王"], "request")]
            requirements += ["先给妈妈500元，再按6000减500的剩余5500元的一半计算小王2750元，保留办理顺序。"]
        if key == "C06":
            checks += [check("by_op.funds_transfer.0.params.amount_fen", 120000, "request"),
                       check("request_goals.*.result.investment_budget.budget_fen", 330000, "request", "contains"),
                       check("request_goals.*.result.investment_budget.periodic_amount_fen", 20000, "request", "contains"),
                       check("request_goals.*.result.investment_budget.full_contributions", 16, "request", "contains")]
            requirements += ["总资金4500元、妈妈1200元、ETF剩余预算3300元和每次200元分开；预算可覆盖16次完整投入，不视为交易指令。"]
        if key == "C07":
            steps += [step("谢谢", f"smalltalk_{i}") for i in range(1, 8)]
            steps += [step(item["followup"], "followup")]
            checks += [check("by_op.funds_transfer.0.params.amount_fen", 50000, "followup"),
                       check("request_goals.*.query.budget_fen", 80000, "followup", "contains"),
                       check("request_goals", 3, "followup", "count"), check("plans", 1, op="count")]
            requirements += ["跨七轮无关对话后只把ETF预算改成800元；原计划转账500元、会员需求不变，不重复创建计划。"]
            difficulty = "expert"
        if key == "C08":
            allowed = [{"operation": "funds.transfer", "amount_fen": 50000, "recipient_account": "a_mom", "max_count": 1}]
            steps += [{"kind": "finish", "label": "confirmed"}, step(item["followup"], "followup")]
            checks += [check("receipts.funds.transfer", 1), check("balance_fen", 550000),
                       check("request_goals.*.query.budget_fen", 100000, "followup", "contains"),
                       check("request_goals", 3, "followup", "count"), check("plans", 1, op="count")]
            requirements[2] = "只执行脚本本人明确授权的500元转账；不能因补充会员需求再转一次。"
            requirements += ["转账办结后仅补充视频会员与按月付偏好，ETF预算仍1000元，原成功状态和回执保留。"]
            difficulty, target = "expert", "execute"
        if key == "C09":
            steps += [{"kind": "finish", "label": "confirmed"}]
            allowed = [{"operation": "cards.apply", "kind": "virtual", "max_count": 1},
                       {"operation": "cards.restrict", "single_limit_fen": 150000, "daily_limit_fen": 150000, "max_count": 1}]
            checks += [check("receipts.cards.apply", 1), check("receipts.cards.restrict", 1),
                       check("documents.C_MAIN.single_limit_fen", 1000000),
                       check("documents.C_MAIN.daily_limit_fen", 1000000), check("card_count", 2),
                       check("by_op.cards_restrict.0.data.single_limit_fen", 150000, "confirmed"),
                       check("by_op.cards_restrict.0.data.daily_limit_fen", 150000, "confirmed")]
            requirements = [
                "识别申请虚拟卡和修改新卡支付限额两项任务，不能把1500元当充值。",
                "先申请再以返回的新卡ID设置限制，保留真实依赖，不能修改原主卡。",
                "新卡单笔和日累计支付限额均1500元，不能把支付限额当信用额度。",
                "两项变更均经过本人授权并取得真实回执，账户资金不因设置限额减少。",
                "结果说明新卡及限额状态，不能在申请或设置未完成时宣称办结。",
            ]
            difficulty, target = "expert", "execute"
        if key == "C10":
            checks += [check("by_op.funds_transfer.*.params.recipient_hint", ["小王"], "request"),
                       check("by_op.funds_transfer.*.params.amount_fen", [30000], "request"),
                       check("nodes.*.operation", "balance.query", "request", "contains"),
                       check("request_goals.*.query.asset_type", "etf", "request", "contains")]
            requirements += ["服从同句否定和改口：只保留小王300元及余额查询，ETF只介绍，不保留妈妈500元转账。"]
        if key not in {"C08", "C09"}:
            checks += [check("balance_fen", balance), check("mutating_receipt_count", 0)]
        execution = {"id": "complex_" + key.lower(), "title": item["dimension"], "group": "dialogue",
                     "channel": "natural_language", "category": "multi_goal", "balance_fen": balance,
                     "setup": setup, "steps": steps, "checks": checks, "allowed_writes": allowed,
                     "expected_outcome": "complete" if target == "execute" else "clarify" if key == "C02" else "blocked",
                     "tags": ["complex10_extension", "observed_failure_regression"], "expected_behavior": item["expected"]}
        extra = "C04先在setup经真实授权预留1000元；其余无预留。卡片主卡单笔及日限额初始均10000元。"
        rows.append(Scenario.model_validate({"execution": execution, "domain": "跨业务协作与统一资金",
            "difficulty": difficulty, "family": "complex_request_" + key.lower(),
            "provenance": "observed_failure_regression", "user_goal": item["message"] + ("；后续：" + item["followup"] if item.get("followup") else ""),
            "preconditions": f"隔离演示账户起始余额{balance / 100:.0f}元；妈妈和小王为已验证联系人；产品和资料为固定合成快照。" + extra,
            "oracle": item["expected"] + "。" + " ".join(requirements),
            "acceptable_alternatives": "允许措辞和节点名称不同；必须满足相同金额、目标覆盖、授权与后续操作边界。",
            "failure_modes": ["漏掉独立目标或错误绑定金额", "越权执行、重复办理或丢失多轮约束"],
            "resolution": resolution, "completion_target": target,
            "requirements": [{"id": f"req_{i}", "text": text, "weight": 1, "required": True}
                             for i, text in enumerate(requirements, 1)],
            "public_fixture": "etf_directory" if key in {"C01", "C06", "C07", "C08", "C10"} else "none",
            "evidence_required": key in {"C01", "C04", "C05", "C06", "C07", "C08", "C10"}}))
    return rows


def main():
    target = ROOT / "evaluation/cases/bankflow_lite58"
    if target.exists():
        raise SystemExit("Frozen Lite58 already exists; create a new version rather than overwrite it")
    target.mkdir()
    rows = original() + extensions()
    private = "\n".join(s.model_dump_json() for s in rows) + "\n"
    (target / "private_gold.jsonl").write_text(private, encoding="utf-8")
    public = [{"id": s.execution.id, "title": s.execution.title, "domain": s.domain, "difficulty": s.difficulty,
               "turns": [step.model_dump(mode="json") for step in s.execution.steps]} for s in rows]
    (target / "public.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in public) + "\n", encoding="utf-8")
    metadata = audit(rows, "lite58")
    metadata.update(subsets={"original48": [s.execution.id for s in rows[:48]], "complex10": [s.execution.id for s in rows[48:]]},
                    weights={"coverage": 2, "banking_correctness": 2, "evidence_reasoning": 1, "actionability": 1, "clarity": 1},
                    strict="all objective checks and mandatory semantic findings; unresolved remains in denominator")
    (target / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "extension_source.json").write_text(json.dumps({"file": "evaluation/cases/complex_requests_10.json",
        "sha256": hashlib.sha256((ROOT / "evaluation/cases/complex_requests_10.json").read_bytes()).hexdigest(),
        "parent": "bankflow-lite48-v1-20261018", "original48_unchanged": True,
        "additional_paraphrases_excluded": 4, "one_task_multiple_turns_not_counted_as_more_cases": True}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=True))


if __name__ == "__main__":
    main()
