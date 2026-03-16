import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Star, register
from astrbot.core.star.filter.command import GreedyStr


@dataclass(slots=True)
class ReplyRule:
    name: str
    pattern: str
    reply: str
    match_type: str
    groups: list[str]
    exclude_keywords: list[str]
    ignore_case: bool
    continue_after_reply: bool
    cooldown_seconds: float
    priority: int
    max_reply_count: int
    reply_count: int
    compiled_pattern: re.Pattern[str] | None = None


class SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@register(
    "GroupKeywordReply",
    "Codex",
    "按规则匹配群消息并自动发送预设回复",
    "1.3.0",
    "https://github.com/taolicx/astrbot_plugin_group_keyword_reply",
)
class GroupKeywordReplyPlugin(Star):
    def __init__(self, context: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.plugin_dir = Path(__file__).resolve().parent
        self.webui_dir = self.plugin_dir / "webui"
        self._rules_cache_key = ""
        self._rules_cache: list[ReplyRule] = []
        self._last_trigger_at: dict[str, float] = {}
        self._web_runner: web.AppRunner | None = None
        self._web_site: web.TCPSite | None = None
        self._web_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._start_webui_server()

    async def terminate(self) -> None:
        await self._stop_webui_server()

    def _save_plugin_config(self, updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            self.config[key] = value

        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()

    def _plugin_enabled(self) -> bool:
        value = self.config.get("enabled", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _ignore_self_messages(self) -> bool:
        value = self.config.get("ignore_self_messages", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _group_whitelist(self) -> list[str]:
        return self._parse_groups(self.config.get("group_whitelist", []))

    def _webui_host(self) -> str:
        value = str(self.config.get("webui_host", "127.0.0.1") or "").strip()
        return value or "127.0.0.1"

    def _webui_port(self) -> int:
        value = self.config.get("webui_port", 18082)
        try:
            return max(1, min(int(value), 65535))
        except (TypeError, ValueError):
            return 18082

    def _display_host(self) -> str:
        host = self._webui_host()
        if host in {"0.0.0.0", "::"}:
            return "127.0.0.1"
        return host

    def _webui_url(self) -> str:
        return f"http://{self._display_host()}:{self._webui_port()}"

    def _raw_rules(self) -> Any:
        return self.config.get("rules_json", "[]")

    def _make_rules_cache_key(self, raw_rules: Any) -> str:
        if isinstance(raw_rules, str):
            return raw_rules
        try:
            return json.dumps(raw_rules, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(raw_rules)

    def _parse_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _parse_on_off(self, value: str) -> bool | None:
        text = str(value or "").strip().lower()
        if text in {"on", "true", "1", "yes", "开", "开启"}:
            return True
        if text in {"off", "false", "0", "no", "关", "关闭"}:
            return False
        return None

    def _parse_groups(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            return [item.strip() for item in re.split(r"[,，]", text) if item.strip()]
        return [str(value).strip()]

    def _normalize_match_type(self, value: Any) -> str:
        match_type = str(value or "keyword").strip().lower()
        aliases = {
            "contains": "keyword",
            "keyword": "keyword",
            "exact": "exact",
            "full": "exact",
            "regex": "regex",
            "re": "regex",
        }
        return aliases.get(match_type, "keyword")

    def _raw_rule_items(self) -> list[dict[str, Any]]:
        raw_rules = self._raw_rules()
        if isinstance(raw_rules, list):
            return [item for item in raw_rules if isinstance(item, dict)]

        text = str(raw_rules or "").strip()
        if not text:
            return []

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning(f"GroupKeywordReply rules_json is invalid JSON: {exc}")
            return []

        if not isinstance(data, list):
            logger.warning("GroupKeywordReply rules_json must be a JSON array.")
            return []

        return [item for item in data if isinstance(item, dict)]

    def _save_rule_items(self, items: list[dict[str, Any]]) -> None:
        self._rules_cache_key = ""
        self._rules_cache = []
        self._save_plugin_config(
            {"rules_json": json.dumps(items, ensure_ascii=False, indent=2)}
        )

    def _increment_rule_reply_count(self, rule_name: str) -> None:
        items = self._raw_rule_items()
        changed = False
        for item in items:
            if str(item.get("name") or "").strip() == rule_name:
                try:
                    current = int(item.get("reply_count", 0))
                except (TypeError, ValueError):
                    current = 0
                item["reply_count"] = current + 1
                changed = True
                break
        if changed:
            self._save_rule_items(items)

    def _is_reply_limit_reached(self, rule: ReplyRule) -> bool:
        return rule.max_reply_count > 0 and rule.reply_count >= rule.max_reply_count

    def _load_rules(self) -> list[ReplyRule]:
        raw_rules = self._raw_rules()
        cache_key = self._make_rules_cache_key(raw_rules)
        if cache_key == self._rules_cache_key:
            return self._rules_cache

        parsed_rules = self._raw_rule_items()
        rules: list[ReplyRule] = []

        for index, item in enumerate(parsed_rules):
            enabled = self._parse_bool(item.get("enabled", True), True)
            if not enabled:
                continue

            name = str(item.get("name") or f"rule_{index + 1}").strip()
            pattern = str(item.get("pattern") or "").strip()
            reply = str(item.get("reply") or "").strip()
            if not pattern or not reply:
                logger.warning(
                    f"GroupKeywordReply skipped rule '{name}' because pattern or reply is empty."
                )
                continue

            match_type = self._normalize_match_type(item.get("match_type", "keyword"))
            ignore_case = self._parse_bool(item.get("ignore_case", True), True)
            continue_after_reply = self._parse_bool(
                item.get("continue_after_reply", False), False
            )
            groups = self._parse_groups(item.get("groups", []))
            exclude_keywords = self._parse_groups(item.get("exclude_keywords", []))

            try:
                cooldown_seconds = max(float(item.get("cooldown_seconds", 0)), 0.0)
            except (TypeError, ValueError):
                cooldown_seconds = 0.0

            try:
                priority = int(item.get("priority", index + 1))
            except (TypeError, ValueError):
                priority = index + 1

            try:
                max_reply_count = max(int(item.get("max_reply_count", 0)), 0)
            except (TypeError, ValueError):
                max_reply_count = 0

            try:
                reply_count = max(int(item.get("reply_count", 0)), 0)
            except (TypeError, ValueError):
                reply_count = 0

            compiled_pattern: re.Pattern[str] | None = None
            if match_type == "regex":
                flags = re.IGNORECASE if ignore_case else 0
                try:
                    compiled_pattern = re.compile(pattern, flags)
                except re.error as exc:
                    logger.warning(
                        f"GroupKeywordReply skipped regex rule '{name}' because it is invalid: {exc}"
                    )
                    continue

            rules.append(
                ReplyRule(
                    name=name,
                    pattern=pattern,
                    reply=reply,
                    match_type=match_type,
                    groups=groups,
                    exclude_keywords=exclude_keywords,
                    ignore_case=ignore_case,
                    continue_after_reply=continue_after_reply,
                    cooldown_seconds=cooldown_seconds,
                    priority=priority,
                    max_reply_count=max_reply_count,
                    reply_count=reply_count,
                    compiled_pattern=compiled_pattern,
                )
            )

        rules.sort(key=lambda item: item.priority)
        self._rules_cache_key = cache_key
        self._rules_cache = rules
        return self._rules_cache

    def _build_reply_text(
        self,
        rule: ReplyRule,
        event: AstrMessageEvent,
        original_text: str,
        match_result: re.Match[str] | bool,
    ) -> str:
        values: SafeFormatDict = SafeFormatDict(
            {
                "rule_name": rule.name,
                "message": original_text,
                "sender_id": event.get_sender_id(),
                "sender_name": event.get_sender_name(),
                "group_id": event.get_group_id(),
                "platform_name": event.get_platform_name(),
                "platform_id": event.get_platform_id(),
            }
        )

        if isinstance(match_result, re.Match):
            values["match"] = match_result.group(0)
            for idx, group in enumerate(match_result.groups(), start=1):
                values[f"g{idx}"] = group or ""
            for key, value in match_result.groupdict().items():
                values[key] = value or ""

        return rule.reply.format_map(values)

    def _cooldown_key(self, group_id: str, rule_name: str) -> str:
        return f"{group_id}:{rule_name}"

    def _is_in_cooldown(self, group_id: str, rule: ReplyRule) -> bool:
        if rule.cooldown_seconds <= 0:
            return False
        key = self._cooldown_key(group_id, rule.name)
        last_trigger_at = self._last_trigger_at.get(key, 0.0)
        return (time.time() - last_trigger_at) < rule.cooldown_seconds

    def _mark_triggered(self, group_id: str, rule: ReplyRule) -> None:
        if rule.cooldown_seconds <= 0:
            return
        self._last_trigger_at[self._cooldown_key(group_id, rule.name)] = time.time()

    def _group_matches(self, rule: ReplyRule, group_id: str) -> bool:
        if not rule.groups:
            return True
        return group_id in rule.groups

    def _has_excluded_keyword(self, rule: ReplyRule, text: str) -> bool:
        source = text.casefold() if rule.ignore_case else text
        for keyword in rule.exclude_keywords:
            target = keyword.casefold() if rule.ignore_case else keyword
            if target and target in source:
                return True
        return False

    def _match_rule(self, rule: ReplyRule, text: str) -> re.Match[str] | bool:
        if rule.match_type == "regex":
            if rule.compiled_pattern is None:
                return False
            return rule.compiled_pattern.search(text) or False

        source = text.casefold() if rule.ignore_case else text
        pattern = rule.pattern.casefold() if rule.ignore_case else rule.pattern

        if rule.match_type == "exact":
            return source == pattern
        return pattern in source

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        index_path = self.webui_dir / "index.html"
        if not index_path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(index_path)

    async def _handle_state(self, request: web.Request) -> web.Response:
        payload = {
            "enabled": self._plugin_enabled(),
            "ignore_self_messages": self._ignore_self_messages(),
            "group_whitelist": self._group_whitelist(),
            "rules": self._raw_rule_items(),
            "webui_url": self._webui_url(),
        }
        return web.json_response(payload)

    async def _handle_save(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "请求体不是合法 JSON。"}, status=400)

        enabled = self._parse_bool(payload.get("enabled", True), True)
        ignore_self = self._parse_bool(payload.get("ignore_self_messages", True), True)
        whitelist = self._parse_groups(payload.get("group_whitelist", []))
        rules = payload.get("rules", [])

        if not isinstance(rules, list):
            return web.json_response({"ok": False, "message": "rules 必须是数组。"}, status=400)

        for item in rules:
            if not isinstance(item, dict):
                return web.json_response({"ok": False, "message": "规则项必须是对象。"}, status=400)
            if not str(item.get("name") or "").strip():
                return web.json_response({"ok": False, "message": "规则名称不能为空。"}, status=400)
            if not str(item.get("pattern") or "").strip():
                return web.json_response({"ok": False, "message": f"规则 {item.get('name')} 的匹配内容不能为空。"}, status=400)
            if not str(item.get("reply") or "").strip():
                return web.json_response({"ok": False, "message": f"规则 {item.get('name')} 的回复内容不能为空。"}, status=400)
            try:
                max_reply_count = max(int(item.get("max_reply_count", 0)), 0)
                item["max_reply_count"] = max_reply_count
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "message": f"规则 {item.get('name')} 的最大回复次数必须是数字。"}, status=400)
            try:
                reply_count = max(int(item.get("reply_count", 0)), 0)
                item["reply_count"] = reply_count
            except (TypeError, ValueError):
                item["reply_count"] = 0
            if self._normalize_match_type(item.get("match_type", "keyword")) == "regex":
                try:
                    re.compile(str(item.get("pattern") or ""))
                except re.error as exc:
                    return web.json_response({"ok": False, "message": f"规则 {item.get('name')} 的正则无效：{exc}"}, status=400)

        self._save_plugin_config(
            {
                "enabled": enabled,
                "ignore_self_messages": ignore_self,
                "group_whitelist": ",".join(whitelist),
                "rules_json": json.dumps(rules, ensure_ascii=False, indent=2),
            }
        )
        self._rules_cache_key = ""
        self._rules_cache = []
        return web.json_response({"ok": True, "message": "保存成功。"})

    def _create_web_application(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/api/state", self._handle_state),
                web.post("/api/save", self._handle_save),
            ]
        )
        return app

    async def _start_webui_server(self) -> None:
        async with self._web_lock:
            if self._web_runner is not None:
                return

            app = self._create_web_application()
            runner = web.AppRunner(app, access_log=None)
            try:
                await runner.setup()
                site = web.TCPSite(runner, host=self._webui_host(), port=self._webui_port())
                await site.start()
            except OSError as exc:
                logger.error(f"关键词回复 WebUI 启动失败 {self._webui_host()}:{self._webui_port()}：{exc}")
                await runner.cleanup()
                return

            self._web_runner = runner
            self._web_site = site
            logger.info(f"关键词回复 WebUI 已启动：{self._webui_url()}")

    async def _stop_webui_server(self) -> None:
        async with self._web_lock:
            runner = self._web_runner
            self._web_runner = None
            self._web_site = None
            if runner is None:
                return
            await runner.cleanup()
            logger.info("关键词回复 WebUI 已停止。")

    @filter.command_group("关键词回复")
    def keyword_reply(self) -> None:
        """关键词回复管理"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("面板", alias={"webui", "panel"})
    async def open_panel(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        yield event.plain_result(f"关键词回复管理面板：\n{self._webui_url()}").use_t2i(False)

    @keyword_reply.command("状态", alias={"关键词回复状态"})
    async def keyword_reply_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        rules = self._load_rules()
        whitelist = self._group_whitelist()
        lines = [
            f"插件启用: {'是' if self._plugin_enabled() else '否'}",
            f"忽略机器人自身消息: {'是' if self._ignore_self_messages() else '否'}",
            f"插件级白名单: {','.join(whitelist) if whitelist else '未设置'}",
            f"已加载启用规则数: {len(rules)}",
            f"面板地址: {self._webui_url()}",
        ]
        if rules:
            lines.append("规则列表:")
            for rule in rules[:10]:
                group_text = ",".join(rule.groups) if rule.groups else "全部群"
                exclude_text = ",".join(rule.exclude_keywords) if rule.exclude_keywords else "无"
                limit_text = "不限" if rule.max_reply_count <= 0 else f"{rule.reply_count}/{rule.max_reply_count}"
                lines.append(
                    f"- {rule.name} [{rule.match_type}] -> {group_text} | 排除 {exclude_text} | 次数 {limit_text}"
                )
        yield event.plain_result("\n".join(lines)).use_t2i(False)

    @keyword_reply.command("列表")
    async def keyword_reply_list(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        items = self._raw_rule_items()
        if not items:
            yield event.plain_result("当前还没有任何关键词回复规则。").use_t2i(False)
            return

        lines = ["当前规则列表："]
        for item in items:
            name = str(item.get("name") or "").strip()
            match_type = self._normalize_match_type(item.get("match_type", "keyword"))
            pattern = str(item.get("pattern") or "").strip()
            enabled = self._parse_bool(item.get("enabled", True), True)
            groups = self._parse_groups(item.get("groups", []))
            excludes = self._parse_groups(item.get("exclude_keywords", []))
            try:
                reply_count = max(int(item.get("reply_count", 0)), 0)
            except (TypeError, ValueError):
                reply_count = 0
            try:
                max_reply_count = max(int(item.get("max_reply_count", 0)), 0)
            except (TypeError, ValueError):
                max_reply_count = 0
            limit_text = f"{reply_count}/{max_reply_count}" if max_reply_count > 0 else f"{reply_count}/不限"
            lines.append(
                f"- {name} | {'开' if enabled else '关'} | {match_type} | {pattern} | "
                f"{','.join(groups) if groups else '全部群'} | 排除 {','.join(excludes) if excludes else '无'} | {limit_text}"
            )
        yield event.plain_result("\n".join(lines)).use_t2i(False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("添加")
    async def keyword_reply_add(
        self, event: AstrMessageEvent, raw: GreedyStr = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        text = str(raw or "").strip()
        parts = [part.strip() for part in text.split("|")]
        if len(parts) < 4:
            yield event.plain_result(
                "用法：/关键词回复 添加 名称 | keyword/exact/regex | 匹配内容 | 回复内容 | 群号1,群号2 | 开 | 次数上限 | 排除词1,排除词2"
            )
            return

        name, match_type, pattern, reply = parts[:4]
        groups = self._parse_groups(parts[4]) if len(parts) >= 5 else []
        enabled = self._parse_on_off(parts[5]) if len(parts) >= 6 else True
        enabled = True if enabled is None else enabled
        max_reply_count = 0
        if len(parts) >= 7 and str(parts[6]).strip():
            try:
                max_reply_count = max(int(parts[6]), 0)
            except (TypeError, ValueError):
                yield event.plain_result("最大回复次数必须是数字，0 表示不限。")
                return
        exclude_keywords = self._parse_groups(parts[7]) if len(parts) >= 8 else []

        match_type = self._normalize_match_type(match_type)
        if match_type == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                yield event.plain_result(f"正则无效：{exc}")
                return

        items = self._raw_rule_items()
        for item in items:
            if str(item.get("name") or "").strip() == name:
                yield event.plain_result(f"已存在同名规则：{name}")
                return

        items.append(
            {
                "name": name,
                "enabled": enabled,
                "groups": groups,
                "exclude_keywords": exclude_keywords,
                "match_type": match_type,
                "pattern": pattern,
                "reply": reply,
                "ignore_case": True,
                "continue_after_reply": False,
                "cooldown_seconds": 0,
                "priority": len(items) + 1,
                "max_reply_count": max_reply_count,
                "reply_count": 0,
            }
        )
        self._save_rule_items(items)
        yield event.plain_result(f"规则已添加：{name}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("删除")
    async def keyword_reply_delete(
        self, event: AstrMessageEvent, name: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        name = str(name or "").strip()
        if not name:
            yield event.plain_result("用法：/关键词回复 删除 规则名")
            return

        items = self._raw_rule_items()
        next_items = [
            item for item in items if str(item.get("name") or "").strip() != name
        ]
        if len(next_items) == len(items):
            yield event.plain_result(f"未找到规则：{name}")
            return

        self._save_rule_items(next_items)
        yield event.plain_result(f"规则已删除：{name}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("开关")
    async def keyword_reply_toggle(
        self, event: AstrMessageEvent, name: str = "", status: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        enabled = self._parse_on_off(status)
        if not name or enabled is None:
            yield event.plain_result("用法：/关键词回复 开关 规则名 开|关")
            return

        items = self._raw_rule_items()
        changed = False
        for item in items:
            if str(item.get("name") or "").strip() == str(name).strip():
                item["enabled"] = enabled
                changed = True
                break
        if not changed:
            yield event.plain_result(f"未找到规则：{name}")
            return

        self._save_rule_items(items)
        yield event.plain_result(f"规则 {name} 已设为：{'开' if enabled else '关'}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("回复")
    async def keyword_reply_set_reply(
        self, event: AstrMessageEvent, name: str = "", reply: GreedyStr = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        name = str(name or "").strip()
        reply = str(reply or "").strip()
        if not name or not reply:
            yield event.plain_result("用法：/关键词回复 回复 规则名 新回复内容")
            return

        items = self._raw_rule_items()
        changed = False
        for item in items:
            if str(item.get("name") or "").strip() == name:
                item["reply"] = reply
                changed = True
                break
        if not changed:
            yield event.plain_result(f"未找到规则：{name}")
            return

        self._save_rule_items(items)
        yield event.plain_result(f"规则 {name} 的回复已更新。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("排除")
    async def keyword_reply_set_excludes(
        self, event: AstrMessageEvent, name: str = "", excludes: GreedyStr = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        name = str(name or "").strip()
        if not name:
            yield event.plain_result("用法：/关键词回复 排除 规则名 排除词1,排除词2")
            return

        exclude_list = self._parse_groups(excludes)
        items = self._raw_rule_items()
        changed = False
        for item in items:
            if str(item.get("name") or "").strip() == name:
                item["exclude_keywords"] = exclude_list
                changed = True
                break
        if not changed:
            yield event.plain_result(f"未找到规则：{name}")
            return

        self._save_rule_items(items)
        yield event.plain_result(
            f"规则 {name} 的排除关键词已更新：{','.join(exclude_list) if exclude_list else '无'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("次数")
    async def keyword_reply_set_limit(
        self, event: AstrMessageEvent, name: str = "", value: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        name = str(name or "").strip()
        value = str(value or "").strip()
        if not name or not value:
            yield event.plain_result("用法：/关键词回复 次数 规则名 次数上限（0 表示不限）")
            return

        try:
            max_reply_count = max(int(value), 0)
        except (TypeError, ValueError):
            yield event.plain_result("次数上限必须是数字，0 表示不限。")
            return

        items = self._raw_rule_items()
        changed = False
        for item in items:
            if str(item.get("name") or "").strip() == name:
                item["max_reply_count"] = max_reply_count
                changed = True
                break
        if not changed:
            yield event.plain_result(f"未找到规则：{name}")
            return

        self._save_rule_items(items)
        yield event.plain_result(
            f"规则 {name} 的最大回复次数已更新为：{'不限' if max_reply_count == 0 else max_reply_count}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("重置次数")
    async def keyword_reply_reset_count(
        self, event: AstrMessageEvent, name: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        name = str(name or "").strip()
        if not name:
            yield event.plain_result("用法：/关键词回复 重置次数 规则名")
            return

        items = self._raw_rule_items()
        changed = False
        for item in items:
            if str(item.get("name") or "").strip() == name:
                item["reply_count"] = 0
                changed = True
                break
        if not changed:
            yield event.plain_result(f"未找到规则：{name}")
            return

        self._save_rule_items(items)
        yield event.plain_result(f"规则 {name} 的已回复次数已清零。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("群")
    async def keyword_reply_set_groups(
        self, event: AstrMessageEvent, name: str = "", groups: GreedyStr = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        name = str(name or "").strip()
        if not name:
            yield event.plain_result("用法：/关键词回复 群 规则名 群号1,群号2")
            return

        group_list = self._parse_groups(groups)
        items = self._raw_rule_items()
        changed = False
        for item in items:
            if str(item.get("name") or "").strip() == name:
                item["groups"] = group_list
                changed = True
                break
        if not changed:
            yield event.plain_result(f"未找到规则：{name}")
            return

        self._save_rule_items(items)
        yield event.plain_result(
            f"规则 {name} 的群范围已更新：{','.join(group_list) if group_list else '全部群'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("白名单")
    async def keyword_reply_whitelist(
        self, event: AstrMessageEvent, action: str = "", group_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        action = str(action or "").strip()
        group_id = str(group_id or "").strip()
        whitelist = self._group_whitelist()

        if action in {"查看", "list", "ls", ""}:
            yield event.plain_result(
                f"插件级白名单：{','.join(whitelist) if whitelist else '未设置'}"
            )
            return

        if action in {"添加", "add"}:
            if not group_id:
                yield event.plain_result("用法：/关键词回复 白名单 添加 群号")
                return
            if group_id not in whitelist:
                whitelist.append(group_id)
                self._save_plugin_config({"group_whitelist": whitelist})
            yield event.plain_result(f"白名单已添加：{group_id}")
            return

        if action in {"删除", "移除", "del", "remove"}:
            if not group_id:
                yield event.plain_result("用法：/关键词回复 白名单 删除 群号")
                return
            whitelist = [item for item in whitelist if item != group_id]
            self._save_plugin_config({"group_whitelist": whitelist})
            yield event.plain_result(f"白名单已删除：{group_id}")
            return

        yield event.plain_result("用法：/关键词回复 白名单 查看|添加|删除 [群号]")

    @keyword_reply.command("帮助")
    async def keyword_reply_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        yield event.plain_result(
            "关键词回复命令：\n"
            "/关键词回复 面板\n"
            "/关键词回复 状态\n"
            "/关键词回复 列表\n"
            "/关键词回复 添加 名称 | keyword/exact/regex | 匹配内容 | 回复内容 | 群号1,群号2 | 开\n"
            "/关键词回复 删除 规则名\n"
            "/关键词回复 开关 规则名 开|关\n"
            "/关键词回复 回复 规则名 新回复内容\n"
            "/关键词回复 排除 规则名 排除词1,排除词2\n"
            "/关键词回复 次数 规则名 次数上限\n"
            "/关键词回复 重置次数 规则名\n"
            "/关键词回复 群 规则名 群号1,群号2\n"
            "/关键词回复 白名单 查看\n"
            "/关键词回复 白名单 添加 群号\n"
            "/关键词回复 白名单 删除 群号"
        ).use_t2i(False)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        if not self._plugin_enabled():
            return

        if self._ignore_self_messages() and event.get_sender_id() == event.get_self_id():
            return

        message_text = event.get_message_str().strip()
        if not message_text:
            return

        group_id = event.get_group_id().strip()
        if not group_id:
            return

        whitelist = self._group_whitelist()
        if whitelist and group_id not in whitelist:
            return

        for rule in self._load_rules():
            if not self._group_matches(rule, group_id):
                continue

            match_result = self._match_rule(rule, message_text)
            if not match_result:
                continue

            if self._has_excluded_keyword(rule, message_text):
                continue

            if self._is_in_cooldown(group_id, rule):
                continue

            if self._is_reply_limit_reached(rule):
                continue

            reply_text = self._build_reply_text(rule, event, message_text, match_result).strip()
            if not reply_text:
                logger.warning(
                    f"GroupKeywordReply matched rule '{rule.name}' but reply text is empty."
                )
                continue

            self._mark_triggered(group_id, rule)
            self._increment_rule_reply_count(rule.name)

            if rule.continue_after_reply:
                await event.send(MessageChain().message(reply_text).use_t2i(False))
                return

            event.should_call_llm(False)
            event.set_result(event.plain_result(reply_text).use_t2i(False).stop_event())
            return
