from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from bank_agent.errors import BankError
from bank_agent.storage import MemoryRecord


def preference(memory, text, **kwargs):
    return memory.remember("u_demo", text, kind="preference", source_type="user_preference",
                           roles=["bills"], scope="user", conflict_key="report_style", **kwargs)


def test_new_keyed_preference_supersedes_search_but_preserves_original(client):
    memory = client.app.state.memory
    old = preference(memory, "偏好详细的文字账单。")
    memory.search("u_demo", "详细文字账单", "bills")
    new = preference(memory, "偏好简洁的表格账单。", expected_version=1)
    result = memory.search("u_demo", "详细文字账单", "bills")
    assert [item["id"] for item in result["items"]] == [new]
    original = memory.reread("u_demo", old, role="bills")
    assert original["text"] == "偏好详细的文字账单。"
    assert original["memory_status"] == "superseded" and original["superseded_by"] == new
    assert memory.reread("u_demo", new, role="bills")["version"] == 2
    assert preference(memory, "偏好简洁的表格账单。", expected_version=1) == new  # Safe idempotent replay.
    with pytest.raises(BankError) as caught:
        preference(memory, "另一个偏好。", expected_version=1)
    assert caught.value.code == "MEMORY_VERSION_CONFLICT" and caught.value.current_version == 2


def test_concurrent_stale_updates_cannot_both_commit(client):
    memory = client.app.state.memory
    preference(memory, "最初账单偏好。")

    def update(text):
        try:
            return preference(memory, text, expected_version=1)
        except BankError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        result = list(workers.map(update, ["改为图表。", "改为文字。"]))
    assert result.count("MEMORY_VERSION_CONFLICT") == 1
    current = [row for row in memory.list("u_demo") if row["status"] == "active"]
    assert len(current) == 1 and current[0]["version"] == 2


def test_conflict_identity_is_user_role_and_scope_bound_and_unkeyed_text_is_append_only(client):
    memory = client.app.state.memory
    first = preference(memory, "偏好文字。")
    other = memory.remember("u_li", "偏好表格。", kind="preference", source_type="user_preference",
                            roles=["bills"], scope="user", conflict_key="report_style")
    wealth = memory.remember("u_demo", "偏好简洁。", kind="preference", source_type="user_preference",
                             roles=["wealth"], scope="user", conflict_key="report_style")
    unkeyed = [memory.remember("u_demo", text, kind="preference", source_type="user_preference",
                               roles=["bills"], scope="user") for text in ["请用表格。", "不要用表格。"]]
    assert set(i["id"] for i in memory.search("u_demo", "表格", "bills")["items"]) == {first, *unkeyed}
    assert memory.reread("u_li", other)["version"] == memory.reread("u_demo", wealth)["version"] == 1


def test_source_identity_is_immutable_and_user_message_cannot_replace_bank_evidence(client):
    memory = client.app.state.memory
    memory.remember("u_demo", "原始要求。", scope="user", roles=["funds"], record_id="memory_fixed")
    with pytest.raises(BankError) as caught:
        memory.remember("u_demo", "改写历史。", scope="user", roles=["funds"], record_id="memory_fixed")
    assert caught.value.code == "MEMORY_IMMUTABLE"
    with pytest.raises(BankError):
        memory.remember("u_demo", "用户说余额很多。", scope="user", roles=["funds"], conflict_key="balance")
    assert memory.reread("u_demo", "memory_fixed")["text"] == "原始要求。"


def test_product_snapshots_follow_exact_product_versions_and_legacy_snapshots_are_superseded(client):
    memory = client.app.state.memory
    old = memory.remember("u_demo", "模拟产品旧锁定期。", kind="product", source_type="product_snapshot",
                          source_id="P_SYNTHETIC", payload={"lock_days": 30}, scope="user", roles=["wealth"])
    # Simulate an original record written before conflict metadata existed.
    with memory.db.write() as session:
        row = session.get(MemoryRecord, old)
        row.payload = {"lock_days": 30}
    new = memory.remember("u_demo", "模拟产品新锁定期。", kind="product", source_type="product_snapshot",
                          source_id="P_SYNTHETIC", payload={"lock_days": 10}, scope="user", roles=["wealth"])
    assert [row["id"] for row in memory.search("u_demo", "锁定期", "wealth")["items"]] == [new]
    assert memory.reread("u_demo", old)["text"] == "模拟产品旧锁定期。"


def test_preference_api_exposes_conflict_and_current_version(client, headers):
    payload = {"text": "偏好文字账单。", "domains": ["bills"], "key": "report_style"}
    first = client.post("/api/memory/preferences", headers=headers, json=payload)
    assert first.status_code == 200
    conflict = client.post("/api/memory/preferences", headers=headers, json={**payload, "text": "偏好表格。"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "MEMORY_VERSION_CONFLICT"
    assert client.post("/api/memory/preferences", headers=headers,
                       json={**payload, "text": "偏好表格。", "expected_version": 1}).status_code == 200
    rows = client.get("/api/memory", headers=headers).json()
    assert sorted(row["version"] for row in rows) == [1, 2]
    assert len([row for row in rows if row["status"] == "active"]) == 1


def test_expiry_and_deletion_do_not_revive_old_values_or_reuse_revision_numbers(client):
    memory = client.app.state.memory
    first = preference(memory, "最初偏好。")
    with memory.db.write() as session:
        session.get(MemoryRecord, first).expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    second = preference(memory, "过期后新偏好。", expected_version=0)
    assert memory.reread("u_demo", second)["version"] == 2
    memory.forget("u_demo", second)
    third = preference(memory, "删除后新偏好。", expected_version=0)
    assert memory.reread("u_demo", third)["version"] == 3
    with pytest.raises(BankError):
        preference(memory, "使用过期版本覆盖。", expected_version=2)


def test_explicit_product_source_versions_cannot_move_backwards(client):
    memory = client.app.state.memory
    values = dict(kind="product", source_type="product_snapshot", source_id="P_VERSIONED", scope="user", roles=["wealth"])
    memory.remember("u_demo", "新版产品条款。", payload={"version": 2, "lock_days": 10}, **values)
    with pytest.raises(BankError) as caught:
        memory.remember("u_demo", "旧版产品条款。", payload={"version": 1, "lock_days": 30}, **values)
    assert caught.value.code == "MEMORY_SOURCE_VERSION_STALE"
    with pytest.raises(BankError) as caught:
        memory.remember("u_demo", "同版本矛盾条款。", payload={"version": 2, "lock_days": 30}, **values)
    assert caught.value.code == "MEMORY_SOURCE_VERSION_CONFLICT"
    with pytest.raises(BankError):
        memory.remember("u_demo", "相同结构却矛盾的原文。", payload={"version": 2, "lock_days": 10}, **values)
