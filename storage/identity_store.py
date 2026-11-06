# 旧绑定写入口也更新统一身份修订号，不能绕过撤销传播。
"""人物绑定与绑定审计的持久化层（阶段 1：身份与受众底座）。

设计口径（与方案文档一致）：
- 记忆行只存来源账号 ``source_account``（如 ``qq:123456``），不把 person_id
  烤进记忆；读取时用当前 active 绑定解析成 person_id。
- 绑定修正采用“关闭旧版本行、插入新版本行”，不物理删除，可回滚。
- 审计表只追加：记录 before/after、渠道、操作人、依据、文件哈希、理由、时间。
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import aiosqlite

from astrbot.api import logger
from ..core.identity_keys import canonical_account, identity_alias, normalize_platform
from .oneid_store import OneIdStoreMixin, IdentityConflict

BINDING_STATUS_ACTIVE = "active"
BINDING_STATUS_REVOKED = "revoked"
BINDING_STATUS_SUPERSEDED = "superseded"
BINDING_STATUS_PENDING = "pending"
BINDING_STATUS_APPROVED = "approved"
BINDING_STATUS_REJECTED = "rejected"

_BINDING_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS person_binding (
        binding_id TEXT PRIMARY KEY,
        person_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        account_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        version INTEGER NOT NULL DEFAULT 1,
        source TEXT NOT NULL DEFAULT 'manual',
        evidence_type TEXT NOT NULL DEFAULT '',
        evidence_ref TEXT NOT NULL DEFAULT '',
        operator TEXT NOT NULL DEFAULT '',
        reviewer TEXT NOT NULL DEFAULT '',
        valid_from REAL NOT NULL,
        valid_to REAL,
        note TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_person_binding_account
    ON person_binding(platform, account_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_person_binding_person
    ON person_binding(person_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS binding_audit (
        audit_id TEXT PRIMARY KEY,
        binding_id TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 0,
        action TEXT NOT NULL,
        before_json TEXT NOT NULL DEFAULT '{}',
        after_json TEXT NOT NULL DEFAULT '{}',
        channel TEXT NOT NULL DEFAULT 'manual',
        operator_id TEXT NOT NULL DEFAULT '',
        operator_name TEXT NOT NULL DEFAULT '',
        reviewer_id TEXT NOT NULL DEFAULT '',
        evidence_type TEXT NOT NULL DEFAULT '',
        evidence_ref TEXT NOT NULL DEFAULT '',
        file_hash TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        request_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_binding_audit_binding
    ON binding_audit(binding_id, created_at)
    """,
    # 阶段 3 绑定管理：自动识别结果先进待审队列，人工逐条通过后才落正式绑定。
    """
    CREATE TABLE IF NOT EXISTS binding_candidate (
        candidate_id TEXT PRIMARY KEY,
        person_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        account_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'auto',
        evidence_type TEXT NOT NULL DEFAULT '',
        evidence_ref TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        matched_rule TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        submitted_by TEXT NOT NULL DEFAULT '',
        reviewed_by TEXT NOT NULL DEFAULT '',
        review_reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        reviewed_at REAL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_binding_candidate_status
    ON binding_candidate(status, platform, account_id)
    """,
    # 绑定管理的小配置（auto_approve 等）与绑定表放在同一库，审计可同源。
    """
    CREATE TABLE IF NOT EXISTS binding_setting (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        updated_by TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL
    )
    """,
)


def _json(value: Any) -> str:
    return json.dumps(value if isinstance(value, (dict, list)) else {}, ensure_ascii=False)


