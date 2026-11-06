# one_id 聚合关系在读取时解析，不改写历史正文或向量。
"""统一身份投影、影响预览与使用点校验；只读历史，不改正文/向量。"""

from __future__ import annotations

import json
from dataclasses import replace

from ..identity import (
    audience_allows, legacy_shared_scope_allows, shared_record_identity_allows,
    source_account_for, infer_audience, sensitivity_level,
)
from ..identity_access import IdentityAccess, identity_access, current_identity_access
from ..identity_keys import identity_alias
from ..memory_scope import adapter_sql, adapter_visible
from ..person_query import metadata_dict, record_account
from ...storage.oneid_store import IdentityConflict, IdentityContextChanged


def _account(value) -> str:
    platform, sep, account = str(value or "").partition(":")
    return source_account_for(platform, account) if sep else ""


def provenance(metadata: dict, bindings: dict) -> dict:
    """当前账号关系决定归属；旧 one_id/person_id 快照不扩大读取权限。"""
    source = record_account(metadata)
    raw_accounts = metadata.get("subject_accounts")
    subjects = {_account(item) for item in raw_accounts} if isinstance(raw_accounts, list) else set()
    subjects.discard("")
    if source:
        subjects.add(source)
    owner = _account(metadata.get("owner_account"))
    resolve = lambda account: bindings.get(account, "person:" + account)
    mixed = bool(metadata.get("identity_history_mixed")) or len(subjects) > 1
    uncertain = bool(metadata.get("identity_provenance_unknown")) or not subjects
    return {
        "source_account": source,
        "subject_accounts": sorted(subjects),
        "subject_one_ids": sorted({resolve(account) for account in subjects}),
        "owner_account": owner,
        "owner_one_id": resolve(owner) if owner else "",
        "provenance_status": "mixed" if mixed else "unknown" if uncertain else "account",
        "owner_provenance_status": "account" if owner else "unknown",
    }


