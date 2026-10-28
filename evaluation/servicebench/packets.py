"""The candidate channel contains system answers, never gold or user text."""
import json

from evaluation.browser import load_browser_capture
from evaluation.trace import scrub

from .fixtures import public_snapshot
from .judge import Packet, Reference


def api_text(result):
    parts = []
    for turn in result.get("responses", []):
        reply = turn["response"]
        text = []
        for key in ("message", "assistant_message", "summary"):
            if isinstance(reply.get(key), str) and reply[key].strip():
                text.append(reply[key])
        report = reply.get("report") or {}
        if report.get("summary"):
            text.append(str(report["summary"]))
        for claim in report.get("findings", []):
            if claim.get("text"):
                text.append(claim["text"])
        # These are literal returned error messages, not bank state rendered by
        # the evaluator. Structured success data is not inserted as an answer.
        for node in reply.get("nodes", []):
            error = node.get("error") or {}
            if error.get("message"):
                text.append(error["message"])
            if error.get("next_step"):
                text.append(error["next_step"])
        if (reply.get("error") or {}).get("message"):
            text.append(reply["error"]["message"])
        if text:
            parts.append("[系统回复 "+str(turn.get("label", ""))+"]\n"+"\n".join(dict.fromkeys(text)))
    return "\n\n".join(parts)


