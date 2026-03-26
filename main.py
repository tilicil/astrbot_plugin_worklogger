"""Work Logger Plugin for AstrBot.

Allows users to record daily work content via natural language or commands,
generates daily / weekly / monthly reports (optionally with LLM summaries),
and can push image-format reports to configured sessions on a schedule.

Commands:
    /worklog add <content>           -- Record a work log entry.
    /worklog today                   -- Show today's log entries.
    /worklog report [daily|weekly|monthly] -- Generate a report on demand.
    /worklog session                 -- Display the current session ID.
    /worklog help                    -- Show usage help.

LLM Tool:
    The plugin also registers a function tool so that the LLM can
    automatically record work content when the user describes their work
    in natural language.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import AsyncGenerator

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .report_generator import ReportGenerator
from .storage import WorkLogStorage

_LLM_SYSTEM_PROMPT = (
    "你是一位专业的工作助理，擅长将零散的工作记录整理成结构清晰、语言简练的总结报告。"
    "请直接输出报告内容，不要添加多余的解释或元评论。"
)

_LLM_USER_PROMPT_TMPL = """\
请根据以下{period}的原始工作记录，生成一份专业的工作总结报告。

要求：
1. 提炼工作重点，归纳主要成果。
2. 对工作内容进行合理分类（如：功能开发、问题修复、会议协作等）。
3. 保留原有的日期和人员信息。
4. 语言简洁专业，格式清晰便于阅读。
5. 在报告开头注明汇报周期。

原始工作记录：
{raw_text}

请输出工作总结报告："""

_HELP_TEXT = """📋 工作记录插件使用说明

【记录工作】
• 自然语言：直接告诉bot你今天做了什么（需要配置LLM）
• 命令方式：/worklog add <工作内容>

【查看记录】
• /worklog today         — 查看今日工作记录
• /worklog session       — 查看当前会话ID（用于配置推送）

【删除记录】
• /worklog delete <编号> — 删除今日指定编号的记录
• 自然语言：告诉bot删除第N条记录（需要LLM）

【生成报告】
• /worklog report daily   — 生成昨日工作日报
• /worklog report weekly  — 生成上周工作周报
• /worklog report monthly — 生成上月工作月报

【会话组（同一人多会话合并）】
• /worklog group info              — 查看当前会话所在的组
• /worklog group list              — 列出所有会话组
• /worklog group join <组名>       — 将当前会话加入指定组（组不存在则自动创建）
• /worklog group leave             — 将当前会话退出所在组
• /worklog group delete <组名>     — 删除整个会话组（慎用）
同一组内的所有会话记录在查询/报告时将自动合并。

