import json
import re
import time
from dataclasses import dataclass
from collections.abc import AsyncGenerator
from typing import Any

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
    ignore_case: bool
    continue_after_reply: bool
    cooldown_seconds: float
    priority: int
    compiled_pattern: re.Pattern[str] | None = None


class SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@register(
    "GroupKeywordReply",
    "Codex",
    "Automatically match configured group messages and send configured replies",
    "1.1.0",
    "https://github.com/taolicx/astrbot_plugin_group_keyword_reply",
)
class GroupKeywordReplyPlugin(Star):
    def __init__(self, context: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._rules_cache_key = ""
        self._rules_cache: list[ReplyRule] = []
        self._last_trigger_at: dict[str, float] = {}

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
        value = self.config.get("group_whitelist", [])
        return self._parse_groups(value)

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

    def _parse_groups(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            return [item.strip() for item in text.split(",") if item.strip()]
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

    def _find_rule_item(self, name: str) -> tuple[int, dict[str, Any]] | tuple[None, None]:
        items = self._raw_rule_items()
        target = str(name or "").strip()
        index = 0
        item: dict[str, Any]
        for index, item in enumerate(items):
            if str(item.get("name") or "").strip() == target:
                return index, item
        return None, None

    def _parse_on_off(self, value: str) -> bool | None:
        text = str(value or "").strip().lower()
        if text in {"on", "true", "1", "yes", "开", "开启"}:
            return True
        if text in {"off", "false", "0", "no", "关", "关闭"}:
            return False
        return None

    def _load_rules(self) -> list[ReplyRule]:
        raw_rules = self._raw_rules()
        cache_key = self._make_rules_cache_key(raw_rules)
        if cache_key == self._rules_cache_key:
            return self._rules_cache

        parsed_rules: list[dict[str, Any]]
        if isinstance(raw_rules, list):
            parsed_rules = [item for item in raw_rules if isinstance(item, dict)]
        else:
            text = str(raw_rules or "").strip()
            if not text:
                parsed_rules = []
            else:
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    logger.warning(f"GroupKeywordReply rules_json is invalid JSON: {exc}")
                    self._rules_cache_key = cache_key
                    self._rules_cache = []
                    return self._rules_cache
                if not isinstance(data, list):
                    logger.warning("GroupKeywordReply rules_json must be a JSON array.")
                    self._rules_cache_key = cache_key
                    self._rules_cache = []
                    return self._rules_cache
                parsed_rules = [item for item in data if isinstance(item, dict)]

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

            try:
                cooldown_seconds = max(float(item.get("cooldown_seconds", 0)), 0.0)
            except (TypeError, ValueError):
                cooldown_seconds = 0.0

            try:
                priority = int(item.get("priority", index + 1))
            except (TypeError, ValueError):
                priority = index + 1

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
                    ignore_case=ignore_case,
                    continue_after_reply=continue_after_reply,
                    cooldown_seconds=cooldown_seconds,
                    priority=priority,
                    compiled_pattern=compiled_pattern,
                )
            )

        rules.sort(key=lambda item: item.priority)
        self._rules_cache_key = cache_key
        self._rules_cache = rules
        return self._rules_cache

    def _group_matches(self, rule: ReplyRule, group_id: str) -> bool:
        if not rule.groups:
            return True
        return group_id in rule.groups

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

    @filter.command_group("关键词回复")
    def keyword_reply(self) -> None:
        """Group keyword reply management"""

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
        ]
        if rules:
            lines.append("规则列表:")
            for rule in rules[:10]:
                group_text = ",".join(rule.groups) if rule.groups else "全部群"
                lines.append(f"- {rule.name} [{rule.match_type}] -> {group_text}")
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
            lines.append(
                f"- {name} | {'开' if enabled else '关'} | {match_type} | {pattern} | "
                f"{','.join(groups) if groups else '全部群'}"
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
                "用法：/关键词回复 添加 名称 | keyword/exact/regex | 匹配内容 | 回复内容"
            )
            return

        name, match_type, pattern, reply = parts[:4]
        groups = self._parse_groups(parts[4]) if len(parts) >= 5 else []
        enabled = self._parse_on_off(parts[5]) if len(parts) >= 6 else True
        enabled = True if enabled is None else enabled

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
                "match_type": match_type,
                "pattern": pattern,
                "reply": reply,
                "ignore_case": True,
                "continue_after_reply": False,
                "cooldown_seconds": 0,
                "priority": len(items) + 1,
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
            "/关键词回复 状态\n"
            "/关键词回复 列表\n"
            "/关键词回复 添加 名称 | keyword/exact/regex | 匹配内容 | 回复内容 | 群号1,群号2 | 开\n"
            "/关键词回复 删除 规则名\n"
            "/关键词回复 开关 规则名 开|关\n"
            "/关键词回复 回复 规则名 新回复内容\n"
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

            if self._is_in_cooldown(group_id, rule):
                continue

            reply_text = self._build_reply_text(rule, event, message_text, match_result).strip()
            if not reply_text:
                logger.warning(
                    f"GroupKeywordReply matched rule '{rule.name}' but reply text is empty."
                )
                continue

            self._mark_triggered(group_id, rule)

            if rule.continue_after_reply:
                await event.send(MessageChain().message(reply_text).use_t2i(False))
                return

            event.should_call_llm(False)
            event.set_result(event.plain_result(reply_text).use_t2i(False).stop_event())
            return
