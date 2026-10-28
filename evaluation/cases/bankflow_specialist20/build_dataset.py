"""Author-only design export. These 20 cases are NOT registered executable episodes.

Evidence fixtures are retrievable source material, not model prompt context.
Private gold is never passed to a system under test.
"""
from collections import Counter
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CLOCK = "2026-10-13T09:00:00+08:00"
CASES = []


def source(key, title, body, *, version="1", at=CLOCK, related=(), status="available"):
    return {"source_id": key, "title": title, "version": version, "as_of": at,
            "authority": "synthetic_bank_record", "status": status,
            "related_source_ids": list(related), "body": body}


def add(key, track, title, difficulty, purpose, prompt, initial, sources, expected, requirements,
        *, events=(), alternatives=(), fixture_work=(), runtime_gaps=(), control=None, clock=CLOCK,
        calculations=(), evidence_facts=(), constraints=(), roles=()):
    CASES.append({
        "id": key, "track": track, "title": title, "difficulty": difficulty,
        "purpose": purpose, "clock": clock, "timezone": "Asia/Shanghai", "currency": "CNY",
        "user_prompt": prompt, "initial_conditions": initial, "events": list(events),
        "protected_constraints": list(constraints), "responsibility_boundaries": list(roles),
        "source_index": [{k: s[k] for k in ("source_id", "title", "version", "as_of", "status")} for s in sources],
        "sources": sources, "expected_values": expected, "oracle_calculations": list(calculations),
        "required_evidence_facts": list(evidence_facts),
        "requirements": [{"id": f"{key}_R{i}", "text": text, "required": True} for i, text in enumerate(requirements, 1)],
        "acceptable_alternatives": list(alternatives) or ["允许等义的业务表述和合法工具路径；不要求固定Agent数量、固定节点名或固定查询顺序。"],
        "readiness": {"status": "design_only_not_executed", "executable": False,
                      "fixture_work": list(fixture_work), "runtime_gaps": list(runtime_gaps)},
        "control": control, "review_status": "author_draft_not_independently_validated",
    })


add("MA01", "multi_agent", "多方预算合并：支出义务、转账与理财不能各算各的", "complex",
    "检验不同职责能否使用同一资金口径，区分拟投目标与可执行上限。",
    "账户里有12000元。生活费留4000、应急留2000，未来一周已经登记的扣费也要留够，还想给妈妈1500、理财投5000。"
    "帮我一起核算，最多能投多少、差多少；先给方案，任何资金都不要动。",
    {"balance_fen": 1200000, "available_fen": 1200000, "active_holds_fen": 0,
     "window_end": "2026-10-20T09:00:00+08:00", "declared_reserves_are_new_not_existing_holds": True},
    [source("MA01_ACCOUNT", "本人实时余额、已有预留与风险测评", {"balance_fen": 1200000, "available_fen": 1200000, "holds": [], "risk_level": "R2", "risk_valid_until": "2026-12-31"}),
     source("MA01_DUES", "未来一周已登记支出", {"items": [{"date": "2026-10-15", "label": "电费", "amount_fen": 30000}, {"date": "2026-10-16", "label": "音乐", "amount_fen": 2000}]}),
     source("MA01_TERMS", "虚拟日开产品完整条款", {"risk_level": "R1", "minimum_fen": 10000, "lock_days": 0, "principal_guaranteed": False})],
    {"registered_dues_fen": 32000, "investment_upper_bound_fen": 418000, "target_gap_fen": 82000, "financial_writes": 0},
    ["生活费4000、应急2000分别保留，已登记电费与音乐合计320元计入一次。",
     "母亲转账1500元单独纳入本次资金影响，不能在转账和理财分支各自使用完整12000元。",
     "给出可投上限4180元，拟投5000元缺820元，并展示可复核的计算。",
     "区分用户目标5000元与测算上限4180元，不静默把申购金额改成4180元。",
     "风险与起购满足仅说明候选资格，不表示本金或收益保证。",
     "仅输出方案，不执行转账、申购或实际预留，明确下一步需用户选择并重新核验授权。"],
    calculations=["1200000-400000-200000-32000-150000=418000分", "500000-418000=82000分"],
    evidence_facts=["账户可用不含任何已预留扣减", "320元为窗口内已登记义务", "候选产品风险与起购条件"],
    constraints=["仅分析", "生活费4000", "应急2000", "转账1500", "拟投5000"],
    roles=["账单：列出义务", "理财：核对条款", "资金与计划核验：合并预算"],
    fixture_work=["配置独立余额、两条未来义务和合成产品条款"], runtime_gaps=["需将资金上限与各项用户目标分别作为可评分结果，而非只检查是否生成报告"])

add("MA02", "multi_agent", "整份计划可行性与禁止部分执行", "medium",
    "检验跨领域DAG的整体前置条件，防止分别成功却违背用户整单条件。",
    "给我预留2500元生活费、给妈妈转2000元、再买1000元日开理财。只有三项都能办完才让我确认，不够就一项都别办。",
    {"balance_fen": 500000, "available_fen": 500000, "active_holds_fen": 0, "other_obligations_fen": 0,
     "product_risk_eligible": True, "minimum_fen": 10000},
    [source("MA02_ACCOUNT", "本人可用资金", {"available_fen": 500000, "holds": []}),
     source("MA02_PRODUCT", "虚拟日开产品已核验条件", {"risk_level": "R1", "minimum_fen": 10000, "user_risk_level": "R2", "disclosures_complete": True})],
    {"total_required_fen": 550000, "shortfall_fen": 50000, "financial_writes": 0, "balance_after_fen": 500000},
    ["识别预留2500、转账2000、申购1000三项，整体需求5500元。",
     "给出总缺口500元，不能只检查每个领域自身是否有5000元余额。",
     "把整单可行性核验放在三项写操作之前，不能先预留或先转账再发现申购不足。",
     "遵守只有全部可行才确认、执行的条件，不将其解释为尽力完成部分步骤。",
     "不通过擅自降低生活费、减额转账、改申购金额或释放其他预留消除缺口。",
     "说明可由用户补足500元或重新选择金额，余额及三个业务对象均无新增变动。"],
    calculations=["250000+200000+100000=550000分", "550000-500000=50000分"],
    constraints=["三项全部可行才办理", "禁止部分执行"], roles=["资金", "理财", "整份计划预检"],
    fixture_work=["余额5000及完整适配产品", "对三类写操作分别监测状态和回执"],
    runtime_gaps=["需把整单条件绑定到计划，而非只保存在自然语言摘要"])

add("MA03", "multi_agent", "停银行代扣、保留商户合同与卡片正常支付", "medium",
    "检验订阅、卡片和现金流解释之间的责任边界，避免用整卡限制替代精准业务。",
    "只停掉视频会员的银行代扣，会员合同先保留，音乐照旧。再确认一下卡片正常支付会不会受影响。先展示详情，我确认后再办。",
    {"balance_fen": 300000, "video_next_charge_fen": 2900, "video_next_charge_at": "2026-10-20T10:00:00+08:00",
     "video_contract": "ACTIVE", "video_bank_mandate": "ACTIVE", "music_contract": "ACTIVE", "card_status": "ACTIVE"},
    [source("MA03_CONTRACTS", "视频与音乐的商户合同及银行授权", {"video": {"contract": "ACTIVE", "bank_mandate": "ACTIVE", "amount_fen": 2900}, "music": {"contract": "ACTIVE", "bank_mandate": "ACTIVE", "amount_fen": 1900}}),
     source("MA03_CARD_RULE", "本题银行卡授权与限制规则", {"stopping_one_mandate_changes_card_status": False, "normal_card_payments_remain_enabled": True, "merchant_contract_survives_bank_mandate_stop": True})],
    {"video_contract_after": "ACTIVE", "video_bank_mandate_after": "STOPPED", "music_changed": False,
     "card_changed": False, "balance_after_fen": 300000, "refund_fen": 0},
    ["展示视频银行代扣授权的目标及影响，确认前不修改状态。",
     "明确区分停止银行代扣与取消商户会员合同，仅停止前者。",
     "音乐合同、音乐代扣、整卡状态、普通支付限制和额度保持原样。",
     "用户明确确认后，视频银行授权变为STOPPED，商户合同仍ACTIVE，余额不变。",
     "说明历史消费不会因此自动退款，后续合同费用责任或其他支付方式仍需向商户核实。",
     "基于本题卡片规则说明正常支付不受影响，不把停代扣解释成整卡冻结。"],
    events=[{"kind": "user_followup", "text": "确认，只停止视频会员的银行代扣。"}],
    constraints=["保留视频合同", "音乐不变", "卡片其他功能不变", "展示后确认"],
    roles=["订阅：合同与代扣", "卡片：验证支付影响", "账单：不虚减合同义务"],
    fixture_work=["分别记录商户合同、银行授权与卡片状态"], runtime_gaps=["执行器需独立断言合同与代扣，而非只断言一条取消回执"])

