# 人和机器人共用 one_id；人格与账号编号保持独立。
"""身份键的共享定义；账号、主体和机器人人格各自保留自己的编号。"""

from __future__ import annotations

import re
from typing import Any

_ALIASES = {
    "aiocqhttp": "qq", "onebot": "qq", "napcat": "qq", "snowluma": "qq",
    "qq": "qq", "qq_official": "qq", "bilibili": "bili", "bili": "bili",
}


def normalize_platform(value: Any) -> str:
    key = str(value or "").strip().casefold()
    if key in _ALIASES:
        return _ALIASES[key]
    # 仅折叠已知适配器前缀；未知平台中的下划线不是账号合并证据。
    base = key.split("_", 1)[0]
    return _ALIASES.get(base, key)


def canonical_account(platform: Any, account_id: Any) -> str:
    platform = normalize_platform(platform)
    account = str(account_id or "").strip()
    if not platform or not re.fullmatch(r"[a-z0-9_.-]{1,80}", platform):
        raise ValueError("缺少或无效的平台命名空间")
    if not account or len(account) > 256 or any(ord(c) < 32 for c in account):
        raise ValueError("缺少或无效的平台账号 ID")
    # 平台稳定 UID 保持大小写；群号/昵称不得由调用方冒充账号。
    return f"{platform}:{account}"


def canonical_account_sql(platform: str, account_id: str) -> str:
    """仅接受内部列表达式；迁移和约束使用与 Python 同一平台别名定义。"""
    raw = f"lower(trim({platform}))"
    base = f"CASE WHEN instr({raw},'_')>0 THEN substr({raw},1,instr({raw},'_')-1) ELSE {raw} END"
    cases = " ".join(f"WHEN '{key}' THEN '{value}'" for key, value in _ALIASES.items())
    normalized = f"CASE {raw} {cases} ELSE CASE {base} {cases} ELSE {raw} END END"
    return f"({normalized}) || ':' || trim({account_id})"


def identity_alias(*, one_id: Any = "", person_id: Any = "") -> str:
    new, old = str(one_id or "").strip(), str(person_id or "").strip()
    if new and old and new != old:
        raise ValueError("one_id 与 person_id 必须同值")
    value = new or old
    if not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("缺少或无效的 one_id")
    return value


def with_identity_alias(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    value = identity_alias(one_id=result.get("one_id"), person_id=result.get("person_id"))
    return {**result, "one_id": value, "person_id": value}
