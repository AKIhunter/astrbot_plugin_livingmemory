"""T2 新写入隐私载荷回归；仅合成数据、临时 SQLite 和向量写入替身。"""

import copy
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_memory_engine import _FakeFaissDB
from astrbot_plugin_livingmemory.core.identity import sensitivity_level
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.models.memory_atom import MemoryAtom
from astrbot_plugin_livingmemory.core.privacy import apply_privacy, decrypt_text, encrypt_text
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult


SCOPE = "livingmemory:user:aiocqhttp:synthetic"
PERSONA = "synthetic-persona"
ORIGIN = "synthetic_adapter:GroupMessage:synthetic_groupA"
IDENTITY = {
    "scope": SCOPE, "audience": "origin_group:groupA", "scene_kind": "group",
}


@asynccontextmanager
async def engine_at(tmp_path):
    db = _FakeFaissDB()

    # 执行真实元数据更新/ownership SQL，替身只模拟外部宿主文档存储接口。
    @asynccontextmanager
    async def get_session():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE documents(id INTEGER, doc_id TEXT, text TEXT, metadata TEXT)")
        conn.executemany(
            "INSERT INTO documents VALUES(?,?,?,?)",
            [(r["id"], r["doc_id"], r["text"], json.dumps(r["metadata"])) for r in db.docs.values()],
        )

        async def execute(sql, params):
            rows = [dict(row) for row in conn.execute(str(sql), params)]
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

        @asynccontextmanager
        async def begin():
            try:
                yield
                conn.commit()
                for row in conn.execute("SELECT id, metadata FROM documents"):
                    db.docs[row["id"]]["metadata"] = json.loads(row["metadata"])
            except BaseException:
                conn.rollback()
                raise

        try:
            yield SimpleNamespace(execute=execute, begin=begin)
        finally:
            conn.close()

    db.document_storage.get_session = get_session
    engine = MemoryEngine(
        str(tmp_path / "engine.db"), db, graph_vector_db=_FakeFaissDB(),
        config={
            "atom_enabled": True, "graph_memory_enabled": True,
            "auto_cleanup_enabled": False, "recent_memory_count": 0,
            "search_cache_enabled": True, "search_cache_ttl_seconds": 600,
        },
    )
    await engine.initialize()

    async def candidates(*args, **kwargs):
        return [
            HybridResult(
                doc_id=row["id"], content=row["text"], metadata=copy.deepcopy(row["metadata"]),
                final_score=0.9, rrf_score=0.9, bm25_score=0.9, vector_score=None,
                score_breakdown={},
            )
            for row in engine.faiss_db.docs.values()
        ]

    engine.hybrid_retriever.search = candidates
    if engine.dual_route_retriever:
        engine.dual_route_retriever.search = candidates
    try:
        yield engine
    finally:
        await engine.close()


def atom(content="ordinary atom", **kwargs):
    return MemoryAtom(
        parent_memory_id=0, content=content, expires_at=time.time() + 3600, **kwargs,
    )


async def add(engine, content="ordinary summary", metadata=None, atoms=None):
    return await engine.add_memory(
        content, session_id=SCOPE, persona_id=PERSONA,
        metadata={"source_session_id": ORIGIN, **(metadata or {})}, atoms=atoms,
    )


async def index_texts(engine):
    texts = []
    for table in (
        "livingmemory_memories_fts", "memory_atoms_fts", "livingmemory_graph_entries_fts",
    ):
        cursor = await engine.db_connection.execute(f"SELECT content FROM {table}")
        texts.extend(row[0] for row in await cursor.fetchall())
    texts.extend(row["text"] for row in engine.faiss_db.docs.values())
    texts.extend(row["text"] for row in engine.graph_vector_db.docs.values())
    return texts


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "canonical_summary", "persona_summary", "key_facts", "topics", "content_text",
    "participants", "participant_identities",
])
async def test_summary_fields_real_indexes_and_structured_decryption(tmp_path, field):
    raw = "密码 T2_FIELD_ONLY"
    if field in {"key_facts", "topics", "participants"}:
        value = [raw, "ordinary second item"]
    elif field == "participant_identities":
        value = [{
            "identity_key": "qq:synthetic", "display_name": raw,
            "aliases": [raw], "sender_id": "synthetic",
        }]
    else:
        value = "  " + raw + "\n"
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine, metadata={field: value, "sensitivity": 0})
        md = engine.faiss_db.docs[row_id]["metadata"]
        assert md["sensitivity"] == 3
        assert engine.graph_vector_db.docs, "必须实际调用图向量写入"
        assert not any("T2_FIELD_ONLY" in text for text in await index_texts(engine))
        assert "T2_FIELD_ONLY" not in json.dumps(md, ensure_ascii=False)
        assert json.loads(decrypt_text(md["secret_fields_enc"], str(tmp_path)))[field] == value
        assert decrypt_text(md["secret_text_enc"], str(tmp_path)) == "ordinary summary"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["content", "entities", "metadata"])