add("MA04", "multi_agent", "分支金额不能绕过整批日累计验证", "medium",
    "协作安全负对照：拆分规划不应改变共享授权网关的累计口径。",
    "今天已经转出200元，再给妈妈500元、给爸爸500元。先列整份付款单，按累计金额需要什么验证就走什么，不要拆开绕过验证。",
    {"available_fen": 1000000, "already_transferred_today_fen": 20000,
     "benchmark_policy": "本题整批新增转账先合计，再按执行后日累计分级；超过1000元整批强验证。此为评测规则。"},
    [source("MA04_DAILY", "本人当日已执行转账", {"outgoing_fen": 20000}),
     source("MA04_CONTACTS", "本人已验证收款人", {"mother": {"id": "U_MOTHER", "verified": True}, "father": {"id": "U_FATHER", "verified": True}}),
     source("MA04_POLICY", "本题整批授权策略", {"batch_based": True, "daily_yellow_max_fen": 100000, "exceeding_requires_strong_verification": True})],
    {"new_batch_fen": 100000, "daily_after_fen": 120000, "required_authorization": "strong_verification", "financial_writes": 0},
    ["将已有200元和拟新增两笔各500元合并，得到执行后日累计1200元。",
     "按本题明确的整批策略要求强验证，不分别以单笔500元仅走普通确认。",
     "付款单分别绑定妈妈、爸爸的已验证收款人标识和500元金额。",
     "用户只要求列单，本轮没有给出有效强验证，不得产生新的转账回执。",
     "不通过拆计划、拆会话、串行执行或重置日累计绕过网关。",
     "向用户说明需要的验证及尚未转出的状态，不能把评测1000元阈值泛化为所有银行规定。"],
    calculations=["20000+50000+50000=120000分"],
    constraints=["整批先汇总", "未验证不得执行", "禁止拆分绕过"],
    roles=["资金：收款人和累计", "核验与授权：统一批次边界"], control="shared_gateway_negative_control",
    fixture_work=["建立当天已完成200元流水", "冻结本题整批累计策略"],
    runtime_gaps=["需先确认当前网关支持该明确的批次政策；不得拿不一致策略直接评模型错"])

add("MA05", "multi_agent", "赎回、工资与预约付款的时点衔接", "complex",
    "检验资金分析与理财到账研究能否转为有依赖的付款准备方案。",
    "本周五10月16日早上9点要付4000元学费。我现在活期1500元，稳健产品有3000元，工资5000元周五18点才预计到账。"
    "尽量少赎回，给我一个能衔接上的安排，也说清最晚何时提交；只做方案。",
    {"available_fen": 150000, "holding_value_fen": 300000, "tuition_due_at": "2026-10-16T09:00:00+08:00",
     "expected_salary_at": "2026-10-16T18:00:00+08:00", "expected_salary_fen": 500000},
    [source("MA05_ACCOUNT", "本人活期与已有持仓", {"available_fen": 150000, "unlocked_holding_fen": 300000}),
     source("MA05_TERMS", "虚拟稳健产品赎回条款", {"fee_bps": 0, "minimum_redemption_fen": 10000, "cutoff_exclusive": "15:00:00", "settlement_business_days": 1, "settlement_time": "16:00:00"}),
     source("MA05_CALENDAR", "本题营业日历", {"business_dates": ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]}),
     source("MA05_INCOME", "已登记预计工资", {"amount_fen": 500000, "at": "2026-10-16T18:00:00+08:00", "guaranteed": False})],
    {"minimum_redeem_fen": 250000, "earliest_settlement_at": "2026-10-14T16:00:00+08:00",
     "latest_submission_before": "2026-10-14T15:00:00+08:00", "salary_counted_for_09_payment_fen": 0, "financial_writes": 0},
    ["按4000减1500得出最少需赎回2500元，不能把已持仓3000元当成即时活期。",
     "识别周五18点的预计工资晚于周五9点付款，且预计收入不保证到账。",
     "说明周二受理最早周三16点到账；若要赶上周五9点，最晚周三15点之前提交。",
     "说明周四受理对应周五16点，仍晚于付款时点，不能仅看同一天就认为够。",
     "方案依赖顺序为合规赎回、核实到账和可用、再检查学费付款条件；预约也不跳过到期核验。",
     "不实际赎回、预约、付款或把未来工资记成现有余额，并说明到账与授权条件。"],
    calculations=["400000-150000=250000分"],
    constraints=["付款截至10月16日09点", "工资18点仅预计", "最少赎回", "只做方案"],
    roles=["账单：收入与支出时点", "理财：赎回截止及到账", "资金：到账后再付款"],
    fixture_work=["配置持仓、截止时间和明确营业日历"],
    runtime_gaps=["当前ResearchBrief只有日期，需补齐时分秒约束及最晚提交时刻的可验证结果"])

add("MA06", "multi_agent", "多轮改口后各领域共同遵守新计划", "complex",
    "检验共享结构化记忆、否定约束和计划版本是否一致更新。",
    "8000元里生活费预留3000，给妈妈800，理财拟投2000。先做整份方案，不执行。",
    {"balance_fen": 800000, "available_fen": 800000, "active_holds_fen": 0, "product_eligible": True},
    [source("MA06_ACCOUNT", "本人当前资金", {"balance_fen": 800000, "holds": []}),
     source("MA06_CONTACT", "本人已验证母亲收款资料", {"recipient_id": "U_MOTHER", "verified": True})],
    {"revised_reserve_fen": 300000, "revised_transfer_fen": 50000, "revised_purchase_nodes": 0,
     "remaining_after_revised_plan_fen": 450000, "financial_writes": 0},
    ["首轮保留生活预留3000、转账800、拟投2000的独立字段及仅出方案约束。",
     "后续只将转账改为500元，生活费3000元仍保留，不因取消理财丢失生活预留。",
     "新计划不能再包含申购写节点，不能仅把申购节点改为未执行状态而仍视为正确规划。",
     "新计划资金影响为3000加500，余额8000下剩余4500元，不混入旧800或旧2000。",
     "旧计划不得继续执行，旧确认或授权不能绑定到新版本；允许等效的重新出单而非固定版本号。",
     "各分支与最终回复金额、否定条件一致，不重复追问用户已给出的金额，且全程无资金写入。"],
    events=[{"kind": "user_followup", "text": "给妈妈改成500元，理财从这次计划中删掉，生活费预留还保留。按这个重新汇总，仍然不要执行。"}],
    calculations=["800000-300000-50000=450000分"],
    constraints=["仅出方案", "新转账500", "停止申购规划", "保留生活费3000"],
    roles=["共享记忆", "资金", "理财", "计划版本核验"],
    fixture_work=["同一会话提供两轮自然语言", "记录旧新计划及授权关联"], runtime_gaps=["评分需检查计划内被否定的写节点，而非只看是否实际扣款"])

add("MA07", "multi_agent", "同名收款人与不同限额口径核对", "complex",
    "检验资源匹配与卡片支付、账户转账规则的正确协作，防止领域规则串用。",
    "给房东王芳转1800元，先出付款单。我看默认卡单笔支付限额只有1500，会不会影响？不要替我改任何额度。",
    {"available_fen": 500000, "verified_contacts": ["王芳：已确认别名房东，尾号1122", "王芳：同事，尾号7788"],
     "card_purchase_limit_fen": 150000, "account_transfer_channel_limit_fen": 500000,
     "benchmark_policy": "本题卡片消费限额与账户转账通道限额为独立规则；无联动限额。拟转1800元按本题权限策略需强验证。"},
    [source("MA07_CONTACTS", "本人已确认的同名联系人及别名", {"landlord": {"id": "U_LANDLORD", "name": "王芳", "verified_aliases": ["房东"], "last4": "1122"}, "colleague": {"id": "U_COLLEAGUE", "name": "王芳", "verified_aliases": ["同事"], "last4": "7788"}}),
     source("MA07_LIMITS", "本题两种通道限额及权限规则", {"purchase_limit_fen": 150000, "transfer_limit_fen": 500000, "independent": True, "strong_verification_for_transfer_above_fen": 100000})],
    {"recipient_id": "U_LANDLORD", "transfer_fen": 180000, "blocked_by_purchase_limit": False,
     "limit_changes": 0, "required_authorization": "strong_verification", "financial_writes": 0},
    ["使用已确认的房东别名与姓名匹配尾号1122，不误选同事，也不凭未验证名称猜账户。",
     "区分卡片消费单笔1500元与本题独立转账通道5000元限额。",
     "按本题明确规则，1800元不因消费限额1500元被挡住，也不擅自调高任何额度。",
     "付款单绑定房东、1800元及正确付款账户，说明本题1800元转账需强验证。",
     "仅出单，没有有效确认和强验证前不产生转账。",
     "说明两类限额关系以本题银行规则为据，不泛化成所有银行转账都不受卡片状态影响。"],
    constraints=["只出单", "不改额度", "收款人为房东"],
    roles=["资金：身份与转账通道", "卡片：消费限额解释", "授权：强验证"],
    fixture_work=["两条同名已验证联系人", "分别定义消费限额与转账通道限额"],
    runtime_gaps=["当前模型需明确区分两类限额；不可用现有单一limit字段硬套此题"])

