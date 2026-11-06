# one_id 状态机使用真实 SQLite 验证事务、并发与数据不变量。
"""真实 SQLite 的非空身份状态机；所有账号和正文均为合成样本。"""

import asyncio
import hashlib
import json
import sqlite3

import aiosqlite
import pytest
import pytest_asyncio

from astrbot_plugin_livingmemory.storage.identity_store import IdentityStore, _BINDING_SCHEMA
from astrbot_plugin_livingmemory.storage.oneid_store import IdentityConflict
from astrbot_plugin_livingmemory.core.identity_keys import identity_alias, normalize_platform


@pytest_asyncio.fixture
async def store(tmp_path):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)")
        db.executemany("INSERT INTO documents VALUES(?,?,?)", [
            (1, "synthetic human fact", json.dumps({"source_account": "qq:q1", "session_id": "qq:FriendMessage:q1"})),
            (2, "synthetic bot observation", json.dumps({"source_account": "bili:b2", "persona_id": "default"})),
        ])
    value = IdentityStore(str(path))
    await value.initialize()
    for one_id, kind in (("person:existing", "human"), ("same-bot", "bot"), ("other-human", "human")):
        await value.register_entity(one_id=one_id, entity_type=kind, operator="admin")
    return value


def memory_hash(store):
    with sqlite3.connect(store.db_path) as db:
        return hashlib.sha256(repr(db.execute("SELECT * FROM documents ORDER BY id").fetchall()).encode()).hexdigest()


async def submit(store, *, one_id="person:existing", account_id="q1", key="submit-1", **kwargs):
    return await store.submit_application(
        one_id=one_id, platform="qq", account_id=account_id, operator="admin",
        idempotency_key=key, evidence={"type": "manual_verification", "ref": "synthetic:proof"}, **kwargs,
    )


async def approve(store, application, key="review-1"):
    return await store.review_application(
        application["application_id"], expected_application=application, reviewer="reviewer",
        idempotency_key=key, reason="synthetic verified ownership",
    )


