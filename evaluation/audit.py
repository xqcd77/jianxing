"""Static dataset coverage inventory. This does not run models or award scores."""
from collections import Counter
import hashlib
import json

from bank_agent.bank_rules import DEFINITIONS

from .blueprints import inventory
from .dataset import core35
from .schemas import SUPPORTED_STEP_KINDS, validate_executable


def dataset_audit():
    cases, blueprints = core35(), inventory()
    from .dev100 import dev100
    expanded = dev100()
    for case in cases:
        validate_executable(case)
    references = {}
    weak_queries = []
    for case in cases:
        # Explicit references are auditable; a natural-language mention is not
        # evidence that the corresponding operation was ever exercised.
        operations = {s["operation"] for s in case.allowed_writes}
        operations |= {c.path.removeprefix("receipts.") for c in case.checks if c.path.startswith("receipts.")}
        references[case.id] = sorted(operations & set(DEFINITIONS))
        if any(c.op == "exists" and c.path.endswith(".data") for c in case.checks):
            weak_queries.append(case.id)
    referenced = set().union(*(set(v) for v in references.values()))
    return {
        "purpose": "Static development-set audit; references are not measured operation coverage",
        "core": {
            "cases": len(cases), "development_only": True, "human_double_labelled": False,
            "dataset_hash": hashlib.sha256(json.dumps([c.model_dump(mode="json") for c in cases],
                ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "groups": dict(Counter(c.group for c in cases)),
            "channels": dict(Counter(c.channel for c in cases)),
            "categories": dict(Counter(c.category for c in cases)),
            "starting_balance_fen": dict(Counter(str(c.balance_fen) for c in cases)),
            "risk_levels": dict(Counter(str(c.risk_level) for c in cases)),
            "risk_valid_until": dict(Counter(c.risk_valid_until for c in cases)),
            "multiple_chat_turn_cases": [c.id for c in cases if sum(s.kind == "chat" for s in c.steps) > 1],
            "readonly_ledger_guard_cases": [c.id for c in cases if any(x.path == "new_ledger_entry_count" for x in c.checks)],
            "query_data_exists_check_cases": weak_queries,
            "state_assertion_count": sum(x.source == "state" for c in cases for x in c.checks),
            "observation_assertion_count": sum(x.source != "state" for c in cases for x in c.checks),
            "supported_step_kinds": sorted(SUPPORTED_STEP_KINDS),
            "used_step_kinds": dict(Counter(s.kind for c in cases for s in c.setup + c.steps)),
            "explicit_operation_references_by_case": references,
            "referenced_catalog_operations": sorted(referenced),
            "catalog_operations_without_explicit_reference": sorted(set(DEFINITIONS) - referenced),
            "catalog_operation_count": len(DEFINITIONS),
        },
        "dev100": {"cases": len(expanded), "groups": dict(Counter(c.group for c in expanded)),
            "distinct_starting_balances": len({c.balance_fen for c in expanded}),
            "risk_levels": sorted({c.risk_level for c in expanded}),
            "expired_assessment_cases": [c.id for c in expanded if c.risk_valid_until < "2026-10-13"],
            "development_only": True, "human_double_labelled": False},
        "blueprint": {"cases": len(blueprints), "executable_cases": len(expanded),
            "core_already_implemented_separately": len(cases),
            "groups": dict(Counter(c.group for c in blueprints)),
            "remaining_to_implement": len(blueprints) - len(expanded)},
        "cautions": [
            "Do not count 100 blueprint rows as 100 completed executions.",
            "Do not count an explicit operation reference as successful runtime coverage.",
            "An exists assertion can be useful alongside value assertions; inspect each listed case.",
            "Multiple chat turns do not by themselves prove dialogue or correction coverage.",
        ],
    }
