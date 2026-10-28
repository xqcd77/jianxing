"""Independent integer-fen aggregation of raw rows; never an app service call."""
from collections import defaultdict


def ledger_totals(rows):
    periods = defaultdict(lambda: defaultdict(lambda: {'amount_fen': 0, 'count': 0}))
    for row in rows:
        for period in {row['posted_at'][:4], row['posted_at'][:7]}:
            item = periods[period][row['kind']]
            item['amount_fen'] += row['amount_fen']
            item['count'] += 1
    return {'source': 'independent_raw_ledger_integer_sum', 'unit': 'fen',
        'rule': '按真实入账行分别求和；疑似重复仍是已扣款，不得凭描述添加、删除或退款。预置余额不等于期间收入。',
        'by_period_kind': {p: dict(v) for p, v in sorted(periods.items())}}
