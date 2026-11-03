"""读取侧身份解析：事件 → person_id + 来源账号 + 场景 + 受众。

阶段 1 只做底座：记忆仍挂在 ``livingmemory:user:<platform>:<identity>``，
额外标注 ``source_account`` / ``audience``；查询时通过 person_binding 解析
person_id，并用 ``legacy_scopes`` 双读旧会话范围，不退化成全库检索。
"""

from __future__ import annotations

import re
from typing import Any

from .memory_scope import _event_value, resolve_event_identity, resolve_memory_scope

# 平台别名归一：平台类型不等于适配器实例，这里只做“账号命名空间”归一。
_PLATFORM_ALIASES = {
    "aiocqhttp": "qq",
    "onebot": "qq",
    "napcat": "qq",
    "snowluma": "qq",
    "qq": "qq",
    "qq_official": "qq",
    "bilibili": "bili",
    "bili": "bili",
}


def normalize_platform(value: Any) -> str:
    key = str(value or "").strip().casefold()
    if key in _PLATFORM_ALIASES:
        return _PLATFORM_ALIASES[key]
    # 阶段 1 回写：适配器实例名常带后缀（snowluma_onebot / qq_official 等），
    # 按前缀再归一次，避免同一账号因实例名不同被拆成两个人。
    base = key.split("_")[0]
    return _PLATFORM_ALIASES.get(base, base)


def source_account_for(platform: str, account_id: str) -> str:
    platform = normalize_platform(platform)
    account_id = str(account_id or "").strip()
    if not platform or not account_id:
        return ""
    return f"{platform}:{account_id}"


