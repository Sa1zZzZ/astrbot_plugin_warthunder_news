# -*- coding: utf-8 -*-
"""
astrbot_plugin_warthunder_news
==============================

战争雷霆官方论坛公告监控推送插件。

功能：
    1. 定时轮询《战争雷霆》官方论坛 RSS（每 10 分钟一次，可配置）：
       https://forum.warthunder.com/c/official-news-and-information/7.rss
    2. 自动分类公告：
       - 游戏客户端更新：标题匹配 `Update \\d+\\.\\d+`，抓取主题详情页首帖完整正文；
       - 社区更新：标题包含 "Community Update"，抓取主题详情页并只提取
         指定 CM（社区管理员）的发言；
       - 其他官方公告：除上述两类之外的全部公告，抓取主题详情页首帖完整正文。
    3. 调用 LLM（使用 AstrBot 当前启用的对话模型）按照用户提供的
       人工模板进行总结：输出翻译为简体中文，保留所有数值与条件细节，
       涉及军武装备时写出具体型号并尽量对齐游戏内载具译名。
    4. 通过 OneBot v11 的 send_group_msg 动作推送到指定 QQ 群。
    5. 支持群内手动查询指令：/news 播报最新一条其他官方公告、
       /cmnews 播报最新 Community Update、/upnews 播报最新游戏版本更新公告。
    6. 本地存档：把完整原文与 LLM 处理结果按类型保存到本地（archive.json），
       出现新版本时覆盖旧存档；查询指令优先直接发送存档，避免重复抓取与调用 LLM。
    7. 提供 WebUI 可视化视图（pages/archive-manager）：查看三类公告的本地存档
       概览，并可随时强制刷新指定条目（重新抓取 + 重新总结），
       用于论坛刚发公告但轮询尚未捕获、或想重新生成摘要的场景。
    8. 使用本地 JSON 文件记录已处理的主题 ID，防止重启后重复推送。
    9. 支持三类白名单：公告关键词白/黑名单、指令触发群白名单、
       指令使用者（QQ 号）白名单。

配置：
    所有配置项都在 AstrBot WebUI 的「插件管理 -> 本插件 -> 配置」页面中设置，
    由插件目录下的 _conf_schema.json 定义（目标群号、CM 群组名、轮询间隔、
    LLM 总结模板、关键词白/黑名单、指令白名单等），保存后即时生效。

WebUI 视图：
    插件目录下的 pages/archive-manager 提供一个「公告存档」页面，入口在 WebUI
    的插件详情页；页面通过 context.register_web_api 注册的后端接口读取存档概览
    并触发强制刷新。该能力依赖较新版本的 AstrBot，旧版本会自动跳过注册，
    不影响插件其余功能。

运行期数据：
    插件目录下自动生成两个文件，均无需手动维护：
      - seen_topics.json：已处理主题 ID 去重记录；
      - archive.json：最新一类公告的完整原文与 LLM 处理结果（本地存档）。
"""

import asyncio
import html as html_lib
import json
import os
import re
from typing import Optional

import aiohttp
import feedparser

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:
    # 插件 Web API 与 WebUI 可视化视图所需的接口（较新版本 AstrBot 才提供）
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # 兼容旧版本核心：仅跳过 WebUI 刷新面板，不影响其他功能
    error_response = json_response = None
    request = None

# ============================== 常量定义 ==============================

# 官方论坛"官方新闻与公告"版块的 RSS 订阅源（Discourse 论坛）
RSS_URL = "https://forum.warthunder.com/c/official-news-and-information/7.rss"

# 游戏客户端更新标题匹配规则，例如 "Update 2.45.0.112"
UPDATE_TITLE_RE = re.compile(r"Update \d+\.\d+")

# 从 Discourse 主题链接中提取数字主题 ID：
# 例如 https://forum.warthunder.com/t/community-update-xxx/76543 -> "76543"
TOPIC_ID_RE = re.compile(r"/t/[^/]+/(\d+)")

