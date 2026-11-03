"""Integration checks using the host's actual SQLModel DocumentStorage."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

native = pytest.importorskip("astrbot.core.db.vec_db.faiss_impl.document_storage")
from astrbot_plugin_livingmemory.core.managers.memory_engine_crud import MemoryEngineCrudMixin
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from tests.test_unified_context import setup_service, search, KB


class NativeProbe(MemoryEngineCrudMixin):
    pass


@pytest.mark.asyncio
async def test_actual_integer_memory_id_and_native_metadata_are_scoped(tmp_path):
    path = tmp_path / "native.db"
    storage = native.DocumentStorage(str(path))
    await storage.initialize()
    metadata = {"session_id": "session", "persona_id": "persona", "status": "active"}
    identifier = await storage.insert_document("uuid-not-integer", "native experience", metadata)
    try:
        probe = NativeProbe()
        probe.faiss_db = SimpleNamespace(document_storage=storage)
        probe.hybrid_retriever = SimpleNamespace(search=AsyncMock(return_value=[
            HybridResult(doc_id=identifier, content="native experience", metadata=metadata,
                         final_score=1, rrf_score=1, bm25_score=1, vector_score=1),
        ]))
        probe._apply_atom_policy = AsyncMock(side_effect=lambda rows: rows)
        probe._filter_by_retrieval_policy = lambda rows: rows
        found = await probe.search_memories_scoped("native", session_id="session", persona_id="persona")
        assert found[0]["memory_id"] == identifier
        # 同一候选仍在检索层中，但原生当前记录已撤销，会在返回前被排除。
        async with aiosqlite.connect(path) as db:
            await db.execute("UPDATE documents SET metadata=? WHERE id=?", (
                json.dumps({**metadata, "status": "deleted"}), identifier,
            ))
            await db.commit()
        assert await probe.search_memories_scoped("native", session_id="session", persona_id="persona") == []
    finally:
        await storage.engine.dispose()


@pytest.mark.asyncio
async def test_actual_chunk_uuid_read_and_deleted_chunk_rejection(tmp_path):
    storage = native.DocumentStorage(str(tmp_path / "chunks.db"))
    await storage.initialize()
    identifier = await storage.insert_document("chunk", "Claude 橙发", {"kb_id": KB, "kb_doc_id": "doc"})
    try:
        service, helper, _, _ = setup_service()
        helper.vec_db.document_storage = storage
        assert (await search(service))["knowledge"][0]["content"] == "Claude 橙发"
        async with aiosqlite.connect(tmp_path / "chunks.db") as db:
            await db.execute("DELETE FROM documents WHERE id=?", (identifier,))
            await db.commit()
        assert (await search(service))["knowledge"] == []
    finally:
        await storage.engine.dispose()
