# 操作者来自后台认证，不能由请求正文伪造。
"""后台认证的主体、申请、投影和解绑接口。"""

from __future__ import annotations

from quart import request

from ..identity_keys import identity_alias
from ...storage.oneid_store import IdentityConflict, IdentityNotFound


def authenticated_operator() -> str:
    """原生 PluginRequest 的 username 由宿主认证填充，不能来自请求正文。"""
    try:
        from astrbot.api.web import request as native_request
        username = native_request.username
        if isinstance(username, str) and username.strip():
            return username.strip()
    except (ImportError, AttributeError, RuntimeError):
        pass
    try:
        from quart import g
        username = getattr(g, "username", "")
        if isinstance(username, str) and username.strip():
            return username.strip()
    except RuntimeError:
        pass
    raise PermissionError("需要登录 AstrBot 管理后台")


class OneIdHandler:
    def __init__(self, plugin, utils):
        self.plugin, self.utils = plugin, utils

    async def handle(self, action):
        try:
            actor = authenticated_operator()
            initializer = self.plugin.initializer
            store, engine = initializer.identity_store, initializer.memory_engine
            if store is None or engine is None:
                raise RuntimeError("身份服务未初始化")
            body = await request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                raise ValueError("请求正文必须为对象")
            params = dict(request.args)
            params.update(body)
            # 两个命名同时出现必须一致；不能默默选择某一个。
            target = identity_alias(one_id=params.get("one_id"), person_id=params.get("person_id")) \
                if params.get("one_id") or params.get("person_id") else ""
            if action == "entities":
                data = await store.list_entities(limit=int(params.get("limit", 100)),
                                                 after=str(params.get("after") or ""))
            elif action == "register":
                data = await store.register_entity(
                    one_id=target, entity_type=params.get("entity_type", "unknown"),
                    display_name=params.get("display_name", ""), operator=actor,
                    request_id=params.get("idempotency_key", ""),
                )
            elif action == "applications":
                data = await store.list_applications(
                    status=params.get("status", "pending"), limit=int(params.get("limit", 100)),
                    after=str(params.get("after") or ""),
                )
            elif action == "submit":
                # 自动识别必须由实际事件的 Python 入口提交，网页不能伪造事件来源。
                if params.get("source", "manual") != "manual":
                    raise ValueError("自动申请只能由可信事件入口提交")
                data = await store.submit_application(
                    one_id=target, platform=params.get("platform", ""),
                    account_id=params.get("account_id", ""), operator=actor,
                    idempotency_key=params.get("idempotency_key", ""),
                    evidence=params.get("evidence", {}), display_name=params.get("display_name", ""),
                    correction=params.get("correction", False),
                )
            elif action == "review":
                data = await store.review_application(
                    params.get("application_id", ""),
                    expected_application=params.get("expected_application"),
                    reviewer=actor, idempotency_key=params.get("idempotency_key", ""),
                    decision=params.get("decision", "approve"), reason=params.get("reason", ""),
                )
            elif action == "preview":
                data = await engine.preview_identity_change(
                    one_id=target, platform=params.get("platform", ""),
                    account_id=params.get("account_id", ""), binding_id=params.get("binding_id", ""),
                )
            elif action == "unlink":
                data = await store.unlink_account(
                    one_id=target, binding_id=params.get("binding_id", ""),
                    expected_version=params.get("expected_version"),
                    expected_revision=params.get("expected_revision"), operator=actor,
                    reason=params.get("reason", ""), idempotency_key=params.get("idempotency_key", ""),
                )
            elif action == "projection":
                data = await engine.project_identity(
                    one_id=target, owner_one_id=params.get("owner_one_id", ""),
                    session_id=params.get("session_id") or None,
                    persona_id=params.get("persona_id") or None, include_records=True,
                    limit=int(params.get("limit", 100)), offset=int(params.get("offset", 0)),
                )
                data.pop("memory_ids", None)  # 分页返回，避免把全部 ID 放进首屏。
            else:
                raise ValueError("未知身份操作")
            return self.utils.ok(data)
        except PermissionError as exc:
            return {**self.utils.error(str(exc)), "code": "authentication_required"}, 401
        except IdentityNotFound as exc:
            return {**self.utils.error(str(exc)), "code": exc.code}, 404
        except IdentityConflict as exc:
            return {**self.utils.error(str(exc)), "code": exc.code}, 409
        except (ValueError, TypeError) as exc:
            return {**self.utils.error(str(exc)), "code": "invalid_input"}, 400
        except Exception:
            return {**self.utils.error("身份服务暂不可用"), "code": "identity_unavailable"}, 503
