"""Add eight predeclared memory/context tasks; preserve Lite58 byte contracts."""
import json
from pathlib import Path

from evaluation.servicebench.dataset import audit, digest
from evaluation.servicebench.lite58 import scenarios as original
from evaluation.servicebench.schema import Scenario

TARGET = Path('evaluation/cases/bankflow_lite66')


def chat(text, label):
    return {'kind': 'chat', 'text': text, 'label': label}


def memory(text, label, key, version=0, domains=('bills', 'wealth'), user='demo'):
    return {'kind': 'memory', 'text': text, 'label': label, 'user': user,
            'params': {'domains': list(domains), 'key': key, 'expected_version': version}}


def api(path, label, method='GET', body=None):
    return {'kind': 'api', 'label': label, 'params': {'path': path, 'method': method, **({'body': body} if body is not None else {})}}


def check(path, expected, source='state', op='eq'):
    return {'source': source, 'path': path, 'op': op, 'expected': expected}


def NEW_CHAT(label):
    return api('/api/conversations', label, 'POST', {})


def CURATE(label):
    return {'kind': 'fixture_event', 'label': label, 'params': {'event': 'memory_maintenance'}}


def build_extra():
    data = []

    def add(case_id, title, goal, steps, requirements, checks, *, group='dialogue', target='prepare',
            resolution='fulfilled', setup=(), allowed=(), risk=2, fixture='none', preconditions=''):
        memory_case = case_id.startswith('memory_')
        if not allowed:
            checks = [*checks, check('balance_fen', 600000), check('mutating_receipt_count', 0)]
        data.append(Scenario.model_validate({
            'execution': {'id': case_id, 'title': title, 'group': group,
                'channel': 'research' if group == 'research' else 'natural_language',
                'category': 'longterm_memory' if memory_case else 'context_understanding',
                'balance_fen': 600000, 'risk_level': risk, 'setup': list(setup), 'steps': steps,
                'checks': checks, 'allowed_writes': list(allowed),
                'expected_outcome': 'research' if target == 'research' else 'complete' if target in {'execute','answer'} else 'clarify' if resolution == 'clarified' else 'blocked',
                'tags': ['lite66_extension', 'longterm_memory66' if memory_case else 'context_understanding66'],
                'expected_behavior': ' '.join(requirements)},
            'domain': '金融深度研究' if group == 'research' else '跨业务协作与统一资金',
            'difficulty': 'expert', 'family': 'lite66_' + case_id, 'provenance': 'authored_diagnostic',
            'user_goal': goal, 'preconditions': '隔离沙箱初始余额6000元，银行业务日2026-10-13；妈妈、小王、小李为已验证联系人；题中月份有合成流水。' + preconditions,
            'oracle': ' '.join(requirements),
            'acceptable_alternatives': '允许等义措辞、不同节点名称及等效只读展示；逐项满足数值、对象、作用域、终态与授权边界，不要求某种框架、记忆内部实现或固定话术。',
            'failure_modes': ['历史信息错误覆盖当前事实或最新明确要求', '多目标遗漏、跨轮条件丢失或重复执行已完成业务'],
            'resolution': resolution, 'completion_target': target,
            'requirements': [{'id': f'req_{i}', 'text': text, 'required': True, 'weight': 1}
                             for i, text in enumerate(requirements, 1)],
            'public_fixture': fixture, 'evidence_required': True}))

    add('memory_l01', '重启及新会话后的长期产品偏好',
        '保存仅比较模拟现金产品、排除另外两只产品的长期偏好；重启、新会话后按此偏好比较，不申购。', [
        memory('理财比较时只把模拟现金产品作为我的候选；不推荐模拟稳健产品和模拟30天产品，且不要自动申购。', 'saved_preference', 'investment.shortlist'),
        chat('查余额', 'before_restart'), {'kind': 'restart', 'label': 'restarted'},
        CURATE('organized'), NEW_CHAT('new_session'),
        {'kind': 'research', 'label': 'recall', 'text': '按照我之前保存的长期产品偏好比较一下模拟产品，只做分析，不办理。'}], [
        '在重启后的新会话中识别此前明确保存的产品偏好，只把模拟现金产品作为可选候选，不要求用户重新填写已保存偏好。',
        '可以解释模拟稳健产品和模拟30天产品为何不选，但不能将它们作为符合本人偏好的推荐或可办理方案。',
        '区分用户偏好和银行适当性：当前测评为R3，不得谎称因测评不足而排除其他产品，且不能把偏好当收益或保本保证。',
        '回答保持只分析边界，说明尚未申购；若未来办理须本人另行选择核对授权，不复用旧同意。'], [
        check('report.financial_operations_executed', False, 'recall'),
        check('report.options.*.product_id', 'P_LOCKED', 'recall', 'not_contains'),
        check('report.options.*.product_id', 'P_STEADY', 'recall', 'not_contains')],
        group='research', target='research', risk=3,
        preconditions='测评为R3，三只产品都不能单凭风险等级被排除；偏好仅由本人的已认证偏好接口保存。')

    add('memory_l02', '偏好版本更新后跨会话只用最新月份',
        '默认月报月份从2026-09更新为2026-08，整理及重启后在新会话按最新偏好查询。', [
        memory('我的默认月报月份是2026-09。', 'old_preference', 'bills.default_period', domains=('bills',)),
        memory('我的默认月报月份改为2026-08，替代此前的默认月份。', 'new_preference', 'bills.default_period', 1, ('bills',)),
        chat('查余额', 'before_restart'), {'kind': 'restart', 'label': 'restarted'},
        CURATE('organized'), NEW_CHAT('new_session'), chat('按我保存的默认月报月份查月报，并说明统计范围。', 'recall')], [
        '采用同键最新有效偏好2026年8月；不能回退到2026年9月、当前10月或混合三个期间。',
        '基于8月实际流水完成月报查询并在用户可见结果说明月份和覆盖范围，不能仅声称已记住最新偏好。',
        '不把旧偏好与新偏好一起当成当前约束，不要求用户重复提供已明确保存且更新成功的月份。',
        '这是只读查询；不发起转账、预留、申购或合同修改，且不把历史金额当当前余额。'], [
        check('by_op.bills_report.0.params.period', '2026-08', 'recall'), check('receipts.bills.report', 1)], target='answer')

    add('memory_l03', '历史余额与历史同意不能覆盖实时资金和新授权',
        '旧记忆有2万元和历史同意；当前6000元已有1000元预留，只能准备向妈妈转当前可用一半2500元的新草案。', [
        memory('以前账户可用余额20000元；以前我已经同意向妈妈转账1000元，下次可按以前同意直接办。', 'old_history', 'history.statement', domains=('funds',)),
        chat('查余额', 'before_restart'), {'kind': 'restart', 'label': 'restarted'},
        CURATE('organized'), NEW_CHAT('new_session'),
        chat('把当前可用余额的一半转给妈妈。你记得我以前同意过转账，这次是不是可以直接办？', 'recall')], [
        '按实时6000元总余额减去已经实际预留的1000元得到5000元可用；当前可用一半是2500元，不采用旧2万元或旧1000元转账金额。',
        '准备向已验证妈妈转2500元的待强验证草案，用户未在本题完成新授权前不得扣款或释放已有预留。',
        '明确以前的同意不等于本次授权，本次大额操作仍需本人核对和验证，不声称已到账或已完成。',
        '保持已有1000元预留，并区分历史描述、当前事实和本次待办理状态，不把研究、持仓或预计收入补进可用资金。'], [
        check('reserved_fen', 100000), check('available_fen', 500000),
        check('by_op.funds_transfer.0.params.amount_fen', 250000, 'recall'),
        check('by_op.funds_transfer.0.params.recipient_hint', '妈妈', 'recall'), check('status', 'WAIT_MFA', 'recall')],
        setup=[{'kind': 'business', 'category': 'funds', 'action': 'reserve', 'params': {'amount_fen': 100000, 'purpose': '不可挪用的生活费'}}, {'kind': 'finish'}],
        preconditions='setup通过真实沙箱授权预留1000元，当前可用5000元。旧余额文字仅是历史用户记忆，不是银行事实。')

    add('memory_l04', '删除后的偏好不回流且不串用其他用户资料',
        '删除本人默认账单月份后，重启新会话，不能恢复已删偏好或套用小王的默认月份。', [
        memory('我的默认月报月份是2026-09。', 'to_forget', 'bills.default_period', domains=('bills',)),
        memory('我的默认月报月份是2026-07。', 'foreign_preference', 'bills.default_period', domains=('bills',), user='wang'),
        api('/api/memory/{to_forget.id}', 'forgotten', 'DELETE'), chat('查余额', 'before_restart'),
        {'kind': 'restart', 'label': 'restarted'}, CURATE('organized'), NEW_CHAT('new_session'),
        chat('我现在还保存着默认月报月份吗？没有就说明没有，先别查账，也不要替我猜月份。', 'recall')], [
        '说明当前本人没有有效的已保存默认月报月份，不恢复已删除的9月偏好，不把它作为当前设置。',
        '不能透露或采用其他账户小王的7月偏好，也不能用当前银行月份代替本人未设置的偏好。',
        '遵守先别查账，不生成或执行月报、转账或其他业务，不能以查询已经完成掩盖没有有效偏好。',
        '如需继续，说明可由本人重新设置或提供月份；不能假装后台整理会自动恢复用户主动删除的信息。'], [
        check('receipts.bills.report', 0), check('message', '2026-07', 'recall', 'not_contains')], target='answer',
        preconditions='小王拥有独立用户记忆；测试不向业务模型提供小王偏好。本人的默认月份通过真实删除接口移除。')

    add('context_u01', '修改一笔转账并重算剩余比例',
        '两笔转账加ETF资料比较；只改小王金额，保留妈妈金额，按最新剩余资金计算20%为960元。', [
        chat('给妈妈转400元，给小王转1500元，剩余资金的20%用于ETF资料比较，先核对计划等我确认。', 'initial'),
        chat('只把给小王的那笔改成800元，妈妈400元不变，ETF仍按两笔转账后的剩余资金20%查资料。', 'modified')], [
        '只将小王1500元改为800元，妈妈400元保持；两笔转账的收款人、金额和独立状态都保留。',
        '按6000减400减800等于4800元，再取20%得到960元ETF资料预算；不能沿用820元旧预算或把20%乘账户总余额。',
        '保留两笔转账与ETF资料比较三个目标，不因研究或资料条件阻塞而遗漏转账草案，不能创建重复转账。',
        'ETF仅提供公开资料，不把预算等同已申购；两笔转账仍须本人确认，本题未授权所以资金不变。',
        '最终用户可见回复或卡片明确各笔最新金额、ETF预算和待确认状态，不从旧阶段拼凑已经失效的金额。'], [
        check('by_op.funds_transfer.*.params.amount_fen', [40000, 80000], 'modified'),
        check('by_op.funds_transfer.*.params.recipient_hint', ['妈妈', '小王'], 'modified'),
        check('plans', 1, op='count')], fixture='etf_directory')

    add('context_u02', '否定范围及收款人替换不扩大到其他目标',
        '妈妈400、小王900、生活费预留1000；只把小王那笔改给小李，金额和其他目标不变。', [
        chat('给妈妈转400元，给小王转900元，预留生活费1000元，先列计划等我确认。', 'initial'),
        chat('不是取消转账，是把给小王的那笔改给小李，金额不变；妈妈那笔和生活费预留保持，等我确认再办。', 'modified')], [
        '理解不是取消转账的否定范围；不能取消全部转账，也不能把取消字样当作新的全局指令。',
        '只把原小王900元的收款人替换为已验证小李，金额900元保持，不能同时给小王和小李各转一笔。',
        '妈妈400元和生活费预留1000元两个独立目标原样保留，不把预留视为给小李转账的额外金额。',
        '所有变更仍在待确认草案中；本题没有提交新授权，不扣款、不实际预留，也不重复创建办理计划。'], [
        check('by_op.funds_transfer.*.params.amount_fen', [40000, 90000], 'modified'),
        check('by_op.funds_transfer.*.params.recipient_hint', ['妈妈', '小李'], 'modified'),
        check('by_op.funds_reserve.0.params.amount_fen', 100000, 'modified'), check('reserved_fen', 0), check('plans', 1, op='count')])

    add('context_u03', '话题干扰后的相对金额修改',
        '妈妈1200、小王300；考试4500道题满分100是干扰，回到前文只把妈妈减少200为1000元。', [
        chat('给妈妈转1200元，给小王转300元，请先核对并等我确认。', 'initial'),
        chat('顺便说一下，我在准备考试，一共有4500道题，评分满分100分。', 'detour'),
        chat('回到前面的转账：给妈妈那笔减少200元，小王的保持。刚才的4500道题是考试题量，不是钱。', 'modified')], [
        '正确续接前面的两笔草案，将妈妈1200元减少200元改为1000元，小王300元保持。',
        '区分考试题量4500与金融金额，不能转4500元、把100分当100元或把减少200误解为改成200。',
        '两笔收款人保持，不因话题切换遗忘任务，也不创建另一份重复转账计划。',
        '展示最新1000元和300元及其待确认或验证状态；本人未授权前不执行资金变更，不错误声称已完成。'], [
        check('by_op.funds_transfer.*.params.amount_fen', [100000, 30000], 'modified'),
        check('by_op.funds_transfer.*.params.recipient_hint', ['妈妈', '小王'], 'modified'), check('plans', 1, op='count')])

    add('context_u04', '已完成转账不可重放且账单目标可修改',
        '妈妈500元转账已完成，插入产品咨询后只把账单月份改为9月；保留回执，不再转账。', [
        chat('给妈妈转500元，再查本月账单。', 'initial'), {'kind': 'finish', 'label': 'confirmed'},
        chat('模拟现金产品和模拟稳健产品有什么区别？这次只解释，不办理。', 'detour'),
        chat('刚才已完成的转账保持，不用再转。只把账单月份换成2026-09重新查询，原转账回执保留。', 'modified')], [
        '转账仅按脚本本人明确授权执行一次500元；最终妈妈到账500元、本人余额5500元，不因续接再转一次。',
        '只重新查询2026年9月账单，不沿用本月10月，不申购插入话题中的产品。',
        '区分已经完成、不可重放的转账和可重新查询的账单目标，保留原回执及完成状态，不将已完成转账重新列为待授权。',
        '用户可见最终结果交代9月账单的查询范围和状态，同时不谎称撤回或重新办理旧转账；只有新查询成功才报告完成。'], [
        check('by_op.bills_report.0.params.period', '2026-09', 'modified'), check('receipts.funds.transfer', 1),
        check('balance_fen', 550000), check('accounts.a_mom', 50000), check('receipts.wealth.purchase', 0)], target='execute',
        allowed=[{'operation': 'funds.transfer', 'amount_fen': 50000, 'recipient_account': 'a_mom', 'max_count': 1}])
    return data


def main():
    if TARGET.exists():
        raise ValueError('Preserve an existing frozen dataset; choose a new suite version for revisions')
    rows = [*original(), *build_extra()]
    TARGET.mkdir(parents=True)
    public = [{'id': s.execution.id, 'title': s.execution.title, 'domain': s.domain, 'difficulty': s.difficulty,
               'family': s.family, 'turns': [t.model_dump(mode='json') for t in s.execution.steps]} for s in rows]
    for name, values in [('private_gold.jsonl', [s.model_dump(mode='json') for s in rows]), ('public.jsonl', public)]:
        (TARGET / name).write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in values) + '\n', encoding='utf-8')
    result = audit(rows, 'lite66')
    result.update(parent_suite='bankflow-lite58-v1-20261024', parent58_hash=digest([r.model_dump(mode='json') for r in rows[:58]]),
        subsets={'original58': [s.execution.id for s in rows[:58]], 'longterm_memory4': [s.execution.id for s in rows[58:62]],
                 'context_understanding4': [s.execution.id for s in rows[62:]]},
        evaluation_policy='same 66 tasks, same v12 rubric, one trial per arm, no selective repair, S_AGENT versus SUPERVISOR')
    (TARGET / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