【自动推送】
在WebUI的插件配置中开启自动推送并设置推送会话ID和时间。"""

_MAX_GROUP_NAME_LEN = 50


def _valid_group_name(name: str) -> bool:
    """Return True if *name* (already stripped) is an acceptable group name."""
    return bool(name) and len(name) <= _MAX_GROUP_NAME_LEN and name.isprintable()


class Main(star.Star):
    """工作记录插件，支持自然语言记录工作并生成日/周/月报告。"""

    def __init__(self, context: star.Context, config: dict = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        plugin_name = getattr(self, "name", "astrbot_plugin_work_logger")
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self._storage = WorkLogStorage(data_dir)
        self._report_gen = ReportGenerator(self._storage)
        self._cron_jobs: list = []
        self._load_config()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Load plugin configuration via self.config (provided by AstrBot)."""
        cfg = getattr(self, "config", None) or {}
        raw_sessions = cfg.get("push_sessions", [])
        if isinstance(raw_sessions, list):
            self._push_sessions: list[str] = [
                stripped
                for s in raw_sessions
                if isinstance(s, str) and (stripped := s.strip())
            ]
        else:
            # Backward compatibility: old comma-separated string format.
            self._push_sessions = [
                s.strip() for s in str(raw_sessions).split(",") if s.strip()
            ]
        self._auto_daily: bool = bool(cfg.get("auto_daily_enabled", False))
        self._daily_time: str = str(cfg.get("daily_push_time", "09:00"))
        self._auto_weekly: bool = bool(cfg.get("auto_weekly_enabled", False))
        self._weekly_cron: str = str(cfg.get("weekly_push_cron", "0 9 * * 1"))
        self._auto_monthly: bool = bool(cfg.get("auto_monthly_enabled", False))
        self._monthly_cron: str = str(cfg.get("monthly_push_cron", "0 9 1 * *"))
        self._ai_enabled: bool = bool(cfg.get("ai_summary_enabled", True))
        self._ai_provider_id: str = str(cfg.get("ai_provider_id", ""))
        self._use_image: bool = bool(cfg.get("use_image_report", True))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Register cron jobs when the plugin is loaded or reloaded."""
        await self._register_cron_jobs()

    async def terminate(self) -> None:
        """Clean up cron jobs when the plugin is disabled or reloaded."""
        for job in list(self._cron_jobs):
            try:
                await self.context.cron_manager.delete_job(job.job_id)
            except Exception as exc:
                logger.debug(f"[work_logger] Failed to remove cron job: {exc}")
        self._cron_jobs.clear()

    # ------------------------------------------------------------------
    # Cron job management
    # ------------------------------------------------------------------

    async def _register_cron_jobs(self) -> None:
        """Register (or re-register) scheduled push jobs based on current config."""
        # Remove any previously registered jobs first.
        await self.terminate()

        if self._auto_daily:
            await self._add_cron(
                name="work_logger_daily",
                cron_expr=self._daily_cron_expr(),
                handler=self._auto_push_daily,
                desc="工作记录插件 — 每日自动推送日报",
            )

        if self._auto_weekly:
            await self._add_cron(
                name="work_logger_weekly",
                cron_expr=self._weekly_cron,
                handler=self._auto_push_weekly,
                desc="工作记录插件 — 每周自动推送周报",
            )

        if self._auto_monthly:
            await self._add_cron(
                name="work_logger_monthly",
                cron_expr=self._monthly_cron,
                handler=self._auto_push_monthly,
                desc="工作记录插件 — 每月自动推送月报",
            )

    def _daily_cron_expr(self) -> str:
        """Convert HH:MM string to a cron expression for daily scheduling."""
        try:
            parts = self._daily_time.strip().split(":")
            h = int(parts[0])
            m = int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("Out of range")
            return f"{m} {h} * * *"
        except Exception:
            logger.warning(
                f"[work_logger] Invalid daily_push_time '{self._daily_time}', "
                "falling back to 09:00."
            )
            return "0 9 * * *"

    async def _add_cron(
        self,
        name: str,
        cron_expr: str,
        handler,
        desc: str,
    ) -> None:
        try:
            job = await self.context.cron_manager.add_basic_job(
                name=name,
                cron_expression=cron_expr,
                handler=handler,
                description=desc,
                timezone="Asia/Shanghai",
            )
            self._cron_jobs.append(job)
            logger.info(f"[work_logger] Registered cron job '{name}' ({cron_expr})")
        except Exception as exc:
            logger.error(f"[work_logger] Failed to register cron job '{name}': {exc}")

    # ------------------------------------------------------------------
    # LLM tool — natural language work recording
    # ------------------------------------------------------------------

    @filter.llm_tool(
        name="record_work_log",
        desc=(
            "当用户想要记录今日工作内容、汇报工作进展、登记完成事项时调用此工具。"
            "该工具会将工作内容保存到本地记录中供后续生成日报使用。"
        ),
    )
    async def record_work_log(
        self,
        event: AstrMessageEvent,
        content: str,
    ) -> str:
        """Record a work log entry extracted from the user's message.

        Args:
            content(string): Complete description of the user's work content.
        """
        if not content or not content.strip():
            return "工作内容不能为空，请重新描述。"
        content = content.strip()
        await self._storage.add_log(
            session=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name(),
            content=content,
        )
        logger.info(
            f"[work_logger] Recorded via LLM tool: "
            f"{event.get_sender_name()} @ {event.unified_msg_origin}: {content[:60]}"
        )
        return f"✅ 已记录工作内容：{content}"

    @filter.llm_tool(
        name="delete_work_log",
        desc=(
            "当用户想要删除某条工作记录、撤销已记录的工作内容、取消一条记录时调用此工具。"
            "调用前请先通过今日工作记录确认要删除的条目编号。"
        ),
    )
    async def delete_work_log(
        self,
        event: AstrMessageEvent,
        entry_index: int,
        date_str: str = "",
    ) -> str:
        """Delete a work log entry by its 1-based index.

        Args:
            entry_index(int): 1-based index of the entry to delete (as shown by /worklog today).
            date_str(string): Date in YYYY-MM-DD format. Leave empty to default to today.
        """
        if entry_index < 1:
            return "编号必须大于 0，请先使用 /worklog today 查看当前记录编号。"
        if date_str:
            try:
                log_date = date.fromisoformat(date_str)
            except ValueError:
                return f"日期格式错误：{date_str}，请使用 YYYY-MM-DD 格式。"
        else:
            log_date = date.today()
        sessions = self._storage.resolve_sessions(event.unified_msg_origin)
        success = await self._storage.delete_log_by_global_index(
            sessions=sessions,
            log_date=log_date,
            global_index=entry_index,
        )
        if success:
            return f"✅ 已删除 {log_date} 的第 {entry_index} 条工作记录。"
        return (
            f"❌ 删除失败，请重试或使用 /worklog today 查看当前记录。"
        )

    # ------------------------------------------------------------------
    # Command group
    # ------------------------------------------------------------------

    @filter.command_group("worklog")
    def worklog(self) -> None:
        """工作记录指令组"""

    @worklog.command("add")
    async def worklog_add(
        self,
        event: AstrMessageEvent,
        content: str = GreedyStr,
    ) -> AsyncGenerator:
        """手动记录工作内容。用法: /worklog add <工作内容>"""
        if not content or not content.strip():
            yield event.plain_result(
                "请输入工作内容。\n用法：/worklog add <工作内容>\n"
                "例如：/worklog add 完成了用户登录模块的开发"
            )
            return
        content = content.strip()
        await self._storage.add_log(
            session=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name(),
            content=content,
        )
        logger.info(
            f"[work_logger] Recorded via command: "
            f"{event.get_sender_name()} @ {event.unified_msg_origin}: {content[:60]}"
        )
        yield event.plain_result(f"✅ 工作内容已记录：{content}")

    @worklog.command("today")
    async def worklog_today(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查看今日工作记录。用法: /worklog today"""
        sessions = self._storage.resolve_sessions(event.unified_msg_origin)
        raw_text, has_data = self._report_gen.get_daily_raw(sessions, date.today())
        if not has_data:
            yield event.plain_result(
                "📭 今天还没有工作记录。\n"
                "• 直接告诉bot你做了什么（需要LLM），或\n"
                "• 使用 /worklog add <内容> 添加记录。"
            )
            return
        yield event.plain_result(raw_text)

    @worklog.command("report")
    async def worklog_report(
        self,
        event: AstrMessageEvent,
        report_type: str = "daily",
    ) -> AsyncGenerator:
        """生成工作报告。用法: /worklog report [daily|weekly|monthly]

        report_type: 报告类型，daily / weekly / monthly（或 日报 / 周报 / 月报）
        """
        alias_map = {
            "日报": "daily",
            "周报": "weekly",
            "月报": "monthly",
            "d": "daily",
            "w": "weekly",
            "m": "monthly",
        }
        report_type = alias_map.get(report_type.strip(), report_type.strip().lower())
        if report_type not in ("daily", "weekly", "monthly"):
            yield event.plain_result(
                "报告类型不正确。请使用：\n"
                "  /worklog report daily   — 昨日日报\n"
                "  /worklog report weekly  — 上周周报\n"
                "  /worklog report monthly — 上月月报"
            )
            return

        yield event.plain_result("⏳ 正在生成报告，请稍候…")

        raw_text, has_data, period_label = self._prepare_report(
            self._storage.resolve_sessions(event.unified_msg_origin), report_type
        )

        if not has_data:
            yield event.plain_result(raw_text)
            return

        final_text = await self._maybe_enhance_with_llm(
            event.unified_msg_origin, raw_text, period_label
        )

        if self._use_image:
            try:
                img_url = await self.text_to_image(final_text)
                yield event.image_result(img_url)
            except Exception as exc:
                logger.error(f"[work_logger] text_to_image failed: {exc}")
                yield event.plain_result(final_text)
        else:
            yield event.plain_result(final_text)

    @worklog.command("session")
    async def worklog_session(self, event: AstrMessageEvent) -> AsyncGenerator:
        """显示当前会话ID，用于配置自动推送目标。用法: /worklog session"""
        yield event.plain_result(
            f"📌 当前会话ID：\n{event.unified_msg_origin}\n\n"
            "将此ID填写到插件配置的「推送目标会话」中即可接收自动推送报告。"
        )

    @worklog.command("delete")
    async def worklog_delete(
        self,
        event: AstrMessageEvent,
        index: int,
    ) -> AsyncGenerator:
        """删除今日指定编号的工作记录。用法: /worklog delete <编号>

        index: 要删除的记录编号（使用 /worklog today 查看编号）
        """
        if index < 1:
            yield event.plain_result(
                "❌ 编号必须大于 0。\n"
                "请先使用 /worklog today 查看记录编号。"
            )
            return
        sessions = self._storage.resolve_sessions(event.unified_msg_origin)
        success = await self._storage.delete_log_by_global_index(
            sessions=sessions,
            log_date=date.today(),
            global_index=index,
        )
        if success:
            yield event.plain_result(f"✅ 已删除今日第 {index} 条工作记录。")
        else:
            yield event.plain_result(
                f"❌ 未找到编号为 {index} 的记录，"
                f"请使用 /worklog today 查看当前记录编号。"
            )

    @worklog.command("help")
    async def worklog_help(self, event: AstrMessageEvent) -> AsyncGenerator:
        """显示帮助信息。用法: /worklog help"""
        yield event.plain_result(_HELP_TEXT)

    # ------------------------------------------------------------------
    # Session group subcommands
    # ------------------------------------------------------------------

    @worklog.group("group")
    def worklog_group(self) -> None:
        """会话组管理指令组"""

    @worklog_group.command("info")
    async def group_info(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查看当前会话所在的会话组。用法: /worklog group info"""
        session = event.unified_msg_origin
        group = self._storage.get_group_for_session(session)
        if group is None:
            yield event.plain_result(
                f"📌 当前会话未加入任何组。\n"
                f"会话ID：{session}\n\n"
                "使用 /worklog group join <组名> 加入或创建一个组。"
            )
            return
        members = self._storage.get_group_sessions(group)
        lines = [f"👥 当前会话所在组：{group}", f"组内共 {len(members)} 个会话："]
        for m in members:
            marker = "（当前）" if m == session else ""
            lines.append(f"  • {m}{marker}")
        yield event.plain_result("\n".join(lines))

    @worklog_group.command("list")
    async def group_list(self, event: AstrMessageEvent) -> AsyncGenerator:
        """列出所有会话组。用法: /worklog group list"""
        groups = self._storage.get_all_groups()
        if not groups:
            yield event.plain_result(
                "📭 当前没有任何会话组。\n"
                "使用 /worklog group join <组名> 创建并加入一个组。"
            )
            return
        lines = [f"👥 会话组列表（共 {len(groups)} 个）："]
        for name, members in groups.items():
            lines.append(f"\n  📂 {name}（{len(members)} 个会话）")
            for m in members:
                lines.append(f"    • {m}")
        yield event.plain_result("\n".join(lines))

    @worklog_group.command("join")
    async def group_join(
        self,
        event: AstrMessageEvent,
        group_name: str = GreedyStr,
    ) -> AsyncGenerator:
        """将当前会话加入指定组（组不存在则自动创建）。用法: /worklog group join <组名>"""
        if not group_name or not group_name.strip():
            yield event.plain_result(
                "请提供组名。\n用法：/worklog group join <组名>"
            )
            return
        group_name = group_name.strip()
        if not _valid_group_name(group_name):
            yield event.plain_result(
                f"❌ 组名无效。组名不能为空、不能超过 {_MAX_GROUP_NAME_LEN} 个字符，且不含控制字符。"
            )
            return
        session = event.unified_msg_origin
        ok, reason = self._storage.add_to_group(group_name, session)
        if ok:
            members = self._storage.get_group_sessions(group_name)
            yield event.plain_result(
                f"✅ 已将当前会话加入组 '{group_name}'。\n"
                f"该组现有 {len(members)} 个会话，查询记录时将自动合并所有成员的记录。"
            )
        else:
            yield event.plain_result(f"❌ 加入失败：{reason}")

    @worklog_group.command("leave")
    async def group_leave(self, event: AstrMessageEvent) -> AsyncGenerator:
        """将当前会话退出所在的会话组。用法: /worklog group leave"""
        session = event.unified_msg_origin
        ok, group_name = self._storage.remove_from_group(session)
        if ok:
            yield event.plain_result(
                f"✅ 已将当前会话从组 '{group_name}' 中移除。\n"
                "如果组内已无成员，该组会被自动删除。"
            )
        else:
            yield event.plain_result(
                "❌ 当前会话不在任何组中。\n"
                "使用 /worklog group join <组名> 加入一个组。"
            )

    @worklog_group.command("delete")
    async def group_delete(
        self,
        event: AstrMessageEvent,
        group_name: str = GreedyStr,
    ) -> AsyncGenerator:
        """删除整个会话组。用法: /worklog group delete <组名>"""
        if not group_name or not group_name.strip():
            yield event.plain_result(
                "请提供要删除的组名。\n用法：/worklog group delete <组名>"
            )
            return
        group_name = group_name.strip()
        if not _valid_group_name(group_name):
            yield event.plain_result(
                f"❌ 组名无效。组名不能为空、不能超过 {_MAX_GROUP_NAME_LEN} 个字符，且不含控制字符。"
            )
            return
        ok = self._storage.delete_group(group_name)
        if ok:
            yield event.plain_result(
                f"✅ 会话组 '{group_name}' 已删除。\n"
                "组内所有会话已脱离该组，但其历史记录不受影响。"
            )
        else:
            yield event.plain_result(
                f"❌ 未找到组 '{group_name}'。\n"
                "使用 /worklog group list 查看现有组。"
            )

    # ------------------------------------------------------------------
    # Scheduled push handlers
    # ------------------------------------------------------------------

    async def _auto_push_daily(self) -> None:
        """Push yesterday's daily report to all configured sessions."""
        yesterday = date.today() - timedelta(days=1)
        logger.info(f"[work_logger] Auto pushing daily report for {yesterday}")
        for session in self._push_sessions:
            await self._push_report(session, "daily")

    async def _auto_push_weekly(self) -> None:
        """Push last week's weekly report to all configured sessions."""
        today = date.today()
        last_monday = today - timedelta(days=today.weekday() + 7)
        logger.info(f"[work_logger] Auto pushing weekly report (from {last_monday})")
        for session in self._push_sessions:
            await self._push_report(session, "weekly")

    async def _auto_push_monthly(self) -> None:
        """Push last month's monthly report to all configured sessions."""
        today = date.today()
        if today.month == 1:
            year, month = today.year - 1, 12
        else:
            year, month = today.year, today.month - 1
        logger.info(f"[work_logger] Auto pushing monthly report for {year}-{month:02d}")
        for session in self._push_sessions:
            await self._push_report(session, "monthly")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prepare_report(
        self,
        sessions: list[str],
        report_type: str,
    ) -> tuple[str, bool, str]:
        """Return (raw_text, has_data, period_label) for the given report type.

        For auto-push scenarios the report covers the *previous* period:
        - daily  → yesterday
        - weekly → last week (Monday-based)
        - monthly → last calendar month
        """
        today = date.today()

        if report_type == "daily":
            yesterday = today - timedelta(days=1)
            raw_text, has_data = self._report_gen.get_daily_raw(sessions, yesterday)
            return raw_text, has_data, "昨日"

        if report_type == "weekly":
            last_monday = today - timedelta(days=today.weekday() + 7)
            raw_text, has_data = self._report_gen.get_weekly_raw(sessions, last_monday)
            return raw_text, has_data, "上周"

        # monthly
        if today.month == 1:
            year, month = today.year - 1, 12
        else:
            year, month = today.year, today.month - 1
        raw_text, has_data = self._report_gen.get_monthly_raw(sessions, year, month)
        return raw_text, has_data, "上月"

    async def _maybe_enhance_with_llm(
        self,
        session: str,
        raw_text: str,
        period: str,
    ) -> str:
        """Return LLM-enhanced text, or raw_text if LLM is unavailable."""
        if not self._ai_enabled:
            return raw_text

        prompt = _LLM_USER_PROMPT_TMPL.format(period=period, raw_text=raw_text)
        try:
            if self._ai_provider_id:
                # Use the explicitly configured provider by ID.
                resp = await self.context.llm_generate(
                    chat_provider_id=self._ai_provider_id,
                    prompt=prompt,
                    system_prompt=_LLM_SYSTEM_PROMPT,
                )
            else:
                # Fall back to the session's current default provider.
                provider = self.context.get_using_provider(session)
                if provider is None:
                    return raw_text
                resp = await provider.text_chat(
                    prompt=prompt,
                    system_prompt=_LLM_SYSTEM_PROMPT,
                )
            text = resp.completion_text if resp else ""
            return text.strip() if text and text.strip() else raw_text
        except Exception as exc:
            logger.warning(f"[work_logger] LLM summary failed: {exc}")
            return raw_text

    async def _push_report(self, session: str, report_type: str) -> None:
        """Generate and push a report to a single *session*."""
        raw_text, has_data, period_label = self._prepare_report(
            self._storage.resolve_sessions(session), report_type
        )

        if not has_data:
            try:
                await self.context.send_message(
                    session=session,
                    message_chain=MessageChain(
                        [Plain(f"📭 {period_label}暂无工作记录。")]
                    ),
                )
            except Exception as exc:
                logger.warning(
                    f"[work_logger] Failed to send 'no data' notice to {session}: {exc}"
                )
            return

        final_text = await self._maybe_enhance_with_llm(session, raw_text, period_label)

        if self._use_image:
            try:
                img_url = await self.text_to_image(final_text)
                await self.context.send_message(
                    session=session,
                    message_chain=MessageChain([Image.fromURL(img_url)]),
                )
                logger.info(
                    f"[work_logger] Pushed {report_type} report image to {session}"
                )
            except Exception as exc:
                logger.error(
                    f"[work_logger] Failed to push {report_type} report to {session}: {exc}"
                )
                # Fallback: send as plain text
                try:
                    await self.context.send_message(
                        session=session,
                        message_chain=MessageChain([Plain(final_text)]),
                    )
                except Exception as plain_exc:
                    logger.error(
                        f"[work_logger] Fallback plain text send also failed: {plain_exc}"
                    )
        else:
            try:
                await self.context.send_message(
                    session=session,
                    message_chain=MessageChain([Plain(final_text)]),
                )
                logger.info(
                    f"[work_logger] Pushed {report_type} report text to {session}"
                )
            except Exception as exc:
                logger.error(
                    f"[work_logger] Failed to push {report_type} report to {session}: {exc}"
                )