def build(scenario, result, episode_path=None, browser=False, clean_dom=False, judge_revision="v3"):
    if browser:
        if episode_path is None:
            raise ValueError("DOM judgement requires a frozen episode")
        capture = load_browser_capture(episode_path, result["manifest_hash"], clean_dom, 'v3' if judge_revision in {'v9','v10','v11','v12'} else 'v2')
        if capture.get("assistant_only") is not True:
            raise ValueError("Use capture_servicebench_dom.cjs; old captures mix user and system text")
        # Keep each state tied to its phase and width. Do not merge all frames as
        # if a final answer had already been visible before confirmation.
        parts = []
        seen_views = set()
        for frame in capture["frames"]:
            text = ("\n".join("["+region["kind"]+"]\n"+region["text"] for region in frame["regions"])
                    if clean_dom else frame.get("assistant_text", ""))
            if text.strip():
                identity = (frame['source'], frame.get('view', 'initial'), text)
                if judge_revision in {'v9','v10','v11','v12'} and identity in seen_views:
                    continue  # Exact duplicate viewport text; no changed wording is discarded.
                seen_views.add(identity)
                view = ' / 用户展开只读详情后' if frame.get('view') == 'read_only_disclosures' else ' / 初始可见'
                parts.append(f"[界面阶段 {frame['source']} / 宽度{frame['width']}{view if judge_revision in {'v9','v10','v11','v12'} else ''}]\n"+text)
        candidate, scope = "\n\n".join(parts), ("browser_dom_regions_v3" if judge_revision in {"v4", "v5", "v6", "v7", "v8"} else "browser_dom_regions_v2") if clean_dom else "browser_dom_replay"
        if clean_dom and judge_revision in {'v9','v10','v11','v12'}:
            scope = 'browser_dom_regions_v4'
    else:
        candidate, scope = api_text(result), "api_response_text"
    case = scenario.execution
    fixture_clock = next((p["clock"] for p in case.patches if p.get("kind") == "specialist_initial"), None)
    dimensions = ["coverage", "actionability", "clarity"]
    # A reporting taxonomy may merge FAQ with safety. Judge applicability comes
    # from the original task kind, not a renamed presentation/domain bucket.
    if case.category != "服务对话":
        dimensions.append("banking_correctness")
    if scenario.evidence_required:
        dimensions.append("evidence_reasoning")
    evidence = [Reference(id="oracle", authority="independent_oracle", text=scenario.oracle),
        Reference(id="initial", authority="bank_snapshot", text=json.dumps({"opening_balance_fen": case.balance_fen,
            "risk_level": case.risk_level, "risk_valid_until": case.risk_valid_until,
            "business_date": fixture_clock[:10] if fixture_clock else "2026-10-13",
            "preconditions": scenario.preconditions, "fixture_changes": case.patches}, ensure_ascii=False)),
        Reference(id="measured", authority="bank_snapshot", text=json.dumps({key: result.get("state", {}).get(key)
            for key in ("balance_fen", "reserved_fen", "available_fen", "receipts", "users", "authorization_tiers",
                        "holding_count", "node_errors")}, ensure_ascii=False))]
    if case.id == "service_model":
        evidence.append(Reference(id="configuration", authority="runtime_config", text=json.dumps(result.get("runtime_config", {}))))
    if clean_dom:
        # Independent raw source, never computed by BillsService. Read only the
        # requesting synthetic user's rows; no other account's data enters a judge.
        import sqlite3
        from pathlib import Path
        database = Path(episode_path).parent/"sandbox"/"bank.sqlite3"
        with sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            raw = [dict(row) for row in connection.execute(
                "SELECT id,kind,amount_fen,category,posted_at,subject,merchant_id FROM transactions WHERE owner_id=? ORDER BY posted_at,id", ("u_demo",))]
        text = json.dumps(raw, ensure_ascii=False)
        for index in range(0, max(1, len(text)), 15000):
            evidence.append(Reference(id=f"independent_ledger_{index//15000+1}", authority="bank_snapshot",
                text="本次隔离账户原始流水，顺序拼接这些分块：\n"+text[index:index+15000]))
        if judge_revision in {'v9','v10','v11','v12'}:
            from .ledger_oracle import ledger_totals
            evidence.append(Reference(id='independent_ledger_totals', authority='bank_snapshot',
                text=json.dumps(ledger_totals(raw), ensure_ascii=False)))
            controls = [{k:event.get(k) for k in ('method','path','user','status')}
                for event in result.get('trace', []) if event.get('kind') == 'api'
                and event.get('path', '').endswith(('/approve','/approve-batch'))]
            evidence.append(Reference(id='controller_ui_approvals', authority='bank_snapshot',
                text=json.dumps({'source':'recorded_authenticated_test_user_ui_requests',
                    'meaning':'这些是回放脚本中真实发生的用户审批请求，不是模型自行授权。可能同意或拒绝；HTTP成功不等于付款成功，具体执行仍核对授权和回执。',
                    'requests':controls, 'grants':result.get('state',{}).get('grants',[])}, ensure_ascii=False)))
    if scenario.public_fixture != "none":
        snapshot = public_snapshot(scenario.public_fixture)
        evidence.append(Reference(id="public_snapshot", authority="public_fixture", text=json.dumps(snapshot, ensure_ascii=False)))
        evidence.append(Reference(id="public_access", authority="bank_snapshot", text=json.dumps(result.get("public_requests", []), ensure_ascii=False)))
    for observation in result.get("observations", {}).values():
        report = observation.get("report") or {}
        if report:
            for item in report.get("evidence", []):
                ref = "research_"+item["id"]
                if ref in {e.id for e in evidence}:
                    continue
                text = json.dumps({"tool": item["tool"], "version": item["version"], "data": item["data"]}, ensure_ascii=False)
                if len(text) <= 16000:
                    evidence.append(Reference(id=ref, authority="research_source", text=text))
                else:
                    # Preserve every byte in ordered chunks. Do not filter to a
                    # flattering excerpt or infer absence from omitted rows.
                    pieces = [text[i:i+15000] for i in range(0, len(text), 15000)]
                    for index, piece in enumerate(pieces, 1):
                        evidence.append(Reference(id=f"{ref}_part_{index}", authority="research_source",
                            text=f"完整来源{ref}；分块{index}/{len(pieces)}；按顺序拼接以下原文：\n"+piece))
    if judge_revision == 'v12' and 'longterm_memory66' in case.tags:
        import sqlite3
        from pathlib import Path
        database = Path(episode_path).parent / 'sandbox/bank.sqlite3'
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            records = [dict(r) for r in connection.execute(
                "SELECT id,owner_id,conversation_id,scope,kind,roles,source_id,source_type,text,version,expires_at,"
                "coalesce(json_extract(payload,'$._memory_resolution.status'),'active') AS status "
                "FROM shared_memories ORDER BY owner_id,id")]
        text = json.dumps({'source': 'independent_readonly_sql', 'records': records}, ensure_ascii=False)
        for index in range(0, len(text), 15000):
            evidence.append(Reference(id=f'memory_snapshot_{index//15000+1}', authority='bank_snapshot',
                text='顺序拼接记忆原文快照；按owner与有效版本核对，非候选回答：\n' + text[index:index+15000]))
    dialogue = ([{'kind': s.kind, 'user': s.user, 'label': s.label, 'text': s.text,
                  'action': s.params if s.kind in {'memory','api','fixture_event'} else {}}
                 for s in case.steps if s.text or s.kind in {'api','restart','fixture_event'}]
                if judge_revision == 'v12' else [{'kind': s.kind, 'text': s.text} for s in case.steps if s.text])
    packet = Packet(user_goal=scenario.user_goal, dialogue=dialogue,
        expected_behavior=scenario.oracle, acceptable_alternatives=scenario.acceptable_alternatives,
        candidate_output=candidate, requirements=[r.model_dump(mode="json") for r in scenario.requirements],
        evidence=evidence, evidence_required=scenario.evidence_required, applicable_dimensions=dimensions, observation_scope=scope)
    if len(json.dumps(packet.model_dump(mode="json"), ensure_ascii=False)) > 160000:
        raise ValueError("JUDGE_INPUT_BUDGET_EXCEEDED_WITHOUT_TRUNCATION")
    return Packet.model_validate(scrub(packet.model_dump(mode="json")))
