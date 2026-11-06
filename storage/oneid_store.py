# 人和机器人共用主体表；申请、审核、解绑和审计原子提交。
"""统一主体、不可变申请和条件解绑；同库事务，不改记忆或索引。

person_binding 是旧兼容表，person_id 是 one_id 的同值别名。新申请单独建表，
避免改变旧候选更新/auto_approve 的已公开契约。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import aiosqlite

from ..core.identity_keys import canonical_account, canonical_account_sql, identity_alias, with_identity_alias

ONEID_SCHEMA_VERSION = 1
ONEID_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS oneid_entity(
        one_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK(entity_type IN ('human','bot','unknown')), display_name TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS identity_revision(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)""",
    "INSERT OR IGNORE INTO identity_revision VALUES(1,0)",
    """CREATE TABLE IF NOT EXISTS identity_account_revision(
        account_key TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS oneid_application(
        application_id TEXT PRIMARY KEY, one_id TEXT NOT NULL REFERENCES oneid_entity(one_id),
        platform TEXT NOT NULL, account_id TEXT NOT NULL, account_key TEXT NOT NULL,
        entity_type TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL CHECK(source IN ('manual','auto')),
        evidence_json TEXT NOT NULL, account_revision INTEGER NOT NULL,
        binding_snapshot_json TEXT NOT NULL, source_snapshot_json TEXT NOT NULL,
        correction INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending', submitted_by TEXT NOT NULL,
        reviewed_by TEXT NOT NULL DEFAULT '', review_reason TEXT NOT NULL DEFAULT '',
        binding_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
        updated_at REAL NOT NULL, expires_at REAL NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_oneid_application_account
        ON oneid_application(account_key,status)""",
    """CREATE TABLE IF NOT EXISTS identity_operation(
        actor TEXT NOT NULL, action TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(actor,action,idempotency_key))""",
)


class IdentityConflict(ValueError):
    code = "identity_conflict"
    retryable = False


class IdentityNotFound(ValueError):
    code = "identity_not_found"
    retryable = False


class IdentityContextChanged(IdentityConflict):
    code = "identity_context_changed"


