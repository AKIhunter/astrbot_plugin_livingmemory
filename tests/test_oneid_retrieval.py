# one_id 使用合成非空数据验证撤销和候选前限制。
"""非空 SQLite：投影/候选过滤/旧版本使用点，独立期望不依赖实现算结果。"""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from astrbot_plugin_livingmemory.core.identity_access import IdentityAccess, identity_access, current_identity_access
from astrbot_plugin_livingmemory.core.managers.memory_engine_identity import MemoryEngineIdentityMixin
from astrbot_plugin_livingmemory.core.retrieval.bm25_retriever import BM25Retriever
from astrbot_plugin_livingmemory.storage.identity_store import IdentityStore
from astrbot_plugin_livingmemory.storage.oneid_store import IdentityContextChanged
from astrbot_plugin_livingmemory.core.page_api_modules.oneid_handler import authenticated_operator


class ProjectionEngine(MemoryEngineIdentityMixin):
    pass


class Tokenizer:
    async def tokenize_async(self, text, **kwargs):
        return text.split()


@pytest_asyncio.fixture
async def runtime(tmp_path):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)")
        fixtures = [
            (1, "synthetic tea", {"source_account": "qq:a", "owner_account": "qq:bot", "session_id": "qq:FriendMessage:a"}),
            (2, "synthetic tea", {"source_account": "bili:b", "owner_account": "bili:bot", "session_id": "bili:FriendMessage:b"}),
            (3, "synthetic tea", {"source_account": "qq:other", "owner_account": "qq:bot", "session_id": "qq:FriendMessage:other"}),
            (4, "synthetic tea", {"one_id": "human", "person_id": "human", "session_id": "group"}),
            (5, "synthetic tea", {"subject_accounts": ["qq:a", "qq:other"], "identity_history_mixed": True, "session_id": "group"}),
        ]
        for memory_id, content, metadata in fixtures:
            db.execute("INSERT INTO documents VALUES(?,?,?)", (
                memory_id, content, json.dumps({**metadata, "status": "active", "persona_id": "p"}),
            ))
        for memory_id in range(6, 106):
            db.execute("INSERT INTO documents VALUES(?,?,?)", (
                memory_id, "synthetic synthetic synthetic tea", json.dumps({
                    "source_account": "qq:other", "session_id": "shared", "persona_id": "p", "status": "active",
                }),
            ))
    store = IdentityStore(str(path))
    await store.initialize()
    await store.register_entity(one_id="human", entity_type="human", operator="admin")
    await store.register_entity(one_id="bot", entity_type="bot", operator="admin")
    for platform, account, one_id in [
        ("qq", "a", "human"), ("bili", "b", "human"),
        ("qq", "bot", "bot"), ("bili", "bot", "bot"),
    ]:
        await store.upsert_binding(one_id=one_id, platform=platform, account_id=account,
                                   operator="admin", reason="synthetic fixture")
    engine = ProjectionEngine()
    engine.identity_store = store
    engine.db_connection = await aiosqlite.connect(path)
    bm25 = BM25Retriever(str(path), Tokenizer())
    await bm25.initialize()
    for memory_id in range(1, 106):
        await bm25.add_document(memory_id, "synthetic tea" if memory_id < 6 else "synthetic synthetic synthetic tea")
    yield engine, store, bm25
    await engine.db_connection.close()


@pytest.mark.asyncio
async def test_person_and_bot_projection_keep_owner_and_subject_separate(runtime):
    engine, _, _ = runtime
    human = await engine.project_identity(one_id="human", include_records=True)
    assert human["memory_ids"] == [2, 1]
    assert human["attributed_memory_count"] == 2
    assert human["uncertain_memory_count"] == 2
    owner = await engine.project_identity(owner_one_id="bot")
    assert owner["memory_ids"] == [3, 2, 1]
    bot_subject = await engine.project_identity(one_id="bot")
    assert bot_subject["memory_ids"] == []


@pytest.mark.asyncio
async def test_projection_keeps_storage_session_separate_from_original_scene(runtime):
    engine, _, _ = runtime
    await engine.db_connection.execute(
        "UPDATE documents SET metadata=json_set(metadata, '$.source_session_id', "
        "'qq:GroupMessage:original-group') WHERE id=1"
    )
    await engine.db_connection.commit()
    result = await engine.project_identity(one_id="human", include_records=True)
    record = next(row for row in result["records"] if row["memory_id"] == 1)
    assert record["scene"] == "qq:GroupMessage:original-group"
    assert record["storage_session_id"] == "qq:FriendMessage:a"


