"""长期保留的时间采样、隐私分组和原子生命周期回归。"""
import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.retention import RetentionSampler
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_livingmemory.core.processors.atom_classifier import _classify_single
from astrbot_plugin_livingmemory.storage.atom_store import AtomStore

START = 1636200000.0
CONF = dict(long_term_retention_enabled=True, retention_recent_days=180,
            retention_sample_days=90, retention_atom_days=3650)


def meta(day, **extra):
    return dict(create_time=START + day * 86400, importance=0.01,
                source_session_id="qq:FriendMessage:100", source_account="qq:100",
                audience="self_private", **extra)


def sample(rows, day=1826):
    sampler = RetentionSampler(CONF, START + day * 86400)
    for doc_id, text, metadata in rows:
        sampler.observe(doc_id, text, metadata)
    return sampler.protected_ids()


def test_unique_events_and_fact_values_are_never_removed():
    rows = [(i, f"2021年11月{i}日，我买的相机型号是A{i}。", meta(i))
            for i in range(1, 10)]
    assert sample(rows) == set(range(1, 10))


def test_repeated_routine_keeps_first_recent_and_interval_samples():
    rows = [(i + 1, f"2021年11月{i % 28 + 1}日，我散步走了5000步。", meta(i))
            for i in range(1826)]
    kept = sample(rows)
    assert 1 in kept and 1826 in kept
    assert set(range(1647, 1827)) <= kept
    assert len(kept) < 210
    all_buckets = {int(m["create_time"] // (90 * 86400)) for _, _, m in rows}
    kept_buckets = {int(m["create_time"] // (90 * 86400))
                    for doc_id, _, m in rows if doc_id in kept}
    assert kept_buckets == all_buckets
    # 再次清理不会把已经留下的时间锚点删掉。
    assert sample([row for row in rows if row[0] in kept]) == kept


def test_different_accounts_and_audiences_cannot_supply_duplicate_evidence():
    rows = [(i, "2021年11月1日，我吃了牛肉饭。", meta(i)) for i in range(1, 4)]
    rows[1][2]["source_account"] = "qq:200"
    rows[2][2]["audience"] = "group_public"
    assert sample(rows) == {1, 2, 3}
    # 同一天多次复制仍不能被当作跨日期重复。
    assert sample([(i, rows[0][1], meta(1)) for i in range(1, 9)]) == set(range(1, 9))


def test_missing_scope_time_and_long_term_types_are_protected():
    rows = [(i, "重复偏好", meta(i, atom_types=["preference"])) for i in range(1, 7)]
    rows += [(7, "无来源", {"create_time": START}), (8, "无日期", {"session_id": "s"})]
    assert sample(rows) == set(range(1, 9))


def test_daily_ingestion_and_cleanup_match_end_of_period_sampling():
    full = [(i + 1, "2021年11月1日，我吃了牛肉饭。", meta(i)) for i in range(1826)]
    surviving = []
    for day, row in enumerate(full, 1):
        surviving.append(row)
        kept = sample(surviving, day=day)
        surviving = [item for item in surviving if item[0] in kept]
    assert {row[0] for row in surviving} == sample(full)


def test_retention_config_is_opt_in_and_rejects_invalid_intervals():
    from astrbot_plugin_livingmemory.core.base.config_validator import ForgettingAgentConfig
    assert ForgettingAgentConfig().long_term_retention_enabled is False
    with pytest.raises(ValueError):
        ForgettingAgentConfig(retention_min_occurrences=2)
    with pytest.raises(ValueError):
        ForgettingAgentConfig(retention_sample_days=0)


@pytest.mark.asyncio
async def test_cleanup_uses_native_delete_only_for_redundant_rows(tmp_path):
    engine = MemoryEngine(str(tmp_path / "memory.db"), None, config=dict(CONF))
    engine.db_connection = await aiosqlite.connect(engine.db_path)
    engine.db_connection.row_factory = aiosqlite.Row
    await engine.db_connection.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")
    rows = [(i + 1, "2021年11月1日，我吃了牛肉饭。", json.dumps(meta(i)))
            for i in range(365)]
    rows.append((366, "我第一次登山住在青溪客栈。", json.dumps(meta(1))))
    await engine.db_connection.executemany("INSERT INTO documents VALUES (?,?,?)", rows)
    await engine.db_connection.commit()
    engine.batch_delete_memories = AsyncMock(side_effect=lambda ids: len(ids))
    with patch("time.time", return_value=START + 1826 * 86400):
        count = await engine.cleanup_old_memories()
    deleted = set(engine.batch_delete_memories.call_args.args[0])
    assert count > 350 and 1 not in deleted and 365 not in deleted and 366 not in deleted
    engine.config["long_term_retention_enabled"] = False
    with patch("time.time", return_value=START + 1826 * 86400):
        assert await engine.cleanup_old_memories() == 366
    await engine.close()


@pytest.mark.asyncio
async def test_long_term_atoms_survive_five_years_but_plans_and_unknown_expire(tmp_path):
    store = AtomStore(str(tmp_path / "atoms.db"), CONF)
    await store.initialize()
    ids = {}
    with patch("time.time", return_value=START):
        for kind in AtomType:
            atom = MemoryAtom(parent_memory_id=1, atom_type=kind, content=kind.value)
            ids[kind] = await store.insert(atom)
    with patch("time.time", return_value=START + 1826 * 86400):
        assert await store.expire_stale_atoms() == 2
        for kind, atom_id in ids.items():
            atom = await store.get(atom_id)
            assert atom.is_expired() == (kind in {AtomType.PLANNED, AtomType.UNKNOWN})
    with patch("time.time", return_value=START + 3651 * 86400):
        assert await store.expire_stale_atoms() == 4


@pytest.mark.asyncio
async def test_enable_extends_only_live_atoms_without_resurrection_or_restart_drift(tmp_path):
    store = AtomStore(str(tmp_path / "atoms.db"))
    await store.initialize()
    with patch("time.time", return_value=START):
        live = await store.insert(MemoryAtom(1, AtomType.FACTUAL, "仍有效的事实"))
        dead = await store.insert(MemoryAtom(1, AtomType.PLANNED, "已经到期的计划"))
        replaced = await store.insert(MemoryAtom(1, AtomType.FACTUAL, "已经替代的事实"))
    async with store._connect() as db:
        await db.execute("UPDATE memory_atoms SET status='superseded' WHERE id=?", (replaced,))
        await db.commit()
    store.config = CONF
    with patch("time.time", return_value=START + 10 * 86400):
        await store.initialize()
    assert (await store.get(live)).expires_at == START + 3650 * 86400
    assert (await store.get(replaced)).ttl_days == 180
    assert (await store.get(dead)).ttl_days == 2
    with patch("time.time", return_value=START + 20 * 86400):
        await store.initialize()
        await store.reinforce(live)
    assert (await store.get(live)).expires_at == START + 3670 * 86400
    with patch("time.time", return_value=START + 30 * 86400):
        await store.initialize()
    assert (await store.get(live)).expires_at == START + 3670 * 86400


@pytest.mark.parametrize("text, expected", [
    ("昨天参加登山活动", AtomType.EPISODIC),
    ("2021年11月6日，我第一次参加登山活动。", AtomType.EPISODIC),
    ("我和朋友约定每周一一起去游泳。", AtomType.RELATIONAL),
    ("明天下午去参加会议", AtomType.PLANNED),
    ("2021年11月6日，我准备明天去开会。", AtomType.PLANNED),
    ("2099年11月6日，去参加会议。", AtomType.PLANNED),
])
def test_history_and_recurring_events_are_not_one_off_plans(text, expected):
    assert _classify_single(text)[0] == expected
