# AstrBot Group Keyword Reply

Automatically match configured group messages and send configured replies.

## Features

- Group-only auto reply
- Supports keyword, exact, and regex matching
- Optional group whitelist per rule
- Template variables in reply text
- Cooldown per rule to avoid spam
- Optional `continue_after_reply` to let other plugins or the LLM continue processing

## Install

Place this plugin in:

```text
.astrbot/data/plugins/astrbot_plugin_group_keyword_reply
```

Then restart AstrBot or reload plugins.

## Config

The main config field is `rules_json`. It must be a JSON array.

Each rule supports:

- `name`: Rule name
- `enabled`: Whether the rule is enabled
- `groups`: List of group IDs. Empty means all groups
- `match_type`: `keyword`, `exact`, or `regex`
- `pattern`: Match content
- `reply`: Reply text after a match
- `ignore_case`: Whether matching ignores case
- `continue_after_reply`: Whether processing should continue after replying
- `cooldown_seconds`: Cooldown in the same group
- `priority`: Lower number means higher priority

## Reply Variables

`reply` supports:

- `{message}`: Original message text
- `{sender_id}`: Sender ID
- `{sender_name}`: Sender nickname
- `{group_id}`: Group ID
- `{rule_name}`: Current rule name

If `match_type` is `regex`, it also supports:

- `{match}`: Full matched text
- `{g1}`, `{g2}`: Captured groups
- Named groups, for example `{name}`

## Example Rules

```json
[
  {
    "name": "ad-warning",
    "enabled": true,
    "groups": ["123456789"],
    "match_type": "keyword",
    "pattern": "代挂",
    "reply": "本群禁止发布代挂广告，请遵守群规。",
    "ignore_case": true,
    "continue_after_reply": false,
    "cooldown_seconds": 10,
    "priority": 1
  },
  {
    "name": "welcome-regex",
    "enabled": true,
    "groups": [],
    "match_type": "regex",
    "pattern": "^我是(?P<name>.+)$",
    "reply": "收到，欢迎你 {name}。",
    "ignore_case": true,
    "continue_after_reply": false,
    "cooldown_seconds": 0,
    "priority": 2
  }
]
```

## Command

- `关键词回复状态`: Show whether the plugin is enabled and how many rules are loaded