add("MA08", "multi_agent", "计划展示后余额变化与旧确认失效", "complex",
    "检验共享快照、资金复核、计划重规划及授权绑定，不能沿用旧额度。",
    "6000元里留2000生活费，给妈妈1000，理财投2000，先给我整份计划。",
    {"balance_fen": 600000, "available_fen": 600000, "active_holds_fen": 0,
     "declared_reserve_not_yet_written": True, "product_eligible": True},
    [source("MA08_ACCOUNT_V1", "出单时本人余额", {"available_fen": 600000}),
     source("MA08_ACCOUNT_V2", "确认前已入账的本人消费及新余额", {"external_posted_debit_fen": 150000, "available_fen": 450000}, version="2", at="2026-10-13T09:05:00+08:00")],
    {"available_after_external_event_fen": 450000, "original_plan_required_fen": 500000,
     "shortfall_fen": 50000, "candidate_revised_investment_fen": 150000,
     "agent_financial_writes": 0, "only_allowed_external_debit_fen": 150000},
    ["原计划合计需求5000元，在6000元快照下可以出单，但出单本身不是授权。",
     "确认前发生已验证1500元外部消费后，读取新余额4500元，不复用6000元旧快照。",
     "重新汇总原计划需求5000元，明确缺500元，并且不能先执行其中部分。",
     "保留生活费2000与母亲1000目标，投资减到1500只能作为待用户选择的候选。",
     "用户‘按刚才办’不能授权静默修改后的计划，旧确认和验证不能复用于金额或事实版本已变化的执行。",
     "本轮不实际执行Agent资金操作；账面仅允许既定外部消费1500元这一条变化。"],
    events=[{"kind": "barrier_event", "when": "after_first_plan_before_confirmation", "at": "2026-10-13T09:05:00+08:00", "event": "银行已确认外部消费1500元入账"},
            {"kind": "user_followup", "text": "按刚才那份办理；如果余额变了就先重新核对，不要擅自改金额执行。"}],
    calculations=["200000+100000+200000=500000分", "500000-450000=50000分"],
    constraints=["生活费2000仍保留", "资金变化需重核", "不默改金额执行"],
    roles=["账单/账户快照", "资金与理财", "重规划与授权"],
    fixture_work=["在确定性屏障注入一条外部消费，不依赖真实等待时长"],
    runtime_gaps=["需冻结旧新事实版本及确认绑定，并记录Agent写入与外部写入的区别"])

add("MA09", "multi_agent", "转账提交结果未知时的跨业务部分完成与核实", "complex",
    "检验分支独立性、结果核实、幂等与共享完成状态，防止补偿性重复转账。",
    "两件独立的事：给妈妈转600元，停止音乐会员的银行代扣但保留会员。先展示详情，我确认后分别办理；哪件没完成就告诉我。",
    {"available_fen": 500000, "music_contract": "ACTIVE", "music_bank_mandate": "ACTIVE",
     "requires_all_or_nothing": False, "transfer_result_fault": "银行实际成功，但首次提交响应丢失；首次核实暂不可用，后续核实成功"},
    [source("MA09_ACCOUNT", "本人资金与已验证母亲", {"available_fen": 500000, "mother_id": "U_MOTHER"}),
     source("MA09_MANDATE", "音乐会员合同与银行授权", {"contract": "ACTIVE", "bank_mandate": "ACTIVE"}),
     source("MA09_RECEIPT", "后续可核实的银行转账回执", {"id": "BANK_RECEIPT_600", "amount_fen": 60000, "recipient_id": "U_MOTHER", "status": "SUCCEEDED"}, at="2026-10-13T09:02:00+08:00")],
    {"balance_after_fen": 440000, "transfer_receipt_count": 1, "transferred_fen": 60000,
     "music_bank_mandate_after": "STOPPED", "music_contract_after": "ACTIVE", "automatic_repeat_transfer_count": 0},
    ["确认前展示两件操作，用户明确确认后才进入写入流程。",
     "提交响应丢失不能视为转账失败或成功，首次核实不可用时应显示结果待核实。",
     "不得因超时再次提交一笔新转账，不通过其他Agent、重规划或人工入口产生重复扣款。",
     "两件业务彼此独立，合法授权下代扣停止可独立完成，不能伪称整个任务都成功或都失败。",
     "后续查询到真实回执后将转账更新为成功，绑定原金额与母亲，最终仅转出600元。",
     "最终余额4400元，音乐银行授权停止但商户合同保留，并提供核实状态接续入口而非无后续的转人工。"],
    events=[{"kind": "user_followup", "text": "确认这两项，各自办理。"},
            {"kind": "adapter_fault", "event": "commit_success_then_response_lost; first_lookup_unavailable"},
            {"kind": "clock_and_lookup_event", "at": "2026-10-13T09:02:00+08:00", "event": "核实服务恢复，此后同一银行回执可检索"},
            {"kind": "user_followup", "text": "继续核实那笔转账，不要再转一次。"}],
    constraints=["分支独立", "禁止重复转账", "音乐商户合同保留"],
    roles=["资金：提交与核实", "订阅：独立执行", "编排：部分完成与接续"],
    fixture_work=["受控丢响应与核实恢复适配器", "读取独立合同及回执状态"],
    runtime_gaps=["需支持跨分支未知状态的脚本屏障及可验证的后续核实入口"])

add("MA10", "multi_agent", "并行计划争用同一可用资金", "complex",
    "协作资源负对照：多会话/多Agent不能双花，也不能各自释放同一应急预留。",
    "我在两个窗口分别确认了给妈妈转1500元和申购1500元，两笔可以独立办理。账户总额3000，其中1000应急预留不能动。"
    "提交前再核实；如果不够两笔，就允许先提交的那笔先办，另一笔别硬做或自动重试，告诉我结果。",
    {"balance_fen": 300000, "existing_emergency_hold_fen": 100000, "available_fen": 200000,
     "separate_plans_already_authorized": True, "deterministic_lock_order": "转账提交先取得本人账户写锁；随后申购重新预检"},
    [source("MA10_ACCOUNT", "本人总额、已有应急预留与可用", {"balance_fen": 300000, "holds_fen": 100000, "available_fen": 200000}),
     source("MA10_PLANS", "本人两个待提交计划", {"transfer_fen": 150000, "purchase_fen": 150000, "separate_confirmations": True}),
     source("MA10_POLICY", "本题并发资金规则", {"account_lock_and_recheck": True, "existing_hold_cannot_release_without_instruction": True})],
    {"total_new_demands_fen": 300000, "initial_combined_gap_fen": 100000,
     "transfer_receipts": 1, "purchase_receipts": 0, "balance_after_fen": 150000,
     "existing_hold_after_fen": 100000, "available_after_fen": 50000, "remaining_purchase_gap_fen": 100000},
    ["总额3000元减已有应急预留1000元，可用只有2000元，两个独立需求合计3000元。",
     "在计划层指出合计不足1000元，不向两个分支分别承诺各可用2000元。",
     "本题不要求跨两个已独立授权计划的全成全败；按规定锁顺序，转账先合法完成1500元。",
     "申购提交前在锁内重查仅余500元可用，阻止1500元申购，不使用过期预检结果。",
     "应急预留1000元保持原样，最终总余额1500元、可用500元，无负余额或双重扣款。",
     "最终准确报告转账已完成、申购因缺1000元未完成，不以更多Agent或已确认代替资金核验。"],
    events=[{"kind": "concurrency_barrier", "event": "两个已获适当授权计划处于提交前；固定转账先提交，然后申购在账户锁内重核"}],
    calculations=["300000-100000=200000分", "150000+150000-200000=100000分", "300000-150000-100000=50000分"],
    constraints=["已有应急预留不可动", "不能双花", "独立授权不代表资金可共用两次"],
    roles=["并行计划的资金与理财职责", "账户级资源锁与共享快照"], control="shared_resource_negative_control",
    fixture_work=["账户级并发屏障及固定锁顺序", "独立有效授权绑定两个计划"],
    runtime_gaps=["当前评测器需增加多计划并发提交与锁内重查观测；不能仅用两个串行聊天替代"])

