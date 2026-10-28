"""Business prerequisites and ordering edges have different failure semantics."""

import re

from .errors import BankError

REFERENCE = re.compile(r"^\$([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*)$")


def references(value):
    if isinstance(value, str) and value.startswith("$"):
        match = REFERENCE.fullmatch(value)
        if not match:
            raise BankError("INVALID_REFERENCE", "结果引用必须使用$任务编号.字段名格式")
        yield match.group(1), match.group(2)
    elif isinstance(value, dict):
        for item in value.values():
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def topological(specs):
    nodes = {n.id: n for n in specs}
    if len(nodes) != len(specs) or not nodes or len(nodes) > 64:
        raise BankError("INVALID_DAG", "计划节点编号重复、为空或超过64个")
    done, result = set(), []
    while len(result) < len(specs):
        ready = []
        for node in specs:
            edges = {*node.depends_on, *node.after}
            if not edges <= nodes.keys():
                raise BankError("MISSING_DEPENDENCY", "计划引用不存在的节点")
            if node.id not in done and edges <= done:
                ready.append(node)
        if not ready:
            raise BankError("DAG_CYCLE", "计划依赖存在循环")
        node = ready[0]  # Stable input order; t10 must not precede t2.
        result.append(node)
        done.add(node.id)
    return result


def precedes(nodes, before, after):
    seen = set()

    def walk(key):
        if key == before:
            return True
        if key in seen:
            return False
        seen.add(key)
        node = nodes[key]
        return any(walk(dep) for dep in [*node.depends_on, *node.after])

    return walk(after)
