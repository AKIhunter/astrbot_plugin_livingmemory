"""Adapt unit-test engines to the deployed identity-aware public interface."""
from unittest.mock import AsyncMock


def identity_engine_fixture(engine):
    engine.identity_store = None
    engine.validate_identity_results = AsyncMock(side_effect=lambda rows: rows)

    async def scoped(query, k, identity, persona_id=None, **kwargs):
        return await engine.search_memories(
            query=query, k=k, session_id=identity["scope"], persona_id=persona_id,
            **kwargs,
        )

    engine.search_memories_for_identity = AsyncMock(side_effect=scoped)
    return engine


def summary_window_fixture(manager):
    async def window(session_id, limit):
        start = await manager.get_session_metadata(session_id, "last_summarized_index", 0)
        pending = await manager.get_session_metadata(session_id, "pending_summary", None)
        if isinstance(pending, dict):
            start = min(start, pending.get("start_index", start))
        total = await manager.store.get_message_count(session_id)
        rows = await manager.get_messages_range(
            session_id=session_id, start_index=start, end_index=min(total,start+limit)
        )
        for i,row in enumerate(rows):
            if not isinstance(getattr(row,"id",None),int):
                row.id = start+i+1
        return start,rows

    async def commit(session_id, last_message, revision):
        await manager.update_session_metadata(session_id,"last_summarized_index",last_message.id)
        await manager.update_session_metadata(session_id,"pending_summary",None)
        await manager.invalidate_cache(session_id)
        return True

    manager.get_summary_window = AsyncMock(side_effect=window)
    manager.commit_summary_cursor = AsyncMock(side_effect=commit)
    return manager
