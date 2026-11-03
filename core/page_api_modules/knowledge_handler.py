"""Admin-only native KB views and explicit unified-context policy management.

Registered exclusively below the authenticated /api/plug page namespace.
Never return original file paths, provider configuration, or raw exceptions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from uuid import UUID

from quart import request

from ..unified_context import parse_bindings


class KnowledgeHandler:
    def __init__(self, utils, plugin):
        self.utils = utils
        self.plugin = plugin

    def response(self, data=None, error=None):
        payload = self.utils.error(error) if error else self.utils.ok(data)
        return payload, 200, {"Cache-Control": "no-store"}

    def policy_data(self):
        cfg = self.plugin.config_manager.get("unified_context", {})
        serialized = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        revision = hashlib.sha256(serialized.encode()).hexdigest()
        try:
            bindings = [asdict(b) for b in parse_bindings(cfg.get("bindings_json", "[]"))]
            valid = True
        except (ValueError, AttributeError):
            bindings, valid = [], False
        return {
            "enabled": isinstance(cfg, dict) and cfg.get("enabled") is True,
            "bindings": bindings, "valid": valid, "revision": revision,
        }

    @staticmethod
    def uuid(value):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value

    @staticmethod
    def page():
        page = int(request.args.get("page", "1"))
        if not 1 <= page <= 10000:
            raise ValueError
        return page

    async def helper(self, kb_id):
        manager = getattr(self.plugin.context, "kb_manager", None)
        helper = await manager.get_kb(self.uuid(kb_id)) if manager else None
        if helper is None or str(helper.kb.kb_id) != kb_id:
            raise ValueError
        return helper

    async def catalog(self):
        try:
            manager = getattr(self.plugin.context, "kb_manager", None)
            if manager is None:
                return self.response(error="知识库服务尚未就绪")
            policy = self.policy_data()
            items = []
            for kb in await manager.list_kbs():
                # 白名单序列化，不能把 provider、文件路径等宿主对象整体返回。
                items.append({
                    "kb_id": str(kb.kb_id), "name": str(kb.kb_name)[:100],
                    "description": str(kb.description or "")[:500],
                    "documents": kb.doc_count, "chunks": kb.chunk_count,
                    "public": policy["enabled"] and any(
                        b["purpose"] == "public_comment" and kb.kb_id in b["kb_ids"]
                        for b in policy["bindings"]
                    ),
                })
            return self.response({"items": items, "policy": policy})
        except Exception:
            return self.response(error="读取资料目录失败，请刷新重试")

    async def documents(self):
        try:
            helper = await self.helper(request.args.get("kb_id"))
            page = self.page()
            docs = await helper.list_documents(offset=(page - 1) * 20, limit=20)
            return self.response({
                "items": [
                    {"doc_id": d.doc_id, "name": d.doc_name, "chunks": d.chunk_count}
                    for d in docs if str(d.kb_id) == str(helper.kb.kb_id)
                ],
                "page": page, "total": await helper.count_documents(),
            })
        except (ValueError, TypeError, AttributeError):
            return self.response(error="资料库不存在或分页参数无效")
        except Exception:
            return self.response(error="读取文档失败，请刷新重试")

    async def preview(self):
        try:
            kb_id, doc_id = request.args.get("kb_id"), self.uuid(request.args.get("doc_id"))
            helper, page = await self.helper(kb_id), self.page()
            doc = await helper.get_document(doc_id)
            # 原生 get_document 是全局查询，必须在读取正文前校验归属。
            if doc is None or str(doc.kb_id) != kb_id:
                return self.response(error="文档不存在或不属于该资料库")
            chunks = await helper.get_chunks_by_doc_id(doc_id, offset=(page - 1) * 10, limit=10)
            if await helper.get_document(doc_id) is None:
                return self.response(error="文档已删除，请刷新目录")
            items = [
                {"chunk_id": c["chunk_id"], "content": str(c["content"])[:6000],
                 "truncated": len(str(c["content"])) > 6000}
                for c in chunks if c.get("kb_id") == kb_id and c.get("doc_id") == doc_id
            ]
            return self.response({
                "name": doc.doc_name, "items": items, "page": page,
                "total": await helper.get_chunk_count_by_doc_id(doc_id),
            })
        except (ValueError, TypeError, AttributeError):
            return self.response(error="文档标识或分页参数无效")
        except Exception:
            return self.response(error="读取文档预览失败，请刷新重试")

    async def save_policy(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            return self.response(error="授权参数无效")
        try:
            bindings = parse_bindings(json.dumps(payload.get("bindings"), ensure_ascii=False))
            # 开启时核对库 ID；关闭/撤销必须允许清理已不存在的旧库授权。
            if payload["enabled"]:
                for kb_id in {k for b in bindings for k in b.kb_ids}:
                    await self.helper(kb_id)
            # 检查放在所有 await 之后，避免旧页面覆盖其他管理员刚撤销的授权。
            if payload.get("revision") != self.policy_data()["revision"]:
                return self.response(error="授权已被修改，请刷新后重新核对")
            self.plugin.config_manager.apply_updates({
                "unified_context.enabled": payload["enabled"],
                "unified_context.bindings_json": json.dumps(
                    [asdict(b) for b in bindings], ensure_ascii=False,
                ),
            })
            return self.response(self.policy_data())
        except (ValueError, TypeError, AttributeError):
            return self.response(error="授权无效，请检查用途、人格、会话和资料库")
        except Exception:
            return self.response(error="保存授权失败，原授权保持不变")

    async def test(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return self.response(error="查询参数无效")
        query, identity = payload.get("query"), payload.get("identity")
        if (
            not isinstance(query, str) or not query.strip() or len(query) > 1200
            or not isinstance(identity, dict)
            or set(identity) != {"caller", "purpose", "persona_id", "session_id"}
            or any(not isinstance(v, str) for v in identity.values())
        ):
            return self.response(error="请输入有效查询并选择已保存的授权")
        if payload.get("revision") != self.policy_data()["revision"]:
            return self.response(error="授权已被修改，请刷新后重试")
        try:
            return self.response(await self.plugin.search_context(query, **identity))
        except Exception:
            return self.response(error="检索暂不可用，请稍后重试")
