"""Translate owned current product preferences, never balances or authorizations."""
import re


def product_exclusions(items, products, current_request):
    # A deliberate current override has priority; merely asking to recall an old
    # preference must not silently erase it.
    if re.search(r'(?:这次|本次).{0,12}(?:不按|不用|取消|忽略|改掉).{0,10}偏好', current_request):
        return []
    excluded = set()
    for item in items:
        text = item.get('text', '')
        clauses = re.split(r'[；;。\n]', text)
        for clause in clauses:
            if re.search(r'不是不|并非不|不再排除|取消排除|不要只|不是只|并非只', clause):
                continue  # An ambiguous or negated selection is not a prohibition.
            named = {p['id'] for p in products if p['name'] in clause or p['id'] in clause}
            if re.search(r'不推荐|排除|不考虑|不选|不买|不购买|不要(?:推荐|考虑|选择|买|购买)|别(?:推荐|考虑|选|买)', clause):
                excluded.update(named)
            if re.search(r'只(?:把|将|比较|考虑|选)', clause) and len(named) == 1:
                excluded.update(p['id'] for p in products if p['id'] not in named)
    return sorted(excluded)
