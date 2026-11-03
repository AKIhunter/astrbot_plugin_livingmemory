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
from typing import Any
from uuid import uuid4

import aiosqlite

from astrbot.api import logger

BINDING_STATUS_ACTIVE = "active"
BINDING_STATUS_REVOKED = "revoked"
BINDING_STATUS_SUPERSEDED = "superseded"

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
)


def _json(value: Any) -> str:
    return json.dumps(value if isinstance(value, (dict, list)) else {}, ensure_ascii=False)


class IdentityStore:
    """person_binding / binding_audit 的读写入口（与 livingmemory.db 共用文件）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    @staticmethod
    def _now() -> float:
        return time.time()

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode = WAL")
            for statement in _BINDING_SCHEMA:
                await db.execute(statement)
            await db.commit()
        logger.info("[IdentityStore] person_binding / binding_audit 已就绪")

    # ---------------------------------------------------------------- 读取
    async def find_active(self, platform: str, account_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM person_binding
                WHERE platform = ? AND account_id = ? AND status = 'active'
                ORDER BY version DESC LIMIT 1
                """,
                (str(platform), str(account_id)),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def resolve_person(self, platform: str, account_id: str) -> str:
        """当前 active 绑定 → person_id；未绑定时按来源账号稳定派生，不做猜测合并。"""
        if not platform or not account_id:
            return ""
        binding = await self.find_active(platform, account_id)
        if binding:
            return str(binding.get("person_id") or "")
        return f"person:{platform}:{account_id}"

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
        self, *, person_id: str = "", include_history: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM person_binding"
        clauses, params = [], []
        if person_id:
            clauses.append("person_id = ?")
            params.append(str(person_id))
        if not include_history:
            clauses.append("status = 'active'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY platform, account_id, version DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

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
        person_id: str,
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
    ) -> dict[str, Any]:
        """按 (platform, account_id) 幂等 upsert；人物变化时版本化并保留旧行。"""
        person_id = str(person_id or "").strip()
        platform = str(platform or "").strip().lower()
        account_id = str(account_id or "").strip()
        if not person_id or not platform or not account_id:
            raise ValueError("person_id / platform / account_id 均不能为空")

        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM person_binding
                WHERE platform = ? AND account_id = ? AND status = 'active'
                ORDER BY version DESC LIMIT 1
                """,
                (platform, account_id),
            )
            active = await cursor.fetchone()
            active = dict(active) if active else None

            if active is None:
                cursor = await db.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) AS max_version
                    FROM person_binding WHERE platform = ? AND account_id = ?
                    """,
                    (platform, account_id),
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
                }
                await db.execute(
                    """
                    INSERT INTO person_binding(
                        binding_id, person_id, platform, account_id, display_name,
                        status, version, source, evidence_type, evidence_ref,
                        operator, reviewer, valid_from, valid_to, note,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(binding.values()),
                )
                await self._append_audit(
                    db, action="create", binding_id=binding["binding_id"],
                    version=binding["version"], before={}, after=binding,
                    channel=channel, operator_id=operator, operator_name=operator,
                    reviewer_id=reviewer, evidence_type=evidence_type,
                    evidence_ref=evidence_ref, file_hash=file_hash,
                    reason=reason, request_id=request_id,
                )
                await db.commit()
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
                await self._append_audit(
                    db, action="update", binding_id=active["binding_id"],
                    version=int(active["version"]), before=active, after=updated,
                    channel=channel, operator_id=operator, operator_name=operator,
                    reviewer_id=reviewer, evidence_type=evidence_type,
                    evidence_ref=evidence_ref, file_hash=file_hash,
                    reason=reason, request_id=request_id,
                )
                await db.commit()
                return updated

            # 人物归属变化：旧行软失效，插入新版本；历史记忆一行不动。
            cursor = await db.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS max_version
                FROM person_binding WHERE platform = ? AND account_id = ?
                """,
                (platform, account_id),
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
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(new_binding.values()),
            )
            await self._append_audit(
                db, action="merge", binding_id=new_binding["binding_id"],
                version=new_binding["version"], before=active, after=new_binding,
                channel=channel, operator_id=operator, operator_name=operator,
                reviewer_id=reviewer, evidence_type=evidence_type,
                evidence_ref=evidence_ref, file_hash=file_hash,
                reason=reason, request_id=request_id,
            )
            await db.commit()
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
    ) -> bool:
        """软失效：置 revoked 并写 valid_to，不删除行。"""
        now = self._now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
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
            await self._append_audit(
                db, action="revoke", binding_id=str(binding_id),
                version=int(before["version"]), before=before, after=after,
                channel=channel, operator_id=operator, operator_name=operator,
                reviewer_id=reviewer, reason=reason, request_id=request_id,
            )
            await db.commit()
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
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM person_binding WHERE binding_id = ?",
                (str(binding_id),),
            )
            current = await cursor.fetchone()
            if not current:
                return None
            current = dict(current)
            cursor = await db.execute(
                """
                SELECT * FROM person_binding
                WHERE platform = ? AND account_id = ? AND version = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (current["platform"], current["account_id"], int(to_version)),
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
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(restored.values()),
            )
            await self._append_audit(
                db, action="rollback", binding_id=restored["binding_id"],
                version=restored["version"], before=current, after=restored,
                channel=channel, operator_id=operator, operator_name=operator,
                reviewer_id=reviewer, reason=reason, request_id=request_id,
            )
            await db.commit()
            return restored

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
    ) -> dict[str, Any]:
        """按 (platform, account_id) 幂等 upsert。

        - merge：新增缺失绑定、同人更新显示名；文件中未出现的旧绑定不动。
        - replace：处理完文件后，撤销未出现的 active 绑定（需调用方先预览确认）。
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
        for index, row in enumerate(rows):
            person_id = str(row.get("person_id") or "").strip()
            platform = str(row.get("platform") or "").strip().lower()
            account_id = str(row.get("account_id") or "").strip()
            if not person_id or not platform or not account_id:
                result["conflicts"].append({
                    "row": index + 2, "reason": "缺少 person_id/platform/account_id",
                })
                continue
            seen.add((platform, account_id))
            active = await self.find_active(platform, account_id)
            if active and str(active["person_id"]) != person_id:
                result["conflicts"].append({
                    "row": index + 2,
                    "platform": platform,
                    "account_id": account_id,
                    "current_person_id": active["person_id"],
                    "uploaded_person_id": person_id,
                    "reason": "同一账号指向不同人物，待人工审",
                })
                continue
            before = active
            binding = await self.upsert_binding(
                person_id=person_id,
                platform=platform,
                account_id=account_id,
                display_name=str(row.get("display_name") or ""),
                source="upload",
                evidence_type=str(row.get("evidence_type") or ""),
                evidence_ref=str(row.get("evidence_ref") or ""),
                operator=operator,
                reviewer=reviewer,
                note=str(row.get("note") or ""),
                channel=channel,
                reason=reason,
                file_hash=file_hash,
                request_id=request_id,
            )
            if before is None:
                result["created"] += 1
            elif before.get("updated_at") != binding.get("updated_at") and \
                    before.get("display_name") != binding.get("display_name"):
                result["updated"] += 1
            else:
                result["unchanged"] += 1

        if mode == "replace":
            for active in await self.list_bindings():
                key = (str(active["platform"]), str(active["account_id"]))
                if key in seen:
                    continue
                if await self.revoke_binding(
                    str(active["binding_id"]), operator=operator,
                    reviewer=reviewer, reason="replace 文件中未出现",
                    channel=channel, request_id=request_id,
                ):
                    result["revoked"] += 1
        return result