def parse_session_origin(session_id: str) -> tuple[str, str, str]:
    """从 unified_msg_origin 粗解析 (platform, kind, scene_id)。

    只作为兜底；优先使用事件自带的 platform/group/sender 字段。
    """
    parts = str(session_id or "").strip().split(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[2] or ":" in parts[2]:
        return "", "", ""
    platform = normalize_platform(parts[0])
    kind = {"GroupMessage": "group", "FriendMessage": "private",
            "PrivateMessage": "private"}.get(parts[1], "")
    # AstrBot event_bus 的 QQ unique_session 构造顺序为 sender_group。
    scene_id = parts[2].rsplit("_", 1)[-1] if kind == "group" else parts[2]
    return platform, kind, scene_id


def group_scene_id(value: Any) -> str:
    """兼容真实群号、组合 key 和历史误存的完整 UMO；不能取适配器名前缀。"""
    value = str(value or "").strip()
    if ":" in value:
        _, kind, scene_id = parse_session_origin(value)
        return scene_id if kind == "group" else ""
    return value.rsplit("_", 1)[-1]


def resolve_scene(event: Any) -> tuple[str, str]:
    """返回 (scene_kind, scene_id)：group / private / video_comment / video_thread。"""
    session_id = _event_value(event, "unified_msg_origin", "unified_msg_origin")
    group_id = _event_value(event, "get_group_id", "group_id")
    if group_id:
        return "group", group_scene_id(group_id)
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        try:
            video_id = str(getter("video_id") or getter("bili_bvid") or "").strip()
            root_rpid = str(getter("root_rpid") or getter("bili_root_rpid") or "").strip()
            if video_id:
                return ("video_thread" if root_rpid else "video_comment",
                        f"{video_id}:{root_rpid}" if root_rpid else video_id)
        except Exception:
            pass
    platform, kind, scene_id = parse_session_origin(session_id)
    if kind:
        return kind, scene_id
    if re.search(r"BV[0-9A-Za-z]{10}", session_id):
        match = re.search(r"BV[0-9A-Za-z]{10}", session_id)
        return "video_comment", match.group(0)
    return "unknown", ""


def audience_for(kind: str, scene_id: str) -> str:
    if kind == "group":
        gid = group_scene_id(scene_id)
        return f"origin_group:{gid}" if gid else ""
    if kind in {"video_comment", "video_thread"}:
        return "public"
    return "self_private" if kind == "private" and scene_id else ""


def session_ref_for(platform: str, kind: str, scene_id: str) -> str:
    from urllib.parse import quote

    return f"session:{normalize_platform(platform)}:{kind}:{quote(str(scene_id or ''), safe='')}"


def _sender_account(event: Any, config: Any) -> tuple[str, str]:
    platform = normalize_platform(_event_value(event, "get_platform_name", "platform"))
    sender_id = _event_value(event, "get_sender_id", "sender_id")
    identity = resolve_event_identity(config, event)
    # 账号优先用稳定 sender_id；缺失时才退回规范化身份。
    account_id = sender_id or identity
    return source_account_for(platform, account_id), account_id


async def build_identity_context(
    config: Any,
    event: Any,
    store: Any | None = None,
    *,
    persona_id: str = "",
) -> dict[str, Any]:
    """建立一次请求的可信上下文；任何字段缺失都保守降级，不扩大读取范围。"""
    # 阶段 1 回写：resolve_scene 只返回 (场景类型, 场景 ID)；平台另有来源，不能一起解包。
    session_id = _event_value(event, "unified_msg_origin", "unified_msg_origin")
    kind, scene_id = resolve_scene(event)
    raw_platform = str(_event_value(event, "get_platform_name", "platform") or "").strip().casefold()
    platform = normalize_platform(raw_platform)
    if not platform:
        platform = parse_session_origin(session_id)[0]
    source_account, account_id = _sender_account(event, config)
    audience = audience_for(kind, scene_id)
    scope = resolve_memory_scope(config, event) or ""
    person_id = ""
    person_scopes: list[str] = []
    if store is not None and source_account:
        try:
            person_id = await store.resolve_person(platform, account_id)
            # 已绑定同一人物的其他账号：读取时一并纳入（仍逐条过受众过滤），
            # 这样“改绑定”才会真正影响召回；未绑定时为空，范围不扩大。
            for binding in await store.accounts_for(person_id) or []:
                bound_platform = normalize_platform(binding.get("platform") or "")
                bound_account = str(binding.get("account_id") or "").strip()
                if bound_platform and bound_account:
                    suffix = f"{bound_account.casefold()}"
                    person_scopes.append(f"livingmemory:user:{bound_platform}:{suffix}")
                    # 现有记忆按适配器实例名（如 snowluma_onebot）落 scope，
                    # 同平台绑定时再补一个原样前缀，避免旧范围读不到。
                    if raw_platform and bound_platform == platform and raw_platform != platform:
                        person_scopes.append(f"livingmemory:user:{raw_platform}:{suffix}")
        except Exception:
            person_id = ""
            person_scopes = []
    if not person_id and source_account:
        person_id = f"person:{normalize_platform(platform)}:{account_id}"
    return {
        "platform": normalize_platform(platform),
        "account_id": account_id,
        "source_account": source_account,
        "person_id": person_id,
        "scene_kind": kind,
        "scene_id": scene_id,
        "audience": audience,
        "origin_session_id": session_id,
        "origin_session_ref": session_ref_for(platform, kind, scene_id),
        "scope": scope,
        # 同一人物已绑定的其他账号范围（仅在有 active 绑定时出现）。
        "person_scopes": person_scopes,
        # 旧记录双读：只读当前发生会话，不退化成全库。
        "legacy_scopes": [session_id] if session_id else [],
        "persona_id": persona_id,
    }


def build_identity_from_refs(
    *,
    platform: str,
    kind: str,
    scene_id: str,
    account_id: str = "",
    persona_id: str = "",
    origin_session_id: str = "",
) -> dict[str, Any]:
    """给外部插件（从绫/BiliBot）用已校验的 session_ref/actor_ref 构造上下文。"""
    platform = normalize_platform(platform)
    kind = kind if kind in {"group", "private", "video_comment", "video_thread"} else "unknown"
    if kind == "group":
        scene_id = group_scene_id(scene_id)
    source_account = source_account_for(platform, account_id)
    audience = audience_for(kind, scene_id)
    return {
        "platform": platform,
        "account_id": str(account_id or ""),
        "source_account": source_account,
        "person_id": f"person:{platform}:{account_id}" if account_id else "",
        "scene_kind": kind,
        "scene_id": str(scene_id or ""),
        "audience": audience,
        "origin_session_id": origin_session_id,
        "origin_session_ref": session_ref_for(platform, kind, scene_id),
        "scope": f"livingmemory:user:{platform}:{str(account_id).casefold()}"
                 if account_id else "",
        # 外部插件的历史镜像按原始 session_id 落库，这里把它作为具体旧范围双读，
        # 不传空 scope，避免退化成全库检索。
        "legacy_scopes": [str(origin_session_id)] if str(origin_session_id or "").strip() else [],
        "persona_id": persona_id,
    }


def infer_audience(metadata: dict[str, Any]) -> str:
    """来源优先校正旧错误标签；显式私聊限制不能被群来源放宽。"""
    if not isinstance(metadata, dict):
        return ""
    explicit = str(metadata.get("audience") or "")
    window = metadata.get("source_window")
    origin = str(
        metadata.get("source_session_id")
        or (window.get("session_id") if isinstance(window, dict) else "")
        or metadata.get("session_id")
        or ""
    )
    _, kind, scene_id = parse_session_origin(origin)
    if kind:
        inferred = audience_for(kind, scene_id)
        if not inferred:
            return ""
        return "self_private" if explicit == "self_private" else inferred
    # 合并 scope 本身没有来源语义；只能使用写入方的有效受众声明。
    if origin and not origin.startswith("livingmemory:"):
        return ""
    if explicit in {"public", "self_private"}:
        return explicit
    if explicit.startswith("origin_group:") and group_scene_id(explicit[13:]):
        return audience_for("group", explicit[13:])
    return ""


def sensitivity_level(metadata: dict[str, Any]) -> int:
    """兼容级别别名并取最高级；坏值按最高敏感级处理，不能降为公开。"""
    if not isinstance(metadata, dict):
        return 3
    levels = [0]
    nested = metadata.get("privacy")
    for values in (metadata, nested if isinstance(nested, dict) else {}):
        for key in ("sensitivity", "sensitivity_level", "privacy_level"):
            if key not in values:
                continue
            value = values[key]
            if isinstance(value, bool) or str(value) not in {"0", "1", "2", "3"}:
                levels.append(3)
            else:
                levels.append(int(value))
    return max(levels)


def audience_allows(metadata: dict[str, Any], context: dict[str, Any]) -> bool:
    """按“当前场景 × 当前人物 × 等级”判断一条记忆能否进入上下文。"""
    if not isinstance(metadata, dict) or not isinstance(context, dict):
        return False
    audience = str(context.get("audience") or "")
    record_audience = infer_audience(metadata)
    visible_to = str(metadata.get("visible_to") or "")
    sensitivity = sensitivity_level(metadata)
    if not record_audience or context.get("scene_kind") == "unknown":
        return False
    if audience not in {"self_private", "public"} and not (
        audience.startswith("origin_group:") and audience[13:]
    ):
        return False

    if visible_to == "self_private" and audience != "self_private":
        return False
    if visible_to not in {"", "internal", "public", "origin_group", "self_private"}:
        if not visible_to.startswith("origin_group:") or (
            audience != "self_private" and visible_to != audience
        ):
            return False
    if sensitivity >= 2:
        # 先确认来源，再处理敏感级；私聊捷径不得放行 unknown。
        if audience == "self_private":
            return True
        return (
            sensitivity == 2 and audience.startswith("origin_group:")
            and audience == record_audience
        )
    if record_audience == "public":
        return True
    if record_audience == "self_private":
        return audience == "self_private"
    if record_audience.startswith("origin_group:"):
        return record_audience == audience
    return False
