# one_id 核心门面保留旧接口，并接入真实事件的受限自动申请。
"""
main.py - LivingMemory 插件主文件
负责插件注册、初始化和生命周期管理
"""

from .core.memory_scope import adapter_access
import asyncio
from .core.memory_scope import adapter_access
import re
from collections.abc import AsyncGenerator
from importlib import metadata as importlib_metadata
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .core.base.config_manager import ConfigManager
from .core.command_handler import CommandHandler
from .core.event_handler import EventHandler
from .core.i18n_backend import init as i18n_init
from .core.i18n_backend import t
from .core.managers.backup_manager import BackupManager
from .core.page_api import PluginPageApi
from .core.unified_context import UnifiedContextService
from .core.passive_group_capture import (
    PassiveGroupCaptureFilter,
    get_active_plugin,
    is_plugin_enabled_for_session,
    is_session_enabled,
    set_active_plugin,
)
from .core.plugin_initializer import PluginInitializer
from .core.tools import (
    AgenticMemorySearchTool,
    MemoryForgetTool,
    MemoryMemorizeTool,
    MemoryReadTool,
    MemorySearchTool,
)

_MIN_ASTRBOT_VERSION = "4.24.2"
_ASTRBOT_DISTRIBUTION_NAMES = ("AstrBot", "astrbot")


def _parse_version(v: str) -> tuple[int, ...]:
    m = re.match(r"v?(\d+(?:\.\d+)*)", v.strip(), re.IGNORECASE)
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def _version_lt(current: str, minimum: str) -> bool:
    current_parts = _parse_version(current)
    minimum_parts = _parse_version(minimum)
    if not current_parts or not minimum_parts:
        return False
    width = max(len(current_parts), len(minimum_parts))
    return current_parts + (0,) * (width - len(current_parts)) < minimum_parts + (
        0,
    ) * (width - len(minimum_parts))


def _detect_astrbot_version() -> str | None:
    for distribution_name in _ASTRBOT_DISTRIBUTION_NAMES:
        try:
            return importlib_metadata.version(distribution_name)
        except importlib_metadata.PackageNotFoundError:
            continue
        except Exception as exc:
            logger.debug(f"读取 AstrBot 分发版本失败 ({distribution_name}): {exc}")

    for module_name in ("astrbot.core.config.default", "astrbot.core.config"):
        try:
            module = __import__(module_name, fromlist=["VERSION"])
            version_value = getattr(module, "VERSION", None)
        except Exception as exc:
            logger.debug(f"读取 AstrBot 模块版本失败 ({module_name}): {exc}")
            continue
        if version_value:
            return str(version_value)

    return None


_CURRENT_ASTRBOT_VERSION = _detect_astrbot_version()

if _CURRENT_ASTRBOT_VERSION is None:
    logger.debug("未能检测到 AstrBot 版本，跳过 LivingMemory 版本兼容提示")
elif _version_lt(_CURRENT_ASTRBOT_VERSION, _MIN_ASTRBOT_VERSION):
    logger.warning(
        f"AstrBot 版本 {_CURRENT_ASTRBOT_VERSION} 低于推荐版本 {_MIN_ASTRBOT_VERSION}。"
        f"插件视图 / WebUI 功能可能不可用。建议升级 AstrBot 以获得完整体验。"
    )