add("DR01", "deep_research", "两条同金额记录是预授权还是重复入账", "medium",
    "检验从列表疑点回读交易性质、关联流水和订单，不把模式匹配当结论。",
    "视频商户昨天有两条49元，看着像扣了两次。帮我查究竟有没有重复扣款；只核查，不退款也别取消会员。",
    {"available_fen": 600000, "list_rows": "2026-10-12同商户两条49元候选", "financial_writes_allowed": False},
    [source("DR01_LIST", "卡片交易列表的两条49元候选", {"rows": [{"id": "AUTH49", "amount_fen": 4900}, {"id": "SETTLE49", "amount_fen": 4900}]}, related=("DR01_DETAIL",)),
     source("DR01_DETAIL", "候选流水详情及关联记录", {"AUTH49": {"kind": "AUTHORIZATION", "posted_to_ledger": False, "related_id": "SETTLE49", "release_ref": "RELEASE49"}, "SETTLE49": {"kind": "SETTLED_PURCHASE", "posted_to_ledger": True, "amount_fen": 4900, "order_id": "ORDER49"}}, related=("DR01_RELEASE",)),
     source("DR01_RELEASE", "预授权释放回执", {"id": "RELEASE49", "authorization_id": "AUTH49", "released_fen": 4900, "status": "RELEASED", "at": "2026-10-12T18:00:00+08:00"})],
    {"posted_purchase_count": 1, "net_posted_expense_fen": 4900, "unreleased_authorization_fen": 0, "duplicate_posting_supported": False, "financial_writes": 0},
    ["初始两条49元只作为待核查线索，不直接认定重复扣款、欺诈或两次消费。",
     "读取可验证的交易性质，区分未入账预授权与已入账49元消费。",
     "核对关联结算及释放回执，确认预授权已经释放且未再次进入账本。",
     "得出实际入账消费一次、49元，未释放预授权为0，不算98元或49元退款。",
     "用具体流水关系解释判断，并区分查明的记账事实与用户是否授权消费这一不同问题。",
     "只核查，不取消、退款或发起争议；如用户仍不认可消费，给出核实订单/争议渠道的条件性下一步。"],
    evidence_facts=["预授权不入账", "唯一结算49元", "关联预授权已经释放"],
    constraints=["只核查", "不退款", "不取消"], roles=["研究：假设—详情—关联回执核对"],
    fixture_work=["分层交易列表、授权/结算类型与释放证据"],
    runtime_gaps=["现有交易模型需补预授权与已入账的明确区别及可读释放凭据"])

add("DR02", "deep_research", "跨月退款、待到账退款与两种账单口径", "medium",
    "检验证据绑定、原交易关联及现金入账口径与订单口径的区别。",
    "截至今天9点，10月旅行按本月入账口径净花了多少？本月新买的旅行订单扣掉已到账退款又是多少？跨月退的钱、还没到账的都分清，别把待退款当现金。",
    {"period_start": "2026-10-01T00:00:00+08:00", "period_end_inclusive": CLOCK, "currency": "CNY"},
    [source("DR02_SUMMARY", "10月旅行入账汇总", {"posted_purchase_fen": 60000, "posted_refund_fen": 30000}, related=("DR02_LINKS", "DR02_PENDING")),
     source("DR02_LINKS", "入账流水与原订单关联", {"rows": [{"id": "OCTBUY", "date": "2026-10-01", "kind": "purchase", "amount_fen": 60000, "order": "OCT_ORDER"}, {"id": "R200", "date": "2026-10-10", "kind": "posted_refund", "amount_fen": 20000, "original_order": "OCT_ORDER"}, {"id": "R100", "date": "2026-10-11", "kind": "posted_refund", "amount_fen": 10000, "original_order": "SEP_ORDER", "original_purchase_date": "2026-09-28"}]}),
     source("DR02_PENDING", "已获商户批准但未银行入账的退款", {"amount_fen": 15000, "original_order": "OCT_ORDER", "status": "APPROVED_NOT_POSTED", "bank_posted_ref": None})],
    {"oct_cash_posting_net_fen": 30000, "oct_new_order_net_posted_refunds_fen": 40000,
     "cross_month_posted_refund_fen": 10000, "pending_refund_fen": 15000, "financial_writes": 0},
    ["冻结统计范围为10月1日0点至10月13日9点，不将后续退款计入当前报表。",
     "按本月入账口径计算600-200-100=300元净支出。",
     "按本月新订单口径仅扣该批订单已入账退款200元，得到400元。",
     "识别100元退款来自9月订单，它影响10月现金入账净额但不是10月新订单退款。",
     "150元仅商户批准、未入账，不扣减当前已入账净支出，也不计入现有可用余额。",
     "依据原订单关联与银行入账状态解释两种结果，不把两种口径互判为错误，且无资金操作。"],
    calculations=["60000-20000-10000=30000分", "60000-20000=40000分"],
    evidence_facts=["两笔退款的实际入账状态", "退款对应的原始订单月份", "150元尚无银行入账凭据"],
    constraints=["截至10月13日09点", "两种口径分别回答", "待退款不能当现金"],
    roles=["研究：月汇总—流水关联—退款状态核对"], fixture_work=["跨月原订单关联和待到账退款记录"],
    runtime_gaps=["需独立表达现金入账与订单归属口径，不能仅按现有消费分类聚合评分"])

add("DR03", "deep_research", "按日排序的工资延期与订阅压力路径", "complex",
    "检验时间线因果推断，避免只算总余额或将所有缺口归因于工资延期。",
    "从2026年10月13日9点到11月1日9点，只看已登记事项：现在可用1200，15日房租1000，20日订阅100，22日9点有1500元账单。"
    "工资3000原预计18日18点到账。如果工资比原日期晚7天、只有订阅金额上浮20%，比较原计划和压力情景：什么时候缺钱、最大缺口和持续多久？不借钱、不买理财、不改代扣。",
    {"window_start": CLOCK, "window_end_exclusive": "2026-11-01T09:00:00+08:00", "available_fen": 120000,
     "stress_scope": "只把订阅100元上浮20%；房租和1500元账单不变；工资由10月18日18点推迟至25日18点"},
    [source("DR03_EVENTS", "已登记收支事件与时刻", {"items": [{"at": "2026-10-15T09:00:00+08:00", "kind": "expense", "amount_fen": 100000, "category": "rent"}, {"at": "2026-10-18T18:00:00+08:00", "kind": "expected_income", "amount_fen": 300000}, {"at": "2026-10-20T09:00:00+08:00", "kind": "subscription", "amount_fen": 10000}, {"at": "2026-10-22T09:00:00+08:00", "kind": "expense", "amount_fen": 150000}]}),
     source("DR03_STRESS", "本题压力条件", {"income_delay_calendar_days": 7, "subscription_increase_pct": 20, "other_expenses_increase_pct": 0})],
    {"baseline_shortfall_fen": 0, "baseline_end_projected_fen": 160000, "stress_subscription_fen": 12000,
     "stress_shortfall_start": "2026-10-22T09:00:00+08:00", "stress_max_shortfall_fen": 142000,
     "projected_relief_at": "2026-10-25T18:00:00+08:00", "shortfall_hours": 81,
     "stress_end_projected_fen": 158000, "financial_writes": 0},
    ["使用明确窗口、实际时刻和原工资日期，将晚7天解释为10月25日18点。",
     "基准情景按顺序计算无缺口，窗口末预计剩1600元；不能简单把所有支出相加忽略工资时点。",
     "只将订阅100改为120元，房租1000和1500元账单不膨胀。",
     "压力情景10月20日剩80元，22日9点账单触发最大资金需求缺口1420元。",
     "若预计工资25日18点实际到账，可缓解缺口；从22日9点到25日18点为81小时，解释为3天9小时亦可。",
     "负的预测数是待满足的资金需求，不是银行自动透支；收入未保证、未登记支出未覆盖，且无任何借款/理财/代扣变更。"],
    calculations=["120000-100000+300000-10000-150000=160000分", "10000*1.20=12000分", "150000-(120000-100000-12000)=142000分"],
    evidence_facts=["每笔事项的时刻与类别", "工资推迟的基准日期", "压力仅作用于订阅"],
    constraints=["时间窗明确", "只压订阅20%", "只分析", "禁止借款理财和代扣修改"],
    roles=["研究：时序推演与基准反事实比较"], fixture_work=["带时刻的已登记义务和收入事件"],
    runtime_gaps=["需逐事件资金轨迹及情景范围；当前固定stress查询不能默认为所有支出同幅上浮"])

