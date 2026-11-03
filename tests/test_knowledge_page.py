"""Admin API boundaries, stale writes, and document ownership."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from astrbot_plugin_livingmemory.core.page_api_modules.knowledge_handler import KnowledgeHandler
from astrbot_plugin_livingmemory.core.page_api_modules.settings_handler import SettingsHandler
from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager

KB = "11111111-1111-4111-8111-111111111111"
DOC = "22222222-2222-4222-8222-222222222222"
BINDING = dict(caller="bilibot", purpose="public_comment", persona_id="persona", kb_ids=[KB])


class Config(dict):
    def save_config(self):
        pass


def setup():
    config = Config({"unified_context": {"enabled": True, "bindings_json": json.dumps([BINDING])}})
    kb = SimpleNamespace(kb_id=KB, kb_name="<img src=x>", description="public", doc_count=1, chunk_count=1)
    doc = SimpleNamespace(kb_id=KB, doc_id=DOC, doc_name="doc", chunk_count=1, file_path="/PRIVATE")
    helper = SimpleNamespace(
        kb=kb, get_document=AsyncMock(return_value=doc),
        get_chunks_by_doc_id=AsyncMock(return_value=[
            dict(kb_id=KB, doc_id=DOC, chunk_id="chunk", content="<script>secret</script>"),
            dict(kb_id="other", doc_id=DOC, chunk_id="foreign", content="PRIVATE"),
        ]),
        get_chunk_count_by_doc_id=AsyncMock(return_value=1),
        list_documents=AsyncMock(return_value=[doc]), count_documents=AsyncMock(return_value=1),
    )
    plugin = SimpleNamespace(
        config_manager=ConfigManager(config),
        context=SimpleNamespace(kb_manager=SimpleNamespace(
            get_kb=AsyncMock(return_value=helper), list_kbs=AsyncMock(return_value=[kb]),
        )),
        search_context=AsyncMock(return_value={"status": "ok", "knowledge": [], "experiences": []}),
    )
    handler = KnowledgeHandler(PageApiUtils(), plugin)
    app = Quart(__name__)
    for name in ("catalog", "documents", "preview", "save_policy", "test"):
        app.add_url_rule("/" + name, name, getattr(handler, name), methods=["GET", "POST"])
    return handler, helper, app.test_client()


@pytest.mark.asyncio
async def test_catalog_is_no_store_and_exposes_no_file_or_provider_details():
    _, _, client = setup()
    response = await client.get("/catalog")
    assert response.headers["Cache-Control"] == "no-store"
    data = (await response.get_json())["data"]
    assert data["items"][0]["public"] is True
    assert "file_path" not in json.dumps(data) and "/PRIVATE" not in json.dumps(data)


@pytest.mark.asyncio
async def test_preview_checks_ownership_before_reading_chunks():
    _, helper, client = setup()
    helper.get_document.return_value.kb_id = "other"
    response = await client.get(f"/preview?kb_id={KB}&doc_id={DOC}")
    assert (await response.get_json())["status"] == "error"
    helper.get_chunks_by_doc_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_rejects_foreign_chunks_and_bounds_content():
    _, helper, client = setup()
    helper.get_chunks_by_doc_id.return_value[0]["content"] = "X" * 9000
    response = await client.get(f"/preview?kb_id={KB}&doc_id={DOC}")
    data = (await response.get_json())["data"]
    assert len(data["items"]) == 1
    assert data["items"][0]["truncated"] and len(data["items"][0]["content"]) == 6000


@pytest.mark.parametrize("suffix", ["page=0", "page=10001", "page=bad", "kb_id=../private"])
@pytest.mark.asyncio
async def test_invalid_page_or_id_does_not_read_documents(suffix):
    _, helper, client = setup()
    query = suffix if suffix.startswith("kb_id") else f"kb_id={KB}&{suffix}"
    response = await client.get("/documents?" + query)
    assert (await response.get_json())["status"] == "error"
    helper.list_documents.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_page_cannot_restore_revoked_grant():
    handler, _, client = setup()
    revision = handler.policy_data()["revision"]
    handler.plugin.config_manager.apply_updates({"unified_context.enabled": False})
    response = await client.post("/save_policy", json=dict(
        enabled=True, bindings=[BINDING], revision=revision,
    ))
    assert (await response.get_json())["status"] == "error"
    assert handler.policy_data()["enabled"] is False


@pytest.mark.asyncio
async def test_save_rechecks_revision_after_async_library_validation():
    handler, _, client = setup()
    revision = handler.policy_data()["revision"]
    async def revoke(kb_id):
        handler.plugin.config_manager.apply_updates({"unified_context.enabled": False})
        return SimpleNamespace(kb=SimpleNamespace(kb_id=kb_id))
    handler.plugin.context.kb_manager.get_kb.side_effect = revoke
    response = await client.post("/save_policy", json=dict(
        enabled=True, bindings=[BINDING], revision=revision,
    ))
    assert (await response.get_json())["status"] == "error"
    assert not handler.policy_data()["enabled"]


@pytest.mark.asyncio
async def test_disable_still_works_after_library_is_deleted():
    handler, _, client = setup()
    handler.plugin.context.kb_manager.get_kb.return_value = None
    response = await client.post("/save_policy", json=dict(
        enabled=False, bindings=[BINDING], revision=handler.policy_data()["revision"],
    ))
    assert (await response.get_json())["status"] == "ok"
    assert not handler.policy_data()["enabled"]


@pytest.mark.parametrize("payload", [[], None, {"enabled": "true"}, {"enabled": True, "bindings": "*"}])
@pytest.mark.asyncio
async def test_malformed_policy_body_is_not_saved(payload):
    handler, _, client = setup()
    before = handler.policy_data()
    response = await client.post("/save_policy", json=payload)
    assert (await response.get_json())["status"] == "error"
    assert handler.policy_data() == before


@pytest.mark.asyncio
async def test_test_endpoint_uses_service_without_inventing_session_scope():
    handler, _, client = setup()
    identity = {k: BINDING[k] for k in ("caller", "purpose", "persona_id")}
    identity["session_id"] = ""
    response = await client.post("/test", json={
        "query": "Claude", "identity": identity, "revision": handler.policy_data()["revision"],
    })
    assert (await response.get_json())["status"] == "ok"
    handler.plugin.search_context.assert_awaited_once_with("Claude", **identity)


def test_generic_settings_cannot_bypass_policy_revision_check():
    handler, _, _ = setup()
    generic = SettingsHandler(PageApiUtils(), handler.plugin)
    assert not any(p.startswith("unified_context.") for p in generic._allowed_paths())