@pytest.mark.asyncio
async def test_sql_identity_filter_precedes_topk_with_poisoned_candidates(runtime):
    engine, _, bm25 = runtime
    access = await engine.identity_read_access(one_id="human")
    with identity_access(access):
        assert {row.doc_id for row in await bm25.search("synthetic", limit=1)} <= {1, 2}
        assert len(await bm25.search("synthetic", limit=1)) == 1
        assert {row.doc_id for row in await bm25.search("synthetic", limit=100)} == {1, 2}
    assert current_identity_access.get() is None


@pytest.mark.asyncio
async def test_unlink_projection_and_preview_preserve_data_and_other_account(runtime):
    engine, store, _ = runtime
    with sqlite3.connect(store.db_path) as db:
        before = db.execute("SELECT * FROM documents ORDER BY id").fetchall()
    preview = await engine.preview_identity_change(platform="bili", account_id="b")
    assert preview["affected_memory_ids"] == [2]
    binding = preview["binding"]
    await store.unlink_account(
        one_id="human", binding_id=binding["binding_id"], expected_version=binding["version"],
        expected_revision=preview["identity_revision"], operator="admin", reason="synthetic unlink",
        idempotency_key="u",
    )
    assert (await engine.project_identity(one_id="human"))["memory_ids"] == [1]
    assert (await engine.project_identity(one_id="person:bili:b"))["memory_ids"] == [2]
    with sqlite3.connect(store.db_path) as db:
        assert before == db.execute("SELECT * FROM documents ORDER BY id").fetchall()


@pytest.mark.asyncio
async def test_old_access_and_final_result_rejected_after_unlink(runtime):
    engine, store, _ = runtime
    access = await engine.identity_read_access(one_id="human")
    paused = asyncio.Event()
    resume = asyncio.Event()

    async def inflight():
        with identity_access(access):
            paused.set()
            await resume.wait()
            return await access.current()

    task = asyncio.create_task(inflight())
    await paused.wait()
    binding = await store.find_active("bili", "b")
    await store.revoke_binding(binding["binding_id"], operator="admin", reason="synthetic unlink")
    resume.set()
    assert await task is False
    with pytest.raises(IdentityContextChanged):
        await engine.validate_identity_results([SimpleNamespace(metadata={"_identity_revision": access.revision})])
    assert current_identity_access.get() is None


@pytest.mark.asyncio
async def test_nested_identity_access_cannot_widen_scope(runtime):
    engine, store, _ = runtime
    access = await engine.identity_read_access(one_id="human")
    with identity_access(access):
        with identity_access(IdentityAccess(frozenset(range(1, 106)), access.revision, store)):
            assert current_identity_access.get().memory_ids == frozenset({1, 2})
    assert current_identity_access.get() is None


@pytest.mark.asyncio
async def test_stale_summary_cannot_enrich_old_owner(runtime):
    engine, store, _ = runtime
    revision = await store.get_revision()
    binding = await store.find_active("qq", "bot")
    await store.revoke_binding(binding["binding_id"], operator="admin", reason="synthetic unlink")
    with pytest.raises(IdentityContextChanged):
        await engine.enrich_identity_metadata({"identity_revision": revision, "owner_account": "qq:bot"})


@pytest.mark.asyncio
async def test_body_cannot_supply_authenticated_operator():
    from quart import Quart
    app = Quart(__name__)
    async with app.test_request_context("/identity", method="POST", json={"operator": "admin", "username": "admin"}):
        with pytest.raises(PermissionError):
            authenticated_operator()