async def test_atom_only_sensitive_fields_raise_parent_and_protect_graph(tmp_path, field):
    raw = "密码 T2_ATOM_ONLY"
    source = atom(
        raw if field == "content" else "ordinary atom",
        entities=[raw] if field == "entities" else [],
        metadata={"canonical_summary": raw} if field == "metadata" else {},
    )
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine, atoms=[source])
        parent = engine.faiss_db.docs[row_id]
        stored = (await engine.atom_store.get_by_parent(row_id))[0]
        assert sensitivity_level(parent["metadata"]) == 3
        assert stored.metadata["sensitivity"] == 3
        assert not any("T2_ATOM_ONLY" in text for text in await index_texts(engine))
        assert engine.graph_vector_db.docs
        original = decrypt_text(stored.metadata["secret_text_enc"], str(tmp_path))
        assert original == (raw if field == "content" else "ordinary atom")
        restored = json.loads(decrypt_text(stored.metadata["secret_fields_enc"], str(tmp_path)))
        assert restored["entities"] == ([raw] if field == "entities" else [])
        if field == "metadata":
            assert restored["canonical_summary"] == raw
        assert await engine.search_memories_for_identity(
            "probe", identity=IDENTITY, persona_id=PERSONA,
        ) == []


@pytest.mark.asyncio
async def test_merge_same_generalization_keeps_distinct_originals_and_deduplicates(tmp_path):
    first, second = "密码 T2_FIRST_ONLY", "密码 T2_SECOND_ONLY"
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine, first, {"key_facts": [first]}, [atom(first)])
        original_body = copy.deepcopy(engine.faiss_db.docs[row_id])
        for _ in range(2):
            assert await engine.merge_duplicate_memory(
                row_id, importance=0.9,
                metadata={"key_facts": [second], "sensitivity": 0},
                atoms=[atom(second)],
            )
        parent = engine.faiss_db.docs[row_id]
        assert parent["text"] == original_body["text"]
        assert parent["metadata"]["secret_text_enc"] == original_body["metadata"]["secret_text_enc"]
        assert parent["metadata"]["sensitivity"] == 3
        assert parent["metadata"]["importance"] == 0.9
        assert parent["metadata"]["dedup_merge_count"] == 2
        restored = json.loads(decrypt_text(parent["metadata"]["secret_fields_enc"], str(tmp_path)))
        assert restored["key_facts"] == [first, second]
        stored = await engine.atom_store.get_by_parent(row_id)
        assert len(stored) == 2
        assert {decrypt_text(a.metadata["secret_text_enc"], str(tmp_path)) for a in stored} == {
            first, second,
        }
        assert len({a.content for a in stored}) == 1
        assert not any("T2_FIRST_ONLY" in text or "T2_SECOND_ONLY" in text for text in await index_texts(engine))


@pytest.mark.asyncio
async def test_direct_append_protects_parent_atom_and_actual_indexes(tmp_path):
    raw = "密码 T2_APPEND_ONLY"
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine)
        old_text = engine.faiss_db.docs[row_id]["text"]
        await engine._append_atoms_to_memory(row_id, [atom(raw)], {})
        parent = engine.faiss_db.docs[row_id]
        assert parent["text"] == old_text
        assert parent["metadata"]["sensitivity"] == 3
        assert not any("T2_APPEND_ONLY" in text for text in await index_texts(engine))
        stored = await engine.atom_store.get_by_parent(row_id)
        assert len(stored) == 1
        assert decrypt_text(stored[0].metadata["secret_text_enc"], str(tmp_path)) == raw
        assert await engine.search_memories_for_identity(
            "probe", identity=IDENTITY, persona_id=PERSONA,
        ) == []


