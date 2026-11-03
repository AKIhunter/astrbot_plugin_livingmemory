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
    parts = str(session_id or "").split(":")
    if len(parts) < 2:
        return "", "", ""
    platform = normalize_platform(parts[0].split("_")[0] if parts[0] else "")
    marker = parts[1].lower()
    kind = "group" if "group" in marker else "private" if "friend" in marker or "private" in marker else ""
    scene_id = parts[2] if len(parts) > 2 else ""
    return platform, kind, scene_id


def resolve_scene(event: Any) -> tuple[str, str]:
    """返回 (scene_kind, scene_id)：group / private / video_comment / video_thread。"""
    session_id = _event_value(event, "unified_msg_origin", "unified_msg_origin")
    group_id = _event_value(event, "get_group_id", "group_id")
    if group_id:
        return "group", group_id
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
    return "private", scene_id


def audience_for(kind: str, scene_id: str) -> str:
    if kind == "group":
        # 阶段 1 回写：统一来源串可能是 <群号>_<用户>，受众标识固定取群号，
        # 保证读写两侧口径一致。
        gid = str(scene_id or "").split("_")[0]
        return "origin_group" if not gid else f"origin_group:{gid}"
    if kind in {"video_comment", "video_thread"}:
        return "public"
    return "self_private"


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
    kind = kind if kind in {"group", "private", "video_comment", "video_thread"} else "private"
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
    """旧记录没有 audience 时，从来源会话推断；推断不出就不放行。"""
    if not isinstance(metadata, dict):
        return ""
    explicit = str(metadata.get("audience") or "")
    if explicit:
        return explicit
    origin = str(
        metadata.get("source_session_id")
        or (metadata.get("source_window") or {}).get("session_id")
        or metadata.get("session_id")
        or ""
    )
    if "GroupMessage" in origin:
        scene_id = origin.split(":", 2)[-1]
        # 群号可能带下划线后缀；取第一个片段即可稳定标识原群。
        return f"origin_group:{scene_id.split('_')[0]}"
    if "FriendMessage" in origin or "PrivateMessage" in origin:
        return "self_private"
    return ""


def audience_allows(metadata: dict[str, Any], context: dict[str, Any]) -> bool:
    """按“当前场景 × 当前人物 × 等级”判断一条记忆能否进入上下文。"""
    audience = str(context.get("audience") or "")
    record_audience = infer_audience(metadata or {})
    visible_to = str((metadata or {}).get("visible_to") or "")
    try:
        sensitivity = int((metadata or {}).get("sensitivity") or 0)
    except (TypeError, ValueError):
        sensitivity = 0

    if visible_to == "self_private" and audience != "self_private":
        return False
    if sensitivity >= 3 and audience != "self_private":
        return False
    if sensitivity == 2:
        # 敏感级 2：本人私聊或原群可见；其他群/公开一律不召回。
        if audience == "self_private":
            return True
        if audience.startswith("origin_group:") and audience == record_audience:
            return True
        return False
    if not record_audience:
        # 旧记录没有受众标记：仅允许由精确旧会话范围双读出来的结果。
        return False
    if record_audience == "public":
        return True
    if record_audience == "self_private":
        return audience == "self_private"
    if record_audience.startswith("origin_group:"):
        return record_audience == audience
    return False