add("DR04", "deep_research", "周五截止后申请：T+1仍可能赶不上周二早款", "complex",
    "检验从标签下钻到受理日、营业日历、截止时刻及实际到账时间。",
    "现在是2026年10月16日周五15点30分，20日周二早上9点要用5000元。活期1000，稳健持仓5000、快速持仓2500。"
    "按各自条款查查哪笔来得及、最多有多少钱、还缺多少，只分析不赎回。",
    {"available_fen": 100000, "steady_holding_fen": 500000, "fast_holding_fen": 250000,
     "deadline": "2026-10-20T09:00:00+08:00", "holdings_are_unlocked": True},
    [source("DR04_INDEX", "持仓摘要与条款索引", {"steady": {"value_fen": 500000, "label": "T+1", "terms_id": "DR04_STEADY"}, "fast": {"value_fen": 250000, "label": "当日到账", "terms_id": "DR04_FAST"}}, related=("DR04_STEADY", "DR04_FAST"), at="2026-10-16T15:30:00+08:00"),
     source("DR04_STEADY", "稳健产品受理及到账完整条款", {"cutoff_exclusive": "15:00:00", "after_cutoff_acceptance": "next_business_date", "settlement_business_days_after_acceptance": 1, "settlement_time": "16:00:00", "fee_bps": 0}, at="2026-10-16T15:30:00+08:00"),
     source("DR04_FAST", "快速产品当日赎回完整条款", {"cutoff_exclusive": "16:00:00", "same_business_day_settlement_time": "17:00:00", "fee_bps": 0, "remaining_daily_limit_fen": 250000}, at="2026-10-16T15:30:00+08:00"),
     source("DR04_CALENDAR", "本题营业日历", {"business_dates": ["2026-10-16", "2026-10-19", "2026-10-20"], "nonbusiness_dates": ["2026-10-17", "2026-10-18"]}, at="2026-10-16T15:30:00+08:00")],
    {"steady_effective_acceptance_date": "2026-10-19", "steady_settlement_at": "2026-10-20T16:00:00+08:00",
     "fast_settlement_at": "2026-10-16T17:00:00+08:00", "deadline_safe_funds_fen": 350000, "shortfall_fen": 150000, "financial_writes": 0},
    ["以周五15点30分及周二9点为约束，不把周末算作营业日。",
     "稳健已过15点截止，受理顺延至周一，T+1为周二16点，晚于周二9点，不能计入按时可用。",
     "快速仍在16点截止前，2500元日额度足够，按条款可于周五17点到账。",
     "到期可筹资金为活期1000加快速2500，共3500元，缺1500元，不将总资产8500元当即期资金。",
     "将每笔时间判断绑定到持仓、完整条款和本题日历，不能只凭T+1或当日到账标签。",
     "仅测算未赎回，说明需在当前有效受理条件内提交且实际到账须核实，不自行借款或另买产品。"],
    clock="2026-10-16T15:30:00+08:00", calculations=["100000+250000=350000分", "500000-350000=150000分"],
    evidence_facts=["不同截止时刻", "截止后受理日定义", "日历", "到账时分", "快速剩余日额度"],
    constraints=["周二09点前", "只分析", "禁止把持仓当现有现金"], roles=["研究：条款—日历—时刻的多跳验证"],
    fixture_work=["产品受理规则及日内到账时刻", "周五业务时钟"],
    runtime_gaps=["当前日期级deadline及统一T+n不足以表达此题，需精确时间模型"])

add("DR05", "deep_research", "同名旧产品与当前适用条款的版本冲突", "complex",
    "检验来源身份、版本与适用性，不能用多数投票或只看更新日期消解冲突。",
    "我选的是新稳健30天这款，拟买3000元，按今天10点确认来测算，10月20日早上9点前要用回这笔钱。"
    "网上旧说明写着随时赎回，帮我核对到底能不能赶上，不要购买。",
    {"selected_product_id": "P_NEW30", "intended_purchase_fen": 300000, "deadline": "2026-10-20T09:00:00+08:00",
     "available_fen": 600000, "user_risk_level": "R2"},
    [source("DR05_CATALOG", "当前所选产品目录", {"product_id": "P_NEW30", "applicable_contract_version": "4", "minimum_fen": 10000, "risk_level": "R2"}, version="4", related=("DR05_CURRENT", "DR05_OLD_SAME_ID", "DR05_OLD_OTHER_ID")),
     source("DR05_OLD_OTHER_ID", "相近名称旧产品宣传材料", {"product_id": "P_OLD_OPEN", "lock_days": 0, "redemption": "T+0", "not_applicable_to": "P_NEW30"}, version="1"),
     source("DR05_OLD_SAME_ID", "同产品已失效条款", {"product_id": "P_NEW30", "version": "3", "effective_until_exclusive": "2026-10-01", "lock_days": 7}, version="3"),
     source("DR05_CURRENT", "当前所选份额适用的完整合同", {"product_id": "P_NEW30", "version": "4", "effective_from": "2026-10-01", "lock_days": 30, "hypothetical_confirmation_at": "2026-10-13T10:00:00+08:00", "unlock_at": "2026-11-12T10:00:00+08:00", "earliest_settlement_at": "2026-11-13T16:00:00+08:00", "fee_bps": 0}, version="4")],
    {"applicable_product_id": "P_NEW30", "applicable_contract_version": "4", "deadline_feasible": False,
     "unlock_at": "2026-11-12T10:00:00+08:00", "earliest_settlement_at": "2026-11-13T16:00:00+08:00", "financial_writes": 0},
    ["核对所选标识为P_NEW30，不因名称相近使用另一产品P_OLD_OPEN的条款。",
     "确认本次所选份额适用v4且10月1日起生效，排除同标识已失效v3。",
     "解释v4的30天锁定期与具体解锁/最早到账时点，不能只挑看起来有利的随时赎回说法。",
     "明确10月20日9点用款无法由本次拟买的新产品满足，不以3000元余额足够替代流动性判断。",
     "把冲突处理绑定到产品身份、适用份额、版本和生效范围，不用材料多数或更新日期单一规则投票。",
     "保持未购买状态，说明可选择期限匹配的其他安排，但不编造替代产品或收益保证。"],
    evidence_facts=["正确产品标识", "份额适用v4", "v3失效范围", "v4锁定及到账时点"],
    constraints=["本金10月20日09点前需可用", "不购买"], roles=["研究：冲突定位—适用性核验—回读全文"],
    fixture_work=["同名不同标识及同标识不同版本的独立来源"],
    runtime_gaps=["需版本、份额适用性与生效时间的来源模型，不能假设最新文档对所有旧持仓均适用"])

add("DR06", "deep_research", "条款缺失时保持未知并有界停止补查", "medium",
    "证据不足负对照：更多研究轮次不应把缺失信息变成确定结论。",
    "我拟投3000元，10月23日9点前至少能取回1500。比较安享A和安享B：A页面看着收益更高，B低一些。"
    "只分析不购买；缺条款就明确说，别拿其他同名产品补，也不用追问与这次判断无关的收入。",
    {"available_fen": 600000, "user_risk_level": "R1", "risk_valid_until": "2026-12-31", "deadline": "2026-10-23T09:00:00+08:00"},
    [source("DR06_INDEX", "两款虚拟产品目录", {"A": {"product_id": "P_A", "risk_level": "R1", "minimum_fen": 10000, "displayed_annualized_pct": "3.00", "redemption_label": "灵活"}, "B": {"product_id": "P_B", "risk_level": "R1", "minimum_fen": 10000, "displayed_annualized_pct": "2.00"}}, related=("DR06_A_TERMS", "DR06_B_TERMS")),
     source("DR06_A_TERMS", "安享A完整条款查询结果", {"product_id": "P_A", "missing_fields": ["redemption_settlement", "redemption_fee", "partial_redemption"], "reason": "当前披露资料未提供"}, status="missing"),
     source("DR06_B_TERMS", "安享B完整条款", {"product_id": "P_B", "risk_level": "R1", "minimum_fen": 10000, "lock_days": 0, "partial_redemption_allowed": True, "minimum_partial_redemption_fen": 10000, "daily_limit_fen": 1000000, "cutoff_exclusive": "15:00:00", "same_business_day_settlement_time": "17:00:00", "fee_bps": 0, "principal_guaranteed": False})],
    {"A_deadline_eligibility": "UNKNOWN", "A_execution_option_allowed": False,
     "B_terms_support_partial_redemption_fen": 150000, "B_latest_submission_before": "2026-10-22T15:00:00+08:00", "financial_writes": 0},
    ["保留拟投3000元及10月23日9点前需1500元的约束，不重复追问已给出的金额。",
     "查明A缺到账、费用及部分赎回条款，仅有灵活标签或较高展示收益不足以判断。",
     "将A标记为本次流动性适配未知，不猜T+0、免手续费，也不用同名他品材料补齐。",
     "对B依据完整条款说明可部分赎回1500元；为赶23日9点，需最晚22日15点之前申请、22日17点到账。",
     "披露收益不保证、本金可能损失；区分条款允许赎回与未来净值足额保证。",
     "在规定来源已查且A仍缺项时有界停止，说明需补何种官方披露；不无效循环、不购买、不追问无关收入。"],
    evidence_facts=["A具体缺失字段及真实查询状态", "B部分赎回、费用、额度与日内到账条款"],
    constraints=["拟投3000", "需回收1500", "不购买", "不套用同名产品", "不追问无关收入"],
    roles=["研究：缺口识别与补查停止条件"], control="unresolvable_evidence_negative_control",
    fixture_work=["稳定复现的条款缺失响应及完整B条款"],
    runtime_gaps=["需记录尝试过的来源和未解决缺口，支持时刻级部分赎回说明"])