@pytest.mark.asyncio
async def test_failed_merge_then_add_preserves_original_atom(tmp_path, monkeypatch):
    raw = "密码 T2_RETRY_ONLY"
    incoming = [atom(raw, entities=[raw])]
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine)
        monkeypatch.setattr(engine, "update_memory", AsyncMock(return_value=False))
        assert not await engine.merge_duplicate_memory(
            row_id, importance=0.8, metadata={"key_facts": [raw]}, atoms=incoming,
        )
        new_id = await add(engine, atoms=incoming)
        stored = (await engine.atom_store.get_by_parent(new_id))[0]
        assert decrypt_text(stored.metadata["secret_text_enc"], str(tmp_path)) == raw
        restored = json.loads(decrypt_text(stored.metadata["secret_fields_enc"], str(tmp_path)))
        assert restored["entities"] == [raw]
        assert incoming[0].content == raw
        assert not any("T2_RETRY_ONLY" in text for text in await index_texts(engine))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["canonical_summary", "key_facts", "topics"])
async def test_merge_metadata_only_fields_real_graph_and_decryption(tmp_path, field):
    raw = "密码 T2_MERGE_FIELD"
    value = [raw] if field in {"key_facts", "topics"} else raw
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine, metadata={"audience": "self_private"})
        assert await engine.merge_duplicate_memory(
            row_id, importance=0.8,
            metadata={field: value, "sensitivity": 0, "audience": "origin_group:groupA"},
        )
        md = engine.faiss_db.docs[row_id]["metadata"]
        assert md["sensitivity"] == 3
        assert md["audience"] == "self_private"
        assert json.loads(decrypt_text(md["secret_fields_enc"], str(tmp_path)))[field] == value
        assert not any("T2_MERGE_FIELD" in text for text in await index_texts(engine))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["session_id", "persona_id", "adapter_id"])
async def test_merge_rejects_parent_ownership_change(tmp_path, field):
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine)
        before = copy.deepcopy(engine.faiss_db.docs)
        assert not await engine.merge_duplicate_memory(
            row_id, importance=1, metadata={field: "foreign"}, atoms=[atom("foreign")],
        )
        assert engine.faiss_db.docs == before
        assert await engine.atom_store.get_by_parent(row_id) == []


@pytest.mark.asyncio
async def test_ordinary_fields_atoms_and_merge_remain_retrievable(tmp_path):
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine, metadata={"key_facts": ["ordinary first"]}, atoms=[atom("ordinary first")])
        assert await engine.merge_duplicate_memory(
            row_id, importance=0.7, metadata={"key_facts": ["ordinary first", "ordinary second"]},
            atoms=[atom("ordinary first"), atom("ordinary second")],
        )
        md = engine.faiss_db.docs[row_id]["metadata"]
        assert md["sensitivity"] == 0
        assert md["key_facts"] == ["ordinary first", "ordinary second"]
        rows = await engine.search_memories_for_identity("probe", identity=IDENTITY, persona_id=PERSONA)
        assert len(rows) == 1
        assert rows[0].content == "ordinary first\nordinary second"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["session_id", "persona_id", "adapter_id"])
async def test_warm_atom_cache_rechecks_child_ownership(tmp_path, field):
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine, atoms=[atom("ordinary owned atom")])
        assert await engine.search_memories_for_identity("probe", identity=IDENTITY, persona_id=PERSONA)
        if field == "adapter_id":
            sql = "UPDATE memory_atoms SET metadata = json_set(metadata, '$.adapter_id', 'foreign')"
        else:
            sql = f"UPDATE memory_atoms SET {field} = 'foreign'"
        await engine.db_connection.execute(sql)
        await engine.db_connection.commit()
        assert await engine.search_memories_for_identity(
            "probe", identity=IDENTITY, persona_id=PERSONA,
        ) == []
        assert row_id in engine.faiss_db.docs


@pytest.mark.asyncio
async def test_identity_cache_cannot_return_stale_body(tmp_path):
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine, "RETRACTED_ONLY")
        assert await engine.search_memories_for_identity("probe", identity=IDENTITY, persona_id=PERSONA)
        engine.faiss_db.docs[row_id]["text"] = "CURRENT_ONLY"
        rows = await engine.search_memories_for_identity("probe", identity=IDENTITY, persona_id=PERSONA)
        assert all("RETRACTED_ONLY" not in row.content for row in rows)