# 插件元数据由 metadata.yaml 提供。AstrBot 3.5.19+ 会自动识别继承 Star 的插件类，
# 已废弃的 @register 装饰器不再使用，避免宿主未来移除它时插件无法加载。
class LivingMemoryPlugin(Star):
    """LivingMemory 插件主类"""

    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.context = context

        # 获取插件数据目录
        data_dir = str(StarTools.get_data_dir("astrbot_plugin_livingmemory"))

        # 版本变更时自动备份数据（延迟到异步初始化阶段执行，避免 __init__ 中同步 I/O 阻塞）
        self._backup_manager = BackupManager(data_dir)

        # 初始化配置管理器
        self.config_manager = ConfigManager(config)

        # 初始化后端 i18n
        i18n_init(config.get("bot_language", "zh"))

        # 初始化插件初始化器
        self.initializer = PluginInitializer(context, self.config_manager, data_dir)

        # 事件处理器和命令处理器（初始化后创建）
        self.event_handler: EventHandler | None = None
        self.command_handler: CommandHandler | None = None

        # 后台任务跟踪集合
        self._background_tasks: set[asyncio.Task] = set()
        self._component_init_lock = asyncio.Lock()
        self._llm_tools_registered = False
        self._terminating = False

        self.unified_context = UnifiedContextService(self)
        self.page_api = PluginPageApi(self)
        self.page_api.register_routes()
        set_active_plugin(self)

        # 启动非阻塞的初始化任务
        self._create_tracked_task(self._initialize_plugin())

    async def search_context(
        self, query: str, *, caller: str, purpose: str,
        persona_id: str, session_id: str = "",
    ) -> dict:
        """授权插件的统一只读检索入口；不得直接暴露给模型或匿名请求。"""
        return await self.unified_context.search(
            query, caller=caller, purpose=purpose,
            persona_id=persona_id, session_id=session_id,
        )

    async def resolve_identity_context(self, event, persona_id: str = "") -> dict:
        """阶段 1 统一身份解析入口：事件 → 人物 + 来源账号 + 场景 + 受众。"""
        from .core.identity import build_identity_context

        return await build_identity_context(
            self.config_manager, event,
            getattr(self.initializer, "identity_store", None),
            persona_id=persona_id,
        )

    async def search_memories_for_event(
        self, query: str, event, *, persona_id: str = "", k: int = 4,
    ) -> list:
        """供 at_guard/从绫复用的稳定检索入口，避免各自拼接范围。"""
        identity = await self.resolve_identity_context(event, persona_id=persona_id)
        engine = getattr(self.initializer, "memory_engine", None)
        if engine is None:
            return []
        with adapter_access(self.config_manager, event):
            results = await engine.search_memories_for_identity(
                query, identity=identity, persona_id=persona_id or None, k=k,
            )
            return await engine.validate_identity_results(results)

    # ---------------------------------------------------------- 绑定修正（阶段 1）
    async def validate_identity_results(self, results: list) -> list:
        """已接入插件在最终注入/发送前复核；旧快照不代表当前授权。"""
        return await self.initializer.memory_engine.validate_identity_results(results)

    async def project_identity(self, **kwargs) -> dict:
        return await self.initializer.memory_engine.project_identity(**kwargs)

    async def submit_identity_application(self, **kwargs) -> dict:
        """可信 Python 调用方提交手动申请；操作者由调用方认证，不向模型暴露。"""
        if kwargs.get("source", "manual") != "manual":
            raise ValueError("自动申请必须使用实际事件入口")
        return await self.initializer.identity_store.submit_application(**kwargs)

    async def submit_identity_application_for_event(
        self, event, *, platform: str, account_id: str, evidence_ref: str,
        idempotency_key: str, evidence_type: str = "self_declaration",
    ) -> dict:
        from .core.identity import is_synthetic_memory_event
        identity = await self.resolve_identity_context(event)
        if is_synthetic_memory_event(event):
            raise ValueError("合成事件不能生成自动申请")
        account = identity["source_account"]
        return await self.initializer.identity_store.submit_application(
            one_id=identity["one_id"], platform=platform, account_id=account_id,
            operator="event:" + account, idempotency_key=idempotency_key, source="auto",
            source_account=account,
            evidence={"type": evidence_type, "ref": evidence_ref, "actor_account": account},
        )

    async def review_identity_application(self, application_id: str, **kwargs) -> dict:
        return await self.initializer.identity_store.review_application(application_id, **kwargs)

    async def preview_identity_change(self, **kwargs) -> dict:
        return await self.initializer.memory_engine.preview_identity_change(**kwargs)

    async def unlink_identity_account(self, **kwargs) -> dict:
        return await self.initializer.identity_store.unlink_account(**kwargs)

    async def preview_binding_change(
        self, *, source_account: str = "", person_id: str = "", sample_limit: int = 10,
    ) -> dict:
        """变更预览：影响条数、涉及场景、前 N 条样本。只读，不落库。"""
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            return {"total": 0, "by_audience": {}, "memory_ids": [], "samples": []}
        return await store.preview_impact(
            source_account=source_account, person_id=person_id, sample_limit=sample_limit,
        )

    async def apply_binding_change(
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
        reason: str = "",
        channel: str = "manual",
        file_hash: str = "",
        request_id: str = "",
        archive_affected: bool = False,
        expected_binding: dict | None = None,
    ) -> dict:
        """改绑定：版本化软失效 + 审计；不重写已有记忆。

        ``archive_affected=True`` 时把该账号命中的记忆批量移入回收站（只改状态），
        便于人工复核；默认不动记忆，避免误伤。
        """
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            raise RuntimeError("IdentityStore 未初始化")
        before = await store.find_active(platform, account_id)
        preview = await store.preview_impact(source_account=f"{platform}:{account_id}")
        archived = 0
        engine = getattr(self.initializer, "memory_engine", None)
        if archive_affected and engine is not None and preview.get("memory_ids"):
            archived = await engine.archive_memories(
                list(preview["memory_ids"]),
                audit={
                    "archive_reason": reason or "绑定变更",
                    "operator": operator,
                    "binding_request": request_id,
                },
            )
        binding = await store.upsert_binding(
            person_id=person_id, platform=platform, account_id=account_id,
            display_name=display_name, source=source, evidence_type=evidence_type,
            evidence_ref=evidence_ref, operator=operator, reviewer=reviewer,
            note=note, channel=channel, reason=reason, file_hash=file_hash,
            request_id=request_id,
            expected_binding=expected_binding,
        )
        return {"before": before, "binding": binding, "preview": preview, "archived": archived}

    async def rollback_binding_change(
        self,
        binding_id: str,
        to_version: int,
        *,
        restore_memory_ids: list[int] | None = None,
        operator: str = "",
        reviewer: str = "",
        reason: str = "",
        request_id: str = "",
    ) -> dict:
        """回滚绑定版本，并可按需把回收站里的记忆恢复回索引。"""
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            raise RuntimeError("IdentityStore 未初始化")
        binding = await store.rollback_binding(
            binding_id, to_version,
            operator=operator, reviewer=reviewer, reason=reason, request_id=request_id,
        )
        restored = 0
        engine = getattr(self.initializer, "memory_engine", None)
        if binding is not None and engine is not None:
            for memory_id in restore_memory_ids or []:
                try:
                    if await engine.restore_memory(int(memory_id)):
                        restored += 1
                except Exception as e:
                    logger.warning(f"[LivingMemory] 恢复记忆 {memory_id} 失败: {e!r}")
        return {"binding": binding, "restored": restored}

    async def upload_bindings(
        self,
        rows: list[dict],
        *,
        mode: str = "merge",
        file_hash: str = "",
        operator: str = "",
        reviewer: str = "",
        reason: str = "上传关联关系",
        request_id: str = "",
        expected_active: list[dict] | None = None,
    ) -> dict:
        """上传关联关系：默认 merge；冲突行单列待人工审。"""
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            raise RuntimeError("IdentityStore 未初始化")
        return await store.merge_upload(
            rows, mode=mode, file_hash=file_hash, operator=operator,
            reviewer=reviewer, reason=reason, request_id=request_id,
            expected_active=expected_active,
        )

    # ---------------------------------------------------------- 绑定管理（阶段 3）
    async def list_binding_records(
        self,
        *,
        person_id: str = "",
        platform: str = "",
        account_id: str = "",
        status: str = "",
        include_history: bool = False,
    ) -> list[dict]:
        """页面绑定列表：按人物/平台/账号/状态筛选。"""
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            return []
        return await store.list_bindings(
            person_id=person_id, platform=platform, account_id=account_id,
            status=status, include_history=include_history,
        )

    async def binding_audit_records(
        self, *, binding_id: str = "", action: str = "", limit: int = 100, offset: int = 0
    ) -> list[dict]:
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            return []
        return await store.list_audit(
            binding_id=binding_id, action=action, limit=limit, offset=offset,
        )

    async def revoke_binding_change(
        self, binding_id: str, *, operator: str = "", reviewer: str = "",
        reason: str = "", request_id: str = "",
    ) -> bool:
        """单条撤销：软失效并留审计，不删除绑定历史。"""
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            raise RuntimeError("IdentityStore 未初始化")
        return await store.revoke_binding(
            binding_id, operator=operator, reviewer=reviewer,
            reason=reason, channel="page", request_id=request_id,
        )

    async def submit_binding_candidate(self, **kwargs) -> dict:
        """供自动识别链路投递候选；默认进入待审队列，不直接改正式绑定。"""
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            raise RuntimeError("IdentityStore 未初始化")
        return await store.submit_candidate(**kwargs)

    async def list_binding_candidates(
        self, *, status: str = "pending", platform: str = "", person_id: str = "", limit: int = 100
    ) -> list[dict]:
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            return []
        return await store.list_candidates(
            status=status, platform=platform, person_id=person_id, limit=limit,
        )

    async def review_binding_candidate(
        self, candidate_id: str, *, approve: bool = False, reviewer: str = "",
        reason: str = "", request_id: str = "",
        expected_candidate: dict | None = None,
    ) -> dict | None:
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            raise RuntimeError("IdentityStore 未初始化")
        return await store.review_candidate(
            candidate_id, approve=approve, reviewer=reviewer,
            reason=reason, channel="page", request_id=request_id,
            expected_candidate=expected_candidate,
        )

    async def get_binding_settings(self) -> dict:
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            return {"auto_approve": False, "auto_approve_reason": "", "risk_notice": ""}
        return await store.get_settings()

    async def set_binding_auto_approve(
        self, enabled: bool, *, reason: str = "", operator: str = "",
        reviewer: str = "", risk_ack: bool = False, request_id: str = "",
    ) -> dict:
        store = getattr(self.initializer, "identity_store", None)
        if store is None:
            raise RuntimeError("IdentityStore 未初始化")
        return await store.set_auto_approve(
            enabled, reason=reason, operator=operator, reviewer=reviewer,
            risk_ack=risk_ack, channel="page", request_id=request_id,
        )
    def _create_tracked_task(self, coro) -> asyncio.Task:
        """创建并跟踪后台任务"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _initialize_plugin(self):
        """初始化插件"""
        try:
            # 版本变更时自动备份数据（在任何数据库操作之前，通过线程池避免阻塞事件循环）
            await self._backup_manager.backup_if_needed_async()

            # 执行初始化
            success = await self.initializer.initialize()

            if success:
                await self._ensure_runtime_components()

        except Exception as e:
            logger.error(f"插件初始化失败: {e}", exc_info=True)

    async def _ensure_runtime_components(self) -> bool:
        """确保运行期组件（事件/命令处理器、WebUI）已就绪"""
        if self._terminating:
            return False
        if not self.initializer.is_initialized:
            return False

        async with self._component_init_lock:
            if self._terminating:
                return False
            # 检查必要组件是否初始化成功
            if not all(
                [
                    self.initializer.memory_engine,
                    self.initializer.memory_processor,
                    self.initializer.conversation_manager,
                ]
            ):
                logger.error("插件初始化不完整：部分核心组件未能初始化")
                return False

            # 工具和命令共用当前绑定存储；每次请求重新解析 active 绑定。
            self.initializer.memory_engine.identity_store = self.initializer.identity_store

            # 创建事件处理器（幂等）
            if not self.event_handler:
                self.event_handler = EventHandler(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,  # type: ignore[arg-type]
                    memory_processor=self.initializer.memory_processor,  # type: ignore[arg-type]
                    conversation_manager=self.initializer.conversation_manager,  # type: ignore[arg-type]
                    consolidation_manager=self.initializer.consolidation_manager,
                    identity_store=self.initializer.identity_store,
                )

            # 创建命令处理器（幂等）
            if not self.command_handler:
                self.command_handler = CommandHandler(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                    conversation_manager=self.initializer.conversation_manager,
                    index_validator=self.initializer.index_validator,
                    memory_processor=self.initializer.memory_processor,
                    initialization_status_callback=self._get_initialization_status_message,
                )

            self._register_agent_tools_if_needed()

        return True

    def _register_agent_tools_if_needed(self) -> None:
        """在核心组件就绪后注册 Agent 工具（回忆/写入）。

        Beta 自主多轮回忆开启时，用增强版搜索工具替代基线工具（同名
        recall_long_term_memory，注册时二选一），并额外注册深读工具；
        Beta 关闭时保持 f054c82 基线的工具集合与行为不变。开关经宿主
        重载插件后生效。
        """
        if self._llm_tools_registered:
            return
        if not self.initializer.memory_engine or not self.initializer.memory_processor:
            return

        recall_enabled = bool(
            self.config_manager.get("agent_tools.enable_recall_tool", True)
        )
        beta_enabled = recall_enabled and bool(
            self.config_manager.get("agent_tools.enable_agentic_recall_beta", False)
        )

        tools = []
        if recall_enabled:
            search_tool_cls = (
                AgenticMemorySearchTool if beta_enabled else MemorySearchTool
            )
            tools.append(
                search_tool_cls(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                )
            )
        if beta_enabled:
            tools.append(
                MemoryReadTool(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                )
            )
        if self.config_manager.get("agent_tools.enable_memorize_tool", False):
            tools.append(
                MemoryMemorizeTool(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                    memory_processor=self.initializer.memory_processor,
                )
            )
        if self.config_manager.get("agent_tools.enable_forget_tool", False):
            tools.append(
                MemoryForgetTool(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                )
            )

        if tools:
            self.context.add_llm_tools(*tools)
        # 标记注册流程完成，后续不再重复检查。
        # 若用户中途修改 agent_tools 开关，需要重载插件才能生效。
        self._llm_tools_registered = True

    def _schedule_passive_group_capture(self, event: AstrMessageEvent) -> None:
        """Schedule full group capture from a filter without waking the message."""
        if self._terminating or not self.initializer.is_initialized:
            return
        self._create_tracked_task(self._run_passive_group_capture(event))

    async def _run_passive_group_capture(self, event: AstrMessageEvent) -> None:
        try:
            if not await is_session_enabled(event.unified_msg_origin):
                logger.debug(
                    f"[{event.unified_msg_origin}] 当前会话已关闭，跳过被动群聊消息捕获"
                )
                return
            if not await is_plugin_enabled_for_session(event.unified_msg_origin):
                logger.debug(
                    f"[{event.unified_msg_origin}] LivingMemory 已在当前会话禁用，"
                    "跳过被动群聊消息捕获"
                )
                return
            if not await self._ensure_runtime_components():
                logger.debug("插件组件未就绪，跳过被动群聊消息捕获")
                return
            if not self.event_handler:
                return
            await self.event_handler.handle_all_group_messages(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"被动群聊消息捕获失败: {e}", exc_info=True)

    async def _ensure_plugin_ready(self, *, wait: bool = True) -> tuple[bool, str]:
        """确保插件已完成初始化并且运行期组件可用。

        Args:
            wait: True 时等待后台初始化完成（命令/WebUI 请求）；False 时只检查
                当前状态，用于 LLM 请求钩子，避免初始化期间每个请求轮询等待。
        """
        if wait:
            if not await self.initializer.ensure_initialized():
                return False, self._get_initialization_status_message()
        elif not self.initializer.is_initialized:
            return False, self._get_initialization_status_message()

        if not await self._ensure_runtime_components():
            return (
                False,
                t("command.core_not_ready"),
            )

        return True, ""

    def _get_initialization_status_message(self) -> str:
        """获取初始化状态的用户友好消息"""
        if self.initializer.is_initialized:
            return t("init.ready")
        elif self.initializer.is_failed:
            return t(
                "init.failed",
                error=self.initializer.error_message or t("common.unknown_error"),
            )
        else:
            return t(
                "init.in_progress",
                attempts=self.initializer._provider_check_attempts,
            )

    @staticmethod
    def _command_handler_not_ready_message() -> str:
        """命令处理器未就绪时的提示"""
        return t("command.not_ready")

    async def _run_command(
        self, event: AstrMessageEvent, handler_name: str, *args, **kwargs
    ) -> AsyncGenerator[MessageEventResult, None]:
        """统一命令入口：就绪检查 + 委托 command_handler。

        命令函数自身的参数签名保持不变（AstrBot 依赖签名解析命令参数），
        这里只收敛重复的门禁与迭代样板。

        Args:
            event: 命令事件。
            handler_name: CommandHandler 上的处理方法名。
            *args: 传给处理方法的额外位置参数。
            **kwargs: 传给处理方法的额外关键字参数。

        Yields:
            处理方法产生的消息结果；未就绪时产生提示消息。
        """
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return
        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return
        handler = getattr(self.command_handler, handler_name)
        messages = handler(event, *args, **kwargs)
        try:
            while True:
                with adapter_access(
                    self.config_manager, event, write=handler_name == "handle_summarize"
                ):
                    try:
                        item = await anext(messages)
                    except StopAsyncIteration:
                        return
                yield item
        finally:
            with adapter_access(
                self.config_manager, event, write=handler_name == "handle_summarize"
            ):
                await messages.aclose()


    # ==================== 事件钩子 ====================

    @filter.custom_filter(PassiveGroupCaptureFilter, False)
    async def handle_all_group_messages(self, event: AstrMessageEvent):
        """[Passive Filter Hook] Capture group messages without waking AstrBot."""
        # PassiveGroupCaptureFilter schedules the capture task and always returns
        # False, so AstrBot will not invoke this handler or mark the event as wake.
        return

    @filter.on_llm_request()
    async def handle_memory_recall(self, event: AstrMessageEvent, req: ProviderRequest):
        """[Event Hook] Query and inject long-term memory before LLM request"""
        ready, _ = await self._ensure_plugin_ready(wait=False)
        if not ready:
            logger.debug("插件未完成初始化，跳过记忆召回")
            return

        if not self.event_handler:
            return

        await self.event_handler.handle_memory_recall(event, req)

    @filter.on_llm_response()
    async def handle_memory_reflection(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        """[Event Hook] Check if reflection and memory storage is needed after LLM response"""
        ready, _ = await self._ensure_plugin_ready(wait=False)
        if not ready:
            logger.debug("插件未完成初始化，跳过记忆反思")
            return

        if not self.event_handler:
            return

        await self.event_handler.handle_memory_reflection(event, resp)

    @filter.after_message_sent()
    async def handle_session_reset(self, event: AstrMessageEvent, *_args):
        """[Event Hook] After message sent, check if plugin session context needs clearing (/reset or /new)"""
        if not (
            event.get_extra("_clean_group_context_session", False)
            or event.get_extra("_clean_ltm_session", False)
        ):
            return

        ready, _ = await self._ensure_plugin_ready(wait=False)
        if not ready:
            return

        if not self.event_handler:
            return

        await self.event_handler.handle_session_reset(event)

    # ==================== 命令处理 ====================

    @filter.command_group("lmem")
    def lmem(self):
        """Long-term memory management command group /lmem"""
        pass

    @permission_type(PermissionType.ADMIN)
    @lmem.command("status", priority=10)
    async def status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Show memory system status"""
        async for message in self._run_command(event, "handle_status"):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("search", priority=10)
    async def search(
        self, event: AstrMessageEvent, query: str, k: int = 5
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Search memories"""
        async for message in self._run_command(event, "handle_search", query, k):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("forget")
    async def forget(
        self, event: AstrMessageEvent, doc_id: int
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Delete specified memory"""
        async for message in self._run_command(event, "handle_forget", doc_id):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("rebuild-index")
    async def rebuild_index(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Manually rebuild index"""
        async for message in self._run_command(event, "handle_rebuild_index"):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("rebuild-graph")
    async def rebuild_graph(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Manually rebuild graph memory index"""
        async for message in self._run_command(event, "handle_rebuild_graph"):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("migrate-scope")
    async def migrate_scope(
        self,
        event: AstrMessageEvent,
        target: str = "",
        action: str = "",
        source: str = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Migrate existing memories to the given memory scope"""
        async for message in self._run_command(
            event, "handle_migrate_scope", target, action, source
        ):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("webui")
    async def webui(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Show WebUI access information"""
        async for message in self._run_command(event, "handle_webui"):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("summarize")
    async def summarize(
        self, event: AstrMessageEvent, message_count: int | None = None
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Immediately trigger memory summarization for current session"""
        async for message in self._run_command(
            event, "handle_summarize", message_count
        ):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("reset")
    async def reset(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Reset long-term memory context for current session"""
        async for message in self._run_command(event, "handle_reset"):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("cleanup")
    async def cleanup(
        self, event: AstrMessageEvent, mode: str = "preview"
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Clean up memory injection fragments from historical messages

        Args:
            mode: Execution mode, "preview" (default) for rehearsal, "exec" for actual cleanup
        """
        # 判断是否为执行模式
        dry_run = mode.lower() != "exec"

        async for message in self._run_command(
            event, "handle_cleanup", dry_run=dry_run
        ):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("help")
    async def help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Show help information"""
        async for message in self._run_command(event, "handle_help"):
            yield message

    # ==================== 生命周期管理 ====================

    async def terminate(self):
        """Cleanup logic when plugin stops"""
        logger.info("LivingMemory 插件正在停止...")
        self._terminating = True
        if get_active_plugin() is self:
            set_active_plugin(None)

        # 取消所有后台任务
        if self._background_tasks:
            logger.info(f"正在取消 {len(self._background_tasks)} 个后台任务...")
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # 停止初始化后台任务（如Provider重试）
        await self.initializer.stop_background_tasks()

        # 通知EventHandler停止（如果有正在运行的存储任务）
        if self.event_handler:
            await self.event_handler.shutdown()

        # 停止衰减调度器
        await self.initializer.stop_scheduler()

        # 关闭 ConversationManager
        if (
            self.initializer.conversation_manager
            and self.initializer.conversation_manager.store
        ):
            await self.initializer.conversation_manager.store.close()
            logger.info("ConversationManager 已关闭")

        # 关闭 FaissVecDB 之前，先把索引的未落盘变更写掉
        await self.initializer.shutdown_index_persisters()

        # 关闭 MemoryEngine
        if self.initializer.memory_engine:
            await self.initializer.memory_engine.close()
            logger.info("MemoryEngine 已关闭")

        # 关闭 FaissVecDB
        if self.initializer.db:
            await self.initializer.db.close()
            logger.info("FaissVecDB 已关闭")

        if getattr(self.initializer, "embedding_cache", None) is not None:
            await self.initializer.embedding_cache.close()

        logger.info("LivingMemory 插件已成功停止。")