def _encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class OneIdStoreMixin:
    """方法由 IdentityStore 继承；嵌套操作复用它的 _transaction 连接。"""

    def coordinate_with(self, engine) -> None:
        """生命周期内接线；生产写入复用 MemoryEngine 的连接和写锁。"""
        self._transaction_provider = engine._write_transaction
        self._identity_change_callback = engine._invalidate_search_cache

    async def _initialize_oneid(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute(
            "SELECT value FROM binding_setting WHERE key='oneid_schema_version'"
        )
        row = await cursor.fetchone()
        if row and int(row[0]) == ONEID_SCHEMA_VERSION:
            return
        if row and int(row[0]) > ONEID_SCHEMA_VERSION:
            raise RuntimeError("身份数据库版本高于当前代码，请保留原库并使用匹配版本")
        # SQLite backup 包括 WAL 中已提交内容；迁移只在初始化时运行。
        backup = Path(self.db_path).with_name(
            Path(self.db_path).name + f".before-oneid-{uuid4().hex}.sqlite3"
        )
        target = sqlite3.connect(backup)
        try:
            await db.backup(target)
        finally:
            target.close()
        backup.chmod(0o600)
        self.migration_backup = str(backup)
        await db.execute("BEGIN IMMEDIATE")
        try:
            for sql in ONEID_SCHEMA:
                await db.execute(sql)
            cursor = await db.execute("PRAGMA table_info(person_binding)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "canonical_account" not in columns:
                await db.execute(
                    "ALTER TABLE person_binding ADD COLUMN canonical_account TEXT NOT NULL DEFAULT ''"
                )
            cursor = await db.execute("PRAGMA table_info(binding_candidate)")
            columns = {row[1] for row in await cursor.fetchall()}
            for name, declaration in (
                ("account_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("binding_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in columns:
                    await db.execute(f"ALTER TABLE binding_candidate ADD COLUMN {name} {declaration}")
            cursor = await db.execute("SELECT * FROM person_binding")
            rows = [dict(row) for row in await cursor.fetchall()]
            active = {}
            for binding in rows:
                try:
                    key = canonical_account(binding["platform"], binding["account_id"])
                except ValueError:
                    key = "invalid:" + binding["binding_id"]
                await db.execute(
                    "UPDATE person_binding SET canonical_account=? WHERE binding_id=?",
                    (key, binding["binding_id"]),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO oneid_entity VALUES(?, 'unknown', '', ?, ?)",
                    (binding["person_id"], binding["created_at"], binding["updated_at"]),
                )
                if binding["status"] == "active":
                    active.setdefault(key, []).append(binding)
            for key, bindings in active.items():
                # 别名碰撞全部待复核，不用“最新一条”隐式确认归属。
                if key.startswith("invalid:") or len(bindings) > 1:
                    for binding in bindings:
                        await db.execute(
                            "UPDATE person_binding SET status='conflict',valid_to=?,updated_at=? "
                            "WHERE binding_id=?", (time.time(), time.time(), binding["binding_id"]),
                        )
                        await self._append_audit(
                            db, action="oneid_migration_conflict",
                            binding_id=binding["binding_id"], version=binding["version"],
                            before=binding, after={**binding, "status": "conflict"},
                            channel="migration", reason="规范账号存在歧义，待管理员重新申请确认",
                        )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_binding_unique_active_account "
                "ON person_binding(canonical_account) WHERE status='active'"
            )
            canonical_sql = canonical_account_sql("NEW.platform", "NEW.account_id")
            for action in ("INSERT", "UPDATE"):
                await db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS check_binding_canonical_{action.lower()} "
                    f"BEFORE {action} ON person_binding WHEN NEW.status='active' AND "
                    f"NEW.canonical_account != {canonical_sql} BEGIN "
                    "SELECT RAISE(ABORT,'invalid canonical account'); END"
                )
            await db.execute(
                "INSERT OR REPLACE INTO binding_setting VALUES('oneid_schema_version',?,'migration',?)",
                (str(ONEID_SCHEMA_VERSION), time.time()),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    async def _identity_committed(self) -> None:
        callback = getattr(self, "_identity_change_callback", None)
        if callback:
            # 缓存通知不参与提交结果；修订号校验是授权有效性的权威。
            try:
                callback()
            except Exception:
                pass

    @staticmethod
    async def _account_revision(db, key: str) -> int:
        cursor = await db.execute(
            "SELECT revision FROM identity_account_revision WHERE account_key=?", (key,)
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def _identity_changed(self, db, key: str, *, invalidation: str = "conflict") -> None:
        await db.execute("UPDATE identity_revision SET revision=revision+1 WHERE singleton=1")
        await db.execute(
            "INSERT INTO identity_account_revision VALUES(?,1) "
            "ON CONFLICT(account_key) DO UPDATE SET revision=revision+1", (key,),
        )
        await db.execute(
            "UPDATE oneid_application SET status=?,revision=revision+1,updated_at=?,"
            "review_reason='账号关联已变化，请创建新申请' WHERE status='pending' AND "
            "(account_key=? OR json_extract(source_snapshot_json,'$.account_key')=?)",
            (invalidation, self._now(), key, key),
        )
        cursor = await db.execute("SELECT * FROM binding_candidate WHERE status='pending'")
        for row in await cursor.fetchall():
            try:
                candidate_key = canonical_account(row["platform"], row["account_id"])
            except ValueError:
                candidate_key = ""
            if candidate_key == key:
                await db.execute(
                    "UPDATE binding_candidate SET status='rejected',updated_at=?,"
                    "review_reason='账号关联已变化，请重新申请' WHERE candidate_id=?",
                    (self._now(), row["candidate_id"]),
                )

    @staticmethod
    async def _active_for_key(db, key: str) -> dict | None:
        cursor = await db.execute(
            "SELECT * FROM person_binding WHERE canonical_account=? AND status='active'", (key,)
        )
        rows = await cursor.fetchall()
        if len(rows) > 1:
            raise IdentityConflict("同一规范账号存在多个有效关联")
        return dict(rows[0]) if rows else None

    async def get_revision(self, *, _db=None) -> int:
        if _db is not None:
            cursor = await _db.execute("SELECT revision FROM identity_revision WHERE singleton=1")
            row = await cursor.fetchone()
            if not row:
                raise RuntimeError("身份修订号未初始化")
            return int(row[0])
        async with aiosqlite.connect(self.db_path) as db:
            return await self.get_revision(_db=db)

    async def identity_snapshot(self, platform: str, account_id: str) -> dict:
        key = canonical_account(platform, account_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            revision = await self.get_revision(_db=db)
            binding = await self._active_for_key(db, key)
            one_id = binding["person_id"] if binding else "person:" + key
            cursor = await db.execute(
                "SELECT * FROM person_binding WHERE person_id=? AND status='active' "
                "ORDER BY canonical_account", (one_id,),
            )
            accounts = [with_identity_alias(dict(row)) for row in await cursor.fetchall()] if binding else []
            cursor = await db.execute("SELECT * FROM oneid_entity WHERE one_id=?", (one_id,))
            entity = await cursor.fetchone()
            return {
                "one_id": one_id, "person_id": one_id, "identity_revision": revision,
                "account_revision": await self._account_revision(db, key),
                "account_key": key, "binding": with_identity_alias(binding),
                "bound_accounts": accounts, "is_bound": binding is not None,
                "entity_type": entity["entity_type"] if entity else "unknown",
            }

    async def resolve_one_id(self, platform: str, account_id: str) -> str:
        return (await self.identity_snapshot(platform, account_id))["one_id"]

    async def is_context_current(self, context: dict) -> bool:
        revision = context.get("identity_revision") if isinstance(context, dict) else None
        if type(revision) is not int:
            return False
        try:
            return revision == await self.get_revision()
        except Exception:
            return False

    async def register_entity(
        self, *, one_id: str = "", person_id: str = "", entity_type: str = "unknown",
        display_name: str = "", operator: str, request_id: str = "",
    ) -> dict:
        one_id = identity_alias(one_id=one_id, person_id=person_id)
        if entity_type not in {"human", "bot", "unknown"} or not str(operator).strip():
            raise ValueError("主体类型或操作身份无效")
        if len(display_name) > 256:
            raise ValueError("显示名过长")
        async with self._transaction() as db:
            cursor = await db.execute("SELECT * FROM oneid_entity WHERE one_id=?", (one_id,))
            before = await cursor.fetchone()
            if before and before["entity_type"] not in {"unknown", entity_type}:
                raise IdentityConflict("已有主体类型不同，不能隐式把人改为机器人")
            kind = before["entity_type"] if before and entity_type == "unknown" else entity_type
            if before and before["entity_type"] == kind and before["display_name"] == display_name:
                return with_identity_alias(dict(before))
            now = self._now()
            await db.execute(
                "INSERT INTO oneid_entity VALUES(?,?,?,?,?) ON CONFLICT(one_id) "
                "DO UPDATE SET entity_type=excluded.entity_type,display_name=excluded.display_name,"
                "updated_at=excluded.updated_at", (one_id, kind, display_name, now, now),
            )
            cursor = await db.execute("SELECT * FROM oneid_entity WHERE one_id=?", (one_id,))
            entity = dict(await cursor.fetchone())
            if not before or dict(before) != entity:
                await db.execute("UPDATE identity_revision SET revision=revision+1 WHERE singleton=1")
                await self._append_audit(
                    db, action="entity_register", before=dict(before) if before else {}, after=entity,
                    operator_id=operator, request_id=request_id, channel="oneid",
                )
            return with_identity_alias(entity)

    async def list_entities(self, *, limit: int = 100, after: str = "") -> dict:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit 必须为 1–200")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT e.*, (SELECT COUNT(*) FROM person_binding b WHERE "
                "b.person_id=e.one_id AND b.status='active') AS account_count FROM oneid_entity e "
                "WHERE e.one_id>? ORDER BY e.one_id LIMIT ?", (after, limit + 1),
            )
            rows = [with_identity_alias(dict(row)) for row in await cursor.fetchall()]
            return {"items": rows[:limit], "next_cursor": rows[limit-1]["one_id"] if len(rows) > limit else ""}

    @staticmethod
    def _operation_input(actor: str, key: str, payload: dict) -> str:
        if not isinstance(actor, str) or not isinstance(key, str) or not actor.strip() or not key.strip() or len(key) > 128:
            raise ValueError("需要真实操作身份和 1–128 字符幂等键")
        return hashlib.sha256(_encoded(payload).encode()).hexdigest()

    @staticmethod
    async def _replay(db, actor: str, action: str, key: str, fingerprint: str):
        cursor = await db.execute(
            "SELECT fingerprint,result_json FROM identity_operation "
            "WHERE actor=? AND action=? AND idempotency_key=?", (actor, action, key),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise IdentityConflict("幂等键已用于不同请求")
        return json.loads(row["result_json"])

    async def _remember(self, db, actor: str, action: str, key: str, fingerprint: str, result):
        await db.execute(
            "INSERT INTO identity_operation VALUES(?,?,?,?,?,?)",
            (actor, action, key, fingerprint, _encoded(result), self._now()),
        )

    @staticmethod
    def _application(row) -> dict:
        value = with_identity_alias(dict(row))
        for field in ("evidence_json", "binding_snapshot_json", "source_snapshot_json"):
            value[field[:-5]] = json.loads(value.pop(field))
        value["correction"] = bool(value["correction"])
        return value

    async def get_application(self, application_id: str, *, _db=None) -> dict:
        if _db is None:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                return await self.get_application(application_id, _db=db)
        cursor = await _db.execute(
            "SELECT * FROM oneid_application WHERE application_id=?", (application_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise IdentityNotFound("绑定申请不存在")
        return self._application(row)

    async def submit_application(
        self, *, one_id: str = "", person_id: str = "", platform: str, account_id: str,
        operator: str, idempotency_key: str, evidence: dict, source: str = "manual",
        display_name: str = "", source_account: str = "", correction: bool = False,
        expires_in: float = 7 * 86400,
    ) -> dict:
        one_id = identity_alias(one_id=one_id, person_id=person_id)
        key = canonical_account(platform, account_id)
        platform, account_id = key.split(":", 1)
        if source not in {"manual", "auto"} or type(correction) is not bool:
            raise ValueError("申请来源或更正标识无效")
        if type(expires_in) not in {float, int} or not math.isfinite(expires_in) or not 60 <= expires_in <= 30*86400:
            raise ValueError("申请期限必须在 60 秒至 30 天之间")
        if not isinstance(evidence, dict) or not str(evidence.get("ref") or "").strip():
            raise ValueError("需要可追溯的申请依据 ref")
        if len(_encoded(evidence).encode()) > 16384 or len(display_name) > 256:
            raise ValueError("申请依据或显示名过长")
        payload = dict(one_id=one_id, account_key=key, source=source, evidence=evidence,
                       display_name=display_name, source_account=source_account,
                       correction=correction, expires_in=expires_in)
        fingerprint = self._operation_input(operator, idempotency_key, payload)
        async with self._transaction() as db:
            replay = await self._replay(db, operator, "submit", idempotency_key, fingerprint)
            if replay is not None:
                # 已取消/已解绑后的重放显示当前状态，不能复活旧申请。
                return await self.get_application(replay["application_id"], _db=db)
            cursor = await db.execute("SELECT * FROM oneid_entity WHERE one_id=?", (one_id,))
            entity = await cursor.fetchone()
            if not entity:
                raise IdentityNotFound("请先登记目标主体")
            active = await self._active_for_key(db, key)
            if active and active["person_id"] != one_id and not correction:
                raise IdentityConflict("账号已有其他归属，需要显式更正申请及新预览")
            source_snapshot = {}
            if source == "auto":
                # 自动识别只收本人结构化声明/已验证插件事件；仍须人工审批。
                if evidence.get("type") not in {"self_declaration", "verified_account_event"}:
                    raise ValueError("同名、头像、引用、模型猜测不能作为自动申请依据")
                if evidence.get("synthetic") or evidence.get("quoted") or evidence.get("forwarded"):
                    raise ValueError("合成唤醒、引用和转发不生成账号所有权申请")
                if evidence.get("actor_account") != source_account or not source_account:
                    raise ValueError("自动申请必须来自实际发言账号")
                p, sep, a = source_account.partition(":")
                if not sep:
                    raise ValueError("自动申请来源账号无效")
                source_key = canonical_account(p, a)
                if source_key == key:
                    raise ValueError("自动申请应声明另一个平台账号")
                source_binding = await self._active_for_key(db, source_key)
                if not source_binding or source_binding["person_id"] != one_id:
                    raise IdentityConflict("来源账号没有可确认的目标主体")
                source_snapshot = {
                    "account_key": source_key, "binding": source_binding,
                    "revision": await self._account_revision(db, source_key),
                }
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM oneid_application WHERE source='auto' AND "
                    "json_extract(source_snapshot_json,'$.account_key')=? AND "
                    "(status='pending' OR created_at>?)", (source_key, self._now()-86400),
                )
                if (await cursor.fetchone())[0] >= 64:
                    raise IdentityConflict("自动申请过于频繁，请管理员复核已有申请")
            now = self._now()
            application_id = uuid4().hex
            await db.execute(
                "INSERT INTO oneid_application(application_id,one_id,platform,account_id,account_key,"
                "entity_type,display_name,source,evidence_json,account_revision,binding_snapshot_json,"
                "source_snapshot_json,correction,submitted_by,created_at,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (application_id, one_id, platform, account_id, key, entity["entity_type"],
                 display_name, source, _encoded(evidence), await self._account_revision(db, key),
                 _encoded(active or {}), _encoded(source_snapshot), int(correction), operator,
                 now, now, now + expires_in),
            )
            application = await self.get_application(application_id, _db=db)
            await self._append_audit(
                db, action="application_submit", binding_id=application_id, version=1,
                after=application, operator_id=operator, evidence_type=str(evidence.get("type") or ""),
                evidence_ref=str(evidence["ref"]), channel="oneid", request_id=idempotency_key,
            )
            await self._remember(db, operator, "submit", idempotency_key, fingerprint, application)
            return application

    async def list_applications(self, *, status: str = "pending", limit: int = 100, after: str = "") -> dict:
        if status not in {"", "pending", "approved", "rejected", "cancelled", "expired", "conflict"}:
            raise ValueError("申请状态无效")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit 必须为 1–200")
        async with self._transaction() as db:
            cursor = await db.execute(
                "SELECT * FROM oneid_application WHERE status='pending' AND expires_at<=?",
                (self._now(),),
            )
            for row in await cursor.fetchall():
                before = self._application(row)
                await db.execute(
                    "UPDATE oneid_application SET status='expired',revision=revision+1,updated_at=? "
                    "WHERE application_id=?", (self._now(), before["application_id"]),
                )
                after_value = await self.get_application(before["application_id"], _db=db)
                await self._append_audit(
                    db, action="application_expired", binding_id=before["application_id"],
                    version=after_value["revision"], before=before, after=after_value,
                    channel="oneid", reason="申请期限已到",
                )
            cursor = await db.execute(
                "SELECT * FROM oneid_application WHERE application_id>? "
                "AND (?='' OR status=?) ORDER BY application_id LIMIT ?", (after, status, status, limit + 1),
            )
            rows = [self._application(row) for row in await cursor.fetchall()]
            return {"items": rows[:limit],
                    "next_cursor": rows[limit-1]["application_id"] if len(rows)>limit else ""}

    async def review_application(
        self, application_id: str, *, expected_application: dict, reviewer: str,
        idempotency_key: str, decision: str = "approve", reason: str,
    ) -> dict:
        if decision not in {"approve", "reject", "cancel"} or not str(reason).strip():
            raise ValueError("审核动作或依据无效")
        if not isinstance(expected_application, dict) or not expected_application:
            raise ValueError("需要审核时展示的完整申请快照")
        fingerprint = self._operation_input(reviewer, idempotency_key, {
            "application_id": application_id, "expected": expected_application,
            "decision": decision, "reason": reason,
        })
        error = None
        result = None
        async with self._transaction() as db:
            replay = await self._replay(db, reviewer, "review", idempotency_key, fingerprint)
            if replay is not None:
                return replay
            shown = await self.get_application(application_id, _db=db)
            if shown != expected_application or shown["status"] != "pending":
                raise IdentityConflict("申请已变化或已终结，请重新查看")
            if shown["expires_at"] <= self._now():
                terminal, error = "expired", "申请已过期"
            elif decision == "cancel":
                terminal = "cancelled"
            elif decision == "reject":
                terminal = "rejected"
            else:
                key = shown["account_key"]
                active = await self._active_for_key(db, key)
                valid = (
                    await self._account_revision(db, key) == shown["account_revision"]
                    and (active or {}) == shown["binding_snapshot"]
                )
                source = shown["source_snapshot"]
                if source:
                    valid = valid and (
                        await self._account_revision(db, source["account_key"]) == source["revision"]
                        and await self._active_for_key(db, source["account_key"]) == source["binding"]
                    )
                cursor = await db.execute(
                    "SELECT entity_type FROM oneid_entity WHERE one_id=?", (shown["one_id"],)
                )
                entity = await cursor.fetchone()
                valid = valid and bool(entity and entity[0] == shown["entity_type"])
                if not valid:
                    terminal, error = "conflict", "账号归属、主体或证据来源已变化"
                else:
                    terminal = "approved"
            # 先终结当前申请；正式关联变化只作废其他尚待审核的申请。
            await db.execute(
                "UPDATE oneid_application SET status=?,revision=revision+1,reviewed_by=?,"
                "review_reason=?,updated_at=? WHERE application_id=?",
                (terminal, reviewer, reason, self._now(), application_id),
            )
            binding = None
            if terminal == "approved":
                binding = await self.upsert_binding(
                    person_id=shown["one_id"], platform=shown["platform"], account_id=shown["account_id"],
                    display_name=shown["display_name"], source=shown["source"],
                    evidence_type=str(shown["evidence"].get("type") or ""),
                    evidence_ref=shown["evidence"]["ref"], operator=shown["submitted_by"],
                    reviewer=reviewer, reason=reason, channel="oneid",
                    request_id=idempotency_key, expected_binding=shown["binding_snapshot"], _db=db,
                )
                await db.execute(
                    "UPDATE oneid_application SET binding_id=? WHERE application_id=?",
                    (binding["binding_id"], application_id),
                )
            after = await self.get_application(application_id, _db=db)
            result = {"application": after, "binding": with_identity_alias(binding),
                      "identity_revision": await self.get_revision(_db=db)}
            await self._append_audit(
                db, action="application_" + terminal, binding_id=application_id, version=after["revision"],
                before=shown, after=after, operator_id=reviewer, reviewer_id=reviewer,
                reason=reason, request_id=idempotency_key, channel="oneid",
            )
            if not error:
                await self._remember(db, reviewer, "review", idempotency_key, fingerprint, result)
        if error:
            raise IdentityConflict(error)
        return result

    async def unlink_account(
        self, *, binding_id: str, expected_version: int, expected_revision: int,
        one_id: str = "", person_id: str = "", operator: str, reason: str,
        idempotency_key: str,
    ) -> dict:
        one_id = identity_alias(one_id=one_id, person_id=person_id)
        if type(expected_version) is not int or type(expected_revision) is not int or not reason.strip():
            raise ValueError("需要关系版本、预览修订号及解绑依据")
        fingerprint = self._operation_input(operator, idempotency_key, dict(
            binding_id=binding_id, expected_version=expected_version, expected_revision=expected_revision,
            one_id=one_id, reason=reason,
        ))
        async with self._transaction() as db:
            replay = await self._replay(db, operator, "unlink", idempotency_key, fingerprint)
            if replay is not None:
                return replay
            cursor = await db.execute("SELECT * FROM person_binding WHERE binding_id=?", (binding_id,))
            row = await cursor.fetchone()
            if not row:
                raise IdentityNotFound("账号关联不存在")
            binding = dict(row)
            if (
                binding["status"] != "active" or binding["version"] != expected_version
                or binding["person_id"] != one_id
                or await self.get_revision(_db=db) != expected_revision
            ):
                raise IdentityConflict("解绑预览已过期，请重新预览")
            await self.revoke_binding(
                binding_id, operator=operator, reason=reason, channel="oneid",
                request_id=idempotency_key, _db=db,
            )
            result = {"unlinked": True, "binding_id": binding_id, "one_id": one_id, "person_id": one_id,
                      "account_key": binding["canonical_account"],
                      "identity_revision": await self.get_revision(_db=db), "memories_preserved": True}
            await self._remember(db, operator, "unlink", idempotency_key, fingerprint, result)
            return result