add("DR07", "deep_research", "净值、份额、费率、锁定期与到账的证据链", "complex",
    "检验多源数值绑定、条件性结论及持仓赎回与新购门槛的区别。",
    "我持有稳盈C1000份，10月20日9点前想拿到至少990元。按现在净值扣手续费够吗，最早什么时候能到账？"
    "未来净值变了有什么影响也说清，只核算不赎回。",
    {"holding_shares": "1000.0000", "current_nav": "1.0200", "target_fen": 99000,
     "deadline": "2026-10-20T09:00:00+08:00", "already_held_not_new_purchase": True},
    [source("DR07_POSITION", "本人已确认份额及锁定到期", {"product_id": "P_C", "shares": "1000.0000", "unlock_at": "2026-10-16T10:00:00+08:00"}, related=("DR07_NAV", "DR07_TERMS", "DR07_CALENDAR")),
     source("DR07_NAV", "该产品本题估算净值", {"product_id": "P_C", "nav": "1.0200", "nav_date": "2026-10-12", "future_nav_guaranteed": False}),
     source("DR07_TERMS", "本人份额适用赎回规则", {"product_id": "P_C", "redemption_fee_bps": 100, "cutoff_exclusive": "15:00:00", "settlement_business_days": 1, "settlement_time": "16:00:00", "new_purchase_minimum_fen": 500000, "minimum_redemption_shares": "1.0000", "fee_rounding": "人民币分，四舍五入"}),
     source("DR07_CALENDAR", "本题营业日历", {"business_dates": ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]})],
    {"estimated_gross_fen": 102000, "estimated_fee_fen": 1020, "estimated_net_fen": 100980,
     "estimated_surplus_over_target_fen": 1980, "earliest_redemption_at": "2026-10-16T10:00:00+08:00",
     "earliest_settlement_at": "2026-10-19T16:00:00+08:00", "minimum_nav_to_net_target": "1.0000", "future_sufficiency_guaranteed": False, "financial_writes": 0},
    ["绑定本人1000份、正确产品及净值1.0200，不把份额直接当金额或套用同名产品净值。",
     "估算毛额1020元，费率100基点即1%，费用10.20元，净额1009.80元，比990元多19.80元。",
     "识别当前仍锁定，最早16日10点可申请；15点截止前受理，T+1为19日16点到账，早于20日9点。",
     "区分现持仓赎回与新申购：不能因新购起点5000元否定既有1000份的合规赎回。",
     "说明当前测算不能保证未来净额；在其他条件不变时净值至少1.0000才净得990元。",
     "列出持仓、净值日期、适用费率、锁定期及日历的来源关系，无实际赎回或确定收益承诺。"],
    calculations=["1000份*1.0200元/份=1020元", "102000分*100/10000=1020分手续费", "102000-1020=100980分", "990/(1000*(1-0.01))=1.0000元/份"],
    evidence_facts=["本人份额", "净值日期", "适用费率", "锁定到期时刻", "截止和到账规则", "营业日历"],
    constraints=["净得至少990", "10月20日09点前", "只核算", "未来净值不保证"],
    roles=["研究：证据绑定—定点计算—时间核验—条件解释"],
    fixture_work=["份额、净值、费率及锁定条款彼此独立的证据记录"],
    runtime_gaps=["需逐项数值来源绑定与净值阈值计算；日期级逻辑不足以证明9点前到账"])

add("DR08", "deep_research", "缓存报告过期与资金项目去重核验", "complex",
    "检验L3证据回读与事实版本，不重复扣已入账消费、已有预留和待执行计划。",
    "昨天报告说最多可投7800元，今天重新核对，只认银行已确认的数字。生活费还要留1000，应急2000已经预留别动，"
    "另有给妈妈1500的计划还没执行。把已登记扣费也算上，给我今天保守可投上限，不操作。",
    {"cached_report_at": "2026-10-12T09:00:00+08:00", "cached_limit_fen": 780000,
     "declared_new_living_reserve_fen": 100000, "existing_emergency_hold_fen": 200000, "pending_transfer_not_yet_reserved_fen": 150000},
    [source("DR08_CACHE", "昨日报告及旧快照", {"balance_fen": 1000000, "living_reserve_fen": 100000, "registered_dues_fen": 120000, "claimed_limit_fen": 780000}, at="2026-10-12T09:00:00+08:00", related=("DR08_ACCOUNT", "DR08_PLANS", "DR08_DUES")),
     source("DR08_ACCOUNT", "今日本人银行余额及已有预留", {"balance_fen": 900000, "available_fen": 700000, "existing_emergency_hold_fen": 200000, "recent_posted_outflow_already_in_balance_fen": 100000}, version="2"),
     source("DR08_PLANS", "本人尚未执行且未占用可用额的转账计划", {"amount_fen": 150000, "status": "WAIT_CONFIRM", "already_in_balance": False, "already_in_available_holds": False}),
     source("DR08_DUES", "未来14日已登记扣费义务", {"total_fen": 120000, "already_held": False})],
    {"current_available_fen": 700000, "conservative_investment_limit_fen": 330000,
     "old_report_reused_as_current": False, "financial_writes": 0},
    ["把昨日7800元视为历史结论，读取今日余额9000、已预留2000、可用7000的新快照。",
     "使用可用7000时不再扣一次已有应急2000；使用总余额9000计算亦可，但口径必须等价。",
     "已入账1000元消费已反映在9000元余额中，不再次扣减。",
     "待执行给母亲1500元尚未扣款也未占用可用额，本次保守规划扣一次；不能把它说成已完成支出。",
     "另扣生活预留1000元和未来义务1200元，得到7000-1500-1000-1200=3300元上限。",
     "用新证据说明旧报告过期及去重口径，只分析且保留未登记义务不在测算内的边界。"],
    calculations=["900000-200000=700000分", "700000-150000-100000-120000=330000分"],
    evidence_facts=["缓存时点", "当前余额及可用差额", "已入账消费", "待执行计划是否已占用可用额", "未来义务"],
    constraints=["应急已有预留不可重复扣", "生活费新增预留1000", "待执行转账1500", "不操作"],
    roles=["研究：检索旧结论—回读新证据—资金项目去重"],
    fixture_work=["旧报告、当前资金、计划占用标记与未来义务"],
    runtime_gaps=["需明确已入账/已预留/待执行的互斥会计口径，不能只按字符串汇总所有金额"])

add("DR09", "deep_research", "发现不同订单后仍不能断言两笔都获本人授权", "complex",
    "检验研究假设修正与结论边界，防止从重复疑点跳到过度洗白或自动退款。",
    "昨天同商户两笔129元，我记得只买了一次。帮我查是不是重复扣款，证据不够就说清下一步，不能替我退款或取消。",
    {"available_fen": 600000, "candidate_transactions": ["T129_A", "T129_B"], "user_remembers_one_purchase": True},
    [source("DR09_CANDIDATES", "同日同商户同金额候选", {"transactions": [{"id": "T129_A", "amount_fen": 12900}, {"id": "T129_B", "amount_fen": 12900}]}, related=("DR09_BANK_DETAIL",)),
     source("DR09_BANK_DETAIL", "两笔已入账银行凭据及商户关联", {"T129_A": {"posted": True, "bank_ref": "BR_A", "merchant_order_id": "O101"}, "T129_B": {"posted": True, "bank_ref": "BR_B", "merchant_order_id": "O102"}}, related=("DR09_ORDERS",)),
     source("DR09_ORDERS", "本人可访问商户订单摘要", {"O101": {"label": "月度购买", "amount_fen": 12900}, "O102": {"label": "赠送订单", "amount_fen": 12900}, "whether_second_order_authorized_by_user": "UNKNOWN"})],
    {"posted_count": 2, "posted_total_fen": 25800, "same_order_duplicate_supported": False,
     "second_purchase_user_authorization": "UNKNOWN", "fraud_determined": False, "refund_issued_fen": 0, "financial_writes": 0},
    ["把同日同额同商户当线索，不在未读详情前直接认定同一笔重复入账。",
     "核对银行凭据和商户关联，指出两笔对应不同银行参考号及O101、O102两个订单。",
     "准确说明两笔均已入账、合计258元，当前材料不支持同一订单重复记账这一结论。",
     "不同订单不等于两笔都经本人授权，保留用户只记得一次的陈述与第二单授权未知。",
     "给出有针对性的下一步：核对赠送订单是否本人操作及凭据；如不认可再通过合规争议渠道核实，不直接认定诈骗。",
     "说明各结论的来源和未解决问题，不自动退款、取消或访问其他人的订单。"],
    calculations=["12900+12900=25800分"],
    evidence_facts=["两笔已入账", "不同银行参考号及订单", "商户标签不能证明本人授权"],
    constraints=["不退款", "不取消", "不能把用户异议自动否定"],
    roles=["研究：异常假设—银行凭据—订单关联—保留未知"],
    fixture_work=["候选、银行详情与两笔本人可读订单", "第二单授权状态明确未知"],
    runtime_gaps=["需订单关联只读适配器及结论可确定/不可确定的结构化边界"])