# 请求头：部分站点会拦截缺少 UA 的请求，这里模拟常规浏览器请求
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/json, text/html, */*",
}

# 送入 LLM 的公告原文最大字符数，防止超出模型上下文
MAX_CONTENT_CHARS = 12000

# 单条公告最多携带的图片数量（过多会刷屏，也容易触发 QQ 风控）
MAX_IMAGES = 9

# 图片提取正则：覆盖 Discuz 风格 [img]...[/img]、Markdown ![](url)、HTML <img src="url">
IMAGE_BBCODE_RE = re.compile(r"\[img[^\]]*\]\s*(https?://[^\s\[]+?)\s*\[/img\]", re.IGNORECASE)
IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^\s)]+?)\s*\)")
IMAGE_HTML_RE = re.compile(r"<img[^>]+src=[\"']\s*(https?://[^\"']+?)\s*[\"']", re.IGNORECASE)

# LLM 输出中的图片占位标记，例如 [图片1]；兼容中英文方括号与圆括号写法
#（模型偶尔会把 [图片1] 写成全角圆括号形式（图片1））
IMAGE_SLOT_RE = re.compile(
    r"[\[［【(（]\s*(?:图片|image|img)\s*(\d+)\s*[\]］】)）]", re.IGNORECASE
)

# 模板中的占位说明文字，模型若原样照抄则需要在后处理中剔除
LITERAL_IMAGE_LINE_RE = re.compile(
    r"^\s*图片\s*[（(]\s*如果存在图片\s*[)）]\s*$", re.MULTILINE
)
LITERAL_DESC_LINE_RE = re.compile(
    r"^\s*[（(]\s*图片描述\s*[)）]\s*$", re.MULTILINE
)

# 去重文件最多保留的已处理主题 ID 数量（防止文件无限增长）
MAX_SEEN_IDS = 1000

# 手动查询指令按类型筛选时，类型标识 -> 中文名称（用于「未找到公告」的提示语）
KIND_QUERY_NAMES = {
    "client": "游戏版本更新公告",
    "community": "Community Update 公告",
    "other": "其他官方公告",
}

# 本地存档文件名：按类型保存「完整原文 + LLM 处理结果」，供查询指令直接复用
ARCHIVE_FILENAME = "archive.json"

# 插件名：同时用于 @register 注册与 WebUI 视图后端接口的路由前缀
PLUGIN_NAME = "astrbot_plugin_warthunder_news"

# 默认配置：WebUI 配置缺失或对应字段为空时使用这些兜底值
DEFAULT_CONFIG = {
    # 推送目标的 QQ 群号列表（请修改为你的群号）
    "target_groups": [123456789],
    # 社区管理员（CM）所属的论坛群组名：
    # Community Update 只提取佩戴该群组徽章（页面上用户名旁的
    # <a class="user-group" href="/g/Community Manager">Community Manager</a>）的发言
    "cm_group_name": "Community Manager",
    # 轮询间隔（秒），600 秒 = 10 分钟
    "poll_interval": 600,
    # 单条公告最多携带的图片数量（0 表示完全不发送图片）
    "max_images": MAX_IMAGES,
    # 公告关键词白名单：仅推送标题命中其中任一关键词的公告（空列表 = 不限制）
    "keyword_whitelist": [],
    # 公告关键词黑名单：标题命中即不推送（优先级高于白名单）。
    # 默认屏蔽四类与游戏更新无关的公告（标题为英文，匹配不区分大小写）：
    #   商店类：Store / Pre-order / Pack / Bundle（盖金商店礼包、预购捆绑包等）
    #   电竞类：Esport / Tournament / League / Championship（锦标赛、联赛等）
    #   涂装类：Decal / Camouflage / Skin / WT Live（涂装、贴花、玩家涂装站精选等）
    #   无关游戏内容：Soundtrack / Wallpaper / Fan art / Artwork / Cosplay / Comic / Merch
    #   （原声带、壁纸、同人/美术作品、Cosplay、漫画、周边等）
    "keyword_blacklist": [
        "Store", "Pre-order", "Pack", "Bundle",
        "Esport", "Tournament", "League", "Championship",
        "Decal", "Camouflage", "Skin", "WT Live",
        "Soundtrack", "Wallpaper", "Fan art", "Artwork", "Cosplay", "Comic", "Merch",
    ],
    # 指令触发群白名单：仅这些群号可使用查询指令（空列表 = 不限制）
    "command_group_whitelist": [],
    # 指令使用者白名单（QQ 号）：仅这些 QQ 号可使用查询指令（空列表 = 不限制）
    "command_user_whitelist": [],
    # 人工总结模板：LLM 将严格按此模板输出（WebUI 配置优先）
    "llm_template": (
        "大标题\n"
        "发布者ID：\n"
        "总结后的二级标题1\n"
        "总结后的内容1\n"
        "------\n"
        "总结后的二级标题2\n"
        "总结后的内容2\n"
        "图片（如果存在图片）\n"
        "（图片描述）\n"
        "图片（如果存在图片）\n"
        "（图片描述）\n"
        "-------"
    ),
}


@register(
    PLUGIN_NAME,
    "SatzZz",
    "战争雷霆官方论坛公告监控推送插件（RSS 轮询 + LLM 模板总结 + OneBot 群推送）",
    "1.5.0",
)
class WarThunderNewsPlugin(Star):
    """战争雷霆公告推送插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        # config 由 AstrBot 根据插件目录下的 _conf_schema.json 自动生成并注入，
        # 即 WebUI「插件管理 -> 本插件 -> 配置」页面上用户保存的配置。
        super().__init__(context)
        self.raw_config = config
        cfg = self._load_config(config)

        # ---------- 基本配置 ----------
        self.target_groups: list = self._norm_int_list(cfg["target_groups"])  # 推送目标群号
        self.cm_group_name: str = str(cfg["cm_group_name"]).strip()           # CM 群组名
        self.poll_interval: int = max(60, int(cfg["poll_interval"]))          # 下限 60 秒
        self.max_images: int = max(0, int(cfg["max_images"]))                 # 0 表示不发图片
        self.llm_template: str = str(cfg["llm_template"])

        # ---------- 白名单 ----------
        # 关键词白/黑名单作用于「自动推送」；两个指令白名单作用于查询指令
        self.keyword_whitelist: list = self._norm_str_list(cfg["keyword_whitelist"])
        self.keyword_blacklist: list = self._norm_str_list(cfg["keyword_blacklist"])
        self.command_group_whitelist: set = self._norm_id_set(cfg["command_group_whitelist"])
        self.command_user_whitelist: set = self._norm_id_set(cfg["command_user_whitelist"])

        # ---------- 去重记录（本地 JSON 文件） ----------
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.seen_file = os.path.join(plugin_dir, "seen_topics.json")
        # _first_run=True 表示本地没有任何去重记录，首轮只记录不推送，
        # 避免插件首次安装时把论坛所有历史公告全部推送到群里
        self._first_run = not os.path.exists(self.seen_file)
        self.seen_ids: set = set()  # 已处理主题 ID 集合（字符串）
        self._load_seen()

        # ---------- 本地存档 ----------
        # 按类型（client / community / other）保存「完整原文 + LLM 处理结果」，
        # 查询指令优先直接发送存档内容；轮询发现新版本时会覆盖旧存档。
        self.archive_file = os.path.join(plugin_dir, ARCHIVE_FILENAME)
        self.archives: dict = self._load_archives()

        # ---------- 运行时状态 ----------
        self._session: Optional[aiohttp.ClientSession] = None  # 复用的 HTTP 会话
        self._stopped = False    # 插件卸载/重载标记，用于让轮询协程自行退出
        self._poll_task = None   # 兜底方式创建的轮询任务句柄

        # ---------- WebUI 可视化视图（可选能力） ----------
        self._register_view_apis()

        # ---------- 注册定时任务 ----------
        # 核心会把传入的协程对象包装为后台任务在启动后持续运行。
        # 轮询间隔由 _poll_loop 内部的 asyncio.sleep(self.poll_interval) 控制。
        try:
            self.context.register_task(
                self._poll_loop(), "战争雷霆公告定时轮询（每 10 分钟）"
            )
        except TypeError:
            # 兼容极旧版本核心的 register_task 签名差异，改为自行创建任务
            logger.warning("[战争雷霆公告] register_task 调用失败，改用 asyncio.create_task 兜底。")
            self._poll_task = asyncio.create_task(self._poll_loop())

    # ============================== 配置与去重 ==============================

    def _load_config(self, config) -> dict:
        """合并配置：以内置默认值为底，用 WebUI 注入的配置覆盖。

        config 来自 _conf_schema.json 生成的插件配置（AstrBotConfig）。
        若插件未生成 schema 或核心未注入（config 为 None），则退回内置默认值。
        """
        cfg = dict(DEFAULT_CONFIG)
        if config is None:
            logger.warning("[战争雷霆公告] 未收到 WebUI 配置，使用内置默认配置。")
            return cfg
        try:
            for key in DEFAULT_CONFIG:
                value = config.get(key)
                if value is not None:
                    cfg[key] = value
        except Exception as e:
            logger.error(f"[战争雷霆公告] 读取 WebUI 配置失败：{e}，使用内置默认配置。")
        return cfg

    @staticmethod
    def _norm_str_list(value) -> list:
        """把配置里的列表规范为「去空白、去重」的字符串列表。

        用户在 WebUI 里输入的可能是字符串或数字，也可能误填成单个字符串，
        这里统一容错处理。
        """
        if value is None or value == "":
            return []
        # 字符串/数字等标量统一包装成单元素列表，列表/元组/集合则原样遍历
        if isinstance(value, (str, int, float)):
            value = [value]
        try:
            items = [str(v).strip() for v in value]
        except TypeError:
            return []
        result = []
        for item in items:
            if item and item not in result:
                result.append(item)
        return result

    @classmethod
    def _norm_id_set(cls, value) -> set:
        """把群号/QQ 号列表规范为字符串集合，便于与 event 返回的 ID 比对。"""
        return set(cls._norm_str_list(value))

    @classmethod
    def _norm_int_list(cls, value) -> list:
        """把群号列表规范为整数列表（OneBot 的 group_id 需要整数）。"""
        result = []
        for item in cls._norm_str_list(value):
            try:
                gid = int(item)
            except ValueError:
                logger.warning(f"[战争雷霆公告] 忽略无效群号：{item!r}")
                continue
            if gid not in result:
                result.append(gid)
        return result

    def _load_seen(self) -> None:
        """从本地 JSON 文件加载已处理的主题 ID（重启后不会重复推送）。"""
        try:
            with open(self.seen_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.seen_ids = {str(i) for i in data.get("seen_ids", [])}
            # 旧版本（<=1.4.0）不会把「其他官方公告」记入去重记录，
            # 该标记用于升级后首轮先补记基线，避免一次性推送大量历史公告
            self._other_baselined = bool(data.get("other_baselined"))
            logger.info(f"[战争雷霆公告] 已加载去重记录，共 {len(self.seen_ids)} 条。")
        except FileNotFoundError:
            self.seen_ids = set()
            self._other_baselined = False
        except Exception as e:
            logger.error(f"[战争雷霆公告] 去重记录读取失败：{e}")
            self.seen_ids = set()
            self._other_baselined = False

    def _save_seen(self) -> None:
        """把已处理的主题 ID 持久化到本地 JSON 文件。"""
        try:
            # 只保留最近 MAX_SEEN_IDS 条，防止文件无限增长
            ids = sorted(self.seen_ids)[-MAX_SEEN_IDS:]
            self.seen_ids = set(ids)
            with open(self.seen_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"seen_ids": ids, "other_baselined": self._other_baselined},
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.error(f"[战争雷霆公告] 去重记录保存失败：{e}")

    # ============================== 本地存档 ==============================

    def _load_archives(self) -> dict:
        """从本地 JSON 文件加载三类公告的存档，文件不存在或损坏时返回空字典。"""
        try:
            with open(self.archive_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("存档文件根节点不是字典")
            logger.info(
                "[战争雷霆公告] 已加载本地存档："
                + "、".join(f"{kind}={rec.get('title', '')}" for kind, rec in data.items())
            )
            return data
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"[战争雷霆公告] 本地存档读取失败：{e}")
            return {}

    def _save_archive(self, record: dict) -> bool:
        """写入或更新某类型公告的存档，只有更新的版本才覆盖旧版本。

        新旧比较以 Discourse 主题 ID 为准（主题 ID 随发布时间递增）。
        这样即使同一轮轮询里先处理到新公告、后又处理到更旧的公告，
        也不会把新版本的存档覆盖掉。

        返回 True 表示存档已写入/更新，False 表示因版本更旧而被跳过。
        """
        kind = record.get("kind")
        if not kind:
            return False
        old = self.archives.get(kind)
        if old and self._archive_order(old) > self._archive_order(record):
            logger.info(f"[战争雷霆公告] 存档已是更新版本，忽略较旧公告：{record.get('title')}")
            return False
        self.archives[kind] = record
        try:
            with open(self.archive_file, "w", encoding="utf-8") as f:
                json.dump(self.archives, f, ensure_ascii=False, indent=2)
            logger.info(f"[战争雷霆公告] 本地存档已更新（{kind}）：{record.get('title')}")
            return True
        except Exception as e:
            logger.error(f"[战争雷霆公告] 本地存档写入失败：{e}")
            return False

    @staticmethod
    def _archive_order(record: dict) -> int:
        """存档的新旧排序键：用 Discourse 主题 ID（主题 ID 随发布时间递增）。"""
        try:
            return int(record.get("topic_id") or 0)
        except (TypeError, ValueError):
            return 0

    def _render_record(self, record: dict) -> list:
        """把存档记录渲染成「文本 + 图片」分段，并按当前 max_images 截断图片。

        max_images 为 0 时完全不发送图片；存档里的图片数量多于当前上限时，
        一并剔除多余图片留下的 [图片N] 占位标记，避免刷出无意义的标记文本。
        """
        images = record.get("images") or []
        images = images[: self.max_images] if self.max_images > 0 else []
        summary = record.get("summary") or ""
        summary = IMAGE_SLOT_RE.sub(
            lambda m: m.group(0) if int(m.group(1)) <= len(images) else "", summary
        )
        return self._render_segments(summary, images)

    async def _refresh_archive(self, kind: str) -> Optional[dict]:
        """强制重新抓取并处理指定类型的最新公告，覆盖本地存档。

        与 _manual_query 的区别：这里忽略已有存档，一律完整走一遍
        「抓取 + LLM 处理」，用于论坛刚发公告但轮询尚未捕获、
        或对现有摘要不满意想重新生成的场景。成功返回新存档记录，失败返回 None。
        """
        entries = await self._fetch_rss_entries()
        if not entries:
            logger.error("[战争雷霆公告] 刷新存档失败：无法获取 RSS。")
            return None
        for entry in entries:
            entry_kind = self._classify(html_lib.unescape(str(entry.get("title") or "")))
            if entry_kind != kind:
                continue
            record = await self._process_entry(entry, entry_kind)
            self._save_archive(record)
            return record
        logger.warning(f"[战争雷霆公告] 刷新存档失败：RSS 中未找到 {kind} 类型公告。")
        return None

    def _archive_overview(self) -> dict:
        """汇总本地存档与运行状态，供 WebUI 视图展示。"""
        items = []
        for kind, label in (
            ("client", "游戏版本更新"),
            ("community", "Community Update"),
            ("other", "其他官方公告"),
        ):
            record = self.archives.get(kind) or {}
            items.append(
                {
                    "kind": kind,
                    "label": label,
                    "has_archive": bool(record),
                    "title": record.get("title") or "",
                    "topic_id": record.get("topic_id") or "",
                    "link": record.get("link") or "",
                    "author": record.get("author") or "",
                    "published": record.get("published") or "",
                    "images": len(record.get("images") or []),
                    "raw_length": len(record.get("raw") or ""),
                    "summary_length": len(record.get("summary") or ""),
                    # 摘要预览：视图里展示给用户确认内容是否正确
                    "summary": record.get("summary") or "",
                }
            )
        return {
            "plugin_name": PLUGIN_NAME,
            "poll_interval": self.poll_interval,
            "cm_group_name": self.cm_group_name,
            "target_groups": self.target_groups,
            "max_images": self.max_images,
            "seen_count": len(self.seen_ids),
            "items": items,
        }

    # ============================== 基础工具 ==============================

    def _get_session(self) -> aiohttp.ClientSession:
        """懒加载并复用 aiohttp 会话（复用连接池，减少握手开销）。"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @staticmethod
    def _clean_html(raw: str) -> str:
        """去除 HTML 标签、还原实体、整理空白，得到纯文本。"""
        if not raw:
            return ""
        # 去掉 script/style 块
        text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", raw)
        # 常见块级标签转换为换行，保证段落结构可读
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
        # 去掉其余所有标签
        text = re.sub(r"<[^>]+>", " ", text)
        # 还原 HTML 实体（&amp; &quot; 等）
        text = html_lib.unescape(text)
        # 压缩多余空白
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    @staticmethod
    def _norm_group(value) -> str:
        """归一化群组名，便于比对。

        只保留字母和数字并转小写，这样 "Community Manager"、
        "community-manager"、"/g/Community Manager" 等写法都能互相匹配。
        """
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    def _extract_images(self, raw: str) -> list:
        """从公告原文中按出现顺序提取图片链接（去重，最多 self.max_images 张）。

        兼容三种写法：[img]url[/img]、Markdown ![](url)、HTML <img src="url">。
        只提取网络图片（http/https），本地相对路径一律忽略。
        max_images 为 0 时直接返回空列表，即完全不发送图片。
        """
        if not raw or self.max_images <= 0:
            return []
        found = []
        for regex in (IMAGE_BBCODE_RE, IMAGE_MARKDOWN_RE, IMAGE_HTML_RE):
            for m in regex.finditer(raw):
                url = m.group(1).strip().rstrip(",")
                if url and url not in found:
                    found.append(url)
        return found[:self.max_images]

    @staticmethod
    def _mark_images(text: str, images: list) -> str:
        """把原文中的图片语法替换为 [图片N] 占位标记（N 为图片序号）。

        这样模型在阅读原文时能看到图片所在的位置，输出时把标记带到对应位置，
        推送阶段再把它还原成真实图片，实现"图文对应"。
        """
        if not text or not images:
            return text
        index_of = {url: i for i, url in enumerate(images, 1)}

        def repl(m):
            url = m.group(1).strip().rstrip(",")
            idx = index_of.get(url)
            return f"[图片{idx}]" if idx else m.group(0)

        for regex in (IMAGE_BBCODE_RE, IMAGE_MARKDOWN_RE, IMAGE_HTML_RE):
            text = regex.sub(repl, text)
        return text

    def _render_segments(self, summary: str, images: list) -> list:
        """把 LLM 总结文本渲染成「文本 + 图片」分段，图片插入到对应位置。

        约定：LLM 在模板"图片（如果存在图片）"一行输出占位标记 [图片N]，
        这里把该标记替换为第 N 张图片本体，标记前后仍是文本。

        返回：[("text", 文本) | ("image", 图片链接), ...]
        """
        segments = []  # 结果分段
        pos = 0       # 上一次切分结束的位置
        for m in IMAGE_SLOT_RE.finditer(summary):
            index = int(m.group(1))  # 标记中的图片序号（从 1 开始）
            # 标记之前的纯文本
            head = summary[pos:m.start()].strip()
            if head:
                segments.append(("text", head))
            # 序号有效则插入对应图片，否则保留原标记文本（避免信息丢失）
            if 1 <= index <= len(images):
                segments.append(("image", images[index - 1]))
            else:
                segments.append(("text", m.group(0)))
            pos = m.end()

        # 处理最后一段剩余文本
        tail = summary[pos:].strip()
        if tail:
            segments.append(("text", tail))

        # 兜底：LLM 没按约定输出标记时，直接把解析出的图片统一附在末尾
        if not any(kind == "image" for kind, _ in segments):
            segments.extend(("image", url) for url in images)
        # 兜底：既无文本又无图片时，保证至少有一条文本分段
        if not segments:
            segments.append(("text", summary))
        return segments

    @staticmethod
    def _segments_to_onebot(segments: list) -> list:
        """把分段转换为 OneBot v11 send_group_msg 的消息段数组。"""
        msg = []
        for kind, value in segments:
            if kind == "image":
                msg.append({"type": "image", "data": {"file": value}})
            else:
                msg.append({"type": "text", "data": {"text": value}})
        return msg

    @staticmethod
    def _segments_to_chain(segments: list) -> list:
        """把分段转换为 AstrBot 消息链组件（用于指令回复）。"""
        chain = []
        for kind, value in segments:
            chain.append(Image(file=value) if kind == "image" else Plain(value))
        return chain

    def _title_allowed(self, title: str) -> bool:
        """按关键词白/黑名单判断该公告是否应当推送（作用于自动推送）。

        规则：黑名单优先，标题命中黑名单任一关键词则直接不推送；
        若白名单非空，则标题必须命中白名单中的至少一个关键词才推送；
        两个列表都为空时不做任何限制。关键词匹配不区分大小写。
        """
        lower = (title or "").lower()
        for keyword in self.keyword_blacklist:
            if keyword.lower() in lower:
                return False
        if self.keyword_whitelist:
            return any(keyword.lower() in lower for keyword in self.keyword_whitelist)
        return True

    def _command_allowed(self, event: AstrMessageEvent) -> bool:
        """判断该消息是否允许触发查询指令（/news、/cmnews、/upnews）。

        使用者与触发群两个白名单都为空时不作限制；任一白名单非空，
        则对应维度必须命中才放行。私聊消息没有群号，因此当群白名单非空时，
        私聊一律不放行。
        """
        sender_id = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        if self.command_user_whitelist and sender_id not in self.command_user_whitelist:
            return False
        if self.command_group_whitelist and group_id not in self.command_group_whitelist:
            return False
        return True

    @staticmethod
    def _topic_id(entry) -> str:
        """从 RSS 条目中提取稳定的主题 ID（优先取链接中的数字 ID）。"""
        for key in ("id", "link"):
            url = entry.get(key) or ""
            m = TOPIC_ID_RE.search(url)
            if m:
                return m.group(1)
        # 兜底：直接用 RSS 的 id/link 字符串本身作为去重键
        return (entry.get("id") or entry.get("link") or "").strip()

    @staticmethod
    def _classify(title: str) -> str:
        """按标题给公告分类。

        返回值：
            "client"    -> 游戏客户端更新（标题匹配 Update x.y）
            "community" -> 社区更新（标题包含 Community Update）
            "other"     -> 其他官方公告（除上述两类之外的全部公告）
        """
        title = title or ""
        if UPDATE_TITLE_RE.search(title):
            return "client"
        if "Community Update" in title:
            return "community"
        return "other"

    # ============================== 网络请求 ==============================

    async def _fetch_rss_entries(self) -> list:
        """拉取并解析 RSS，返回条目列表（feedparser 的 entry 字典）。"""
        try:
            async with self._get_session().get(RSS_URL, headers=REQUEST_HEADERS) as resp:
                if resp.status != 200:
                    logger.error(f"[战争雷霆公告] RSS 拉取失败，HTTP 状态码：{resp.status}")
                    return []
                data = await resp.read()  # feedparser 接收字节流，自动处理编码
        except Exception as e:
            logger.error(f"[战争雷霆公告] RSS 拉取异常：{e}")
            return []

        feed = feedparser.parse(data)
        entries = list(feed.entries)
        if feed.bozo and not entries:
            # bozo=1 表示 feed 格式异常；有条目则容忍，无条目则视为失败
            logger.warning(f"[战争雷霆公告] RSS 解析异常：{getattr(feed, 'bozo_exception', '')}")
        return entries

    async def _get_json(self, url: str) -> Optional[dict]:
        """请求 JSON 接口并返回解析结果，失败返回 None。"""
        try:
            async with self._get_session().get(url, headers=REQUEST_HEADERS) as resp:
                if resp.status != 200:
                    logger.error(f"[战争雷霆公告] 请求失败（HTTP {resp.status}）：{url}")
                    return None
                return await resp.json(content_type=None)  # Discourse 返回的 Content-Type 有时不标准
        except Exception as e:
            logger.error(f"[战争雷霆公告] 请求异常：{url} -> {e}")
            return None

    async def _fetch_first_post(self, topic_link: str) -> str:
        """抓取主题详情页首帖的完整正文（Markdown 原文优先，其次清洗 HTML）。

        用于「游戏版本更新」类公告：RSS 摘要可能被截断，
        这里直接取详情页首帖的完整内容，保证「完整获取」。
        获取失败时返回空字符串，由调用方决定降级方案。
        """
        if not topic_link:
            return ""
        # https://forum.warthunder.com/t/xxx/76543 -> https://forum.warthunder.com/t/xxx/76543.json
        url = topic_link.rstrip("/") + ".json?include_raw=1"
        data = await self._get_json(url)
        if not data:
            return ""
        posts = (data.get("post_stream") or {}).get("posts") or []
        if not posts:
            return ""
        first = posts[0]
        text = str(first.get("raw") or "").strip()
        if not text:
            text = self._clean_html(str(first.get("cooked") or ""))
        return text

    async def _fetch_cm_posts(self, topic_link: str) -> list:
        """抓取社区更新主题详情页，只提取 CM（社区管理员）的发言。

        实现：把 RSS 里的主题链接转为 Discourse JSON 接口（链接后拼 .json），
        从 post_stream.posts 中筛选「佩戴 CM 群组徽章」的楼层，
        优先取 Markdown 原文（raw），没有则把 HTML（cooked）清洗为纯文本。

        CM 的判定不依赖任何硬编码用户名，而是依据页面上显示在发帖人名字旁的
        群组徽章：<a class="user-group" href="/g/Community Manager">Community Manager</a>。
        该徽章在帖子 JSON 中对应 primary_group_name / flair_name 字段，
        以及 title_is_group 为真时的 user_title 字段，与配置的 cm_group_name 比对。

        返回：[(用户名, 发言文本), ...]，找不到 CM 发言时返回空列表。
        """
        if not topic_link:
            return []
        # https://forum.warthunder.com/t/xxx/76543 -> https://forum.warthunder.com/t/xxx/76543.json
        url = topic_link.rstrip("/") + ".json?include_raw=1"
        data = await self._get_json(url)
        if not data:
            return []

        target_group = self._norm_group(self.cm_group_name)
        posts = (data.get("post_stream") or {}).get("posts") or []
        result = []
        for post in posts:
            username = str(post.get("username") or "").strip()
            # 收集该帖中所有可能承载群组徽章的字段，逐一与目标群组名比对
            group_flags = [
                post.get("primary_group_name"),  # 主要身份组
                post.get("flair_name"),          # 群组徽章（flair）名称
            ]
            # 头衔本身就是群组时（title_is_group=True），头衔也代表群组徽章
            if post.get("title_is_group"):
                group_flags.append(post.get("user_title"))
            is_cm = any(self._norm_group(flag) == target_group for flag in group_flags)
            if not is_cm:
                continue  # 只保留 CM 的帖子
            text = str(post.get("raw") or "").strip()
            if not text:
                text = self._clean_html(str(post.get("cooked") or ""))
            if text:
                result.append((username, text))
        return result

    # ============================== 内容处理 ==============================

    async def _llm_summarize(self, title: str, content: str, images: list, author: str) -> str:
        """调用 AstrBot 当前启用的 LLM，按人工模板总结公告原文。

        要求模型把内容翻译为简体中文、保留所有数值与条件细节；涉及军武装备时
        使用具体型号称呼并尽量对齐《战争雷霆》游戏内的载具译名。图片不做识别，
        模板中的图片描述行由模型引用原文里图片附近的文字说明来完成。
        未配置 LLM 或调用失败时，降级返回原文摘录，保证推送不中断。
        """
        # 截断过长原文，避免超出模型上下文
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + "\n（内容过长已截断）"

        try:
            provider = self.context.get_using_provider()
        except Exception as e:
            logger.error(f"[战争雷霆公告] 获取 LLM Provider 失败：{e}")
            provider = None

        if provider is None:
            logger.warning("[战争雷霆公告] 未配置可用的 LLM，降级为返回原文摘录。")
            return "（未配置 LLM，以下为原文摘录）\n" + content[:800]

        # 组装输出要求：通用规则 + 图片相关规则
        rules = [
            "1. 输出必须全部使用简体中文：原文（包括标题）中的英文等内容一律翻译成"
            "中文，不要整句保留英文；",
            "2. 涉及特定游戏或现实中军武装备（坦克、飞机、军舰、武器系统等）时，"
            "必须写出对应的具体装备型号称呼，不要只写「坦克」「战斗机」这类泛称；",
            "3. 装备名称优先采用《战争雷霆》中文客户端的载具译名"
            "（例如 Mustang 写「野马」、Panther 写「黑豹」、Abrams 写「艾布拉姆斯」、"
            "Leopard 写「豹」、Spitfire 写「喷火」）；若无法确定游戏内译名，"
            "就保留原文的型号代号，不要自创译名；",
            "4. 型号代号中的字母和数字必须原样保留（如 T-34-85、M4A1、F-4E、"
            "MiG-21bis、Leopard 2A7V），不得翻译或改写；",
            "5. 保留原文中的所有数值（版本号、日期、时间、倍率、折扣比例、"
            "活动时长、兑换价格等）；",
            "6. 保留所有条件细节（参与条件、限制、注意事项）；",
            "7. 模板中的每个栏目都要输出，原文没有涉及的栏目可留空，但不要删除栏目；",
            f"8. 「大标题」填翻译成中文后的公告标题，「发布者ID」填发帖人：{author}；",
            "9. 不要编造原文没有的内容，只输出总结结果本身，不要用代码块包裹。",
        ]
        if images:
            # 原文中已用 [图片N] 标记图片位置，模型只需原样保留这些标记即可
            rules += [
                "10. 原文中已经用 [图片N] 标出了每张图片的位置（见原文图片清单），"
                "总结时必须把这些标记原样保留在原文对应的位置，保持图文对应，"
                "不得移动、合并、删除或改写为其他格式；",
                "11. 每张图片标记后另起一行，粘贴原文中该图片附近的文字说明"
                "（本插件不识别图片内容，请勿根据图片画面本身编造描述）；"
                "原文没有说明文字时可以不写；",
                "12. 模板中的「图片（如果存在图片）」和「（图片描述）」只是格式占位"
                "说明，严禁把这两行文字原样输出到总结里；",
            ]
            image_list = "\n".join(f"[图片{i}] {url}" for i, url in enumerate(images, 1))
        else:
            rules.append(
                "10. 本公告没有图片，模板中的「图片（如果存在图片）」"
                "和「（图片描述）」两行整体省略，且严禁把这两行文字原样输出。"
            )
            image_list = "（无）"

        # 组装提示词：模板 + 标题 + 发布者 + 图片清单 + 原文
        prompt = (
            "请严格按照下面的模板格式总结这篇《战争雷霆》官方公告。\n"
            "要求：\n" + "\n".join(rules) + "\n\n"
            f"【总结模板】\n{self.llm_template}\n\n"
            f"【公告标题】\n{title}\n\n"
            f"【发布者ID】\n{author}\n\n"
            f"【原文图片清单】\n{image_list}\n\n"
            f"【公告原文】\n{content}"
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=(
                    "你是《战争雷霆》官方公告的总结与翻译助手。输出必须全部使用简体"
                    "中文，严格遵循用户给出的模板结构，保留全部数值与条件细节；涉及"
                    "军武装备时必须写出具体型号称呼并尽量对齐游戏内的载具译名，只输出"
                    "总结内容本身。"
                ),
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if text:
                # 后处理：剔除模型可能原样照抄的模板占位说明行
                #（「图片（如果存在图片）」「（图片描述）」本身不是要输出的内容），
                # 并收敛因此产生的多余空行
                text = LITERAL_IMAGE_LINE_RE.sub("", text)
                text = LITERAL_DESC_LINE_RE.sub("", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                return text
            logger.error("[战争雷霆公告] LLM 返回内容为空，降级为原文摘录。")
        except Exception as e:
            logger.error(f"[战争雷霆公告] LLM 总结失败：{e}，降级为原文摘录。")
        return "（LLM 总结失败，以下为原文摘录）\n" + content[:800]

    async def _process_entry(self, entry, kind: str) -> dict:
        """完整抓取公告原文并调用 LLM 处理，返回可直接存档的记录。

        返回的 dict 同时包含「完整原文」与「LLM 处理结果」，会写入本地存档，
        供查询指令与自动推送复用（见 _save_archive / _render_record）。
        """
        title = html_lib.unescape(str(entry.get("title") or "")).strip()
        link = str(entry.get("link") or "").strip()
        author = html_lib.unescape(str(entry.get("author") or "")).strip()

        # 1. 按类型完整抓取原文；is_html 标记原文是否为 HTML（决定是否需要清洗）
        if kind == "community":
            # 社区更新：抓取详情页并只提取 CM 的发言（Markdown 原文）
            is_html = False
            cm_posts = await self._fetch_cm_posts(link)
            if cm_posts:
                source = "\n\n".join(f"[{name}]\n{text}" for name, text in cm_posts)
                # 发布者ID 取所有发言过的 CM 用户名
                author = "、".join(dict.fromkeys(name for name, _ in cm_posts))
            else:
                source = str(entry.get("summary") or "")
                is_html = True
                logger.warning(f"[战争雷霆公告] 未在主题中找到 CM 发言，降级使用 RSS 摘要：{title}")
            kind_label = "社区更新（CM 发言）"
        else:
            # 游戏版本更新 / 其他官方公告：抓取详情页首帖完整正文（Markdown 原文）
            source = await self._fetch_first_post(link)
            is_html = False
            if not source:
                # 详情页抓取失败时降级使用 RSS 摘要（Discourse 的 description，HTML）
                source = str(entry.get("summary") or "")
                is_html = True
                logger.warning(f"[战争雷霆公告] 详情页获取失败，降级使用 RSS 摘要：{title}")
            kind_label = "游戏客户端更新" if kind == "client" else "其他官方公告"

        if not source:
            source = "（未获取到正文内容，请点击原文链接查看）"

        # 2. 提取图片，并把原文里的图片语法替换成 [图片N] 标记，保留图文位置
        images = self._extract_images(source)
        marked = self._mark_images(source, images)

        # 3. HTML 原文清洗为纯文本；Markdown 原文保持原样
        content = self._clean_html(marked) if is_html else marked
        if not content.strip():
            content = "（未获取到正文内容，请点击原文链接查看）"

        # 4. LLM 按模板总结
        summary = await self._llm_summarize(title, content, images, author or "官方")

        # 5. 追加原文链接（并标注类型）
        summary = (
            summary.rstrip()
            + f"\n\n——————————————\n类型：{kind_label}\n原文链接：{link}"
        )

        return {
            "kind": kind,
            "topic_id": self._topic_id(entry),
            "title": title,
            "link": link,
            "author": author or "官方",
            "published": str(entry.get("published") or ""),
            "raw": source,        # 完整原文（未处理）
            "summary": summary,   # LLM 处理后的文本（含 [图片N] 占位标记）
            "images": images,     # 原文中的图片链接列表
        }

    # ============================== 消息推送 ==============================

    async def _send_to_groups(self, segments: list) -> bool:
        """通过 OneBot v11 的 send_group_msg 动作把公告推送到所有目标群。

        segments 为 _render_record 返回的「文本 + 图片」分段列表，
        文本与图片按顺序组成一条消息，图片会出现在原文对应的位置。

        返回 True 表示至少成功推送到一个群；全部失败返回 False
        （失败时本轮不写入去重记录，下一轮轮询会自动重试）。
        """
        if not self.target_groups:
            logger.warning("[战争雷霆公告] 未配置推送目标群号（target_groups），跳过推送。")
            return False

        # 按适配器类型名查找 aiocqhttp（OneBot v11）平台实例
        platform = self.context.get_platform("aiocqhttp")
        if platform is None:
            logger.error("[战争雷霆公告] 未找到 aiocqhttp（OneBot v11）平台适配器，无法推送。")
            return False
        bot = getattr(platform, "bot", None)
        if bot is None:
            logger.error("[战争雷霆公告] aiocqhttp 适配器尚未就绪（bot 实例为空），无法推送。")
            return False

        # OneBot v11 标准消息段数组（文本段与图片段混排）
        message = self._segments_to_onebot(segments)
        success = False
        for gid in self.target_groups:
            try:
                await bot.call_action("send_group_msg", group_id=int(gid), message=message)
                success = True
                logger.info(f"[战争雷霆公告] 公告已推送至群 {gid}")
            except Exception as e:
                logger.error(f"[战争雷霆公告] 推送到群 {gid} 失败：{e}")
            # 群与群之间稍作间隔，避免发送过快触发风控
            await asyncio.sleep(1.0)
        return success

    # ============================== 轮询与查询 ==============================

    async def _poll_once(self) -> None:
        """执行一次轮询：检测新公告 -> 分类 -> 处理并归档 -> 过滤后推送 -> 记录去重。"""
        entries = await self._fetch_rss_entries()
        if not entries:
            return

        # 首次运行：只把当前所有条目记为已见，不推送历史公告
        if self._first_run:
            for entry in entries:
                tid = self._topic_id(entry)
                if tid:
                    self.seen_ids.add(tid)
            self._other_baselined = True  # 全新安装的首轮基线已覆盖所有类型
            self._save_seen()
            self._first_run = False
            logger.info("[战争雷霆公告] 首次运行完成基线记录，此后仅推送新公告。")
            return

        # 从旧版本（<=1.4.0）升级：旧版本不会把「其他官方公告」记入去重记录，
        # 首轮先把当前 RSS 中的其他类型公告补记为已见，避免一次性推送大量历史公告
        if not self._other_baselined:
            for entry in entries:
                title = html_lib.unescape(str(entry.get("title") or ""))
                if self._classify(title) != "other":
                    continue
                tid = self._topic_id(entry)
                if tid:
                    self.seen_ids.add(tid)
            self._other_baselined = True
            self._save_seen()
            logger.info("[战争雷霆公告] 已完成「其他官方公告」基线补记，此后仅推送新公告。")

        # 遍历全部条目（RSS 按最新在前排列），处理所有未见过的新公告
        for entry in entries:
            if self._stopped:
                return
            tid = self._topic_id(entry)
            if not tid or tid in self.seen_ids:
                continue
            title = html_lib.unescape(str(entry.get("title") or ""))
            kind = self._classify(title)

            logger.info(f"[战争雷霆公告] 发现新公告：{title}")
            # 完整抓取原文 + LLM 处理，并写入本地存档（出现新版本时覆盖旧存档）
            record = await self._process_entry(entry, kind)
            self._save_archive(record)

            # 关键词白/黑名单只决定「是否推送到群」，不影响本地存档的更新
            if not self._title_allowed(title):
                logger.info(f"[战争雷霆公告] 标题命中关键词过滤规则，不推送：{title}")
                self.seen_ids.add(tid)
                self._save_seen()
                continue

            if await self._send_to_groups(self._render_record(record)):
                # 至少一个群推送成功才算处理完成，写入去重记录
                self.seen_ids.add(tid)
                self._save_seen()
            else:
                logger.error("[战争雷霆公告] 本轮推送失败，下一轮轮询将重试该公告。")

    async def _poll_loop(self) -> None:
        """定时轮询主循环：每 poll_interval 秒执行一次 _poll_once。"""
        # 等待 AstrBot 平台适配器完成连接
        await asyncio.sleep(10)
        logger.info(f"[战争雷霆公告] 定时轮询已启动，间隔 {self.poll_interval} 秒。")
        while not self._stopped:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 任何异常都不能让轮询协程退出，记录后继续下一轮
                logger.exception(f"[战争雷霆公告] 轮询出现异常：{e}")
            await asyncio.sleep(self.poll_interval)
        logger.info("[战争雷霆公告] 定时轮询已停止。")

    async def _manual_query(self, kind: str) -> list:
        """处理手动查询指令：优先返回本地存档，无存档时才实时抓取并归档。

        RSS 条目按发布时间倒序排列，因此「第一个命中的条目」就是时间上最新的一条。
        kind 为 "client" / "community" / "other"，只返回对应类型的最新一条。

        返回：[("text", 文本) | ("image", 图片链接), ...]

        注意：手动查询只做展示，不触发群推送、不改动自动推送的去重记录。
        """
        # 1. 优先使用本地存档（由轮询自动更新，出现新版本时会覆盖旧存档）
        record = self.archives.get(kind)
        if record:
            return self._render_record(record)

        # 2. 本地没有可用存档：实时抓取最新一条、处理并写入存档
        entries = await self._fetch_rss_entries()
        if not entries:
            return [("text", "暂时无法获取战争雷霆论坛公告，请稍后再试。")]

        for entry in entries:
            entry_kind = self._classify(html_lib.unescape(str(entry.get("title") or "")))
            if entry_kind != kind:
                continue  # 跳过其他类型的公告
            record = await self._process_entry(entry, entry_kind)
            self._save_archive(record)
            return self._render_record(record)

        return [("text", f"暂未在论坛找到{KIND_QUERY_NAMES[kind]}。")]

    # ============================== WebUI 视图接口 ==============================

    def _register_view_apis(self) -> None:
        """注册 WebUI 可视化视图（pages/archive-manager）所需的后端接口。

        该能力依赖较新版本的 AstrBot（提供 context.register_web_api 与
        astrbot.api.web）。旧版本上仅跳过注册并给出提示，不影响插件其他功能，
        只是 pages/ 目录下的页面不会被加载。
        """
        register_web_api = getattr(self.context, "register_web_api", None)
        if register_web_api is None or json_response is None:
            logger.warning(
                "[战争雷霆公告] 当前 AstrBot 版本不支持插件 Web API，"
                "WebUI 的「公告存档」刷新面板不可用（其余功能不受影响）。"
            )
            return
        try:
            register_web_api(
                f"/{PLUGIN_NAME}/archive",
                self.api_archive_list,
                ["GET"],
                "公告本地存档概览",
            )
            register_web_api(
                f"/{PLUGIN_NAME}/archive/refresh",
                self.api_archive_refresh,
                ["POST"],
                "强制刷新公告本地存档",
            )
        except Exception as e:
            logger.error(f"[战争雷霆公告] 注册 WebUI 视图接口失败：{e}")

    async def api_archive_list(self):
        """视图接口：返回三类公告的本地存档概览与运行状态。"""
        return json_response(self._archive_overview())

    async def api_archive_refresh(self):
        """视图接口：强制刷新指定类型的本地存档。

        请求体：{"kind": "client" | "community" | "other" | "all"}
        """
        payload = await request.json(default={})
        kind = str(payload.get("kind") or "").strip()
        if kind == "all":
            kinds = ["client", "community", "other"]
        elif kind in ("client", "community", "other"):
            kinds = [kind]
        else:
            return error_response("kind 必须是 client、community、other 或 all", status_code=400)

        refreshed, failed = [], []
        for item in kinds:
            try:
                record = await self._refresh_archive(item)
            except Exception as e:
                logger.exception(f"[战争雷霆公告] 刷新存档异常（{item}）：{e}")
                record = None
            (refreshed if record else failed).append(item)

        data = self._archive_overview()
        data["refreshed"] = refreshed
        data["failed"] = failed
        return json_response(data)

    # ============================== 指令处理 ==============================

    async def _manual_reply(self, event: AstrMessageEvent, kind: str, tip: str):
        """手动查询指令的公共回复流程：白名单校验 -> 查询 -> 返回消息链。

        三个查询指令（/news、/cmnews、/upnews）只有查询类型和提示语不同，
        这里统一处理，避免重复代码。
        """
        # 指令白名单校验：未通过时不作任何回复，避免被用来探测
        if not self._command_allowed(event):
            logger.info(
                f"[战争雷霆公告] 忽略指令：用户 {event.get_sender_id()} "
                f"群 {event.get_group_id() or '私聊'} 不在指令白名单内。"
            )
            return
        yield event.plain_result(tip)
        try:
            segments = await self._manual_query(kind)
        except Exception as e:
            logger.exception(f"[战争雷霆公告] 手动查询失败：{e}")
            yield event.plain_result(f"查询失败：{e}")
            return
        yield event.chain_result(self._segments_to_chain(segments))

    @filter.command("news")
    async def news(self, event: AstrMessageEvent):
        """播报最新的其他官方公告（除 Community Update 与游戏版本更新之外）。

        群内发送 /news 触发（"/" 为 AstrBot 的默认唤醒前缀，
        指令名本身是 "news"，实际触发词由 AstrBot 唤醒前缀配置决定）。
        返回的消息链中会包含公告图片（若有）。

        若配置了指令触发群白名单或使用者白名单，不在名单内的消息会被静默忽略。
        """
        async for result in self._manual_reply(
            event, "other", "正在查询最新的战争雷霆官方公告，请稍候……"
        ):
            yield result

    @filter.command("cmnews")
    async def cmnews(self, event: AstrMessageEvent):
        """播报最新的 Community Update（社区更新）公告。

        触发方式与指令白名单规则同 /news。
        """
        async for result in self._manual_reply(
            event, "community", "正在查询最新的 Community Update，请稍候……"
        ):
            yield result

    @filter.command("upnews")
    async def upnews(self, event: AstrMessageEvent):
        """播报最新的游戏版本更新（客户端更新）公告。

        触发方式与指令白名单规则同 /news。
        """
        async for result in self._manual_reply(
            event, "client", "正在查询最新的游戏版本更新公告，请稍候……"
        ):
            yield result

    # ============================== 生命周期 ==============================

    async def terminate(self):
        """插件卸载/重载时清理资源：停止轮询、关闭 HTTP 会话。"""
        self._stopped = True
        if self._poll_task is not None:
            self._poll_task.cancel()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("[战争雷霆公告] 插件已卸载，资源已释放。")
