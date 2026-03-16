# AstrBot 群关键词自动回复插件

这个插件用于：

- 监听群消息
- 当消息命中你配置的规则时
- 自动发送你预设的回复内容

它适合做这些事：

- 群规提醒
- 广告词拦截提醒
- 新人欢迎语
- 固定问答
- 简单正则触发回复

## 生效范围

- 只处理群消息
- 不处理私聊

## 安装位置

把插件放在：

```text
.astrbot/data/plugins/astrbot_plugin_group_keyword_reply
```

然后重启 AstrBot，或重载插件。

## 怎么使用

这个插件现在支持两种方式：

1. 在 AstrBot 插件配置里直接修改 `rules_json`
2. 通过聊天命令增删改规则

如果你不想手改 JSON，推荐直接用命令管理。

## 插件级白名单

你可以设置一个插件级群白名单。

- 白名单为空：所有群都可以触发
- 白名单不为空：只有白名单里的群才会触发

管理命令：

- `/关键词回复 白名单 查看`
- `/关键词回复 白名单 添加 123456789`
- `/关键词回复 白名单 删除 123456789`

## rules_json 格式

`rules_json` 必须是一个 JSON 数组。  
每一项就是一条规则。

每条规则支持这些字段：

- `name`
说明：规则名称，方便你自己识别。

- `enabled`
说明：是否启用这条规则。

- `groups`
说明：哪些群生效。  
填群号数组，例如 `["123456789"]`。  
留空数组 `[]` 表示所有群都生效。

- `match_type`
说明：匹配方式。支持：
  - `keyword`：关键词包含匹配
  - `exact`：完全匹配
  - `regex`：正则匹配

- `pattern`
说明：匹配内容。

- `reply`
说明：命中后发送的回复文本。

- `ignore_case`
说明：是否忽略大小写。

- `continue_after_reply`
说明：回复后是否继续让其他插件或 LLM 继续处理。
  - `false`：回复后就停止后续处理
  - `true`：回复后继续后续处理

- `cooldown_seconds`
说明：同一群里这条规则的冷却时间，单位秒。  
冷却期间再次命中不会重复发送。

- `priority`
说明：优先级，数字越小越先匹配。

## 回复模板变量

`reply` 支持这些变量：

- `{message}`：原始消息文本
- `{sender_id}`：发送者 ID
- `{sender_name}`：发送者昵称
- `{group_id}`：群号
- `{rule_name}`：当前命中的规则名

如果 `match_type` 是 `regex`，还额外支持：

- `{match}`：完整匹配内容
- `{g1}`、`{g2}`：第 1、2 个捕获组
- 命名分组变量

例如：

```json
{
  "match_type": "regex",
  "pattern": "^我是(?P<name>.+)$",
  "reply": "收到，欢迎你 {name}。"
}
```

## 最常用示例

### 1. 关键词包含匹配

只要消息里出现“代挂”，就回复提醒：

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
  }
]
```

### 2. 完全匹配

只有消息完全等于“群规”时才回复：

```json
[
  {
    "name": "群规查询",
    "enabled": true,
    "groups": [],
    "match_type": "exact",
    "pattern": "群规",
    "reply": "群规如下：1. 禁广告 2. 禁刷屏 3. 禁人身攻击",
    "ignore_case": true,
    "continue_after_reply": false,
    "cooldown_seconds": 3,
    "priority": 1
  }
]
```

### 3. 正则匹配

用户说“我是张三”，机器人自动欢迎：

```json
[
  {
    "name": "新人欢迎",
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

## 推荐配置思路

- 想拦广告：`match_type=keyword`
- 想做固定问答：`match_type=exact`
- 想做带变量的欢迎或解析：`match_type=regex`
- 不想和 LLM 同时回复：`continue_after_reply=false`
- 想避免刷屏：把 `cooldown_seconds` 设成 `5` 到 `30`

## 命令

### 状态与列表

- `关键词回复状态`
- `/关键词回复 状态`
- `/关键词回复 列表`

### 添加规则

```text
/关键词回复 添加 名称 | keyword/exact/regex | 匹配内容 | 回复内容 | 群号1,群号2 | 开
```

示例：

```text
/关键词回复 添加 广告提醒 | keyword | 代挂 | 本群禁止发布代挂广告，请遵守群规。 | 123456789 | 开
```

说明：

- 第 5 段群号可省略
- 第 6 段开关可省略，默认开启

### 删除规则

```text
/关键词回复 删除 广告提醒
```

### 开关规则

```text
/关键词回复 开关 广告提醒 开
/关键词回复 开关 广告提醒 关
```

### 修改回复内容

```text
/关键词回复 回复 广告提醒 新的回复内容
```

### 修改规则生效群

```text
/关键词回复 群 广告提醒 123456789,987654321
```

留空表示全部群生效。

### 查看帮助

```text
/关键词回复 帮助
```

## 注意事项

- `rules_json` 必须是合法 JSON
- `groups` 里填的是群号字符串
- `regex` 写错会导致该规则被跳过
- 如果一条消息同时命中多条规则，会按 `priority` 从小到大处理