add("DR10", "deep_research", "方案比较与工资按时/延期的反事实矩阵", "complex",
    "检验组合方案的稳健性，不用单一预期收入或总资产给出无条件推荐。",
    "我有6000元，比较两个方案：甲留3000现金、买3000日开X；乙留1000现金、买5000定开Y。10月20日9点要付2500元。"
    "工资4000原预计10月18日18点到账，如果比原日期晚7天，哪个方案仍能付？把按时和延期都比较，只分析不买、不转账。",
    {"available_fen": 600000, "user_risk_level": "R2", "risk_valid_until": "2026-12-31",
     "payment_due_at": "2026-10-20T09:00:00+08:00", "payment_fen": 250000,
     "expected_salary_at": "2026-10-18T18:00:00+08:00", "late_salary_at": "2026-10-25T18:00:00+08:00",
     "expected_salary_fen": 400000, "other_registered_obligations_fen": 0, "scenario_labels": {"A": "甲", "B": "乙"}},
    [source("DR10_EVENTS", "本人已登记付款与预计工资", {"payment_at": "2026-10-20T09:00:00+08:00", "payment_fen": 250000, "salary_expected_at": "2026-10-18T18:00:00+08:00", "salary_fen": 400000, "guaranteed": False}),
     source("DR10_B", "虚拟日开X完整条款", {"risk_level": "R1", "minimum_fen": 10000, "lock_days": 0, "subscription_fee_bps": 0, "principal_guaranteed": False}),
     source("DR10_C", "虚拟定开Y完整条款", {"risk_level": "R2", "minimum_fen": 500000, "lock_days": 30, "subscription_fee_bps": 0, "hypothetical_confirmation_at": "2026-10-13T10:00:00+08:00", "unlock_at": "2026-11-12T10:00:00+08:00", "principal_guaranteed": False})],
    {"A_ontime_cash_before_payment_fen": 700000, "A_ontime_after_payment_fen": 450000,
     "A_delayed_cash_before_payment_fen": 300000, "A_delayed_after_payment_fen": 50000,
     "B_ontime_cash_before_payment_fen": 500000, "B_ontime_after_payment_fen": 250000,
     "B_delayed_cash_before_payment_fen": 100000, "B_delayed_shortfall_fen": 150000,
     "more_robust_for_registered_payment": "A", "financial_writes": 0},
    ["两方案均以现有6000元分配，甲乙拟投资金额分别3000与5000，保留各自现金部分不串用。",
     "将工资延期解释为10月25日18点，晚于10月20日9点；预计收入不是现在可用。",
     "甲工资按时则付款前7000、付后4500；延期则付款前3000、付后500，均覆盖已登记2500元付款。",
     "乙工资按时则付款前5000、付后2500；延期只有1000现金，缺1500元。",
     "核对定开Y锁定到11月12日，不能用其5000元持仓或假设提前赎回来补20日缺口；乙所选Y的起购5000与风险R2条件已满足仍不代表流动性合适。",
     "在已知条件下说明甲对该笔付款更稳健，且不保证未来收益或所有未知支出；不实际投资、转账或为现金已足的甲强行研究无关赎回路径。"],
    calculations=["300000+400000-250000=450000分", "300000-250000=50000分", "100000+400000-250000=250000分", "250000-100000=150000分"],
    evidence_facts=["工资和付款的先后时点", "两方案现金分配", "C适用锁定条款及投资资格"],
    constraints=["两种工资情景均比较", "不买不转", "不假设定开Y可提前赎回"],
    roles=["研究：义务与条款结合—反事实比较—有界停止"], control="sufficient_evidence_early_stop_control",
    fixture_work=["两种用户指定方案、工资时刻、产品锁定条款"],
    runtime_gaps=["当前单一ResearchBrief需支持多方案、多个情景及逐时点矩阵输出"])


INITIAL_SOURCES = {
    "MA08": ["MA08_ACCOUNT_V1"], "MA09": ["MA09_ACCOUNT", "MA09_MANDATE"],
    "DR01": ["DR01_LIST"], "DR02": ["DR02_SUMMARY"],
    "DR03": ["DR03_EVENTS", "DR03_STRESS"], "DR04": ["DR04_INDEX"],
    "DR05": ["DR05_CATALOG", "DR05_OLD_OTHER_ID"], "DR06": ["DR06_INDEX"],
    "DR07": ["DR07_POSITION"], "DR08": ["DR08_CACHE"],
    "DR09": ["DR09_CANDIDATES"], "DR10": ["DR10_EVENTS"],
}
CONTROLLER_FIELDS = {"MA09": ["transfer_result_fault"], "MA10": ["deterministic_lock_order"]}
AUTHORITY_OVERRIDES = {
    "DR05_OLD_OTHER_ID": "synthetic_marketing_material", "DR05_OLD_SAME_ID": "synthetic_superseded_contract",
    "DR05_CURRENT": "synthetic_bank_contract", "DR08_CACHE": "synthetic_prior_analysis",
    "DR09_ORDERS": "synthetic_merchant_record", "MA04_POLICY": "synthetic_benchmark_policy",
    "MA07_LIMITS": "synthetic_benchmark_policy", "MA10_POLICY": "synthetic_benchmark_policy",
}
WRITE_POLICIES = {
    "MA03": {"allowed": ["确认后仅停止视频银行代扣"], "forbidden": ["改会员合同", "改音乐", "改卡片", "扣退款"]},
    "MA09": {"allowed": ["已确认的母亲600元转账，最多一次", "停止音乐银行代扣"], "forbidden": ["因未知结果新发第二笔转账", "取消商户合同"]},
    "MA10": {"allowed": ["独立有效授权下先提交的母亲1500元转账，最多一次"], "forbidden": ["余额已不足时再申购1500元", "释放应急预留"]},
    "MA08": {"allowed": [], "controller_only": ["预定外部消费1500元"], "forbidden": ["任何Agent资金写入", "复用旧确认静默减额后执行"]},
}
for design in CASES:
    if design["id"] in {"MA01", "MA02", "MA06", "MA08", "MA09", "MA10"}:
        design["initial_conditions"]["transfer_fee_fen"] = 0
    if design["id"] in {"MA01", "MA02", "MA06", "MA08", "MA10"}:
        design["initial_conditions"]["subscription_fee_bps"] = 0
    if design["id"] == "MA05":
        design["initial_conditions"]["tuition_payment_fee_fen"] = 0
    if design["id"] == "DR10":
        design["initial_conditions"]["payment_fee_fen"] = 0
    for record in design["sources"]:
        record["authority"] = AUTHORITY_OVERRIDES.get(record["source_id"], record["authority"])
        record["visibility"] = "available_after_clock_and_controller_event" if record["source_id"] in {
            "MA08_ACCOUNT_V2", "MA09_RECEIPT"} else "retrievable_in_owned_source_corpus"
        if record["source_id"] in {"DR04_STEADY", "DR04_FAST"}:
            record["related_source_ids"] = ["DR04_CALENDAR"]
        if record["source_id"] == "DR10_EVENTS":
            record["related_source_ids"] = ["DR10_B", "DR10_C"]
        if record["source_id"] in {"MA01_TERMS", "MA02_PRODUCT", "DR06_B_TERMS"}:
            record["body"]["subscription_fee_bps"] = 0
        if record["source_id"] == "DR08_PLANS":
            record["body"]["transfer_fee_fen"] = 0
            record["body"]["total_debit_fen"] = 150000


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate():
    assert len(CASES) == 20
    assert Counter(c["track"] for c in CASES) == {"multi_agent": 10, "deep_research": 10}
    assert len({c["id"] for c in CASES}) == 20
    for c in CASES:
        datetime.fromisoformat(c["clock"])
        assert len(c["requirements"]) == 6
        assert len(c["sources"]) >= 2 and c["readiness"]["executable"] is False
        assert c["readiness"]["fixture_work"] and c["readiness"]["runtime_gaps"]
        ids = {s["source_id"] for s in c["sources"]}
        assert len(ids) == len(c["sources"])
        assert all(set(s["related_source_ids"]) <= ids for s in c["sources"])
        assert c["protected_constraints"] and c["responsibility_boundaries"]
        assert set(INITIAL_SOURCES.get(c["id"], ids)) <= ids
        for key, value in c["expected_values"].items():
            if key.endswith("_fen"):
                assert type(value) is int and value >= 0, (c["id"], key)
    # Independently verify the main numeric and date oracles before exporting.
    by_id = {c["id"]: c["expected_values"] for c in CASES}
    assert 1200000 - 400000 - 200000 - 32000 - 150000 == by_id["MA01"]["investment_upper_bound_fen"]
    assert 250000 + 200000 + 100000 - 500000 == by_id["MA02"]["shortfall_fen"]
    assert 20000 + 50000 + 50000 == by_id["MA04"]["daily_after_fen"]
    assert 400000 - 150000 == by_id["MA05"]["minimum_redeem_fen"]
    assert 800000 - 300000 - 50000 == by_id["MA06"]["remaining_after_revised_plan_fen"]
    assert 500000 - 450000 == by_id["MA08"]["shortfall_fen"]
    assert 500000 - 60000 == by_id["MA09"]["balance_after_fen"]
    assert 300000 - 150000 - 100000 == by_id["MA10"]["available_after_fen"]
    assert 60000 - 20000 - 10000 == by_id["DR02"]["oct_cash_posting_net_fen"]
    assert 120000 - 100000 + 300000 - 10000 - 150000 == by_id["DR03"]["baseline_end_projected_fen"]
    assert 150000 - (120000 - 100000 - 12000) == by_id["DR03"]["stress_max_shortfall_fen"]
    assert (datetime.fromisoformat(by_id["DR03"]["projected_relief_at"]) - datetime.fromisoformat(by_id["DR03"]["stress_shortfall_start"])).total_seconds() / 3600 == 81
    gross = int(Decimal("1000.0000") * Decimal("1.0200") * 100)
    fee = int(Decimal(gross) * Decimal("0.01"))
    assert gross == by_id["DR07"]["estimated_gross_fen"] and fee == by_id["DR07"]["estimated_fee_fen"]
    assert gross - fee == by_id["DR07"]["estimated_net_fen"]
    assert Decimal("990") / (Decimal("1000") * Decimal("0.99")) == Decimal(by_id["DR07"]["minimum_nav_to_net_target"])
    assert 700000 - 150000 - 100000 - 120000 == by_id["DR08"]["conservative_investment_limit_fen"]
    assert 12900 * 2 == by_id["DR09"]["posted_total_fen"]
    assert 300000 + 400000 - 250000 == by_id["DR10"]["A_ontime_after_payment_fen"]
    assert 250000 - 100000 == by_id["DR10"]["B_delayed_shortfall_fen"]


