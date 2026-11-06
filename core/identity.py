# one_id 与 person_id 是同一主体的同值字段别名。
"""读取侧身份解析：事件 → person_id + 来源账号 + 场景 + 受众。

阶段 1 只做底座：记忆仍挂在 ``livingmemory:user:<platform>:<identity>``，
额外标注 ``source_account`` / ``audience``；查询时通过 person_binding 解析
person_id，并用 ``legacy_scopes`` 双读旧会话范围，不退化成全库检索。
"""

from __future__ import annotations

import re
from typing import Any

from .audience_policy import audience_policy_for, policy_from_context
from .identity_keys import normalize_platform, canonical_account
from .memory_scope import (
    _config_get, _event_value, parse_value_list, resolve_event_identity, resolve_memory_scope,
    is_event_memory_allowed,
)

def source_account_for(platform: str, account_id: str) -> str:
    platform = normalize_platform(platform)
    account_id = str(account_id or "").strip()
    if not platform or not account_id:
        return ""
    try:
        return canonical_account(platform, account_id)
    except ValueError:
        return ""


def is_synthetic_memory_event(event) -> bool:
    """合成唤醒只看事件来源标志，不用真实用户的文本内容猜测。"""
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    if origin.split(":", 1)[0] in {"agent_wakeup", "cron"}:
        return True
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        # 只识别明确标记，不能把未知对象/适配器占位值当成合成事件。
        for key in ("agent_wakeup", "cron", "is_agent_wakeup", "is_cron"):
            value = getter(key)
            if value is True or (type(value) is int and value != 0):
                return True
            if isinstance(value, str) and value.strip().casefold() in {"true", "1"}:
                return True
    return False


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
    # resolve_scene 只返回 (场景类型, 场景 ID)；平台另有来源，不能一起解包。
    session_id = _event_value(event, "unified_msg_origin", "unified_msg_origin")
    kind, scene_id = resolve_scene(event)
    raw_platform = str(
        _event_value(event, "get_platform_name", "platform") or ""
    ).strip().casefold()
    platform = normalize_platform(raw_platform)
    if not platform:
        platform = parse_session_origin(session_id)[0]
    source_account, account_id = _sender_account(event, config)
    audience = audience_for(kind, scene_id)
    scope = resolve_memory_scope(config, event) or ""
    person_id = ""
    identity_revision = None
    is_bound = False
    entity_type = "unknown"
    owner_account = ""
    owner_one_id = ""
    person_scopes: list[str] = []
    bound_accounts: list[dict[str, str]] = []
    if store is not None and source_account:
        try:
            snapshot_getter = getattr(store, "identity_snapshot", None)
            if callable(snapshot_getter):
                snapshot = await snapshot_getter(platform, account_id)
                person_id = snapshot["one_id"]
                identity_revision = snapshot["identity_revision"]
                is_bound = snapshot["is_bound"]
                entity_type = snapshot["entity_type"]
                accounts = snapshot["bound_accounts"]
            else:
                # 旧消费者只提供 resolve_person/accounts_for 时保留账号自身路径；
                # 没有版本校验能力的范围不被当作新扩展授权。
                person_id = await store.resolve_person(platform, account_id)
                accounts = []
            # 已绑定同一人物的其他账号：读取时一并纳入（仍逐条过受众过滤），
            # 这样“改绑定”才会真正影响召回；未绑定时为空，范围不扩大。
            for binding in accounts:
                bound_platform = normalize_platform(binding.get("platform") or "")
                bound_account = str(binding.get("account_id") or "").strip()
                if bound_platform and bound_account:
                    bound_accounts.append({"platform": bound_platform, "account_id": bound_account})
                    suffix = f"{bound_account.casefold()}"
                    person_scopes.append(
                        f"livingmemory:user:{bound_platform}:{suffix}"
                    )
                    # 现有记忆按适配器实例名（如 snowluma_onebot）落 scope，
                    # 同平台绑定时再补一个原样前缀，避免旧范围读不到。
                    if raw_platform and bound_platform == platform and raw_platform != platform:
                        person_scopes.append(
                            f"livingmemory:user:{raw_platform}:{suffix}"
                        )
        except Exception:
            person_id = ""
            person_scopes = []
            bound_accounts = []
            identity_revision = None
    if not person_id and source_account:
        person_id = f"person:{normalize_platform(platform)}:{account_id}"
    # self_id 是机器人的实际账号；缺失时不使用 sender_id/persona_id/实例兜底。
    self_id = _event_value(event, "get_self_id", "self_id")
    if not self_id:
        message = getattr(event, "message_obj", None)
        self_id = message.get("self_id") if isinstance(message, dict) else getattr(message, "self_id", "")
    if self_id:
        owner_account = source_account_for(platform, self_id)
        if owner_account and store is not None and callable(getattr(store, "identity_snapshot", None)):
            try:
                owner = await store.identity_snapshot(platform, str(self_id))
                # 两次读取不是同一快照时不交付旧的跨账号范围。
                if owner["identity_revision"] != identity_revision:
                    person_scopes, bound_accounts = [], []
                    identity_revision = None
                owner_one_id = owner["one_id"]
            except Exception:
                owner_one_id = ""
    policy = audience_policy_for(config)
    isolated_sessions = parse_value_list(
        _config_get(config, "filtering_settings.isolated_sessions", "")
    )
    if session_id in isolated_sessions:
        # 明确配置的隔离会话优先于新的跨群共享默认值。
        policy = {**policy, "cross_group_recall": False, "private_to_group_recall": False,
                  "sensitive_to_group_recall": False}
    return {
        "platform": normalize_platform(platform),
        "account_id": account_id,
        "source_account": source_account,
        "person_id": person_id,
        "one_id": person_id,
        "identity_revision": identity_revision,
        "is_bound": is_bound,
        "entity_type": entity_type,
        "owner_account": owner_account,
        "owner_one_id": owner_one_id,
        "scene_kind": kind,
        "scene_id": scene_id,
        "audience": audience,
        "origin_session_id": session_id,
        "origin_session_ref": session_ref_for(platform, kind, scene_id),
        "scope": scope,
        # 同一人物已绑定的其他账号范围（仅在有 active 绑定时出现）。
        "person_scopes": person_scopes,
        "bound_accounts": bound_accounts,
        "audience_policy": policy,
        "isolated_sessions": isolated_sessions,
        # 目的场景沿用现有白名单匹配；来源另外校验，旧外部上下文默认不授权。
        "sharing_whitelist": {
            "enabled": _config_get(config, "access_control.whitelist_enabled", False) is True,
            "destination_allowed": is_event_memory_allowed(config, event),
            "allowed_ids": parse_value_list(_config_get(config, "access_control.allowed_ids", "")),
        },
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
        "one_id": f"person:{platform}:{account_id}" if account_id else "",
        "identity_revision": None,
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


def legacy_shared_scope_selectors(context: dict[str, Any]) -> list[tuple[str, bool]]:
    """只枚举同一适配器、当前账号/有效绑定账号的旧会话，绝不使用全库scope。"""
    if context.get("scene_kind") != "group" or not context.get("persona_id"):
        return []
    origin = str(context.get("origin_session_id") or "")
    parts = origin.split(":", 2)
    if len(parts) != 3 or parts[1] != "GroupMessage":
        return []
    platform = normalize_platform(parts[0])
    if platform != context.get("platform") or origin in (context.get("isolated_sessions") or []):
        return []
    policy = policy_from_context(context)
    accounts = [{"platform": platform, "account_id": context.get("account_id")}]
    accounts.extend(context.get("bound_accounts") or [])
    selectors = []
    if policy["cross_group_recall"] and context.get("account_id"):
        # 旧的纯群会话没有个人归属：作为同适配器群共有范围处理，不能猜成某个人。
        selectors.append((f"{parts[0]}:GroupMessage:", True))
    for account in accounts:
        if not isinstance(account, dict) or normalize_platform(account.get("platform")) != platform:
            continue
        account_id = str(account.get("account_id") or "")
        if not account_id or ":" in account_id:
            continue
        # 群UMO为sender_group；没有sender证据的旧群记录不猜测人物归属。
        if policy["cross_group_recall"]:
            selectors.append((f"{parts[0]}:GroupMessage:{account_id}_", True))
        if policy["private_to_group_recall"]:
            selectors.extend((f"{parts[0]}:{kind}:{account_id}", False)
                             for kind in ("FriendMessage", "PrivateMessage"))
    return list(dict.fromkeys(selectors))


def legacy_shared_scope_allows(metadata: dict[str, Any], context: dict[str, Any]) -> bool:
    """发现阶段与返回阶段都验证旧来源；绑定撤销或标签漂移不能借缓存绕过。"""
    scope = str(metadata.get("session_id") or "")
    if not scope or scope in (context.get("isolated_sessions") or []):
        return False
    # 新增共享必须有一致的人格和人物证据；显式冲突不能被scope覆盖。
    if not context.get("persona_id") or metadata.get("persona_id") != context["persona_id"]:
        return False
    if (not metadata.get("source_account") and metadata.get("person_id")
            and metadata["person_id"] != context.get("person_id")):
        return False
    # actor_other_group也以actor_开头；必须从最后一个下划线反解完整sender。
    parts = scope.split(":", 2)
    if len(parts) != 3 or not parts[2] or ":" in parts[2]:
        return False
    exact_selector = (
        f"{parts[0]}:{parts[1]}:{parts[2].rsplit('_', 1)[0]}_"
        if parts[1] == "GroupMessage" and "_" in parts[2] else scope
    )
    selectors = legacy_shared_scope_selectors(context)
    group_shared = parts[1] == "GroupMessage" and "_" not in parts[2] and any(
        value == f"{parts[0]}:GroupMessage:" for value, _ in selectors
    )
    if not group_shared and not any(exact_selector == value for value, _ in selectors):
        return False
    source_account = str(metadata.get("source_account") or "")
    if group_shared and sensitivity_level(metadata) >= 2 and not (
        source_account or metadata.get("person_id") == context.get("person_id")
    ):
        # 纯群旧记录缺少人物依据；新增高敏共享不能把整群隐私猜成当前发言者的。
        return False
    if source_account:
        namespace, _, account = source_account.partition(":")
        allowed = {
            source_account_for(context.get("platform"), context.get("account_id")),
            *(source_account_for(item.get("platform"), item.get("account_id"))
              for item in context.get("bound_accounts") or [] if isinstance(item, dict)),
        }
        if source_account_for(namespace, account) not in allowed:
            return False
        sender = parts[2].rsplit("_", 1)[0] if parts[1] == "GroupMessage" else parts[2]
        if not group_shared and (
            normalize_platform(namespace) != normalize_platform(parts[0]) or account != sender
        ):
            return False
    elif group_shared and metadata.get("person_id") and metadata["person_id"] != context.get("person_id"):
        return False
    window = metadata.get("source_window")
    sources = [
        metadata.get("source_session_id"),
        window.get("session_id") if isinstance(window, dict) else "",
    ]
    if any(source and str(source) != scope for source in sources):
        return False
    # 双重验证：即使SQL返回了同名来源，坏来源或显式私密限制仍不能扩展。
    return audience_allows(metadata, context)


def shared_record_identity_allows(metadata: dict[str, Any], context: dict[str, Any]) -> bool:
    """跨场景扩展不把global或混合范围当作个人归属证明。"""
    audience = infer_audience(metadata)
    if audience == "public" or audience == context.get("audience"):
        return True  # 原本就在可见范围内，保持原规则。
    if not context.get("persona_id") or metadata.get("persona_id") != context["persona_id"]:
        return False
    if (not metadata.get("source_account") and metadata.get("person_id")
            and metadata["person_id"] != context.get("person_id")):
        return False
    window = metadata.get("source_window")
    sources = [
        str(value) for value in (
            metadata.get("source_session_id"),
            window.get("session_id") if isinstance(window, dict) else "",
        ) if value
    ]
    if len(set(sources)) > 1:
        return False
    scope = str(metadata.get("session_id") or "")
    if scope.startswith("livingmemory:user:") and scope in {
        context.get("scope"), *(context.get("person_scopes") or []),
    }:
        if sources:
            # 具体user scope只证明存储范围；明确来源仍须同适配器且属于当前/绑定账号。
            return legacy_shared_scope_allows(
                {**metadata, "session_id": sources[0]}, context,
            )
        account = str(metadata.get("source_account") or "")
        if not account:
            return True  # 既有具体user scope已经是当前身份的归属证明。
        platform, _, account_id = account.partition(":")
        allowed = {
            source_account_for(context.get("platform"), context.get("account_id")),
            *(source_account_for(item.get("platform"), item.get("account_id"))
              for item in context.get("bound_accounts") or [] if isinstance(item, dict)),
        }
        return source_account_for(platform, account_id) in allowed
    window = metadata.get("source_window")
    source = metadata.get("source_session_id") or (
        window.get("session_id") if isinstance(window, dict) else ""
    ) or scope
    return legacy_shared_scope_allows(
        {**metadata, "session_id": source, "source_session_id": source}, context,
    )


def audience_allows(metadata: dict[str, Any], context: dict[str, Any]) -> bool:
    """按“当前场景 × 当前人物 × 等级”判断一条记忆能否进入上下文。"""
    if not isinstance(metadata, dict) or not isinstance(context, dict):
        return False
    gate = context.get("sharing_whitelist")
    if isinstance(gate, dict) and gate.get("enabled") and gate.get("destination_allowed") is not True:
        # 外部插件复用召回接口时也执行白名单，不能依赖聊天入口已提前检查。
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
    policy = policy_from_context(context)
    if sensitivity >= 2 and not (
        context.get("scene_kind") == "group"
        and policy.get("sensitive_to_group_recall", False)
    ):
        # 先确认来源，再处理敏感级；私聊捷径不得放行 unknown。
        if audience == "self_private":
            return True
        return (
            sensitivity == 2 and audience.startswith("origin_group:")
            and audience == record_audience
        )
    window = metadata.get("source_window")
    record_origin = str(metadata.get("source_session_id") or (
        window.get("session_id") if isinstance(window, dict) else ""
    ) or metadata.get("session_id") or "")
    can_share = (
        context.get("scene_kind") == "group"
        and audience.startswith("origin_group:")
        and sensitivity <= policy["cross_group_max_sensitivity"]
        and visible_to != "origin_group"
        and record_origin not in (context.get("isolated_sessions") or [])
        and (sensitivity < 2 or policy.get("sensitive_to_group_recall", False))
        and sharing_whitelist_allows(metadata, context)
    )
    if record_audience == "public":
        return sensitivity < 2 or audience == "self_private" or can_share
    if record_audience == "self_private":
        return audience == "self_private" or (
            can_share and policy["private_to_group_recall"]
        )
    if record_audience.startswith("origin_group:"):
        return (record_audience == audience and sensitivity < 3) or (
            can_share and (record_audience == audience or policy["cross_group_recall"])
        )
    return False


def sharing_whitelist_allows(metadata: dict[str, Any], context: dict[str, Any]) -> bool:
    """扩展共享时，目的和来源均须在白名单；账号绑定本身不授予白名单权限。"""
    gate = context.get("sharing_whitelist")
    if not isinstance(gate, dict):
        # 未携带白名单信息的旧调用只能沿用旧授权，不能共享高敏。
        return sensitivity_level(metadata) < 2
    if not gate.get("enabled"):
        return True
    if gate.get("destination_allowed") is not True:
        return False
    allowed = {str(item).casefold() for item in gate.get("allowed_ids", [])}
    window = metadata.get("source_window")
    origin = str(metadata.get("source_session_id") or (
        window.get("session_id") if isinstance(window, dict) else ""
    ) or metadata.get("session_id") or "")
    platform, kind, scene = parse_session_origin(origin)
    account = str(metadata.get("source_account") or "")
    namespace, _, sender = account.partition(":")
    if not account and kind == "private":
        namespace, sender = platform, scene
    if not account and kind == "group":
        tail = origin.split(":", 2)[-1]
        sender = tail.rsplit("_", 1)[0] if "_" in tail else ""
        namespace = platform
    candidates = {origin, sender, source_account_for(namespace, sender)}
    if kind == "group":
        candidates.add(scene)
    return any(value and value.casefold() in allowed for value in candidates)