class MemoryEngineIdentityMixin:
    async def _identity_rows(self, *, session_id=None, persona_id=None) -> tuple[int, dict, list]:
        store = getattr(self, "identity_store", None)
        if store is None:
            raise RuntimeError("身份服务未初始化")
        revision = await store.get_revision()
        db = self.db_connection
        cursor = await db.execute(
            "SELECT canonical_account,person_id FROM person_binding WHERE status='active'"
        )
        bindings = {row[0]: row[1] for row in await cursor.fetchall()}
        ownership, params = adapter_sql("metadata")
        conditions = [ownership, "json_valid(metadata)"]
        for key, value in (("session_id", session_id), ("persona_id", persona_id)):
            if value is not None:
                conditions.append(f"json_extract(metadata,'$.{key}')=?")
                params.append(value)
        cursor = await db.execute(
            "SELECT id,metadata FROM documents WHERE " + " AND ".join(conditions) +
            " ORDER BY COALESCE(CAST(json_extract(metadata,'$.create_time') AS REAL),0) DESC,id DESC",
            params,
        )
        rows = [(int(row[0]), metadata_dict(row[1])) for row in await cursor.fetchall()]
        if revision != await store.get_revision():
            raise IdentityContextChanged("读取期间身份关联变化，请重试")
        return revision, bindings, rows

    async def project_identity(
        self, *, one_id: str = "", person_id: str = "", owner_one_id: str = "",
        session_id=None, persona_id=None, include_records: bool = False,
        limit: int = 100, offset: int = 0,
    ) -> dict:
        """人/机器人→账号→场景→记忆，统计归属与不确定历史分别列出。"""
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError("分页参数无效")
        target = identity_alias(one_id=one_id, person_id=person_id) if one_id or person_id else ""
        if not target and not owner_one_id:
            raise ValueError("需要主体或持有者 one_id")
        revision, bindings, rows = await self._identity_rows(
            session_id=session_id, persona_id=persona_id,
        )
        store = self.identity_store
        accounts = [
            {**item, "one_id": item["person_id"]}
            for item in await store.list_bindings(person_id=target or owner_one_id)
            if item["status"] == "active"
        ]
        records, uncertain, scenes, counts = [], [], {}, {}
        for memory_id, metadata in rows:
            if str(metadata.get("status") or "active") != "active":
                continue
            derived = provenance(metadata, bindings)
            subject_matches = target in derived["subject_one_ids"]
            owner_matches = owner_one_id == derived["owner_one_id"] if owner_one_id else True
            if target and not subject_matches or not owner_matches:
                if derived["provenance_status"] == "unknown":
                    uncertain.append(memory_id)
                continue
            if derived["provenance_status"] != "account" and target:
                uncertain.append(memory_id)
                continue
            scene = str(metadata.get("source_session_id") or metadata.get("session_id") or "")
            # 同一场景名的不同适配器/人格不能合成一个节点。
            scene_scope = (str(metadata.get("adapter_id") or ""),
                           str(metadata.get("persona_id") or ""), scene)
            scenes[scene_scope] = scenes.get(scene_scope, 0) + 1
            account = derived["source_account"]
            counts[account] = counts.get(account, 0) + 1
            records.append({
                "memory_id": memory_id, "scene": scene,
                # 来源场景与原生存储会话可能不同；图谱过滤必须使用真实存储会话。
                "storage_session_id": str(metadata.get("session_id") or ""),
                "scene_key": json.dumps(scene_scope, ensure_ascii=False),
                "adapter_id": scene_scope[0], "persona_id": scene_scope[1], **derived,
            })
        if revision != await store.get_revision():
            raise IdentityContextChanged("投影已过期，请刷新")
        return {
            "one_id": target, "person_id": target, "owner_one_id": owner_one_id,
            "identity_revision": revision, "memory_ids": [row["memory_id"] for row in records],
            "attributed_memory_count": len(records),
            "recallable_memory_count": None,
            "recall_count_context": "管理投影未提供实际事件，归属数量不代表当前请求可召回数量",
            "uncertain_memory_count": len(set(uncertain)),
            "uncertain_count_scope": "当前适配器/会话/人格内待复核历史，不表示属于此主体",
            "accounts": [{**row, "memory_count": counts.get(_account(
                row["platform"] + ":" + row["account_id"]), 0)} for row in accounts],
            "scenes": [
                {"scene_key": json.dumps(key, ensure_ascii=False), "adapter_id": key[0],
                 "persona_id": key[1], "session_id": key[2], "memory_count": count}
                for key, count in scenes.items()
            ],
            "records": records[offset:offset+limit] if include_records else [],
            "has_more": offset + limit < len(records),
        }

    async def identity_memory_ids(self, one_id: str, session_id=None, persona_id=None) -> list[int]:
        projection = await self.project_identity(
            one_id=one_id, session_id=session_id, persona_id=persona_id,
        )
        return projection["memory_ids"]

    async def identity_read_access(self, **kwargs) -> IdentityAccess:
        projection = await self.project_identity(**kwargs)
        return IdentityAccess(
            frozenset(projection["memory_ids"]), projection["identity_revision"],
            self.identity_store, projection["one_id"], projection["owner_one_id"],
        )

    async def preview_identity_change(
        self, *, platform: str, account_id: str, one_id: str = "", person_id: str = "",
        binding_id: str = "",
    ) -> dict:
        snapshot = await self.identity_store.identity_snapshot(platform, account_id)
        binding = snapshot["binding"]
        if binding_id and (not binding or binding["binding_id"] != binding_id):
            raise IdentityConflict("账号关联已变化")
        target = identity_alias(one_id=one_id, person_id=person_id) if one_id or person_id else snapshot["one_id"]
        revision, bindings, rows = await self._identity_rows()
        if revision != snapshot["identity_revision"]:
            raise IdentityContextChanged("预览期间关联变化")
        key = snapshot["account_key"]
        affected, uncertain, scenes = [], [], set()
        for memory_id, metadata in rows:
            if str(metadata.get("status") or "active") != "active":
                continue
            derived = provenance(metadata, bindings)
            if derived["provenance_status"] == "unknown":
                uncertain.append(memory_id)
            elif key in derived["subject_accounts"] or key == derived["owner_account"]:
                if derived["provenance_status"] == "mixed":
                    uncertain.append(memory_id)
                else:
                    affected.append(memory_id)
                scenes.add(str(metadata.get("source_session_id") or metadata.get("session_id") or ""))
        if not await self.identity_store.is_context_current(snapshot):
            raise IdentityContextChanged("预览已过期")
        return {
            **snapshot, "target_one_id": target, "affected_memory_ids": affected,
            "affected_memory_count": len(affected), "uncertain_memory_count": len(uncertain),
            "uncertain_count_scope": "当前可见范围待复核历史，不自动归给目标主体",
            "scenes": sorted(scenes), "memories_preserved": True,
            "legacy_direct_write": True, "new_application_requires_review": True,
        }

    async def prepare_identity_recall(self, identity: dict, persona_id) -> IdentityAccess | None:
        store = getattr(self, "identity_store", None)
        if store is None:
            return None  # 老宿主维持账号原生路径。
        if not await store.is_context_current(identity):
            raise IdentityContextChanged("身份上下文已变化，请重新解析事件")
        revision, bindings, rows = await self._identity_rows(persona_id=persona_id)
        if revision != identity["identity_revision"]:
            raise IdentityContextChanged("身份上下文已变化")
        original = {identity.get("scope"), *(identity.get("person_scopes") or []),
                    *(identity.get("legacy_scopes") or [])}
        own = _account(identity.get("source_account"))
        allowed_accounts = {own}
        allowed_accounts.update(_account(item.get("platform", "") + ":" + item.get("account_id", ""))
                                for item in identity.get("bound_accounts") or [])
        ids = set()
        for memory_id, metadata in rows:
            scope = metadata.get("session_id")
            if str(metadata.get("status") or "active") != "active" or not audience_allows(metadata, identity):
                continue
            derived = provenance(metadata, bindings)
            if scope not in original and not legacy_shared_scope_allows(metadata, identity):
                continue
            is_extension = scope not in {identity.get("scope"), *(identity.get("legacy_scopes") or [])}
            # 纯群低敏共有记录沿用既有共享权限，不把它算作个人归属。
            audience = infer_audience(metadata)
            ordinary_shared = (
                identity.get("scene_kind") == "group"
                and derived["provenance_status"] == "unknown"
                and sensitivity_level(metadata) < 2
                and (audience == "public" or audience.startswith("origin_group:"))
                and shared_record_identity_allows(metadata, identity)
            )
            owner = identity.get("owner_one_id") or ""
            if owner and derived["owner_one_id"] and derived["owner_one_id"] != owner:
                continue
            if is_extension and owner and not derived["owner_one_id"] and not ordinary_shared:
                continue  # 持有者不明的历史维持原场景，不能猜成当前机器人。
            if is_extension and not ordinary_shared and (
                derived["provenance_status"] != "account" or
                derived["source_account"] not in allowed_accounts or
                identity.get("one_id", identity.get("person_id")) not in derived["subject_one_ids"]
            ):
                continue
            if identity.get("scene_kind") == "group" and not shared_record_identity_allows(metadata, identity):
                continue
            if adapter_visible(metadata):
                ids.add(memory_id)
        if revision != await store.get_revision():
            raise IdentityContextChanged("检索准备期间关联变化")
        return IdentityAccess(frozenset(ids), revision, store,
                              identity.get("one_id") or identity.get("person_id") or "",
                              identity.get("owner_one_id") or "")

    async def validate_identity_results(self, results: list) -> list:
        """调用方在注入/发送前再次调用；修订号变化立即拒绝旧结果。"""
        store = getattr(self, "identity_store", None)
        if store is None:
            return results
        revision = await store.get_revision()
        checked = []
        for result in results:
            metadata = result.metadata if hasattr(result, "metadata") else result.get("metadata", {})
            if "_identity_revision" in metadata and metadata["_identity_revision"] != revision:
                raise IdentityContextChanged("结果产生后身份关联变化，请重新检索")
            checked.append(result)
        return checked

    async def enrich_identity_metadata(self, metadata: dict) -> dict:
        store = getattr(self, "identity_store", None)
        result = dict(metadata)
        if result.get("one_id") or result.get("person_id"):
            value = identity_alias(one_id=result.get("one_id"), person_id=result.get("person_id"))
            result.update(one_id=value, person_id=value)
        if store is None:
            return result
        revision = await store.get_revision()
        expected = result.get("identity_revision")
        if expected is not None and expected != revision:
            raise IdentityContextChanged("总结期间关联变化，请用当前身份重新生成")
        subjects = result.get("subject_accounts")
        if not isinstance(subjects, list):
            source = record_account(result)
            subjects = [source] if source else []
        subjects = sorted({_account(item) for item in subjects} - {""})
        owner = _account(result.get("owner_account"))
        subject_ids = []
        for account in subjects:
            subject_ids.append(await store.resolve_one_id(*account.split(":", 1)))
        owner_id = await store.resolve_one_id(*owner.split(":", 1)) if owner else ""
        if revision != await store.get_revision():
            raise IdentityContextChanged("写入准备期间关联变化")
        result.update(subject_accounts=subjects, subject_one_ids=sorted(set(subject_ids)),
                      owner_account=owner, owner_one_id=owner_id, identity_revision=revision)
        return result
