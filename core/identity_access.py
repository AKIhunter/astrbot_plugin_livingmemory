# one_id 核心接入：兼容原有来源与隔离。
"""请求内身份允许集；计算向量缓存不承担身份授权。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class IdentityAccess:
    memory_ids: frozenset[int]
    revision: int
    store: object
    one_id: str = ""
    owner_one_id: str = ""

    async def current(self) -> bool:
        try:
            return self.revision == await self.store.get_revision()
        except Exception:
            return False


current_identity_access = ContextVar("livingmemory_identity_access", default=None)


async def identity_current() -> bool:
    access = current_identity_access.get()
    return access is None or await access.current()


@contextmanager
def identity_access(access: IdentityAccess):
    # 下游不能用更宽的允许集覆盖上游的限定；退出时恢复上下文。
    previous = current_identity_access.get()
    if previous is not None:
        access = IdentityAccess(
            previous.memory_ids & access.memory_ids, previous.revision,
            previous.store, previous.one_id, previous.owner_one_id,
        )
    token = current_identity_access.set(access)
    try:
        yield access
    finally:
        current_identity_access.reset(token)


def identity_sql(column: str = "id") -> tuple[str, list]:
    access = current_identity_access.get()
    if access is None:
        return "1", []
    # json_each 避免 SQLite 参数数上限，不插值账号或记录 ID。
    return f"{column} IN (SELECT value FROM json_each(?))", [
        json.dumps(sorted(access.memory_ids))
    ]


def identity_visible(memory_id) -> bool:
    access = current_identity_access.get()
    if access is None:
        return True
    try:
        return int(memory_id) in access.memory_ids
    except (TypeError, ValueError):
        return False


def identity_cache_key() -> tuple:
    access = current_identity_access.get()
    if access is None:
        return ()
    fingerprint = hashlib.sha256(
        json.dumps(sorted(access.memory_ids)).encode()
    ).hexdigest()
    return (access.one_id, access.owner_one_id, access.revision, fingerprint)
