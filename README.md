# astrbot_plugin_warthunder_news

战争雷霆官方论坛公告监控推送插件（AstrBot 插件）

定时轮询 [战争雷霆官方论坛 - 官方新闻与公告](https://forum.warthunder.com/c/official-news-and-information/7.rss)，发现新公告后调用 LLM 翻译为中文并按模板总结，通过 OneBot v11 推送到指定 QQ 群。

## 功能

- **定时轮询**：默认每 10 分钟检测一次论坛 RSS，公告按标题自动分为三类：
  - **游戏版本更新**：标题匹配 `Update x.y`，抓取主题详情页首帖完整正文；
  - **Community Update**：标题含 `Community Update`，抓取主题详情页并**只提取佩戴 Community Manager 群组徽章的发言**；
  - **其他官方公告**：除上述两类之外的全部公告，抓取主题详情页首帖完整正文。
- **LLM 翻译总结**：输出全部简体中文，保留数值与条件细节；涉及军武装备时写出具体型号，并尽量对齐游戏中文客户端载具译名（不确定时保留原文型号代号）。
- **图文混排**：自动提取原文图片，LLM 总结保留 `[图片N]` 占位，推送时图片出现在原文对应位置。
- **本地存档**：完整原文与处理结果按类型保存到 `archive.json`，出现新版本时自动覆盖；查询指令优先直接发送存档，命中即零联网、零 LLM 调用。
- **WebUI 可视化**：配置面板 + 「公告存档」视图，可随时强制刷新指定条目（重新抓取 + 重新总结）。
- **去重**：`seen_topics.json` 记录已处理主题 ID，重启后不重复推送。

## 群指令

| 指令 | 说明 |
| --- | --- |
| `/news` | 播报最新一条其他官方公告（除 Community Update 与游戏版本更新之外） |
| `/cmnews` | 只播报最新 Community Update |
| `/upnews` | 只播报最新游戏版本更新公告 |

三条指令优先返回本地存档内容；无存档时才实时抓取并归档。

## 配置（WebUI 插件配置页）

插件目录下的 `_conf_schema.json` 会自动生成可视化表单（插件管理 → 本插件 → 配置）：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `target_groups` | 推送目标群号列表 | QQ群ID |
| `cm_group_name` | CM 论坛群组名 | Community Manager |
| `poll_interval` | 轮询间隔（秒，60–3600） | 600 |
| `max_images` | 单条公告最多图片数（0 = 不发图） | 9 |
| `keyword_whitelist` | 公告关键词白名单（仅控制是否推送，不影响存档） | 空 = 不限制 |
| `keyword_blacklist` | 公告关键词黑名单（优先于白名单，仅控制是否推送，不影响存档） | 默认屏蔽商店类、电竞类、涂装类和无关游戏内容 |
| `command_group_whitelist` | 指令触发群白名单（对三条指令统一生效） | 空 = 不限制 |
| `command_user_whitelist` | 指令使用者 QQ 号白名单 | 空 = 不限制 |
| `llm_template` | LLM 总结模板 | 内置人工模板 |

## WebUI 视图：公告存档

入口：**插件管理 → 本插件 → 插件详情页 → 公告存档**

- 查看三类公告的存档概览（标题、主题 ID、发布者、发布时间、图片数、字数、摘要预览）；
- 「刷新」强制重新抓取原文并调用 LLM 重新总结，覆盖本地存档——用于论坛刚发公告但轮询尚未捕获，或对当前摘要不满意想重新生成的场景；
- 支持按条目刷新或一次性全部刷新。

> 该能力依赖较新版本 AstrBot 的插件视图（`pages/`）与 `register_web_api`；旧版本会自动降级，仅该面板不可用，其余功能不受影响。新增/删除视图后需重载插件。

## 运行期文件

插件目录下自动生成，无需手动维护（已加入 `.gitignore`）：

- `seen_topics.json`：已处理主题 ID 去重记录；
- `archive.json`：本地存档（完整原文 + LLM 处理结果）。

## 依赖

见 [requirements.txt](requirements.txt)：`aiohttp`、`feedparser`。

消息推送依赖 AstrBot 的 aiocqhttp（OneBot v11）平台适配器。

## 注意

- 首次运行只记录基线，不会把论坛历史公告全部推送到群里；
- 关键词白/黑名单作用于**自动推送**，不影响存档更新；
- 手动查询不改动去重记录，也不触发群推送；
- 指令触发词的前缀（`/`）由 AstrBot 唤醒前缀配置决定。

## 链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
