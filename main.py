import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Star, register
from astrbot.core.star.filter.command import GreedyStr


@dataclass(slots=True)
class ReplyBranch:
    title: str
    keywords: list[str]
    reply: str
    match_policy: str = "any"


@dataclass(slots=True)
class ReplyRule:
    name: str
    pattern: str
    reply: str
    match_type: str
    branch_reply_mode: str
    groups: list[str]
    exclude_keywords: list[str]
    ignore_case: bool
    continue_after_reply: bool
    cooldown_seconds: float
    priority: int
    max_reply_count: int
    reply_count: int
    required_keywords: list[str] = field(default_factory=list)
    branch_replies: list[ReplyBranch] = field(default_factory=list)
    compiled_pattern: re.Pattern[str] | None = None


class SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@register(
    "GroupKeywordReply",
    "Codex",
    "按规则匹配群消息并自动发送预设回复",
    "1.4.7",
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
        # Reply-count persistence is deliberately kept off the hot reply path:
        # a synchronous config save before event.stop_event() can delay the user-visible reply.
        self._pending_reply_increments: dict[str, int] = {}
        self._count_flush_task: asyncio.Task[None] | None = None
        self._count_flush_delay_seconds = 2.0
        self._web_runner: web.AppRunner | None = None
        self._web_site: web.TCPSite | None = None
        self._web_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._start_webui_server()

    async def terminate(self) -> None:
        await self._flush_pending_reply_counts_now()
        await self._stop_webui_server()

    def _save_plugin_config(self, updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            self.config[key] = value

        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()

    def _no_store_headers(self) -> dict[str, str]:
        return {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }

    def _clear_rules_cache(self) -> None:
        self._rules_cache_key = ""
        self._rules_cache = []

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
            "关键词": "keyword",
            "包含": "keyword",
            "exact": "exact",
            "full": "exact",
            "整句": "exact",
            "完全一致": "exact",
            "regex": "regex",
            "re": "regex",
            "正则": "regex",
        }
        return aliases.get(match_type, "keyword")

    def _describe_match_type(self, value: Any) -> str:
        match_type = self._normalize_match_type(value)
        labels = {
            "keyword": "关键词包含",
            "exact": "整句一样",
            "regex": "正则匹配",
        }
        return labels.get(match_type, "关键词包含")

    def _normalize_match_policy(self, value: Any) -> str:
        policy = str(value or "any").strip().lower()
        aliases = {
            "any": "any",
            "or": "any",
            "任意": "any",
            "任一": "any",
            "all": "all",
            "and": "all",
            "全部": "all",
            "同时": "all",
        }
        return aliases.get(policy, "any")

    def _normalize_branch_reply_mode(self, value: Any) -> str:
        mode = str(value or "first").strip().lower()
        aliases = {
            "first": "first",
            "single": "first",
            "first_match": "first",
            "第一条": "first",
            "首条": "first",
            "all": "all",
            "multi": "all",
            "all_match": "all",
            "全部": "all",
            "全部命中": "all",
            "依次回复": "all",
        }
        return aliases.get(mode, "first")

    def _describe_branch_reply_mode(self, value: Any) -> str:
        mode = self._normalize_branch_reply_mode(value)
        labels = {
            "first": "命中第一条",
            "all": "命中全部",
        }
        return labels.get(mode, "命中第一条")

    def _coerce_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            return default

    def _coerce_int(self, value: Any, default: int = 0, minimum: int = 0) -> int:
        try:
            return max(int(value), minimum)
        except (TypeError, ValueError):
            return default

    def _prepare_branch_for_editor(
        self, item: Any, index: int = 0
    ) -> dict[str, Any]:
        if not isinstance(item, dict):
            item = {}
        keywords = self._parse_groups(item.get("keywords", []))
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            title = keywords[0] if keywords else f"扩展回复 {index + 1}"
        return {
            "title": title,
            "keywords": keywords,
            "reply": str(item.get("reply") or "").strip(),
            "match_policy": self._normalize_match_policy(
                item.get("match_policy", "any")
            ),
        }

    def _prepare_rule_for_editor(
        self, item: dict[str, Any], index: int = 0
    ) -> dict[str, Any]:
        match_type = self._normalize_match_type(item.get("match_type", "keyword"))
        pattern = str(item.get("pattern") or "").strip()
        required_keywords = self._parse_groups(item.get("required_keywords", []))
        if match_type == "keyword" and not required_keywords and pattern:
            required_keywords = [pattern]

        raw_branches = item.get("branch_replies", [])
        if not isinstance(raw_branches, list):
            raw_branches = []

        return {
            "name": str(item.get("name") or f"规则 {index + 1}").strip(),
            "enabled": self._parse_bool(item.get("enabled", True), True),
            "groups": self._parse_groups(item.get("groups", [])),
            "exclude_keywords": self._parse_groups(item.get("exclude_keywords", [])),
            "match_type": match_type,
            "branch_reply_mode": self._normalize_branch_reply_mode(
                item.get("branch_reply_mode", "first")
            ),
            "pattern": pattern or ",".join(required_keywords),
            "required_keywords": required_keywords,
            "reply": str(item.get("reply") or item.get("default_reply") or "").strip(),
            "branch_replies": [
                self._prepare_branch_for_editor(branch, branch_index)
                for branch_index, branch in enumerate(raw_branches)
            ],
            "ignore_case": self._parse_bool(item.get("ignore_case", True), True),
            "continue_after_reply": self._parse_bool(
                item.get("continue_after_reply", False), False
            ),
            "cooldown_seconds": self._coerce_float(item.get("cooldown_seconds", 0)),
            "priority": self._coerce_int(item.get("priority", index + 1), index + 1, 1),
            "max_reply_count": self._coerce_int(item.get("max_reply_count", 0)),
            "reply_count": self._coerce_int(item.get("reply_count", 0)),
        }

    def _build_editor_state(
        self,
        rules: list[dict[str, Any]] | None = None,
        enabled: bool | None = None,
        ignore_self_messages: bool | None = None,
        group_whitelist: list[str] | None = None,
    ) -> dict[str, Any]:
        rule_items = rules if rules is not None else self._raw_rule_items()
        return {
            "enabled": self._plugin_enabled() if enabled is None else enabled,
            "ignore_self_messages": (
                self._ignore_self_messages()
                if ignore_self_messages is None
                else ignore_self_messages
            ),
            "group_whitelist": (
                self._group_whitelist()
                if group_whitelist is None
                else group_whitelist
            ),
            "rules": [
                self._prepare_rule_for_editor(item, index)
                for index, item in enumerate(rule_items)
            ],
            "webui_url": self._webui_url(),
        }

    def _sanitize_branch_item(
        self, item: Any, index: int, rule_name: str
    ) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError(f"规则 {rule_name} 的第 {index + 1} 条扩展回复格式不正确。")

        keywords = self._parse_groups(item.get("keywords", []))
        if not keywords:
            raise ValueError(f"规则 {rule_name} 的第 {index + 1} 条扩展回复没有填写关键词。")

        reply = str(item.get("reply") or "").strip()
        if not reply:
            raise ValueError(f"规则 {rule_name} 的第 {index + 1} 条扩展回复没有填写回复内容。")

        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            title = keywords[0] if keywords else f"扩展回复 {index + 1}"

        return {
            "title": title,
            "keywords": keywords,
            "reply": reply,
            "match_policy": self._normalize_match_policy(
                item.get("match_policy", "any")
            ),
        }

    def _sanitize_rule_item(
        self, item: Any, index: int = 0
    ) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("规则项必须是对象。")

        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("规则名称不能为空。")

        match_type = self._normalize_match_type(item.get("match_type", "keyword"))
        branch_reply_mode = self._normalize_branch_reply_mode(
            item.get("branch_reply_mode", "first")
        )
        pattern = str(item.get("pattern") or "").strip()
        required_keywords = self._parse_groups(item.get("required_keywords", []))
        reply = str(item.get("reply") or item.get("default_reply") or "").strip()

        raw_branches = item.get("branch_replies", [])
        if raw_branches is None:
            raw_branches = []
        if not isinstance(raw_branches, list):
            raise ValueError(f"规则 {name} 的扩展回复列表格式不正确。")

        branch_replies = [
            self._sanitize_branch_item(branch, branch_index, name)
            for branch_index, branch in enumerate(raw_branches)
        ]

        groups = self._parse_groups(item.get("groups", []))
        exclude_keywords = self._parse_groups(item.get("exclude_keywords", []))
        ignore_case = self._parse_bool(item.get("ignore_case", True), True)
        continue_after_reply = self._parse_bool(
            item.get("continue_after_reply", False), False
        )
        cooldown_seconds = self._coerce_float(item.get("cooldown_seconds", 0))
        priority = self._coerce_int(item.get("priority", index + 1), index + 1, 1)

        try:
            max_reply_count = max(int(item.get("max_reply_count", 0)), 0)
        except (TypeError, ValueError):
            raise ValueError(f"规则 {name} 的最多回复次数必须是数字。")

        try:
            reply_count = max(int(item.get("reply_count", 0)), 0)
        except (TypeError, ValueError):
            reply_count = 0

        if match_type == "keyword":
            if not required_keywords:
                if pattern:
                    required_keywords = [pattern]
                else:
                    raise ValueError(f"规则 {name} 至少要填写一个必备关键词。")
            pattern = ",".join(required_keywords)
            if not reply and not branch_replies:
                raise ValueError(
                    f"规则 {name} 至少要填写一个回复，可以是基础回复，或下面的扩展回复。"
                )
        else:
            branch_reply_mode = "first"
            required_keywords = []
            branch_replies = []
            if not pattern:
                raise ValueError(f"规则 {name} 的触发内容不能为空。")
            if not reply:
                raise ValueError(f"规则 {name} 的回复内容不能为空。")
            if match_type == "regex":
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"规则 {name} 的正则表达式无效：{exc}")

        return {
            "name": name,
            "enabled": self._parse_bool(item.get("enabled", True), True),
            "groups": groups,
            "exclude_keywords": exclude_keywords,
            "match_type": match_type,
            "branch_reply_mode": branch_reply_mode,
            "pattern": pattern,
            "required_keywords": required_keywords,
            "reply": reply,
            "branch_replies": branch_replies,
            "ignore_case": ignore_case,
            "continue_after_reply": continue_after_reply,
            "cooldown_seconds": cooldown_seconds,
            "priority": priority,
            "max_reply_count": max_reply_count,
            "reply_count": reply_count,
        }

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

    def _save_rule_items(
        self, items: list[dict[str, Any]], *, merge_pending_counts: bool = True
    ) -> None:
        if merge_pending_counts:
            self._merge_pending_reply_counts_into_items(items)
        self._clear_rules_cache()
        self._save_plugin_config(
            {"rules_json": json.dumps(items, ensure_ascii=False, indent=2)}
        )

    def _merge_rule_reply_count_increments(
        self, items: list[dict[str, Any]], increments: dict[str, int]
    ) -> bool:
        if not increments:
            return False

        changed = False
        for item in items:
            name = str(item.get("name") or "").strip()
            amount = increments.get(name, 0)
            if amount <= 0:
                continue
            try:
                current = max(int(item.get("reply_count", 0)), 0)
            except (TypeError, ValueError):
                current = 0
            item["reply_count"] = current + amount
            changed = True
        return changed

    def _take_pending_reply_increments(self) -> dict[str, int]:
        increments = {
            name: amount
            for name, amount in self._pending_reply_increments.items()
            if name and amount > 0
        }
        self._pending_reply_increments = {}
        return increments

    def _merge_pending_reply_counts_into_items(self, items: list[dict[str, Any]]) -> bool:
        return self._merge_rule_reply_count_increments(
            items, self._take_pending_reply_increments()
        )

    def _cancel_reply_count_flush_task(self) -> None:
        task = self._count_flush_task
        self._count_flush_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_reply_count_flush(self) -> None:
        task = self._count_flush_task
        if task is not None and not task.done():
            return
        self._count_flush_task = asyncio.create_task(
            self._flush_reply_counts_after_delay()
        )

    async def _flush_reply_counts_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._count_flush_delay_seconds)
            self._flush_pending_reply_counts_to_config()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"GroupKeywordReply failed to flush reply counts: {exc}")
        finally:
            if asyncio.current_task() is self._count_flush_task:
                self._count_flush_task = None

    def _flush_pending_reply_counts_to_config(self) -> None:
        increments = self._take_pending_reply_increments()
        if not increments:
            return

        items = self._raw_rule_items()
        if not items:
            return

        if self._merge_rule_reply_count_increments(items, increments):
            self._save_rule_items(items, merge_pending_counts=False)

    async def _flush_pending_reply_counts_now(self) -> None:
        task = self._count_flush_task
        self._count_flush_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            self._flush_pending_reply_counts_to_config()
        except Exception as exc:
            logger.warning(f"GroupKeywordReply failed to flush reply counts: {exc}")

    def _increment_rule_reply_count(self, rule_name: str, amount: int = 1) -> None:
        if amount <= 0:
            return

        self._pending_reply_increments[rule_name] = (
            self._pending_reply_increments.get(rule_name, 0) + amount
        )

        # Keep the in-memory cache accurate immediately so max_reply_count still
        # gates high-frequency hits before the delayed config flush lands.
        for rule in self._rules_cache:
            if rule.name == rule_name:
                rule.reply_count += amount
                break

        self._schedule_reply_count_flush()

    def _reset_rule_reply_counts(self, rule_name: str | None = None) -> int:
        self._cancel_reply_count_flush_task()
        self._pending_reply_increments = {}

        items = self._raw_rule_items()
        reset_count = 0

        for item in items:
            name = str(item.get("name") or "").strip()
            if rule_name is not None and name != rule_name:
                continue
            item["reply_count"] = 0
            reset_count += 1

        if reset_count > 0:
            self._save_rule_items(items, merge_pending_counts=False)
        return reset_count

    def _is_reply_limit_reached(self, rule: ReplyRule) -> bool:
        return rule.max_reply_count > 0 and rule.reply_count >= rule.max_reply_count

    def _load_rules(self) -> list[ReplyRule]:
        raw_rules = self._raw_rules()
        cache_key = self._make_rules_cache_key(raw_rules)
        if cache_key == self._rules_cache_key:
            return self._rules_cache

        rules: list[ReplyRule] = []
        for index, raw_item in enumerate(self._raw_rule_items()):
            try:
                item = self._sanitize_rule_item(raw_item, index)
            except ValueError as exc:
                logger.warning(str(exc))
                continue

            if not item["enabled"]:
                continue

            compiled_pattern: re.Pattern[str] | None = None
            if item["match_type"] == "regex":
                flags = re.IGNORECASE if item["ignore_case"] else 0
                try:
                    compiled_pattern = re.compile(item["pattern"], flags)
                except re.error as exc:
                    logger.warning(
                        f"GroupKeywordReply skipped regex rule '{item['name']}' because it is invalid: {exc}"
                    )
                    continue

            branch_replies = [
                ReplyBranch(
                    title=str(branch.get("title") or "").strip()
                    or f"扩展回复 {branch_index + 1}",
                    keywords=self._parse_groups(branch.get("keywords", [])),
                    reply=str(branch.get("reply") or "").strip(),
                    match_policy=self._normalize_match_policy(
                        branch.get("match_policy", "any")
                    ),
                )
                for branch_index, branch in enumerate(item.get("branch_replies", []))
            ]

            rules.append(
                ReplyRule(
                    name=item["name"],
                    pattern=item["pattern"],
                    reply=item["reply"],
                    match_type=item["match_type"],
                    branch_reply_mode=item["branch_reply_mode"],
                    groups=item["groups"],
                    exclude_keywords=item["exclude_keywords"],
                    ignore_case=item["ignore_case"],
                    continue_after_reply=item["continue_after_reply"],
                    cooldown_seconds=item["cooldown_seconds"],
                    priority=item["priority"],
                    max_reply_count=item["max_reply_count"],
                    reply_count=item["reply_count"],
                    required_keywords=item["required_keywords"],
                    branch_replies=branch_replies,
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
        reply_template: str,
        event: AstrMessageEvent,
        original_text: str,
        match_result: re.Match[str] | bool,
        branch: ReplyBranch | None = None,
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
                "required_keywords": "、".join(rule.required_keywords),
                "branch_name": branch.title if branch else "",
                "branch_keywords": "、".join(branch.keywords) if branch else "",
            }
        )

        if isinstance(match_result, re.Match):
            values["match"] = match_result.group(0)
            for idx, group in enumerate(match_result.groups(), start=1):
                values[f"g{idx}"] = group or ""
            for key, value in match_result.groupdict().items():
                values[key] = value or ""

        return reply_template.format_map(values)

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

    def _normalize_text_for_match(self, text: str, ignore_case: bool) -> str:
        return text.casefold() if ignore_case else text

    def _has_excluded_keyword(self, rule: ReplyRule, text: str) -> bool:
        source = self._normalize_text_for_match(text, rule.ignore_case)
        for keyword in rule.exclude_keywords:
            target = self._normalize_text_for_match(keyword, rule.ignore_case)
            if target and target in source:
                return True
        return False

    def _message_contains_keywords(
        self, text: str, keywords: list[str], ignore_case: bool, match_policy: str = "all"
    ) -> bool:
        if not keywords:
            return False

        source = self._normalize_text_for_match(text, ignore_case)
        targets = [
            self._normalize_text_for_match(keyword, ignore_case)
            for keyword in keywords
            if str(keyword).strip()
        ]
        if not targets:
            return False

        if self._normalize_match_policy(match_policy) == "any":
            return any(target in source for target in targets)
        return all(target in source for target in targets)

    def _match_rule(self, rule: ReplyRule, text: str) -> re.Match[str] | bool:
        if rule.match_type == "regex":
            if rule.compiled_pattern is None:
                return False
            return rule.compiled_pattern.search(text) or False

        if rule.match_type == "exact":
            source = self._normalize_text_for_match(text, rule.ignore_case)
            pattern = self._normalize_text_for_match(rule.pattern, rule.ignore_case)
            return source == pattern
        return self._message_contains_keywords(
            text,
            rule.required_keywords or ([rule.pattern] if rule.pattern else []),
            rule.ignore_case,
            "all",
        )

    def _select_reply_branches(self, rule: ReplyRule, text: str) -> list[ReplyBranch]:
        if rule.match_type != "keyword":
            return []

        matched_branches: list[ReplyBranch] = []
        for branch in rule.branch_replies:
            if self._message_contains_keywords(
                text, branch.keywords, rule.ignore_case, branch.match_policy
            ):
                matched_branches.append(branch)

        if self._normalize_branch_reply_mode(rule.branch_reply_mode) == "all":
            return matched_branches
        return matched_branches[:1]

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        index_path = self.webui_dir / "index.html"
        if not index_path.exists():
            raise web.HTTPNotFound()
        return web.Response(
            text=index_path.read_text(encoding="utf-8"),
            content_type="text/html",
            charset="utf-8",
            headers=self._no_store_headers(),
        )

    def _create_web_application(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/api/state", self._handle_state),
                web.post("/api/save", self._handle_save),
                web.post("/api/reset-counts", self._handle_reset_counts),
            ]
        )
        return app

    async def _handle_state(self, request: web.Request) -> web.Response:
        payload = self._build_editor_state()
        return web.json_response(payload, headers=self._no_store_headers())

    async def _handle_save(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "message": "提交的数据不是合法的 JSON。"},
                status=400,
            )

        enabled = self._parse_bool(payload.get("enabled", True), True)
        ignore_self = self._parse_bool(payload.get("ignore_self_messages", True), True)
        whitelist = self._parse_groups(payload.get("group_whitelist", []))
        rules = payload.get("rules", [])

        if not isinstance(rules, list):
            return web.json_response(
                {"ok": False, "message": "规则列表格式不正确，请刷新页面后重试。"},
                status=400,
            )

        try:
            normalized_rules = [
                self._sanitize_rule_item(item, index)
                for index, item in enumerate(rules)
            ]
        except ValueError as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=400)

        self._save_plugin_config(
            {
                "enabled": enabled,
                "ignore_self_messages": ignore_self,
                "group_whitelist": ",".join(whitelist),
                "rules_json": json.dumps(normalized_rules, ensure_ascii=False, indent=2),
            }
        )
        self._clear_rules_cache()

        expected_rule_count = len(normalized_rules)
        expected_branch_count = sum(
            len(item.get("branch_replies", [])) for item in normalized_rules
        )
        state = self._build_editor_state()
        saved_rules = state.get("rules", [])
        saved_rule_count = len(saved_rules)
        saved_branch_count = sum(
            len(item.get("branch_replies", []))
            for item in saved_rules
            if isinstance(item, dict)
        )

        if (
            saved_rule_count != expected_rule_count
            or saved_branch_count != expected_branch_count
        ):
            logger.warning(
                "关键词回复保存后校验不一致："
                f"规则 {saved_rule_count}/{expected_rule_count}，"
                f"扩展回复 {saved_branch_count}/{expected_branch_count}"
            )
            return web.json_response(
                {
                    "ok": False,
                    "message": "保存后校验未通过，扩展回复可能没有完整写入，请重试一次。",
                    "state": state,
                },
                status=409,
                headers=self._no_store_headers(),
            )

        return web.json_response(
            {
                "ok": True,
                "message": f"保存成功，已保存 {saved_rule_count} 条规则、{saved_branch_count} 条扩展回复。",
                "state": state,
            },
            headers=self._no_store_headers(),
        )

    async def _handle_reset_counts(self, request: web.Request) -> web.Response:
        payload: dict[str, Any] = {}
        if request.can_read_body:
            try:
                payload = await request.json()
            except Exception:
                payload = {}

        name = str(payload.get("name") or "").strip()
        reset_count = self._reset_rule_reply_counts(name or None)
        if reset_count <= 0:
            message = f"未找到规则：{name}" if name else "当前没有可重置的规则。"
            return web.json_response(
                {"ok": False, "message": message},
                status=404,
                headers=self._no_store_headers(),
            )

        if name:
            message = f"规则 {name} 的已回复次数已清零。"
        else:
            message = f"已一键清零 {reset_count} 条规则的已回复次数。"
        return web.json_response(
            {"ok": True, "message": message},
            headers=self._no_store_headers(),
        )

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
        yield event.plain_result(
            f"关键词回复面板：\n{self._webui_url()}\n"
            "复杂规则更推荐直接在面板里配置。"
        ).use_t2i(False)

    @keyword_reply.command("状态", alias={"关键词回复状态"})
    async def keyword_reply_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        rules = self._load_rules()
        whitelist = self._group_whitelist()
        lines = [
            "关键词回复当前状态：",
            f"开关：{'开启' if self._plugin_enabled() else '关闭'}",
            f"忽略机器人自己的消息：{'是' if self._ignore_self_messages() else '否'}",
            f"插件总范围：{','.join(whitelist) if whitelist else '所有群都可触发'}",
            f"已启用规则数：{len(rules)}",
            f"面板地址：{self._webui_url()}",
        ]
        if rules:
            lines.append("已启用规则：")
            for rule in rules[:10]:
                group_text = ",".join(rule.groups) if rule.groups else "全部群"
                exclude_text = (
                    ",".join(rule.exclude_keywords) if rule.exclude_keywords else "无"
                )
                limit_text = (
                    "不限"
                    if rule.max_reply_count <= 0
                    else f"{rule.reply_count}/{rule.max_reply_count}"
                )
                required_keywords = rule.required_keywords or (
                    [rule.pattern] if rule.pattern else []
                )
                if rule.match_type == "keyword":
                    trigger_text = (
                        f"必备词：{'、'.join(required_keywords)}"
                        if required_keywords
                        else "必备词：未填写"
                    )
                    if rule.branch_replies:
                        trigger_text += (
                            f"，扩展回复 {len(rule.branch_replies)} 条"
                            f"（{self._describe_branch_reply_mode(rule.branch_reply_mode)}）"
                        )
                else:
                    trigger_text = (
                        f"{self._describe_match_type(rule.match_type)}：{rule.pattern}"
                    )
                lines.append(
                    f"- {rule.name} | {trigger_text} | 范围 {group_text} | 排除 {exclude_text} | 次数 {limit_text}"
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
            required_keywords = self._parse_groups(item.get("required_keywords", []))
            branch_replies = item.get("branch_replies", [])
            if not isinstance(branch_replies, list):
                branch_replies = []
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
            limit_text = (
                f"{reply_count}/{max_reply_count}"
                if max_reply_count > 0
                else f"{reply_count}/不限"
            )
            if match_type == "keyword":
                keywords = required_keywords or ([pattern] if pattern else [])
                trigger_text = (
                    f"必备词：{'、'.join(keywords)}" if keywords else "必备词：未填写"
                )
                if branch_replies:
                    trigger_text += (
                        f"，扩展回复 {len(branch_replies)} 条"
                        f"（{self._describe_branch_reply_mode(item.get('branch_reply_mode', 'first'))}）"
                    )
            else:
                trigger_text = f"{self._describe_match_type(match_type)}：{pattern}"
            lines.append(
                f"- {name} | {'开启' if enabled else '关闭'} | {trigger_text} | "
                f"范围 {','.join(groups) if groups else '全部群'} | 排除 {','.join(excludes) if excludes else '无'} | 次数 {limit_text}"
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
                "用法：/关键词回复 添加 名称 | 包含/整句/正则 | 触发内容 | 回复内容 | 群号1,群号2 | 开/关 | 次数上限 | 排除词1,排除词2"
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
                "required_keywords": [pattern] if match_type == "keyword" else [],
                "reply": reply,
                "branch_replies": [],
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
            yield event.plain_result("用法：/关键词回复 重置次数 规则名\n或：/关键词回复 重置次数 全部")
            return

        if name in {"全部", "all", "ALL"}:
            reset_count = self._reset_rule_reply_counts()
            if reset_count <= 0:
                yield event.plain_result("当前没有可重置的规则。")
                return
            yield event.plain_result(
                f"已一键清零 {reset_count} 条规则的已回复次数。"
            )
            return

        reset_count = self._reset_rule_reply_counts(name)
        if reset_count <= 0:
            yield event.plain_result(f"未找到规则：{name}")
            return

        yield event.plain_result(f"规则 {name} 的已回复次数已清零。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @keyword_reply.command("重置全部次数", alias={"一键重置次数", "清零全部次数"})
    async def keyword_reply_reset_all_counts(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        reset_count = self._reset_rule_reply_counts()
        if reset_count <= 0:
            yield event.plain_result("当前没有可重置的规则。")
            return
        yield event.plain_result(f"已一键清零 {reset_count} 条规则的已回复次数。")

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
            "推荐先用面板配置：\n"
            "/关键词回复 面板\n"
            "\n常用命令：\n"
            "/关键词回复 状态\n"
            "/关键词回复 列表\n"
            "/关键词回复 添加 名称 | 包含/整句/正则 | 触发内容 | 回复内容 | 群号1,群号2 | 开/关 | 次数上限 | 排除词1,排除词2\n"
            "/关键词回复 删除 规则名\n"
            "/关键词回复 开关 规则名 开|关\n"
            "/关键词回复 回复 规则名 新回复内容\n"
            "/关键词回复 排除 规则名 排除词1,排除词2\n"
            "/关键词回复 次数 规则名 次数上限\n"
            "/关键词回复 重置次数 规则名\n"
            "/关键词回复 重置次数 全部\n"
            "/关键词回复 重置全部次数\n"
            "/关键词回复 群 规则名 群号1,群号2\n"
            "/关键词回复 白名单 查看\n"
            "/关键词回复 白名单 添加 群号\n"
            "/关键词回复 白名单 删除 群号\n"
            "\n说明：\n"
            "1. “包含”会把你填的内容当作必备关键词。\n"
            "2. 需要“必备关键词 + 不同扩展关键词回不同内容”时，请直接用面板。\n"
            "3. 想把所有规则的已回复次数一起清零，可以用“重置次数 全部”或“重置全部次数”。"
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

            matched_branches = self._select_reply_branches(rule, message_text)
            reply_targets: list[tuple[str, ReplyBranch | None]] = []
            if matched_branches:
                for branch in matched_branches:
                    if branch.reply.strip():
                        reply_targets.append((branch.reply, branch))
            elif rule.reply.strip():
                reply_targets.append((rule.reply, None))

            if not reply_targets:
                continue

            if rule.max_reply_count > 0:
                remaining_reply_count = rule.max_reply_count - rule.reply_count
                if remaining_reply_count <= 0:
                    continue
                reply_targets = reply_targets[:remaining_reply_count]

            reply_texts: list[str] = []
            for reply_template, branch in reply_targets:
                reply_text = self._build_reply_text(
                    rule, reply_template, event, message_text, match_result, branch
                ).strip()
                if not reply_text:
                    logger.warning(
                        f"GroupKeywordReply matched rule '{rule.name}' but reply text is empty."
                    )
                    continue
                reply_texts.append(reply_text)

            if not reply_texts:
                continue

            self._mark_triggered(group_id, rule)
            self._increment_rule_reply_count(rule.name, len(reply_texts))

            if rule.continue_after_reply:
                for reply_text in reply_texts:
                    await event.send(MessageChain().message(reply_text).use_t2i(False))
                return

            if len(reply_texts) == 1:
                event.should_call_llm(False)
                event.set_result(
                    event.plain_result(reply_texts[0]).use_t2i(False).stop_event()
                )
                return

            for reply_text in reply_texts:
                await event.send(MessageChain().message(reply_text).use_t2i(False))
            event.should_call_llm(False)
            event.stop_event()
            return