class IdentityStore(OneIdStoreMixin):
    """person_binding / binding_audit 的读写入口（与 livingmemory.db 共用文件）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    @staticmethod
    def _now() -> float:
        return time.time()

    @asynccontextmanager
    async def _transaction(self, db: aiosqlite.Connection | None = None):
        """复用批量操作的事务；独立写入先锁定快照，再读改写并提交审计。"""
        if db is not None:
            yield db
            return
        provider = getattr(self, "_transaction_provider", None)
        if provider is not None:
            async with provider() as connection:
                yield connection
            await self._identity_committed()
            return
        async with aiosqlite.connect(self.db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        await self._identity_committed()

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode = WAL")
            for statement in _BINDING_SCHEMA:
                await db.execute(statement)
            await db.commit()
            await self._initialize_oneid(db)
        logger.info("[IdentityStore] person_binding / binding_audit 已就绪")

    # ---------------------------------------------------------------- 读取
    async def find_active(self, platform: str, account_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await self._active_for_key(db, canonical_account(platform, account_id))

    async def resolve_person(self, platform: str, account_id: str) -> str:
        """当前 active 绑定 → person_id；未绑定时按来源账号稳定派生，不做猜测合并。"""
        if not platform or not account_id:
            return ""
        return await self.resolve_one_id(platform, account_id)

    async def accounts_for(self, person_id: str) -> list[dict[str, Any]]:
        if not person_id:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM person_binding
                WHERE person_id = ? AND status = 'active'
                ORDER BY platform, account_id
                """,
                (str(person_id),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def list_bindings(
        self,
        *,
        person_id: str = "",
        platform: str = "",
        account_id: str = "",
        status: str = "",
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        """绑定列表：支持按人物/平台/账号/状态筛选（阶段 3 页面用）。"""
        query = "SELECT * FROM person_binding"
        clauses, params = [], []
        if person_id:
            clauses.append("person_id = ?")
            params.append(str(person_id))
        if platform:
            clauses.append("substr(canonical_account,1,instr(canonical_account,':')-1) = ?")
            params.append(normalize_platform(platform))
        if account_id:
            clauses.append("account_id = ?")
            params.append(str(account_id))
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        elif not include_history:
            clauses.append("status = 'active'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY platform, account_id, version DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def list_audit(
        self,
        *,
        binding_id: str = "",
        action: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """审计只读列表：操作人、依据、审核人、文件哈希都在这里回显。"""
        query = "SELECT * FROM binding_audit"
        clauses, params = [], []
        if binding_id:
            clauses.append("binding_id = ?")
            params.append(str(binding_id))
        if action:
            clauses.append("action = ?")
            params.append(str(action))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(500, int(limit or 100))), max(0, int(offset or 0))])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = []
            for row in await cursor.fetchall():
                item = dict(row)
                # 前端要直接看变更前后，这里解析成对象；坏数据保持原始字符串。
                for field in ("before_json", "after_json"):
                    try:
                        item[field] = json.loads(item.get(field) or "{}")
                    except (json.JSONDecodeError, TypeError):
                        item[field] = {}
                rows.append(item)
            return rows

    # ---------------------------------------------------------------- 审计
    @staticmethod
    async def _append_audit(
        db: aiosqlite.Connection,
        *,
        action: str,
        binding_id: str = "",
        version: int = 0,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        channel: str = "manual",
        operator_id: str = "",
        operator_name: str = "",
        reviewer_id: str = "",
        evidence_type: str = "",
        evidence_ref: str = "",
        file_hash: str = "",
        reason: str = "",
        request_id: str = "",
    ) -> None:
        # 审计只追加：不提供 update/delete 入口。
        await db.execute(
            """
            INSERT INTO binding_audit(
                audit_id, binding_id, version, action, before_json, after_json,
                channel, operator_id, operator_name, reviewer_id,
                evidence_type, evidence_ref, file_hash, reason, request_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                binding_id,
                int(version or 0),
                action,
                _json(before or {}),
                _json(after or {}),
                channel,
                operator_id,
                operator_name,
                reviewer_id,
                evidence_type,
                evidence_ref,
                file_hash,
                reason,
                request_id,
                time.time(),
            ),
        )

    # ---------------------------------------------------------------- 写入
    async def upsert_binding(
        self,
        *,
        person_id: str = "",
        one_id: str = "",
        platform: str,
        account_id: str,
        display_name: str = "",
        source: str = "manual",
        evidence_type: str = "",
        evidence_ref: str = "",
        operator: str = "",
        reviewer: str = "",
        note: str = "",
        channel: str = "manual",
        reason: str = "",
        file_hash: str = "",
        request_id: str = "",
        expected_binding: dict[str, Any] | None = None,
        _db: aiosqlite.Connection | None = None,
    ) -> dict[str, Any]:
        """按 (platform, account_id) 幂等 upsert；人物变化时版本化并保留旧行。"""
        person_id = identity_alias(one_id=one_id, person_id=person_id)
        key = canonical_account(platform, account_id)
        platform, account_id = key.split(":", 1)

        now = self._now()
        async with self._transaction(_db) as db:
            active = await self._active_for_key(db, key)
            if expected_binding is not None and (active or {}) != expected_binding:
                raise ValueError("绑定已变化，请刷新后重新编辑")
            await db.execute(
                "INSERT OR IGNORE INTO oneid_entity VALUES(?,'unknown','',?,?)",
                (person_id, now, now),
            )

            if active is None:
                cursor = await db.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) AS max_version
                    FROM person_binding WHERE canonical_account = ?
                    """,
                    (key,),
                )
                max_version = int((await cursor.fetchone())["max_version"] or 0)
                binding = {
                    "binding_id": uuid4().hex,
                    "person_id": person_id,
                    "platform": platform,
                    "account_id": account_id,
                    "display_name": display_name,
                    "status": BINDING_STATUS_ACTIVE,
                    "version": max_version + 1,
                    "source": source,
                    "evidence_type": evidence_type,
                    "evidence_ref": evidence_ref,
                    "operator": operator,
                    "reviewer": reviewer,
                    "valid_from": now,
                    "valid_to": None,
                    "note": note,
                    "created_at": now,
                    "updated_at": now,
                    "canonical_account": key,
                }
                await db.execute(
                    """
                    INSERT INTO person_binding(
                        binding_id, person_id, platform, account_id, display_name,
                        status, version, source, evidence_type, evidence_ref,
                        operator, reviewer, valid_from, valid_to, note,
                        created_at, updated_at, canonical_account
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(binding.values()),
                )
                await self._identity_changed(db, key)
                await self._append_audit(
                    db, action="create", binding_id=binding["binding_id"],
                    version=binding["version"], before={}, after=binding,
                    channel=channel, operator_id=operator, operator_name=operator,
                    reviewer_id=reviewer, evidence_type=evidence_type,
                    evidence_ref=evidence_ref, file_hash=file_hash,
                    reason=reason, request_id=request_id,
                )
                return binding

            if str(active["person_id"]) == person_id:
                # 同人同账号：只更新显示名/证据/备注，不改版本语义。
                changed = any(
                    (active.get(field) or "") != (value or "")
                    for field, value in (
                        ("display_name", display_name),
                        ("evidence_type", evidence_type),
                        ("evidence_ref", evidence_ref),
                        ("note", note),
                    )
                )
                if not changed:
                    return active
                updated = {**active}
                updated.update({
                    "display_name": display_name,
                    "evidence_type": evidence_type,
                    "evidence_ref": evidence_ref,
                    "note": note,
                    "source": source,
                    "operator": operator,
                    "reviewer": reviewer,
                    "updated_at": now,
                })
                await db.execute(
                    """
                    UPDATE person_binding
                    SET display_name = ?, evidence_type = ?, evidence_ref = ?,
                        note = ?, source = ?, operator = ?, reviewer = ?, updated_at = ?
                    WHERE binding_id = ?
                    """,
                    (
                        display_name, evidence_type, evidence_ref, note,
                        source, operator, reviewer, now, active["binding_id"],
                    ),
                )
                await self._identity_changed(db, key)
                await self._append_audit(
                    db, action="update", binding_id=active["binding_id"],
                    version=int(active["version"]), before=active, after=updated,
                    channel=channel, operator_id=operator, operator_name=operator,
                    reviewer_id=reviewer, evidence_type=evidence_type,
                    evidence_ref=evidence_ref, file_hash=file_hash,
                    reason=reason, request_id=request_id,
                )
                return updated

            # 人物归属变化：旧行软失效，插入新版本；历史记忆一行不动。
            cursor = await db.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS max_version
                FROM person_binding WHERE canonical_account = ?
                """,
                (key,),
            )
            max_version = int((await cursor.fetchone())["max_version"] or 0)
            await db.execute(
                """
                UPDATE person_binding
                SET status = 'revoked', valid_to = ?, updated_at = ?
                WHERE binding_id = ?
                """,
                (now, now, active["binding_id"]),
            )
            new_binding = {
                **active,
                "binding_id": uuid4().hex,
                "person_id": person_id,
                "display_name": display_name,
                "status": BINDING_STATUS_ACTIVE,
                "version": max_version + 1,
                "source": source,
                "evidence_type": evidence_type,
                "evidence_ref": evidence_ref,
                "operator": operator,
                "reviewer": reviewer,
                "valid_from": now,
                "valid_to": None,
                "note": note,
                "created_at": now,
                "updated_at": now,
            }
            await db.execute(
                """
                INSERT INTO person_binding(
                    binding_id, person_id, platform, account_id, display_name,
                    status, version, source, evidence_type, evidence_ref,
                    operator, reviewer, valid_from, valid_to, note,
                    created_at, updated_at, canonical_account
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._binding_values(new_binding),
            )
            await self._identity_changed(db, key)
            await self._append_audit(
                db, action="merge", binding_id=new_binding["binding_id"],
                version=new_binding["version"], before=active, after=new_binding,
                channel=channel, operator_id=operator, operator_name=operator,
                reviewer_id=reviewer, evidence_type=evidence_type,
                evidence_ref=evidence_ref, file_hash=file_hash,
                reason=reason, request_id=request_id,
            )
            return new_binding

    async def revoke_binding(
        self,
        binding_id: str,
        *,
        operator: str = "",
        reviewer: str = "",
        reason: str = "",
        channel: str = "manual",
        request_id: str = "",
        file_hash: str = "",
        _db: aiosqlite.Connection | None = None,
    ) -> bool:
        """软失效：置 revoked 并写 valid_to，不删除行。"""
        now = self._now()
        async with self._transaction(_db) as db:
            cursor = await db.execute(
                "SELECT * FROM person_binding WHERE binding_id = ?",
                (str(binding_id),),
            )
            row = await cursor.fetchone()
            if not row or row["status"] != BINDING_STATUS_ACTIVE:
                return False
            before = dict(row)
            after = {**before, "status": BINDING_STATUS_REVOKED, "valid_to": now, "updated_at": now}
            await db.execute(
                """
                UPDATE person_binding SET status = 'revoked', valid_to = ?, updated_at = ?
                WHERE binding_id = ? AND status = 'active'
                """,
                (now, now, str(binding_id)),
            )
            await self._identity_changed(db, before["canonical_account"], invalidation="cancelled")
            # 旧待审项同样终结；迟到审核不能恢复撤销的账号关系。
            await self._append_audit(
                db, action="revoke", binding_id=str(binding_id),
                version=int(before["version"]), before=before, after=after,
                channel=channel, operator_id=operator, operator_name=operator,
                reviewer_id=reviewer, reason=reason, request_id=request_id,
                file_hash=file_hash,
            )
            return True

    async def rollback_binding(
        self,
        binding_id: str,
        to_version: int,
        *,
        operator: str = "",
        reviewer: str = "",
        reason: str = "",
        channel: str = "manual",
        request_id: str = "",
    ) -> dict[str, Any] | None:
        """回滚：读取目标历史版本，新建一版 active，审计记 rollback。"""
        async with self._transaction() as db:
            cursor = await db.execute(
                "SELECT * FROM person_binding WHERE binding_id = ?",
                (str(binding_id),),
            )
            current = await cursor.fetchone()
            if not current:
                return None
            current = dict(current)
            # 历史行不能充当当前版本；撤销后必须走显式的新申请。
            active_current = await self._active_for_key(db, current["canonical_account"])
            if not active_current or active_current["binding_id"] != str(binding_id):
                raise IdentityConflict("历史或已解绑关系不能直接回滚，请创建新申请")
            cursor = await db.execute(
                """
                SELECT * FROM person_binding
                WHERE canonical_account = ? AND version = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (current["canonical_account"], int(to_version)),
            )
            target = await cursor.fetchone()
            if not target:
                return None
            target = dict(target)
            cursor = await db.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS max_version
                FROM person_binding WHERE platform = ? AND account_id = ?
                """,
                (current["platform"], current["account_id"]),
            )
            max_version = int((await cursor.fetchone())["max_version"] or 0)
            cursor = await db.execute(
                "SELECT * FROM person_binding WHERE platform = ? AND account_id = ? AND status = 'active'",
                (current["platform"], current["account_id"]),
            )
            active = await cursor.fetchone()
            now = self._now()
            # 关闭当前 active 行，再插入目标版本的副本（版本号继续递增）。
            await db.execute(
                """
                UPDATE person_binding
                SET status = 'revoked', valid_to = ?, updated_at = ?
                WHERE platform = ? AND account_id = ? AND status = 'active'
                """,
                (now, now, current["platform"], current["account_id"]),
            )
            restored = {
                **target,
                "binding_id": uuid4().hex,
                "status": BINDING_STATUS_ACTIVE,
                "version": max_version + 1,
                "valid_from": now,
                "valid_to": None,
                "source": "manual",
                "operator": operator,
                "reviewer": reviewer,
                "created_at": now,
                "updated_at": now,
            }
            await db.execute(
                """
                INSERT INTO person_binding(
                    binding_id, person_id, platform, account_id, display_name,
                    status, version, source, evidence_type, evidence_ref,
                    operator, reviewer, valid_from, valid_to, note,
                    created_at, updated_at, canonical_account
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._binding_values(restored),
            )
            await self._identity_changed(db, current["canonical_account"])
            await self._append_audit(
                db, action="rollback", binding_id=restored["binding_id"],
                version=restored["version"], before=dict(active) if active else {}, after=restored,
                channel=channel, operator_id=operator, operator_name=operator,
                reviewer_id=reviewer, reason=reason, request_id=request_id,
            )
            return restored

    @staticmethod
    def _binding_values(row: dict) -> tuple:
        """原有 17 列顺序保持，最后追加规范账号辅助列。"""
        return tuple(row[name] for name in (
            "binding_id", "person_id", "platform", "account_id", "display_name", "status",
            "version", "source", "evidence_type", "evidence_ref", "operator", "reviewer",
            "valid_from", "valid_to", "note", "created_at", "updated_at", "canonical_account",
        ))

    # ---------------------------------------------------------------- 绑定设置
    async def get_settings(self) -> dict[str, Any]:
        """读取绑定管理设置；auto_approve 默认关闭。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT key, value, updated_by, updated_at FROM binding_setting")
            values = {str(row["key"]): row for row in await cursor.fetchall()}
        auto_row = values.get("auto_approve")
        reason_row = values.get("auto_approve_reason")
        return {
            "auto_approve": bool(auto_row and str(auto_row["value"]) == "1"),
            "auto_approve_reason": str(reason_row["value"]) if reason_row else "",
            "updated_by": str(auto_row["updated_by"]) if auto_row else "",
            "updated_at": float(auto_row["updated_at"]) if auto_row else 0.0,
            "risk_notice": "开启后自动识别的绑定会跳过逐条人工审核，可能把陌生人合并进同一人物，默认必须关闭。",
        }

    async def set_auto_approve(
        self,
        enabled: bool,
        *,
        reason: str = "",
        operator: str = "",
        reviewer: str = "",
        risk_ack: bool = False,
        channel: str = "page",
        request_id: str = "",
    ) -> dict[str, Any]:
        """开启 auto_approve 必须写明理由并确认风险；变更强制写审计。"""
        reason = str(reason or "").strip()
        if enabled and not reason:
            raise ValueError("开启 auto_approve 必须写明理由")
        if enabled and not risk_ack:
            raise ValueError("开启 auto_approve 需要确认风险提示")
        if type(enabled) is not bool or type(risk_ack) is not bool:
            raise ValueError("auto_approve / risk_ack 必须是布尔值")
        async with self._transaction() as db:
            before = await self.get_settings()
            now = self._now()
            after = {
                **before,
                "auto_approve": enabled,
                "auto_approve_reason": reason,
                "updated_by": operator,
                "updated_at": now,
            }
            for key, value in (
                ("auto_approve", "1" if enabled else "0"),
                ("auto_approve_reason", reason),
            ):
                await db.execute(
                    """
                    INSERT INTO binding_setting(key, value, updated_by, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, operator, now),
                )
            await self._append_audit(
                db, action="config", binding_id="", version=0,
                before=before, after=after, channel=channel,
                operator_id=operator, operator_name=operator,
                reviewer_id=reviewer, reason=reason, request_id=request_id,
            )
        return after

    # ---------------------------------------------------------------- 待审候选
    async def submit_candidate(
        self,
        *,
        person_id: str = "",
        one_id: str = "",
        platform: str,
        account_id: str,
        display_name: str = "",
        source: str = "auto",
        evidence_type: str = "",
        evidence_ref: str = "",
        confidence: float = 0.0,
        matched_rule: str = "",
        note: str = "",
        submitted_by: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        """自动识别结果入待审队列；auto_approve 关闭时不会直接改正式绑定。"""
        person_id = identity_alias(one_id=one_id, person_id=person_id)
        account_key = canonical_account(platform, account_id)
        platform, account_id = account_key.split(":", 1)
        now = self._now()
        async with self._transaction() as db:
            cursor = await db.execute(
                """
                SELECT * FROM binding_candidate
                WHERE platform = ? AND account_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (platform, account_id),
            )
            existing = await cursor.fetchone()
            if existing:
                candidate = {
                    **dict(existing),
                    "person_id": person_id,
                    "display_name": display_name,
                    "source": source,
                    "evidence_type": evidence_type,
                    "evidence_ref": evidence_ref,
                    "confidence": float(confidence or 0),
                    "matched_rule": matched_rule,
                    "note": note,
                    "submitted_by": submitted_by,
                    "updated_at": now,
                }
                await db.execute(
                    """
                    UPDATE binding_candidate
                    SET person_id = ?, display_name = ?, source = ?, evidence_type = ?,
                        evidence_ref = ?, confidence = ?, matched_rule = ?, note = ?,
                        submitted_by = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        person_id, display_name, source, evidence_type, evidence_ref,
                        float(confidence or 0), matched_rule, note, submitted_by,
                        now, candidate["candidate_id"],
                    ),
                )
                action = "candidate_update"
                before = dict(existing)
            else:
                candidate = {
                    "candidate_id": uuid4().hex,
                    "person_id": person_id,
                    "platform": platform,
                    "account_id": account_id,
                    "display_name": display_name,
                    "source": source,
                    "evidence_type": evidence_type,
                    "evidence_ref": evidence_ref,
                    "confidence": float(confidence or 0),
                    "status": BINDING_STATUS_PENDING,
                    "matched_rule": matched_rule,
                    "note": note,
                    "submitted_by": submitted_by,
                    "reviewed_by": "",
                    "review_reason": "",
                    "created_at": now,
                    "updated_at": now,
                    "reviewed_at": None,
                }
                await db.execute(
                    """
                    INSERT INTO binding_candidate(
                        candidate_id, person_id, platform, account_id, display_name,
                        source, evidence_type, evidence_ref, confidence, status,
                        matched_rule, note, submitted_by, reviewed_by, review_reason,
                        created_at, updated_at, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(candidate.values()),
                )
                account_revision = await self._account_revision(db, account_key)
                active_snapshot = await self._active_for_key(db, account_key)
                snapshot_json = json.dumps(active_snapshot or {}, ensure_ascii=False, sort_keys=True)
                await db.execute(
                    "UPDATE binding_candidate SET account_revision=?,binding_snapshot_json=? "
                    "WHERE candidate_id=?", (account_revision, snapshot_json, candidate["candidate_id"]),
                )
                candidate.update(account_revision=account_revision, binding_snapshot_json=snapshot_json)
                action = "candidate_submit"
                before = {}
            await self._append_audit(
                db, action=action, binding_id=candidate["candidate_id"],
                version=0, before=before, after=candidate, channel="auto",
                operator_id=submitted_by, operator_name=submitted_by,
                evidence_type=evidence_type, evidence_ref=evidence_ref,
                reason="自动识别待人工审核", request_id=request_id,
            )

        settings = await self.get_settings()
        if settings.get("auto_approve") and candidate.get("account_revision", 0) == 0:
            reviewed = await self.review_candidate(
                candidate["candidate_id"], approve=True,
                reviewer="auto_approve",
                reason=f"auto_approve：{settings.get('auto_approve_reason') or '已开启'}",
                channel="auto", request_id=request_id,
                expected_candidate=candidate,
            )
            return {
                "candidate": (reviewed or {}).get("candidate", candidate),
                "auto_approved": bool(reviewed and reviewed.get("binding")),
                "binding": (reviewed or {}).get("binding"),
            }
        return {"candidate": candidate, "auto_approved": False}

    async def list_candidates(
        self, *, status: str = "pending", platform: str = "", person_id: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM binding_candidate"
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if platform:
            clauses.append("platform = ?")
            params.append(str(platform).lower())
        if person_id:
            clauses.append("person_id = ?")
            params.append(str(person_id))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(500, int(limit or 100))))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def review_candidate(
        self,
        candidate_id: str,
        *,
        approve: bool = False,
        reviewer: str = "",
        reason: str = "",
        channel: str = "page",
        request_id: str = "",
        expected_candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """逐条审核：通过才写正式绑定，驳回只改候选状态，两种都留审计。"""
        if type(approve) is not bool:
            raise ValueError("approve 必须是布尔值")
        if not str(reviewer or "").strip() or not str(reason or "").strip():
            raise ValueError("请填写审核人和审核依据")
        if not isinstance(expected_candidate, dict) or not expected_candidate:
            raise ValueError("缺少候选完整快照，请刷新后重新审核")
        async with self._transaction() as db:
            cursor = await db.execute(
                "SELECT * FROM binding_candidate WHERE candidate_id = ?",
                (str(candidate_id),),
            )
            row = await cursor.fetchone()
            if not row or row["status"] != BINDING_STATUS_PENDING:
                return None
            candidate = dict(row)
            # 同一候选 ID 会被自动识别更新；锁内核对人审看到的全部字段。
            if candidate != expected_candidate:
                raise ValueError("候选已变化，请刷新后重新审核")
            key = canonical_account(candidate["platform"], candidate["account_id"])
            active = await self._active_for_key(db, key)
            if (
                await self._account_revision(db, key) != candidate["account_revision"]
                or (active or {}) != json.loads(candidate["binding_snapshot_json"])
            ):
                raise IdentityConflict("候选账号归属已变化，请使用新申请")
            if approve and active is None and candidate["account_revision"] > 0:
                raise IdentityConflict("已解绑账号必须通过新的 one_id 申请重新绑定")
            now = self._now()
            binding = None
            if approve:
                await db.execute(
                    "UPDATE binding_candidate SET status='approved' WHERE candidate_id=?",
                    (str(candidate_id),),
                )
                binding = await self.upsert_binding(
                    person_id=candidate["person_id"],
                    platform=candidate["platform"],
                    account_id=candidate["account_id"],
                    display_name=candidate["display_name"],
                    source=candidate["source"] or "auto",
                    evidence_type=candidate["evidence_type"],
                    evidence_ref=candidate["evidence_ref"],
                    operator=candidate["submitted_by"] or reviewer,
                    reviewer=reviewer,
                    note=candidate["note"],
                    channel=channel,
                    reason=reason,
                    request_id=request_id,
                    expected_binding=active or {},
                    _db=db,
                )
            status = BINDING_STATUS_APPROVED if approve else BINDING_STATUS_REJECTED
            after = {
                **candidate,
                "status": status,
                "reviewed_by": reviewer,
                "review_reason": reason,
                "reviewed_at": now,
                "updated_at": now,
            }
            await db.execute(
                """
                UPDATE binding_candidate
                SET status = ?, reviewed_by = ?, review_reason = ?, reviewed_at = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (status, reviewer, reason, now, now, str(candidate_id)),
            )
            await self._append_audit(
                db,
                action="candidate_approve" if approve else "candidate_reject",
                binding_id=str(candidate_id), version=0,
                before=candidate, after=after, channel=channel,
                operator_id=reviewer, operator_name=reviewer, reviewer_id=reviewer,
                evidence_type=candidate["evidence_type"], evidence_ref=candidate["evidence_ref"],
                reason=reason, request_id=request_id,
            )
        return {"candidate": after, "binding": binding}

    # ---------------------------------------------------------------- 变更预览
    async def preview_impact(
        self, *, source_account: str = "", person_id: str = "", sample_limit: int = 10
    ) -> dict[str, Any]:
        """统计按来源账号/人物可影响的记忆：条数、场景分布、前 N 条样本。

        只统计带 ``source_account`` 的新记录；旧记录不猜归属，因此不计入。
        """
        accounts: list[str] = []
        if source_account:
            accounts.append(str(source_account))
        if person_id:
            accounts.extend(
                f"{row['platform']}:{row['account_id']}"
                for row in await self.accounts_for(person_id)
            )
        accounts = list(dict.fromkeys(accounts))
        if not accounts:
            return {"total": 0, "by_audience": {}, "memory_ids": [], "samples": []}

        placeholders = ",".join("?" * len(accounts))
        sql = (
            "SELECT id, text, metadata FROM documents "
            "WHERE json_valid(metadata) "
            f"AND json_extract(metadata, '$.source_account') IN ({placeholders}) "
            "AND COALESCE(json_extract(metadata, '$.status'), 'active') = 'active'"
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, accounts)
            rows = await cursor.fetchall()

        by_audience: dict[str, int] = {}
        samples, ids = [], []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            audience = str(metadata.get("audience") or "unknown")
            by_audience[audience] = by_audience.get(audience, 0) + 1
            ids.append(int(row["id"]))
            if len(samples) < sample_limit:
                samples.append({
                    "memory_id": int(row["id"]),
                    "audience": audience,
                    "sensitivity": int(metadata.get("sensitivity") or 0),
                    "content": str(row["text"] or "")[:120],
                })
        return {
            "total": len(ids),
            "by_audience": by_audience,
            "memory_ids": ids,
            "samples": samples,
        }

    # ---------------------------------------------------------------- 上传合并
    async def merge_upload(
        self,
        rows: list[dict[str, Any]],
        *,
        mode: str = "merge",
        file_hash: str = "",
        operator: str = "",
        reviewer: str = "",
        reason: str = "上传关联关系",
        channel: str = "upload",
        request_id: str = "",
        expected_active: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """按 (platform, account_id) 幂等 upsert。

        - merge：新增缺失绑定、同人更新显示名；文件中未出现的旧绑定不动。
        - replace：快照仍匹配且无坏行/冲突时，原子地撤销未出现的 active 绑定。
        - 冲突行（同一账号指向不同人物）不静默覆盖，单列返回待人工审。
        """
        mode = str(mode or "merge").lower()
        if mode not in {"merge", "replace"}:
            raise ValueError("mode 必须是 merge 或 replace")
        result = {
            "created": 0, "updated": 0, "unchanged": 0,
            "conflicts": [], "revoked": 0,
        }
        seen: set[tuple[str, str]] = set()
        normalized = []
        for index, row in enumerate(rows):
            person_id = identity_alias(one_id=row.get("one_id"), person_id=row.get("person_id"))
            key = canonical_account(row.get("platform"), row.get("account_id"))
            platform, account_id = key.split(":", 1)
            if (platform, account_id) in seen:
                raise ValueError(f"第 {index + 1} 行账号重复，请合并后重新上传")
            seen.add((platform, account_id))
            normalized.append({**row, "person_id": person_id, "platform": platform, "account_id": account_id})
        if not normalized:
            raise ValueError("没有可用的绑定行")

        async with self._transaction() as db:
            cursor = await db.execute("SELECT * FROM person_binding WHERE status = 'active'")
            active_rows = [dict(row) for row in await cursor.fetchall()]
            active_map = {
                tuple(item["canonical_account"].split(":", 1)): item for item in active_rows
            }
            if len(active_map) != len(active_rows):
                raise ValueError("现有绑定存在重复生效账号，请先人工复核")
            if mode == "replace":
                ordered = lambda items: sorted(items, key=lambda item: item["binding_id"])
                if expected_active is None or ordered(expected_active) != ordered(active_rows):
                    raise ValueError("预览已过期，请重新预览并确认")
            for index, row in enumerate(normalized):
                active = active_map.get((row["platform"], row["account_id"]))
                if active and active["person_id"] != row["person_id"]:
                    result["conflicts"].append({
                        "row": index + 1,
                        "platform": row["platform"],
                        "account_id": row["account_id"],
                        "current_person_id": active["person_id"],
                        "uploaded_person_id": row["person_id"],
                        "reason": "同一账号指向不同人物，待人工审",
                    })
            if mode == "replace" and result["conflicts"]:
                raise ValueError("replace 含人物冲突，请先手动更正绑定后重新预览")
            for row in normalized:
                active = active_map.get((row["platform"], row["account_id"]))
                if active and active["person_id"] != row["person_id"]:
                    continue
                binding = await self.upsert_binding(
                    person_id=row["person_id"], platform=row["platform"], account_id=row["account_id"],
                    display_name=str(row.get("display_name") or ""), source="upload",
                    evidence_type=str(row.get("evidence_type") or ""),
                    evidence_ref=str(row.get("evidence_ref") or ""),
                    operator=operator, reviewer=reviewer, note=str(row.get("note") or ""),
                    channel=channel, reason=reason, file_hash=file_hash, request_id=request_id, _db=db,
                )
                if active is None:
                    result["created"] += 1
                elif active != binding:
                    result["updated"] += 1
                else:
                    result["unchanged"] += 1
            if mode == "replace":
                for active in active_rows:
                    if tuple(active["canonical_account"].split(":", 1)) not in seen:
                        if await self.revoke_binding(
                            active["binding_id"], operator=operator, reviewer=reviewer,
                            reason=reason, channel=channel, request_id=request_id,
                            file_hash=file_hash, _db=db,
                        ):
                            result["revoked"] += 1
        return result