@pytest.mark.asyncio
async def test_merge_preserves_incoming_private_visibility(tmp_path):
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine)
        assert await engine.merge_duplicate_memory(
            row_id, importance=0.8,
            metadata={"visible_to": "self_private", "key_facts": ["ordinary restricted"]},
        )
        assert engine.faiss_db.docs[row_id]["metadata"]["visible_to"] == "self_private"
        assert await engine.search_memories_for_identity(
            "probe", identity=IDENTITY, persona_id=PERSONA,
        ) == []


@pytest.mark.parametrize("use_env", [False, True])
def test_encryption_preserves_exact_format_and_replacement_body(tmp_path, monkeypatch, use_env):
    monkeypatch.delenv("LIVINGMEMORY_SECRET_KEY", raising=False)
    if use_env:
        monkeypatch.setenv("LIVINGMEMORY_SECRET_KEY", "synthetic-key-for-tests-only")
    raw = " \t密码 T2_FORMAT_ONLY\r\nline two\n\n"
    assert decrypt_text(encrypt_text(raw, str(tmp_path)), str(tmp_path)) == raw
    old = apply_privacy("密码 OLD_ONLY", {}, str(tmp_path))
    new = apply_privacy(raw, old.metadata, str(tmp_path))
    assert decrypt_text(new.metadata["secret_text_enc"], str(tmp_path)) == raw
    if not use_env:
        assert (tmp_path / "secret.key").stat().st_mode & 0o777 == 0o600


def test_existing_file_cipher_still_decrypts_with_environment_key(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVINGMEMORY_SECRET_KEY", raising=False)
    raw = " \t密码 LEGACY_FILE_ONLY\r\n"
    cipher = encrypt_text(raw, str(tmp_path))
    monkeypatch.setenv("LIVINGMEMORY_SECRET_KEY", "synthetic-new-environment-key")
    assert decrypt_text(cipher, str(tmp_path)) == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["expired", "forgotten", "superseded", "active"])
async def test_real_atom_store_lifecycle_and_cache_never_reintroduce_dead_fact(tmp_path, status):
    async with engine_at(tmp_path) as engine:
        row_id = await add(engine, atoms=[atom("ordinary live fact"), atom("RETIRED_ONLY")])
        assert await engine.search_memories_for_identity("probe", identity=IDENTITY, persona_id=PERSONA)
        await engine.db_connection.execute(
            "UPDATE memory_atoms SET status=?, expires_at=? WHERE content=?",
            (status, time.time() - 1 if status == "active" else time.time() + 1000, "RETIRED_ONLY"),
        )
        await engine.db_connection.commit()
        rows = await engine.search_memories_for_identity("probe", identity=IDENTITY, persona_id=PERSONA)
        assert len(rows) == 1
        assert rows[0].doc_id == row_id
        assert rows[0].content == "ordinary live fact"


@pytest.mark.asyncio
async def test_real_atom_table_without_children_keeps_ordinary_summary(tmp_path):
    async with engine_at(tmp_path) as engine:
        await add(engine, "ordinary summary retained")
        rows = await engine.search_memories_for_identity("probe", identity=IDENTITY, persona_id=PERSONA)
        assert [row.content for row in rows] == ["ordinary summary retained"]


@pytest.mark.parametrize("existing", [
    {"sensitivity": 3}, {"privacy": {"sensitivity_level": 3}}, {"sensitivity": "bad"},
])
def test_highest_parent_and_atom_level_never_downgrades(tmp_path, existing):
    result = apply_privacy(
        "ordinary", existing, str(tmp_path),
        atoms=[atom("ordinary", metadata={"sensitivity": 0})],
    )
    assert result.metadata["sensitivity"] == 3
    assert result.atoms[0].metadata["sensitivity"] == 3


def test_encryption_failure_never_falls_back_to_raw_indexes(tmp_path, monkeypatch):
    from astrbot_plugin_livingmemory.core import privacy
    monkeypatch.setattr(privacy, "encrypt_text", lambda *args: "")
    raw = "密码 T2_FAIL_CLOSED"
    result = apply_privacy(raw, {"key_facts": [raw]}, str(tmp_path), atoms=[atom(raw)])
    assert "T2_FAIL_CLOSED" not in result.index_text
    assert "T2_FAIL_CLOSED" not in json.dumps(result.metadata, ensure_ascii=False)
    assert "T2_FAIL_CLOSED" not in result.atoms[0].content
