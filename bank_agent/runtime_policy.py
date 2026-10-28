"""Ownership of runtime decisions; no second agent loop or session store."""
SDK_MAX_TURNS = 1
OWNERSHIP = {
    'request_understanding':'request_intake',
    'goal_state':'owned_versioned_sql_agenda',
    'model_proposals':'agents_sdk_single_turn',
    'workflow_and_checkpoints':'langgraph',
    'budget_and_telemetry':'harness',
    'request_cancel_and_lease':'durable_chat_task_service',
    'financial_writes_and_authorization':'bank_gateway',
    'authoritative_evidence':'sql_source_reread',
    'semantic_recall':'chroma_rebuildable_index',
}


def role_settings(settings):
    return {role:{'thinking':settings.model_thinking if role in {'bills_research','wealth_research'} else False,
                  'sdk_max_turns':SDK_MAX_TURNS,'financial_tools':False}
            for role in ('supervisor','business','verifier','bills_research','wealth_research')}
