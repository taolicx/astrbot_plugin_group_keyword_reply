# AstrBot 群关键词自动回复插件

这个插件会监听群消息，在命中你配置的规则后，自动发送指定回复。

## 功能

- 仅处理群消息
- 支持指定群号生效
- 支持关键词匹配、完全匹配、正则匹配
- 支持回复模板变量
- 支持命中冷却，避免短时间重复刷屏
- 支持命中后直接截断后续处理，或回复后继续交给其他插件/LLM

## 安装位置

当前目录已经是可直接加载的 AstrBot 插件目录：

```text
.astrbot/data/plugins/astrbot_plugin_group_keyword_reply
```

重启 AstrBot 或重新加载插件后即可生效。

## 配置说明

核心配置项是 `rules_json`，它是一个 JSON 数组。每条规则支持以下字段：

- `name`：规则名称
- `enabled`：是否启用
- `groups`：生效群号列表，留空表示全部群
- `match_type`：`keyword`、`exact`、`regex`
- `pattern`：匹配内容
- `reply`：命中后发送的回复文本
- `ignore_case`：是否忽略大小写
- `continue_after_reply`：是否在发送回复后继续让其他插件/LLM处理
- `cooldown_seconds`：该规则在同一群内的冷却秒数
- `priority`：优先级，数值越小越先匹配

## 回复模板变量

`reply` 支持这些变量：

- `{message}`：原始消息文本
- `{sender_id}`：发送者 ID
- `{sender_name}`：发送者昵称
- `{group_id}`：群号
- `{rule_name}`：当前命中的规则名

如果 `match_type` 是 `regex`，还支持：

- `{match}`：整段匹配内容
- `{g1}`、`{g2}`：第 1、2 个捕获组
- 命名分组，例如正则 `^我是(?P<name>.+)$` 可在回复里写 `{name}`

## 示例

```json
[
  {
    "name": "广告提醒",
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
    "name": "新成员欢迎",
    "enabled": true,
    "groups": ["123456789"],
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

## 命令

- `关键词回复状态`：查看插件是否启用以及当前加载的规则数量
