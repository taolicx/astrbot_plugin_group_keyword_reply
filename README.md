# AstrBot 群关键词自动回复插件

这个插件用于：

- 监听群消息
- 当消息命中你配置的规则时
- 自动发送你预设的回复内容

现在支持三种管理方式：

1. 在 AstrBot 插件配置里直接修改 `rules_json`
2. 通过聊天命令增删改规则
3. 通过插件自带 WebUI 面板管理规则和白名单

## 功能

- 只处理群消息
- 支持关键词匹配、完全匹配、正则匹配
- 支持插件级群白名单
- 支持规则级群范围
- 支持回复模板变量
- 支持冷却时间
- 支持最大回复次数
- 支持中文命令管理
- 支持独立 WebUI 面板

## 安装位置

```text
.astrbot/data/plugins/astrbot_plugin_group_keyword_reply
```

放入后重启 AstrBot，或重载插件。

## WebUI 面板

命令入口：

```text
/关键词回复 面板
```

默认地址：

```text
http://127.0.0.1:18082
```

面板里可以直接：

- 开关插件
- 设置是否忽略机器人自身消息
- 维护插件级白名单
- 新增规则
- 删除规则
- 修改规则内容
- 设置冷却时间
- 设置最大回复次数
- 查看当前已回复次数

## 插件级白名单

- 白名单为空：所有群都可触发
- 白名单不为空：只有白名单里的群可触发

相关命令：

- `/关键词回复 白名单 查看`
- `/关键词回复 白名单 添加 123456789`
- `/关键词回复 白名单 删除 123456789`

## rules_json 格式

`rules_json` 是一个 JSON 数组，每一项是一条规则。

每条规则支持这些字段：

- `name`：规则名称
- `enabled`：是否启用
- `groups`：规则级群范围，留空表示全部群
- `exclude_keywords`：排除关键词列表；如果同一条消息同时包含触发关键词和这些排除词，就不回复
- `match_type`：`keyword`、`exact`、`regex`
- `pattern`：匹配内容
- `reply`：回复内容
- `ignore_case`：是否忽略大小写
- `continue_after_reply`：回复后是否继续让其他插件或 LLM 处理
- `cooldown_seconds`：冷却秒数
- `priority`：优先级，数字越小越先匹配
- `max_reply_count`：最大回复次数，`0` 表示不限
- `reply_count`：当前已回复次数

## 最大回复次数

这是你刚要求的新功能。

例如：

```json
{
  "name": "限次提醒",
  "enabled": true,
  "groups": [],
  "match_type": "keyword",
  "pattern": "测试",
  "reply": "我只会回复两次。",
  "ignore_case": true,
  "continue_after_reply": false,
  "cooldown_seconds": 0,
  "priority": 1,
  "max_reply_count": 2,
  "reply_count": 0
}
```

效果：

- 第 1 次命中：回复
- 第 2 次命中：回复
- 第 3 次及之后：不再回复

## 排除关键词

这是这个分支新增的功能。

例如你设置：

```json
{
  "name": "自动提醒",
  "pattern": "代挂",
  "exclude_keywords": ["测试", "白名单"],
  "reply": "本群禁止发布代挂广告。"
}
```

效果：

- 消息是 `代挂`：会回复
- 消息是 `代挂 测试`：不会回复
- 消息是 `白名单 代挂`：不会回复

也就是说：

- 命中发送关键词
- 但如果同一条消息里还同时命中了排除关键词
- 那这条消息就直接跳过，不触发自动回复

如果把 `max_reply_count` 设成 `0`，表示不限次数。

如果想重新开始计数，可以把 `reply_count` 清零，或者用命令：

```text
/关键词回复 重置次数 规则名
```

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
- 命名分组变量，例如 `{name}`

## 常用命令

- `/关键词回复 状态`
- `/关键词回复 列表`
- `/关键词回复 添加 名称 | keyword/exact/regex | 匹配内容 | 回复内容 | 群号1,群号2 | 开 | 次数上限`
- `/关键词回复 删除 规则名`
- `/关键词回复 开关 规则名 开|关`
- `/关键词回复 回复 规则名 新回复内容`
- `/关键词回复 排除 规则名 排除词1,排除词2`
- `/关键词回复 次数 规则名 次数上限`
- `/关键词回复 重置次数 规则名`
- `/关键词回复 群 规则名 群号1,群号2`
- `/关键词回复 白名单 查看`
- `/关键词回复 白名单 添加 群号`
- `/关键词回复 白名单 删除 群号`
- `/关键词回复 帮助`

## 示例

### 关键词包含匹配

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
    "priority": 1,
    "max_reply_count": 0,
    "reply_count": 0
  }
]
```

### 正则匹配

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
    "priority": 2,
    "max_reply_count": 0,
    "reply_count": 0
  }
]
```