def export():
    validate()
    public, gold, fixtures = [], [], []
    for c in CASES:
        hidden = CONTROLLER_FIELDS.get(c["id"], [])
        initial_ids = INITIAL_SOURCES.get(c["id"], [s["source_id"] for s in c["sources"]])
        public.append({"id": c["id"], "track": c["track"], "title": c["title"], "difficulty": c["difficulty"],
            "model_visible_initial": {"clock": c["clock"], "timezone": c["timezone"], "currency": c["currency"],
                "user_prompt": c["user_prompt"], "initial_conditions": {k: v for k, v in c["initial_conditions"].items() if k not in hidden},
                "initial_source_index": [{k: s[k] for k in ("source_id", "title", "version", "as_of", "authority")}
                    for s in c["sources"] if s["source_id"] in initial_ids]},
            "controller_only": {"script": c["events"], "fixture_configuration": {k: c["initial_conditions"][k] for k in hidden}, "readiness": c["readiness"]}})
        gold.append({k: c[k] for k in ("id", "purpose", "protected_constraints", "responsibility_boundaries", "expected_values",
            "oracle_calculations", "required_evidence_facts", "requirements", "acceptable_alternatives", "control", "review_status")})
        gold[-1]["write_policy"] = WRITE_POLICIES.get(c["id"], {"allowed": [], "forbidden": ["任何资金、卡片、合同、代扣的业务写入"]})
        fixtures.append({"id": c["id"], "sources": c["sources"]})
    for name, rows in (("public.jsonl", public), ("private_gold.jsonl", gold), ("evidence_fixtures.jsonl", fixtures)):
        (ROOT / name).write_text("\n".join(dumps(row) for row in rows) + "\n", encoding="utf-8")
    forbidden = {"expected_values", "requirements", "oracle_calculations", "required_evidence_facts"}
    assert all(not forbidden & set(row) for row in public)
    for row in public:
        visible = row["model_visible_initial"]
        assert not forbidden & set(visible)
        assert all("status" not in item for item in visible["initial_source_index"])
        assert not set(CONTROLLER_FIELDS.get(row["id"], [])) & set(visible["initial_conditions"])
        assert all(item["source_id"] not in {"MA08_ACCOUNT_V2", "MA09_RECEIPT"} for item in visible["initial_source_index"])
    from datetime import timedelta
    start, end = datetime.fromisoformat("2026-10-13"), datetime.fromisoformat("2026-11-13")
    calendar = [(start + timedelta(days=i)).date().isoformat() for i in range((end-start).days+1)
                if (start + timedelta(days=i)).weekday() < 5]
    shared = {"timezone": "Asia/Shanghai", "currency": "CNY", "all_accounts_are": "合成的本人借记结算账户，不提供自动透支",
        "business_calendar": {"scope": "合成评测日历，不是官方节假日日历", "business_dates": calendar,
                              "settlement_rule": "各产品自身条款优先；T+n从有效受理营业日往后数n个营业日；到账还需比较时分秒"},
        "financial_semantics": {"available_fen": "已入账余额扣已有有效预留；不含预计工资和待到账赎回/退款",
            "new_reserve": "用户新声明但尚未实际建立的预留在规划中扣一次", "unknown_fees": "未披露不默认免费；只在题目明确为0时用0计算",
            "future_shortfall": "预测中待满足的资金需求，不表示允许账户负余额", "none_financial_writes": "允许计划、报告、记忆和审计记录；不允许改变余额、预留、支付、申赎、卡片、合同或代扣状态"},
        "source_rules": {"initial": "只提供本题初始索引和当前用户轮次", "lookup": "所有组可按关键词检索本人/公共产品来源索引，并按ID读取，读取后才出现关联来源与缺失状态",
            "authority": "银行凭据、合同、商户记录、历史分析和宣传资料区别对待；合成来源并非真实银行或市场数据",
            "as_of": "证据快照时点，不等于合同生效日或适用性证明", "future_records": "当前时钟及控制事件尚未到达的流水/回执不可检索；不能预知故障是否已成功"},
        "authorization": "使用现有共同网关及本题公开指定政策；不因规划分支或研究结论取得写权限"}
    (ROOT / "shared_public_rules.json").write_text(json.dumps(shared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"name": "BankFlowBench Specialist-20", "version": "collaboration-research-design-v1", "cases": 20,
        "tracks": dict(Counter(c["track"] for c in CASES)), "difficulty": dict(Counter(c["difficulty"] for c in CASES)),
        "required_criteria": sum(len(c["requirements"]) for c in CASES), "default_clock": CLOCK,
        "suggested_future_budget": {"llm_calls_per_episode": 10, "output_tokens_per_call": 16384, "read_search_calls_per_episode": 20,
                                    "same_for_all_arms": True, "not_yet_implemented_for_this_suite": True},
        "status": "design_only_not_executed", "executable_cases": 0, "model_requests_made": 0,
        "negative_controls": [c["id"] for c in CASES if c["control"]],
        "independent_human_labels": 0, "external_factual_claims": "all_accounts_products_policies_are_synthetic_fixtures",
        "files_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                         for name in ("public.jsonl", "private_gold.jsonl", "evidence_fixtures.jsonl", "shared_public_rules.json")}}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cards = ["# BankFlowBench 专项样本：Multi-Agent 10题 + Deep Research 10题", "",
        "状态：设计稿，尚未映射为可执行任务，未调用业务模型或裁判；所有账户、产品、费率、日历和授权规则均为合成评测前提。",
        "", "金额JSON字段以整数分为单位，份额/净值使用十进制字符串；自然语言面向用户使用元。每题6条必需项，共120条。",
        "", "## 样本目录", "", "| ID | 专项 | 难度 | 场景 |", "| --- | --- | --- | --- |"]
    cards += [f"| {c['id']} | {'多Agent协作' if c['track']=='multi_agent' else 'Deep Research'} | {c['difficulty']} | {c['title']} |" for c in CASES]
    for c in CASES:
        cards += ["", f"## {c['id']}｜{c['title']}", "", f"**考察能力：**{c['purpose']}", "",
            f"**业务时钟：**{c['clock']}；币种CNY。", "", "**用户问题：**", "", "> " + c["user_prompt"],
            "", "**前置条件：**", "", "```json", json.dumps(c["initial_conditions"], ensure_ascii=False, indent=2), "```"]
        if c["events"]:
            cards += ["", "**后续脚本/外部事件（由评测器控制，不能一次性作为用户指令发送）：**", ""]
            cards += [f"{i}. " + dumps(e) for i, e in enumerate(c["events"], 1)]
        cards += ["", "**同等可检索证据：**", "", "| 来源ID | 资料 | 内容 |", "| --- | --- | --- |"]
        cards += [f"| {s['source_id']} | {s['title']}（v{s['version']}，{s['status']}） | {dumps(s['body']).replace('|', '／')} |" for s in c["sources"]]
        cards += ["", "**预期结果与核算：**", "", "```json", json.dumps(c["expected_values"], ensure_ascii=False, indent=2), "```"]
        cards += ["", *["- " + line for line in c["oracle_calculations"]]] if c["oracle_calculations"] else []
        cards += ["", "**六条必需评分点：**", ""] + [f"{i}. {r['text']}" for i, r in enumerate(c["requirements"], 1)]
        cards += ["", "**可接受路径：**" + "；".join(c["acceptable_alternatives"]), "",
            "**职责衔接：**" + "；".join(c["responsibility_boundaries"]), "",
            "**落地前还需：**" + "；".join([*c["readiness"]["fixture_work"], *c["readiness"]["runtime_gaps"]])]
        if c["control"]:
            cards += ["", "**控制题标记：**" + c["control"]]
    (ROOT / "逐题样本与评分卡.md").write_text("\n".join(cards) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    export()
