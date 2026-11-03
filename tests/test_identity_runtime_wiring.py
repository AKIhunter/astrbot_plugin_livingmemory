import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_identity_retrieval_contract import (
    CONFIG, SyntheticEvent, activate_pair, retrieval_runtime,
)
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.main import LivingMemoryPlugin


@pytest.mark.asyncio
async def test_runtime_initialization_injects_real_store_into_registered_entries(retrieval_runtime):
    engine, store = retrieval_runtime
    del engine.identity_store
    plugin = LivingMemoryPlugin.__new__(LivingMemoryPlugin)
    plugin.context = Mock()
    plugin.config_manager = ConfigManager(CONFIG)
    plugin._terminating = False
    plugin._component_init_lock = asyncio.Lock()
    plugin._llm_tools_registered = False
    plugin.event_handler = None
    plugin.command_handler = None
    plugin.initializer = SimpleNamespace(
        is_initialized=True, memory_engine=engine, identity_store=store,
        memory_processor=Mock(), conversation_manager=Mock(),
        consolidation_manager=None, index_validator=None,
    )
    assert await plugin._ensure_runtime_components()
    assert engine.identity_store is store
    assert plugin.command_handler.memory_engine is engine
    tool = plugin.context.add_llm_tools.call_args.args[0]
    assert tool.memory_engine is engine
    event = SyntheticEvent("private")
    run_context = SimpleNamespace(context=SimpleNamespace(event=event))

    async def check(expected):
        response = json.loads(await tool.call(run_context, "synthetic", k=20))
        assert {row["id"] for row in response["results"]} == expected
        messages = [msg async for msg in plugin.command_handler.handle_search(event, "synthetic", 20)]
        for doc_id in range(1, 9):
            assert (f"ID: {doc_id}" in messages[0]) == (doc_id in expected)

    await check({1})
    binding = await activate_pair(store)
    await check({1, 2, 3, 4})
    await plugin._ensure_runtime_components()
    assert plugin.context.add_llm_tools.call_count == 1
    assert await store.revoke_binding(binding["binding_id"], operator="test", reason="synthetic")
    await check({1})
