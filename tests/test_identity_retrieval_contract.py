"""Identity and audience contracts using synthetic data, never production fixtures."""

import copy
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from .test_memory_engine import _FakeFaissDB
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.command_handler import CommandHandler
from astrbot_plugin_livingmemory.core.identity import (
    audience_allows, build_identity_context, parse_session_origin, sensitivity_level,
)
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.models.memory_atom import MemoryAtom
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.tools.memory_search_tool import MemorySearchTool
from astrbot_plugin_livingmemory.storage.identity_store import IdentityStore


CONFIG = {
    "filtering_settings": {"memory_scope_mode": "user", "use_persona_filtering": True},
    "recall_engine": {"top_k": 10, "max_k": 20},
}
PERSONA = "contract-persona"


class SyntheticEvent:
    def __init__(self, scene="private"):
        self.scene = scene
        marker, key = ("FriendMessage", "actor")
        if scene in {"group", "cross_group"}:
            marker, key = "GroupMessage", "actor_" + self.get_group_id()
        if scene == "unknown":
            marker = "UnknownMessage"
        self.unified_msg_origin = f"onebot:{marker}:{key}"

    def get_group_id(self):
        return {"group": "g1", "cross_group": "g2"}.get(self.scene, "")

    def get_sender_id(self):
        return "actor"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_extra(self, key):
        return "BV1234567890" if self.scene == "public" and key == "video_id" else None

    def plain_result(self, text):
        return text


@pytest.mark.parametrize("source,expected", [
    ("onebot:GroupMessage:actor_g1", ("qq", "group", "g1")),
    ("qq_official:GroupMessage:OPENID_GROUPID", ("qq", "group", "GROUPID")),
    ("onebot:GroupMessage:g1", ("qq", "group", "g1")),
    ("onebot:FriendMessage:actor", ("qq", "private", "actor")),
    ("onebot:NotGroupMessage:actor_g1", ("qq", "", "actor_g1")),
    ("unknown", ("", "", "")),
])
def test_session_source_order(source, expected):
    assert parse_session_origin(source) == expected


@pytest.mark.parametrize("level", range(4))
@pytest.mark.parametrize("source_scene", ["private", "group", "public", "unknown"])
@pytest.mark.parametrize("request_scene", ["private", "group", "cross_group", "public", "unknown"])
def test_audience_matrix(level, source_scene, request_scene):
    record = {
        "private": {"source_session_id": "qq:FriendMessage:actor", "audience": "public"},
        "group": {"source_session_id": "qq:GroupMessage:actor_g1", "audience": "origin_group:actor"},
        "public": {"audience": "public"},
        "unknown": {"source_window": [], "audience": ""},
    }[source_scene]
    context = {
        "private": {"audience": "self_private"},
        "group": {"audience": "origin_group:g1"},
        "cross_group": {"audience": "origin_group:g2"},
        "public": {"audience": "public"},
        "unknown": {"audience": "", "scene_kind": "unknown"},
    }[request_scene]
    expected = False
    if source_scene != "unknown" and request_scene != "unknown":
        if level >= 2:
            expected = request_scene == "private" or (
                level == 2 and source_scene == "group" and request_scene == "group"
            )
        elif source_scene == "public":
            expected = True
        else:
            expected = source_scene == request_scene
    assert audience_allows({**record, "sensitivity": level}, context) is expected


@pytest.mark.parametrize("level", ["bad", None, -1, 4, 2.5, True, [], {}])
def test_malformed_sensitivity_fails_closed(level):
    metadata = {"audience": "public", "sensitivity": level}
    assert sensitivity_level(metadata) == 3
    assert not audience_allows(metadata, {"audience": "public"})


