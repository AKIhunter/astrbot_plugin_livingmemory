"""Adversarial boundaries for the new retrieval entry point; no live providers."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.managers.memory_engine_crud import MemoryEngineCrudMixin
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.unified_context import UnifiedContextService, parse_bindings

KB = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
PUBLIC = {"caller": "bilibot", "purpose": "public_comment", "persona_id": "大小姐", "kb_ids": [KB]}
SESSION = {**PUBLIC, "purpose": "session", "session_id": "qq:FriendMessage:owner"}


def setup_service(binding=None):
    binding = dict(binding or PUBLIC)
    config = {"unified_context": {"enabled": True, "bindings_json": json.dumps([binding])}}
    manager = ConfigManager(config)
    doc = SimpleNamespace(doc_id="doc", kb_id=KB, doc_name="角色资料")
    chunk = {"doc_id": "chunk", "text": "Claude 橙发", "metadata": {"kb_id": KB, "kb_doc_id": "doc"}}
    storage = SimpleNamespace(get_document_by_doc_id=AsyncMock(return_value=chunk))
    helper = SimpleNamespace(
        kb=SimpleNamespace(kb_id=KB, kb_name="公开资料"),
        init_error=None, get_document=AsyncMock(return_value=doc),
        vec_db=SimpleNamespace(document_storage=storage),
    )
    candidate = {"kb_id": KB, "doc_id": "doc", "chunk_id": "chunk", "content": chunk["text"]}
    kb_manager = SimpleNamespace(
        get_kb=AsyncMock(return_value=helper), get_kb_by_name=AsyncMock(return_value=helper),
        retrieve=AsyncMock(return_value={"results": [candidate], "context_text": "DO_NOT_USE_UNFILTERED"}),
    )
    engine = SimpleNamespace(search_memories_scoped=AsyncMock(return_value=[]))
    plugin = SimpleNamespace(
        config_manager=manager, context=SimpleNamespace(kb_manager=kb_manager),
        initializer=SimpleNamespace(memory_engine=engine),
    )
    service = UnifiedContextService(plugin)
    return service, helper, candidate, config


async def search(service, **overrides):
    options = dict(caller="bilibot", purpose="public_comment", persona_id="大小姐")
    options.update(overrides)
    return await service.search("Claude", **options)


@pytest.mark.asyncio
async def test_public_never_touches_owner_memory_or_unfiltered_context():
    service, _, _, _ = setup_service()
    result = await search(service)
    assert result["status"] == "ok"
    assert result["knowledge"][0]["content"] == "Claude 橙发"
    assert "DO_NOT_USE_UNFILTERED" not in json.dumps(result)
    assert result["experiences"] == []
    service.plugin.initializer.memory_engine.search_memories_scoped.assert_not_awaited()
    assert service.plugin.context.kb_manager.retrieve.call_args.kwargs["kb_names"] == [KB]


@pytest.mark.parametrize("overrides", [
    {"caller": "other"}, {"persona_id": "other"}, {"persona_id": ""},
    {"purpose": "session"}, {"session_id": "qq:FriendMessage:owner"},
    {"session_id": None}, {"caller": ["bilibot"]},
])
@pytest.mark.asyncio
async def test_missing_or_mismatched_identity_denies_before_backend(overrides):
    service, _, _, _ = setup_service()
    result = await search(service, **overrides)
    assert result["status"] in {"denied", "invalid_request"}
    service.plugin.context.kb_manager.retrieve.assert_not_awaited()


@pytest.mark.parametrize("change", [
    {"session_id": "qq:FriendMessage:owner"}, {"caller": "*"}, {"persona_id": ""},
    {"purpose": "public"}, {"kb_ids": ["公开资料"]}, {"kb_ids": [KB, KB]},
    {"kb_ids": "*"}, {"unknown": True},
])
def test_invalid_or_broad_public_grants_rejected(change):
    with pytest.raises(ValueError):
        parse_bindings(json.dumps([{**PUBLIC, **change}]))


@pytest.mark.parametrize("value", ["{}", "null", "bad", json.dumps([PUBLIC, PUBLIC])])
def test_invalid_container_and_duplicate_policy_rejected(value):
    with pytest.raises(ValueError):
        parse_bindings(value)


@pytest.mark.asyncio
async def test_damaged_policy_disables_only_this_feature():
    service, _, _, config = setup_service()
    config["unified_context"]["bindings_json"] = "broken"
    service.plugin.config_manager = ConfigManager({**config, "recall_engine": {"top_k": 9}})
    assert (await search(service))["status"] == "invalid_policy"
    assert service.plugin.config_manager.get("recall_engine.top_k") == 9


@pytest.mark.asyncio
async def test_disabled_feature_is_default():
    service, _, _, _ = setup_service()
    service.plugin.config_manager = ConfigManager({})
    assert (await search(service))["status"] == "disabled"
    service.plugin.context.kb_manager.retrieve.assert_not_awaited()


@pytest.mark.parametrize("mutation", [
    "wrong_kb", "no_doc", "wrong_doc_kb", "no_chunk", "stale_content",
    "wrong_chunk_kb", "wrong_chunk_doc", "wrong_chunk_id",
])
@pytest.mark.asyncio
async def test_cross_scope_deleted_and_stale_knowledge_is_filtered(mutation):
    service, helper, candidate, _ = setup_service()
    chunk = helper.vec_db.document_storage.get_document_by_doc_id.return_value
    if mutation == "wrong_kb":
        candidate["kb_id"] = OTHER
    elif mutation == "no_doc":
        helper.get_document.return_value = None
    elif mutation == "wrong_doc_kb":
        helper.get_document.return_value.kb_id = OTHER
    elif mutation == "no_chunk":
        helper.vec_db.document_storage.get_document_by_doc_id.return_value = None
    elif mutation == "stale_content":
        chunk["text"] = "更新后的内容"
    elif mutation == "wrong_chunk_kb":
        chunk["metadata"]["kb_id"] = OTHER
    elif mutation == "wrong_chunk_doc":
        chunk["metadata"]["kb_doc_id"] = "private"
    elif mutation == "wrong_chunk_id":
        chunk["doc_id"] = "private"
    result = await search(service)
    assert result["knowledge"] == []
    assert result["status"] == "partial"


@pytest.mark.asyncio
async def test_uuid_name_collision_cannot_redirect_retrieval():
    service, _, _, _ = setup_service()
    service.plugin.context.kb_manager.get_kb_by_name.return_value = object()
    assert (await search(service))["knowledge"] == []
    service.plugin.context.kb_manager.retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_during_inflight_query_discards_result():
    service, _, _, _ = setup_service()
    started, finish = asyncio.Event(), asyncio.Event()
    original = service.plugin.context.kb_manager.retrieve.return_value

    async def retrieve(*args, **kwargs):
        started.set()
        await finish.wait()
        return original

    service.plugin.context.kb_manager.retrieve.side_effect = retrieve
    task = asyncio.create_task(search(service))
    await started.wait()
    service.plugin.config_manager = ConfigManager({})
    finish.set()
    result = await task
    assert result["status"] == "denied" and result["knowledge"] == []


@pytest.mark.asyncio
async def test_timeout_retains_other_source_and_cleans_tasks():
    service, _, _, _ = setup_service(SESSION)
    service.timeout_seconds = 0.01
    cancelled = asyncio.Event()

    async def retrieve(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.plugin.context.kb_manager.retrieve.side_effect = retrieve
    engine = service.plugin.initializer.memory_engine
    engine.search_memories_scoped.return_value = [{"source": "experience", "memory_id": 1, "content": "本人经历"}]
    result = await search(service, purpose="session", session_id=SESSION["session_id"])
    assert result["status"] == "partial"
    assert result["sources"]["knowledge"] == "timeout"
    assert result["experiences"][0]["content"] == "本人经历"
    assert cancelled.is_set() and service._active == 0 and not service._callers
    assert engine.search_memories_scoped.call_args.kwargs["session_id"] == SESSION["session_id"]


@pytest.mark.asyncio
async def test_caller_capacity_rejects_before_expensive_work():
    service, _, _, _ = setup_service()
    service._active, service._callers = 2, {"bilibot": 2}
    assert (await search(service))["status"] == "busy"
    service.plugin.context.kb_manager.retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_budget_and_no_sensitive_audit(caplog):
    service, helper, candidate, _ = setup_service()
    text = "SECRET_QUERY_NOT_LOGGED" * 400
    helper.vec_db.document_storage.get_document_by_doc_id.return_value["text"] = text
    candidate["content"] = text
    with caplog.at_level("INFO"):
        result = await service.search(text, caller="bilibot", purpose="public_comment", persona_id="大小姐")
    assert len(result["knowledge"][0]["content"]) == 1600
    assert result["truncated"]
    assert text[:40] not in caplog.text
    assert len(service.plugin.context.kb_manager.retrieve.call_args.args[0]) == 1200


@pytest.mark.parametrize("change", [
    {"session_id": "other"}, {"persona_id": "other"}, {"status": "deleted"},
    {"status": "archived"}, {"status": None},
])
@pytest.mark.asyncio
async def test_experience_rechecks_current_row_scope_and_status(change):
    metadata = {"session_id": "session", "persona_id": "persona", "status": "active", **change}
    result = HybridResult(
        doc_id=1, content="private", metadata={}, final_score=1, rrf_score=1,
        bm25_score=None, vector_score=None,
    )
    host = SimpleNamespace(
        hybrid_retriever=SimpleNamespace(search=AsyncMock(return_value=[result])),
        get_memory=AsyncMock(return_value={"text": "private", "metadata": metadata}),
        _apply_atom_policy=AsyncMock(side_effect=lambda rows: rows),
        _filter_by_retrieval_policy=lambda rows: rows,
    )
    rows = await MemoryEngineCrudMixin.search_memories_scoped(
        host, "test", session_id="session", persona_id="persona"
    )
    assert rows == []


@pytest.mark.asyncio
async def test_experience_live_row_is_returned_without_private_metadata():
    metadata = {"session_id": "session", "persona_id": "persona", "status": "active", "secret": "PRIVATE_METADATA"}
    result = HybridResult(
        doc_id=1, content="content", metadata={}, final_score=1, rrf_score=1,
        bm25_score=None, vector_score=None,
    )
    host = SimpleNamespace(
        hybrid_retriever=SimpleNamespace(search=AsyncMock(return_value=[result])),
        get_memory=AsyncMock(return_value={"text": "content", "metadata": metadata}),
        _apply_atom_policy=AsyncMock(side_effect=lambda rows: rows),
        _filter_by_retrieval_policy=lambda rows: rows,
    )
    rows = await MemoryEngineCrudMixin.search_memories_scoped(
        host, "test", session_id="session", persona_id="persona"
    )
    assert rows == [{"source": "experience", "memory_id": 1, "content": "content"}]
    host.get_memory.return_value["text"] = "revised"
    assert await MemoryEngineCrudMixin.search_memories_scoped(
        host, "test", session_id="session", persona_id="persona"
    ) == []
