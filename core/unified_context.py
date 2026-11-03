"""Scoped knowledge + experience retrieval for trusted in-process plugins.

Policy is administrator-owned. A caller name is attribution, not authentication:
this API must never be exposed as an unauthenticated HTTP or LLM tool endpoint.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from astrbot.api import logger


@dataclass(frozen=True)
class Binding:
    caller: str
    purpose: str
    persona_id: str
    kb_ids: tuple[str, ...]
    session_id: str = ""


def parse_bindings(value: str) -> tuple[Binding, ...]:
    """Reject ambiguous/broad grants; errors intentionally omit policy contents."""
    try:
        if not isinstance(value, str) or len(value) > 20000:
            raise ValueError
        rows = json.loads(value)
        if not isinstance(rows, list) or len(rows) > 32:
            raise ValueError
        bindings = []
        keys = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - {
                "caller", "purpose", "persona_id", "kb_ids", "session_id"
            }:
                raise ValueError
            for key in ("caller", "purpose", "persona_id"):
                text = row.get(key)
                if not isinstance(text, str) or not text.strip() or text != text.strip():
                    raise ValueError
                if len(text) > 200 or "*" in text or any(ord(c) < 32 for c in text):
                    raise ValueError
            purpose = row["purpose"]
            session = row.get("session_id", "")
            if not isinstance(session, str) or len(session) > 256:
                raise ValueError
            if purpose == "public_comment":
                # 旧 BiliBot 记录与主人私聊混存；公开用途一律不读取该记忆库。
                if session:
                    raise ValueError
            elif purpose == "session":
                if (
                    not session.strip() or session != session.strip()
                    or "*" in session or session.startswith("livingmemory:")
                    or any(ord(c) < 32 for c in session)
                ):
                    raise ValueError
            else:
                raise ValueError
            kb_ids = row.get("kb_ids", [])
            if not isinstance(kb_ids, list) or len(kb_ids) > 3:
                raise ValueError
            if any(not isinstance(k, str) or str(UUID(k)) != k for k in kb_ids):
                raise ValueError
            if len(kb_ids) != len(set(kb_ids)):
                raise ValueError
            binding = Binding(
                row["caller"], purpose, row["persona_id"], tuple(kb_ids), session
            )
            key = (binding.caller, purpose, binding.persona_id, session)
            if key in keys:
                raise ValueError
            keys.add(key)
            bindings.append(binding)
        return tuple(bindings)
    except (ValueError, TypeError, AttributeError, KeyError):
        raise ValueError("统一检索授权格式无效；请检查用途、人格、会话和资料库 ID") from None


def _metadata(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


class UnifiedContextService:
    """No content cache, cross-source score blending, writes, or automatic retries."""

    timeout_seconds = 10.0
    source_limit = 4

    def __init__(self, plugin):
        self.plugin = plugin
        self._active = 0
        self._callers: dict[str, int] = {}

    def policy(self):
        cfg = self.plugin.config_manager.get("unified_context", {})
        if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
            return False, ()
        return True, parse_bindings(cfg.get("bindings_json", "[]"))

    async def search(
        self, query: str, *, caller: str, purpose: str,
        persona_id: str, session_id: str = "",
    ) -> dict:
        response = {
            "request_id": uuid4().hex, "knowledge": [], "experiences": [],
            "status": "denied", "sources": {}, "truncated": False,
        }
        if (
            not isinstance(query, str) or not query.strip()
            or any(not isinstance(v, str) for v in (caller, purpose, persona_id, session_id))
        ):
            response["status"] = "invalid_request"
            return response
        try:
            enabled, bindings = self.policy()
        except ValueError:
            response["status"] = "invalid_policy"
            return response
        if not enabled:
            response["status"] = "disabled"
            return response
        binding = next((b for b in bindings if (
            b.caller, b.purpose, b.persona_id, b.session_id
        ) == (caller, purpose, persona_id, session_id)), None)
        if binding is None:
            return response
        if self._active >= 4 or self._callers.get(caller, 0) >= 2:
            response["status"] = "busy"
            return response
        query = query.strip()[:1200]
        self._active += 1
        self._callers[caller] = self._callers.get(caller, 0) + 1
        tasks = {}
        try:
            if binding.kb_ids:
                tasks["knowledge"] = asyncio.create_task(self._knowledge(query, binding))
            else:
                response["sources"]["knowledge"] = "not_authorized"
            if binding.purpose == "session":
                tasks["experiences"] = asyncio.create_task(self._experiences(query, binding))
            else:
                response["sources"]["experiences"] = "public_memory_blocked"
            if tasks:
                done, _ = await asyncio.wait(tasks.values(), timeout=self.timeout_seconds)
                for source, task in tasks.items():
                    if task not in done:
                        response["sources"][source] = "timeout"
                    else:
                        try:
                            response[source], response["sources"][source] = task.result()
                        except (Exception, asyncio.CancelledError):
                            response["sources"][source] = "unavailable"
            # 管理员在查询途中撤销授权时，连已经完成的候选也必须丢弃。
            try:
                current_enabled, current_bindings = self.policy()
            except ValueError:
                current_enabled, current_bindings = False, ()
            if not current_enabled or binding not in current_bindings:
                response.update(status="denied", knowledge=[], experiences=[])
                return response
            budget = 6000
            for source in ("knowledge", "experiences"):
                bounded = []
                for item in response[source]:
                    text = item["content"][:min(1600, budget)]
                    if text:
                        bounded.append({**item, "content": text})
                        budget -= len(text)
                    response["truncated"] |= len(text) != len(item["content"])
                response[source] = bounded
            response["status"] = (
                "partial" if any(s in {"unavailable", "timeout", "filtered"}
                                 for s in response["sources"].values()) else "ok"
            )
            return response
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
            self._active -= 1
            self._callers[caller] -= 1
            if not self._callers[caller]:
                del self._callers[caller]
            # 不记录查询正文、会话 ID、文档正文或异常字符串。
            logger.info(
                "[UnifiedContext] request=%s status=%s knowledge=%d experiences=%d",
                response["request_id"], response["status"],
                len(response["knowledge"]), len(response["experiences"]),
            )

    async def _knowledge(self, query, binding):
        manager = getattr(self.plugin.context, "kb_manager", None)
        if manager is None:
            return [], "unavailable"
        helpers = {}
        for kb_id in binding.kb_ids:
            helper = await manager.get_kb(kb_id)
            if helper is None or getattr(helper, "init_error", None):
                continue
            # 原生接口先按名称匹配：防止另一资料库使用授权 UUID 作名称。
            resolved = await manager.get_kb_by_name(kb_id)
            if resolved is not helper or str(helper.kb.kb_id) != kb_id:
                continue
            helpers[kb_id] = helper
        if not helpers:
            return [], "unavailable"
        result = await manager.retrieve(
            query, kb_names=list(helpers),
            top_k_fusion=12, top_m_final=self.source_limit,
        )
        items, seen = [], set()
        filtered = len(helpers) != len(binding.kb_ids)
        for candidate in (result or {}).get("results", [])[:12]:
            if not isinstance(candidate, dict):
                filtered = True
                continue
            kb_id, doc_id, chunk_id = (candidate.get(k) for k in ("kb_id", "doc_id", "chunk_id"))
            helper = helpers.get(kb_id)
            if helper is None or not doc_id or not chunk_id:
                filtered = True
                continue
            # 用当前原生记录复核所属库、文档、块及正文，拒绝已删除或旧版本候选。
            doc = await helper.get_document(doc_id)
            chunk = await helper.vec_db.document_storage.get_document_by_doc_id(chunk_id)
            md = _metadata((chunk or {}).get("metadata"))
            if (
                doc is None or str(doc.kb_id) != kb_id or str(doc.doc_id) != doc_id
                or not chunk or str(chunk.get("doc_id")) != chunk_id
                or md.get("kb_id") != kb_id or md.get("kb_doc_id") != doc_id
                or chunk.get("text") != candidate.get("content")
                or await manager.get_kb(kb_id) is not helper
            ):
                filtered = True
                continue
            key = (kb_id, doc_id, chunk_id)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "source": "knowledge", "kb_id": kb_id, "doc_id": doc_id,
                "chunk_id": chunk_id, "kb_name": str(helper.kb.kb_name)[:100],
                "doc_name": str(doc.doc_name)[:255], "content": chunk["text"],
            })
            if len(items) == self.source_limit:
                break
        return items, "filtered" if filtered else "ok"

    async def _experiences(self, query, binding):
        engine = getattr(self.plugin.initializer, "memory_engine", None)
        if engine is None:
            return [], "unavailable"
        items = await engine.search_memories_scoped(
            query, k=self.source_limit,
            session_id=binding.session_id, persona_id=binding.persona_id,
        )
        return items, "ok"