@pytest_asyncio.fixture
async def retrieval_runtime(tmp_path, monkeypatch):
    from astrbot_plugin_livingmemory.core import utils
    from astrbot_plugin_livingmemory.core.tools import memory_search_tool

    monkeypatch.setattr(utils, "get_persona_id", AsyncMock(return_value=PERSONA))
    monkeypatch.setattr(memory_search_tool, "get_persona_id", AsyncMock(return_value=PERSONA))
    db = _FakeFaissDB()
    engine = MemoryEngine(
        str(tmp_path / "memory.db"), db, graph_vector_db=_FakeFaissDB(),
        config={"graph_memory_enabled": True, "atom_enabled": True},
    )
    await engine.initialize()
    store = IdentityStore(str(tmp_path / "identity.db"))
    await store.initialize()
    engine.identity_store = store
    fixtures = [
        ("aiocqhttp:actor", "qq:FriendMessage:actor", 0, PERSONA),
        ("qq:linked", "qq:FriendMessage:linked", 0, PERSONA),
        ("qq:linked", "qq:GroupMessage:linked_g1", 2, PERSONA),
        ("qq:linked", "qq:GroupMessage:linked_g1", 3, PERSONA),
        ("qq:linked", "qq:GroupMessage:linked_g2", 0, PERSONA),
        ("qq:linked", "unknown", 2, PERSONA),
        ("qq:linked", "qq:FriendMessage:linked", 0, "other-persona"),
        ("qq:unrelated", "qq:FriendMessage:unrelated", 0, PERSONA),
    ]
    for scope, source, level, persona in fixtures:
        doc_id = await db.insert("synthetic memory", {
            "session_id": "livingmemory:user:" + scope, "source_session_id": source,
            "persona_id": persona, "sensitivity": level, "audience": "public",
            "status": "active", "importance": 0.8, "has_source": True,
        })
        await engine.save_memory_source(doc_id, [{"role": "user", "content": "synthetic detail"}])

    async def candidates(query, k, session_id, persona_id):
        # Candidate ranking is the only fake boundary; scope, freshness and audience run for real.
        return [HybridResult(
            doc_id=row["id"], content=row["text"], metadata=copy.deepcopy(row["metadata"]),
            final_score=0.9, rrf_score=0.9, bm25_score=0.9, vector_score=None,
        ) for row in db.docs.values()]

    engine.hybrid_retriever.search = candidates
    engine.dual_route_retriever.search = candidates
    engine._migrate_session_data_if_needed = AsyncMock(side_effect=AssertionError("migration forbidden"))
    try:
        yield engine, store
    finally:
        engine._migrate_session_data_if_needed.assert_not_awaited()
        await engine.close()


async def activate_pair(store):
    await store.upsert_binding(
        person_id="person:synthetic", platform="qq", account_id="actor",
        operator="test", reason="synthetic fixture",
    )
    return await store.upsert_binding(
        person_id="person:synthetic", platform="qq", account_id="linked",
        operator="test", reason="synthetic fixture",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["tool", "command"])
@pytest.mark.parametrize("scene,active_ids", [
    ("private", {1, 2, 3, 4}), ("group", {3}), ("cross_group", {5}),
    ("public", set()), ("unknown", set()),
])
async def test_binding_activation_and_revocation_changes_real_calls(
    retrieval_runtime, entrypoint, scene, active_ids,
):
    engine, store = retrieval_runtime
    config = ConfigManager(CONFIG)
    event = SyntheticEvent(scene)
    tool = MemorySearchTool(context=object(), config_manager=config, memory_engine=engine)
    command = CommandHandler(
        context=object(), config_manager=config, memory_engine=engine,
        conversation_manager=None, index_validator=None,
    )
    source_spy = AsyncMock(wraps=engine.get_memory_source)
    engine.get_memory_source = source_spy

    async def recall():
        if entrypoint == "tool":
            response = json.loads(await tool.call(
                SimpleNamespace(context=SimpleNamespace(event=event)),
                "synthetic", k=20, include_source=True,
            ))
            assert "error" not in response
            if scene != "private":
                assert all("source_messages" not in row for row in response["results"])
            else:
                assert all(row["source_messages"] for row in response["results"])
            return {row["id"] for row in response["results"]}
        messages = [message async for message in command.handle_search(event, "synthetic", 20)]
        assert len(messages) == 1
        return {int(value) for value in re.findall(r"ID: (\d+)", messages[0])}

    unbound = {1} if scene == "private" else set()
    assert await recall() == unbound
    binding = await activate_pair(store)
    identity = await build_identity_context(config, event, store, persona_id=PERSONA)
    assert identity["person_id"] == "person:synthetic"
    assert "livingmemory:user:qq:linked" in identity["person_scopes"]
    assert await recall() == active_ids
    assert await recall() == active_ids
    assert await store.revoke_binding(binding["binding_id"], operator="test", reason="test revoke")
    assert await store.find_active("qq", "linked") is None
    assert await recall() == unbound
    if scene != "private" or entrypoint == "command":
        source_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_metadata_and_atom_parent_override_cached_candidates(retrieval_runtime):
    engine, store = retrieval_runtime
    await activate_pair(store)
    identity = await build_identity_context(ConfigManager(CONFIG), SyntheticEvent("group"), store)
    for doc_id in (2, 3, 4):
        await engine.atom_store.insert(MemoryAtom(
            parent_memory_id=doc_id, content=f"synthetic atom {doc_id}",
            session_id="livingmemory:user:qq:linked", persona_id=PERSONA,
            expires_at=time.time() + 3600,
        ))
        engine.faiss_db.docs[doc_id]["metadata"]["atom_types"] = ["unknown"]
    for _ in range(2):
        rows = await engine.search_memories_for_identity("synthetic", identity=identity, persona_id=PERSONA)
        assert {row.doc_id for row in rows} == {3}
        assert rows[0].content == "synthetic atom 3"
    engine.faiss_db.docs[3]["metadata"]["sensitivity"] = 3
    assert await engine.search_memories_for_identity("synthetic", identity=identity, persona_id=PERSONA) == []
