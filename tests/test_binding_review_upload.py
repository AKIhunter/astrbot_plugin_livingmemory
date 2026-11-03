"""绑定审核快照及 replace 原子性：真实存储、合成 SQLite，不接触生产数据。"""

import asyncio
import sqlite3

import aiosqlite
import pytest
import pytest_asyncio

from astrbot_plugin_livingmemory.storage.identity_store import IdentityStore


@pytest_asyncio.fixture
async def store(tmp_path):
    instance = IdentityStore(str(tmp_path / "bindings.db"))
    await instance.initialize()
    with sqlite3.connect(instance.db_path) as db:
        db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")
        db.execute("INSERT INTO documents VALUES(1, 'synthetic-body', '{}')")
    return instance


def dump(store):
    with sqlite3.connect(store.db_path) as db:
        return {name: db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
                for name in ("person_binding", "binding_candidate", "binding_audit", "documents")}


async def candidate(store, **changes):
    return (await store.submit_candidate(
        **{"person_id": "displayed", "platform": "qq", "account_id": "keep", **changes}
    ))["candidate"]


async def review(store, shown, approve=True, **extra):
    return await store.review_candidate(
        shown["candidate_id"], approve=approve, reviewer="human", reason="synthetic-review",
        expected_candidate=shown, **extra,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("change", [
    {"person_id": "unseen"}, {"display_name": "new-name"}, {"source": "new-source"},
    {"evidence_type": "new-type"}, {"evidence_ref": "new-ref"}, {"confidence": 0.9},
    {"matched_rule": "new-rule"}, {"note": "new-note"}, {"submitted_by": "another"},
])
async def test_stale_complete_candidate_is_rejected_even_with_same_timestamp(store, monkeypatch, approve, change):
    monkeypatch.setattr(store, "_now", lambda: 1000.0)
    shown = await candidate(store)
    changed = await candidate(store, **change)
    assert shown["candidate_id"] == changed["candidate_id"]
    assert shown["updated_at"] == changed["updated_at"]
    before = dump(store)
    with pytest.raises(ValueError, match="候选已变化"):
        await review(store, shown, approve)
    assert dump(store) == before
    current = (await store.list_candidates())[0]
    result = await review(store, current, approve)
    assert result["candidate"]["status"] == ("approved" if approve else "rejected")
    assert bool(await store.find_active("qq", "keep")) is approve


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [None, {}, {"candidate_id": "partial"}, "id", []])
async def test_missing_or_partial_candidate_never_writes(store, expected):
    shown = await candidate(store)
    before = dump(store)
    with pytest.raises(ValueError):
        await store.review_candidate(shown["candidate_id"], approve=True, reviewer="human",
                                     reason="synthetic", expected_candidate=expected)
    assert dump(store) == before


@pytest.mark.asyncio
async def test_candidate_snapshot_checked_after_waiting_for_writer(store, monkeypatch):
    shown = await candidate(store)
    started = asyncio.Event()
    execute = aiosqlite.Connection.execute

    async def observed_execute(connection, sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            started.set()
        return await execute(connection, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", observed_execute)
    writer = sqlite3.connect(store.db_path)
    writer.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(review(store, shown))
    try:
        await asyncio.wait_for(started.wait(), 2)
        writer.execute("UPDATE binding_candidate SET person_id='unseen'")
        writer.commit()
        before = dump(store)
        with pytest.raises(ValueError, match="候选已变化"):
            await asyncio.wait_for(task, 3)
        assert dump(store) == before
    finally:
        writer.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_review_failure_rolls_back_and_retry_has_one_winner(store):
    shown = await candidate(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("""CREATE TRIGGER fail_review BEFORE UPDATE ON binding_candidate
                      WHEN NEW.status='approved'
                      BEGIN SELECT RAISE(ABORT, 'synthetic-review-failure'); END""")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic-review-failure"):
        await review(store, shown)
    assert dump(store) == before
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER fail_review")
    results = await asyncio.gather(*[
        review(IdentityStore(store.db_path), shown, approve)
        for approve in (True, True, False, False)
    ])
    assert sum(result is not None for result in results) == 1
    audits = [row for row in await store.list_audit()
              if row["action"] in ("candidate_approve", "candidate_reject")]
    assert len(audits) == 1
    assert bool(await store.find_active("qq", "keep")) == (audits[0]["action"] == "candidate_approve")


@pytest.mark.asyncio
async def test_auto_approve_passes_its_own_full_snapshot(store):
    assert (await store.get_settings())["auto_approve"] is False
    await store.set_auto_approve(True, risk_ack=True, reason="synthetic", operator="human")
    result = await store.submit_candidate(person_id="displayed", platform="qq", account_id="keep")
    assert result["auto_approved"] is True
    assert (await store.find_active("qq", "keep"))["person_id"] == "displayed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["second_insert", "revoke_audit"])
async def test_replace_failure_rolls_back_new_rows_revocation_and_audit(store, failure):
    await store.upsert_binding(person_id="original", platform="qq", account_id="keep")
    active = await store.list_bindings()
    with sqlite3.connect(store.db_path) as db:
        if failure == "second_insert":
            db.execute("""CREATE TRIGGER fail_upload BEFORE INSERT ON person_binding
                          WHEN NEW.account_id='second'
                          BEGIN SELECT RAISE(ABORT, 'synthetic-upload-failure'); END""")
        else:
            db.execute("""CREATE TRIGGER fail_upload BEFORE INSERT ON binding_audit
                          WHEN NEW.action='revoke'
                          BEGIN SELECT RAISE(ABORT, 'synthetic-upload-failure'); END""")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic-upload-failure"):
        await store.merge_upload([
            {"person_id": "new", "platform": "qq", "account_id": account}
            for account in ("first", "second")
        ], mode="replace", expected_active=active, operator="human", reason="synthetic")
    assert dump(store) == before


@pytest.mark.asyncio
async def test_replace_stale_snapshot_and_conflict_are_fail_closed(store):
    await store.upsert_binding(person_id="original", platform="qq", account_id="keep")
    active = await store.list_bindings()
    before = dump(store)
    for expected in (None, []):
        with pytest.raises(ValueError, match="预览已过期"):
            await store.merge_upload([{"person_id": "new", "platform": "qq", "account_id": "new"}],
                                     mode="replace", expected_active=expected)
        assert dump(store) == before
    with pytest.raises(ValueError, match="冲突"):
        await store.merge_upload([{"person_id": "unseen", "platform": "qq", "account_id": "keep"}],
                                 mode="replace", expected_active=active)
    assert dump(store) == before