async def unlink(store, binding, *, key="unlink-1"):
    return await store.unlink_account(
        one_id=binding["person_id"], binding_id=binding["binding_id"],
        expected_version=binding["version"], expected_revision=await store.get_revision(),
        operator="admin", reason="synthetic unlink", idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_alias_is_same_value_and_migration_preserves_body(store):
    before = memory_hash(store)
    assert identity_alias(one_id="person:existing", person_id="person:existing") == "person:existing"
    with pytest.raises(ValueError, match="同值"):
        await store.upsert_binding(one_id="A", person_id="B", platform="qq", account_id="q1")
    with pytest.raises(ValueError, match="同值"):
        await store.submit_application(one_id="A", person_id="B", platform="qq", account_id="q1",
                                       operator="admin", idempotency_key="bad", evidence={"ref": "proof"})
    assert (await store.identity_snapshot("qq", "q1"))["one_id"] == "person:qq:q1"
    assert memory_hash(store) == before
    with sqlite3.connect(store.migration_backup) as db:
        assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_new_application_never_uses_legacy_auto_approve(store):
    await store.set_auto_approve(True, reason="legacy synthetic", operator="admin", risk_ack=True)
    before = memory_hash(store)
    app = await submit(store)
    assert app["status"] == "pending"
    assert app["one_id"] == app["person_id"]
    assert await store.find_active("qq", "q1") is None
    assert await submit(store) == app
    result = await approve(store, app)
    assert result["binding"]["one_id"] == result["binding"]["person_id"] == "person:existing"
    assert await store.resolve_person("snowluma_onebot", "q1") == "person:existing"
    assert memory_hash(store) == before


@pytest.mark.asyncio
async def test_one_bot_can_own_multiple_platform_accounts(store):
    first = await submit(store, one_id="same-bot")
    await approve(store, first)
    second = await store.submit_application(
        one_id="same-bot", platform="bilibili", account_id="b2", operator="admin",
        idempotency_key="bili", evidence={"ref": "synthetic:bili"},
    )
    await approve(store, second, "bili-approve")
    snapshot = await store.identity_snapshot("onebot", "q1")
    assert snapshot["one_id"] == snapshot["person_id"] == "same-bot"
    assert snapshot["entity_type"] == "bot"
    assert {item["canonical_account"] for item in snapshot["bound_accounts"]} == {"qq:q1", "bili:b2"}
    with pytest.raises(IdentityConflict):
        await store.register_entity(one_id="same-bot", entity_type="human", operator="admin")


@pytest.mark.asyncio
async def test_competing_approvals_have_single_winner(store):
    first = await submit(store)
    second = await submit(store, one_id="other-human", key="submit-2")
    results = await asyncio.gather(approve(store, first, "r1"), approve(store, second, "r2"), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in results) == 1
    assert sum(isinstance(value, IdentityConflict) for value in results) == 1
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM person_binding WHERE status='active'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_changed_pending_evidence_cannot_replace_displayed_snapshot(store):
    first = await submit(store)
    with pytest.raises(IdentityConflict, match="幂等键"):
        await store.submit_application(
            one_id="person:existing", platform="qq", account_id="q1", operator="admin",
            idempotency_key="submit-1", evidence={"ref": "changed-proof"},
        )
    second = await submit(store, one_id="other-human", key="new-evidence")
    assert first["application_id"] != second["application_id"]
    assert (await store.get_application(first["application_id"])) == first
    tampered = {**first, "evidence": {"ref": "unshown"}}
    with pytest.raises(IdentityConflict):
        await approve(store, tampered)
    assert await store.find_active("qq", "q1") is None


@pytest.mark.asyncio
async def test_replay_unlink_stale_preview_and_explicit_rebind(store):
    before = memory_hash(store)
    app = await submit(store)
    result = await approve(store, app)
    binding = result["binding"]
    replay = await approve(store, app)
    assert replay == result
    pending = await submit(store, key="prior-pending")
    revision = await store.get_revision()
    kwargs = dict(
        one_id=binding["one_id"], binding_id=binding["binding_id"], expected_version=binding["version"],
        expected_revision=revision, operator="admin", reason="synthetic unlink", idempotency_key="unlink",
    )
    detached = await store.unlink_account(**kwargs)
    assert await store.unlink_account(**kwargs) == detached
    assert not await store.is_context_current({"identity_revision": revision})
    assert await store.resolve_one_id("qq", "q1") == "person:qq:q1"
    assert (await store.get_application(pending["application_id"]))["status"] == "cancelled"
    with pytest.raises(IdentityConflict):
        await approve(store, pending, "late")
    assert (await submit(store, key="prior-pending"))["status"] == "cancelled"
    with pytest.raises(IdentityConflict):
        await store.rollback_binding(binding["binding_id"], 1)
    fresh = await submit(store, key="explicit-new-binding")
    rebound = await approve(store, fresh, "new-approve")
    assert rebound["binding"]["version"] == binding["version"] + 1
    with pytest.raises(IdentityConflict):
        await store.unlink_account(**{**kwargs, "idempotency_key": "stale-retry"})
    assert (await store.find_active("qq", "q1"))["binding_id"] == rebound["binding"]["binding_id"]
    assert memory_hash(store) == before


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_every_binding_side_effect(store):
    app = await submit(store)
    revision = await store.get_revision()
    with sqlite3.connect(store.db_path) as db:
        db.execute("""CREATE TRIGGER fail_review BEFORE INSERT ON binding_audit
          WHEN NEW.action='application_approved'
          BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        await approve(store, app)
    assert await store.find_active("qq", "q1") is None
    assert await store.get_revision() == revision
    assert await store.get_application(app["application_id"]) == app
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM identity_operation WHERE action='review'").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,status", [("cancel", "cancelled"), ("reject", "rejected")])
async def test_cancel_reject_never_authorize(store, decision, status):
    app = await submit(store)
    result = await store.review_application(
        app["application_id"], expected_application=app, reviewer="admin", idempotency_key="terminal",
        decision=decision, reason="synthetic reject",
    )
    assert result["application"]["status"] == status
    assert await store.find_active("qq", "q1") is None
    with pytest.raises(IdentityConflict):
        await approve(store, app)


@pytest.mark.asyncio
async def test_expired_application_cannot_authorize(store, monkeypatch):
    monkeypatch.setattr(store, "_now", lambda: 1000)
    app = await submit(store, expires_in=60)
    monkeypatch.setattr(store, "_now", lambda: 1061)
    with pytest.raises(IdentityConflict, match="过期"):
        await approve(store, app)
    assert (await store.get_application(app["application_id"]))["status"] == "expired"
    assert await store.find_active("qq", "q1") is None


@pytest.mark.asyncio
async def test_self_declaration_is_pending_and_revoked_source_invalidates_it(store):
    await store.upsert_binding(person_id="person:existing", platform="qq", account_id="q1")
    binding = await store.find_active("qq", "q1")
    app = await store.submit_application(
        one_id="person:existing", platform="bili", account_id="b2", operator="trusted-plugin",
        idempotency_key="event:1", source="auto", source_account="qq:q1",
        evidence={"type": "self_declaration", "ref": "synthetic:message", "actor_account": "qq:q1"},
    )
    assert app["status"] == "pending"
    await unlink(store, binding)
    assert (await store.get_application(app["application_id"]))["status"] == "cancelled"
    with pytest.raises(IdentityConflict):
        await approve(store, app)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [
    {"type": "same_nickname"}, {"type": "llm_guess"}, {"type": "self_declaration", "quoted": True},
    {"type": "self_declaration", "synthetic": True}, {"type": "self_declaration", "actor_account": "qq:someone-else"},
])
async def test_untrusted_auto_evidence_never_creates_ownership(store, evidence):
    await store.upsert_binding(person_id="person:existing", platform="qq", account_id="q1")
    with pytest.raises(ValueError):
        await store.submit_application(
            one_id="person:existing", platform="bili", account_id="b2", operator="trusted-plugin",
            idempotency_key="bad-event", source="auto", source_account="qq:q1",
            evidence={"ref": "synthetic:message", "actor_account": "qq:q1", **evidence},
        )
    assert (await store.list_applications())["items"] == []


@pytest.mark.asyncio
async def test_correction_requires_explicit_new_application(store):
    await store.upsert_binding(person_id="person:existing", platform="qq", account_id="q1")
    with pytest.raises(IdentityConflict):
        await submit(store, one_id="other-human")
    app = await submit(store, one_id="other-human", correction=True)
    result = await approve(store, app)
    assert result["binding"]["person_id"] == "other-human"
    assert await store.resolve_one_id("qq", "q1") == "other-human"


@pytest.mark.asyncio
async def test_nonempty_legacy_alias_collision_is_not_hidden(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        for sql in _BINDING_SCHEMA:
            db.execute(sql)
        for i, platform in enumerate(("qq", "snowluma_onebot")):
            db.execute(
                "INSERT INTO person_binding(binding_id,person_id,platform,account_id,"
                "valid_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (f"legacy-{i}", f"person:old-{i}", platform, "same", 1, 1, 1),
            )
    store = IdentityStore(str(path))
    await store.initialize()
    assert await store.find_active("qq", "same") is None
    rows = await store.list_bindings(include_history=True)
    assert {row["person_id"] for row in rows} == {"person:old-0", "person:old-1"}
    assert {row["status"] for row in rows} == {"conflict"}
    assert {row["platform"] for row in rows} == {"qq", "snowluma_onebot"}
    assert normalize_platform("custom_platform") == "custom_platform"


@pytest.mark.asyncio
async def test_database_enforces_canonical_account_even_for_direct_writer(store):
    await store.upsert_binding(person_id="person:existing", platform="qq", account_id="q1")
    with sqlite3.connect(store.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="invalid canonical"):
            db.execute(
                "INSERT INTO person_binding(binding_id,person_id,platform,account_id,"
                "canonical_account,valid_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("bad", "other-human", "onebot", "q1", "bili:q1", 1, 1, 1),
            )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            db.execute(
                "INSERT INTO person_binding(binding_id,person_id,platform,account_id,"
                "canonical_account,valid_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("bad", "other-human", "onebot", "q1", "qq:q1", 1, 1, 1),
            )


@pytest.mark.asyncio
async def test_identity_write_uses_native_memory_engine_transaction(store):
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine

    engine = MemoryEngine.__new__(MemoryEngine)
    engine._db_write_lock = asyncio.Lock()
    engine.db_connection = await aiosqlite.connect(store.db_path)
    engine.db_connection.row_factory = aiosqlite.Row
    invalidated = []
    engine._invalidate_search_cache = lambda: invalidated.append(True)
    store.coordinate_with(engine)
    try:
        app = await submit(store)
        assert not engine.db_connection.in_transaction
        result = await approve(store, app)
        assert result["binding"]["person_id"] == "person:existing"
        assert invalidated
        with sqlite3.connect(store.db_path) as db:
            db.execute("""CREATE TRIGGER fail_unlink BEFORE INSERT ON binding_audit
              WHEN NEW.action='revoke'
              BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        revision = await store.get_revision()
        with pytest.raises(sqlite3.IntegrityError):
            await unlink(store, result["binding"])
        assert await store.get_revision() == revision
        assert (await store.find_active("qq", "q1"))["status"] == "active"
        assert not engine.db_connection.in_transaction
    finally:
        await engine.db_connection.close()


@pytest.mark.asyncio
async def test_event_actor_bot_and_persona_are_distinct(store):
    from astrbot_plugin_livingmemory.core.identity import build_identity_context
    from types import SimpleNamespace

    await store.upsert_binding(person_id="person:existing", platform="qq", account_id="q1")
    await store.upsert_binding(person_id="same-bot", platform="qq", account_id="bot-q")
    event = SimpleNamespace(
        get_sender_id=lambda: "q1", get_self_id=lambda: "bot-q",
        get_platform_name=lambda: "onebot", get_group_id=lambda: "",
        unified_msg_origin="onebot:FriendMessage:q1",
    )
    context = await build_identity_context({}, event, store, persona_id="shared-persona")
    assert context["person_id"] == context["one_id"] == "person:existing"
    assert context["owner_account"] == "qq:bot-q"
    assert context["owner_one_id"] == "same-bot"
    assert context["source_account"] == "qq:q1"
    assert context["persona_id"] == "shared-persona"
    assert await store.is_context_current(context)
    event.get_self_id = lambda: ""
    context = await build_identity_context({}, event, store, persona_id="shared-persona")
    assert context["owner_account"] == context["owner_one_id"] == ""
    assert context["one_id"] == "person:existing"