@pytest.mark.asyncio
async def test_authenticated_api_ignores_forged_operator_and_reviewer(runtime):
    from quart import Quart, g
    from astrbot_plugin_livingmemory.core.page_api_modules.oneid_handler import OneIdHandler
    from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils

    engine, store, _ = runtime
    handler = OneIdHandler(SimpleNamespace(
        initializer=SimpleNamespace(identity_store=store, memory_engine=engine),
    ), PageApiUtils())
    app = Quart(__name__)
    async with app.test_request_context("/identity", method="POST", json={
        "person_id": "human", "one_id": "human", "platform": "other",
        "account_id": "new-account", "idempotency_key": "api-submit",
        "operator": "forged", "reviewer": "forged",
        "evidence": {"ref": "synthetic:verified-account"},
    }):
        g.username = "authenticated-admin"
        response = await handler.handle("submit")
    application = response["data"]
    assert application["status"] == "pending"
    assert application["submitted_by"] == "authenticated-admin"
    assert await store.find_active("other", "new-account") is None
    async with app.test_request_context("/identity", method="POST", json={
        "application_id": application["application_id"],
        "expected_application": application, "idempotency_key": "api-review",
        "reviewer": "forged", "reason": "synthetic verified",
    }):
        g.username = "authenticated-reviewer"
        response = await handler.handle("review")
    assert response["data"]["application"]["reviewed_by"] == "authenticated-reviewer"
    assert (await store.find_active("other", "new-account"))["person_id"] == "human"


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"one_id": "human", "person_id": "different"}, 400),
    ({"one_id": "human", "source": "auto"}, 400),
])
async def test_api_rejects_conflicting_alias_and_forged_auto_source(runtime, body, status):
    from quart import Quart, g
    from astrbot_plugin_livingmemory.core.page_api_modules.oneid_handler import OneIdHandler
    from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils

    engine, store, _ = runtime
    revision = await store.get_revision()
    handler = OneIdHandler(SimpleNamespace(
        initializer=SimpleNamespace(identity_store=store, memory_engine=engine),
    ), PageApiUtils())
    app = Quart(__name__)
    async with app.test_request_context("/identity", method="POST", json={
        "platform": "other", "account_id": "new", "idempotency_key": "invalid", **body,
    }):
        g.username = "authenticated-admin"
        _, actual_status = await handler.handle("submit")
    assert actual_status == status
    assert await store.get_revision() == revision
    assert (await store.list_applications())["items"] == []


@pytest.mark.asyncio
async def test_anonymous_api_cannot_register_entity_or_change_revision(runtime):
    from quart import Quart
    from astrbot_plugin_livingmemory.core.page_api_modules.oneid_handler import OneIdHandler
    from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils

    engine, store, _ = runtime
    revision = await store.get_revision()
    handler = OneIdHandler(SimpleNamespace(
        initializer=SimpleNamespace(identity_store=store, memory_engine=engine),
    ), PageApiUtils())
    app = Quart(__name__)
    async with app.test_request_context("/identity", method="POST", json={
        "one_id": "forged-entity", "operator": "admin", "username": "admin",
    }):
        _, status = await handler.handle("register")
    assert status == 401
    assert "forged-entity" not in {
        row["one_id"] for row in (await store.list_entities())["items"]
    }
    assert await store.get_revision() == revision


@pytest.mark.parametrize("flag,expected", [
    (True, True), (1, True), ("true", True), (False, False),
    (None, False), (object(), False),
])
def test_synthetic_filter_requires_explicit_event_flag(flag, expected):
    from astrbot_plugin_livingmemory.core.identity import is_synthetic_memory_event
    event = SimpleNamespace(unified_msg_origin="qq:FriendMessage:a", get_extra=lambda key: flag)
    assert is_synthetic_memory_event(event) is expected


@pytest.mark.asyncio
async def test_same_bot_owner_does_not_grant_other_private_or_adapter_memory(runtime):
    from astrbot_plugin_livingmemory.core.identity import build_identity_context
    from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
    from astrbot_plugin_livingmemory.core.memory_scope import adapter_access, current_adapter_access

    engine, store, _ = runtime
    for memory_id, adapter in ((1, "adapter-a"), (2, "adapter-b"), (3, "adapter-a")):
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.adapter_id',?,'$.audience','self_private') WHERE id=?",
            (adapter, memory_id),
        )
    await engine.db_connection.commit()
    event = SimpleNamespace(
        unified_msg_origin="qq:FriendMessage:a", get_platform_name=lambda: "qq",
        get_platform_id=lambda: "adapter-a", get_sender_id=lambda: "a",
        get_self_id=lambda: "bot", get_group_id=lambda: "", get_extra=lambda key: None,
    )
    config = ConfigManager({"filtering_settings": {"shared_adapter_ids": ["adapter-a"]}})
    identity = await build_identity_context(config, event, store, persona_id="p")
    with adapter_access(config, event):
        access = await engine.prepare_identity_recall(identity, "p")
        assert 1 in access.memory_ids
        assert 2 not in access.memory_ids  # 同一主体的另一个账号仍受适配器限制。
        assert 3 not in access.memory_ids  # 同一机器人持有的其他人私聊不是本人权限。
        other_persona = await engine.prepare_identity_recall(identity, "different-persona")
        assert other_persona.memory_ids == frozenset()
    assert current_adapter_access.get() is None
