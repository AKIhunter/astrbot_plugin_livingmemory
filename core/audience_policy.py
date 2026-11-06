"""配置驱动的读取受众策略；不重写历史标签或索引。"""

from typing import Any

from .memory_scope import _config_get


def audience_policy_for(config: Any) -> dict[str, Any]:
    """缺省按用户选择开放低敏跨群；损坏值关闭扩展，不重置其他配置。"""
    if not isinstance(_config_get(config, "audience", {}), dict):
        return {"cross_group_recall": False, "private_to_group_recall": False,
                "cross_group_max_sensitivity": 0}
    cross_group = _config_get(config, "audience.cross_group_recall", True)
    sensitive = _config_get(config, "audience.sensitive_to_group_recall", False)
    # 旧开关只兼容低敏私聊，不能在升级后静默授权 2/3 级。
    legacy_private = _config_get(config, "audience.private_to_group_recall", False)
    maximum = _config_get(config, "audience.cross_group_max_sensitivity", 1)
    return {
        "cross_group_recall": cross_group is True,
        "sensitive_to_group_recall": sensitive is True,
        "private_to_group_recall": sensitive is True or legacy_private is True,
        "cross_group_max_sensitivity": maximum
        if type(maximum) is int and maximum in (0, 1, 2, 3) else 0,
    }


def policy_from_context(context: dict[str, Any]) -> dict[str, Any]:
    """只有可信身份构造入口携带的策略可扩大受众，旧外部调用保持原边界。"""
    policy = context.get("audience_policy")
    if not isinstance(policy, dict):
        return {
            "cross_group_recall": False,
            "private_to_group_recall": False,
            "cross_group_max_sensitivity": 0,
        }
    return audience_policy_for({"audience": {
        "cross_group_recall": policy.get("cross_group_recall", False),
        "private_to_group_recall": policy.get("private_to_group_recall", False),
        "sensitive_to_group_recall": policy.get("sensitive_to_group_recall", False),
        "cross_group_max_sensitivity": policy.get("cross_group_max_sensitivity", 0),
    }})
