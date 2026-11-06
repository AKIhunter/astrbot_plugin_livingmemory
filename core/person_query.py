# one_id 查询优先使用当前账号关系，不恢复已撤销的历史快照。
"""管理页按当前有效绑定查询；不回写历史记忆，不猜纯群记录的归属。"""

import json
from typing import Any

from .identity import normalize_platform, sensitivity_level, source_account_for
from . import memory_scope


def metadata_dict(value: Any) -> dict:
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def record_account(metadata: dict) -> str:
    explicit = str(metadata.get("source_account") or "")
    if explicit:
        platform, sep, account = explicit.partition(":")
        return source_account_for(platform, account) if sep else ""
    window = metadata.get("source_window")
    origin = str(metadata.get("source_session_id") or (
        window.get("session_id") if isinstance(window, dict) else ""
    ) or metadata.get("session_id") or "")
    if origin.startswith("livingmemory:user:"):
        parts = origin.split(":", 3)
        return source_account_for(parts[2], parts[3]) if len(parts) == 4 else ""
    parts = origin.split(":", 2)
    if len(parts) == 3 and ":" not in parts[2]:
        if parts[1] in {"FriendMessage", "PrivateMessage"}:
            return source_account_for(parts[0], parts[2])
        if parts[1] == "GroupMessage" and "_" in parts[2]:
            return source_account_for(parts[0], parts[2].rsplit("_", 1)[0])
    return ""


def person_matches(raw: Any, person_id: str, bindings_json: str) -> int:
    metadata = metadata_dict(raw)
    if not metadata or not person_id:
        return 0
    account = record_account(metadata)
    if metadata.get("source_account") and not account:
        return 0
    if account:
        bindings = json.loads(bindings_json)
        # 旧 person 标签不覆盖当前绑定；撤销后只回到账号自己的默认人物标识。
        resolved = bindings.get(account, "person:" + account)
        return int(resolved == person_id)
    return 0  # 缺少账号来源的旧标签只作历史提示，不证明可扩展的归属。


async def person_predicate(db, person_id: str, column: str = "metadata") -> tuple[str, list]:
    await db.create_function("lm_person_matches", 3, person_matches, deterministic=True)
    cursor = await db.execute(
        "SELECT platform, account_id, person_id FROM person_binding WHERE status = 'active'"
    )
    bindings: dict[str, str] = {}
    for row in await cursor.fetchall():
        platform, account, person = tuple(row)
        key = source_account_for(platform, account)
        if key in bindings and bindings[key] != person:
            bindings[key] = ""  # 冲突别名不能把一个账号算到两个人。
        else:
            bindings[key] = person
    return f"lm_person_matches({column}, ?, ?) = 1", [
        person_id, json.dumps(bindings, ensure_ascii=False),
    ]


async def install_sensitivity(db) -> None:
    await db.create_function(
        "lm_sensitivity", 1, lambda raw: sensitivity_level(metadata_dict(raw)),
        deterministic=True,
    )


async def person_memory_ids(engine, person_id: str, session_id=None, persona_id=None) -> list[int]:
    if getattr(engine, "identity_store", None) is not None:
        return await engine.identity_memory_ids(person_id, session_id, persona_id)
    db = engine.db_connection
    clause, params = await person_predicate(db, person_id)
    clauses = [clause]
    # 线上适配器隔离也是管理查询边界；旧版本没有此接口时维持原行为。
    adapter_sql = getattr(memory_scope, "adapter_sql", None)
    if callable(adapter_sql):
        ownership, ownership_params = adapter_sql("metadata")
        clauses.append(ownership)
        params.extend(ownership_params)
    for key, value in (("session_id", session_id), ("persona_id", persona_id)):
        if value:
            clauses.append(f"json_extract(metadata, '$.{key}') = ?")
            params.append(value)
    cursor = await db.execute(
        "SELECT id FROM documents WHERE " + " AND ".join(clauses) +
        " ORDER BY COALESCE(CAST(json_extract(metadata, '$.create_time') AS REAL), 0) DESC, id DESC",
        params,
    )
    return [int(row[0]) for row in await cursor.fetchall()]


async def search_person_memories(engine, ids: list[int], query: str, persona_id, limit: int):
    """按该人物真实会话逐一检索，再与当前归属交集；不会被别人的全库前K条挤掉。"""
    if not ids:
        return []
    from contextlib import nullcontext
    from .identity_access import IdentityAccess, identity_access, identity_visible
    store = getattr(engine, "identity_store", None)
    access = IdentityAccess(frozenset(ids), await store.get_revision(), store) if store else None
    with identity_access(access) if access is not None else nullcontext():
        results = await _search_person_memories(engine, ids, query, persona_id, limit)
        if access is not None and not await access.current():
            return []
        return [row for row in results if identity_visible(row.doc_id)]


async def _search_person_memories(engine, ids, query, persona_id, limit):
    cursor = await engine.db_connection.execute(
        "SELECT DISTINCT json_extract(metadata, '$.session_id') FROM documents "
        "WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    )
    scopes = [row[0] for row in await cursor.fetchall() if row[0]]
    allowed = set(ids)
    found = {}
    for scope in scopes:
        results = await engine.search_memories(
            query=query, k=limit, session_id=scope, persona_id=persona_id,
            allow_legacy_migration=False,
        )
        for result in results:
            try:
                memory_id = int(result.doc_id)
            except (ValueError, TypeError):
                continue
            if memory_id not in allowed:
                continue
            previous = found.get(memory_id)
            if previous is None or result.final_score > previous.final_score:
                found[memory_id] = result
    return sorted(found.values(), key=lambda result: result.final_score, reverse=True)[:limit]
