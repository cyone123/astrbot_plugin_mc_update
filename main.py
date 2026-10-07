import asyncio
import html
import json
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List, Set
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
ZENDESK_RELEASE_SECTION = "https://feedback.minecraft.net/api/v2/help_center/en-us/sections/360001186971/articles.json"
ZENDESK_SNAPSHOT_SECTION = "https://feedback.minecraft.net/api/v2/help_center/en-us/sections/360002267532/articles.json"
WIKI_API_URL = "https://minecraft.wiki/api.php"


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> Set[int]:
    """解析单个 Cron 字段（支持 *, */n, a-b, a-b/n, a,b,c）"""
    result: Set[int] = set()
    for part in field_str.split(','):
        part = part.strip()
        if not part:
            continue
        if '/' in part:
            subparts = part.split('/')
            step = int(subparts[1])
            if subparts[0] == '*' or subparts[0] == '':
                start, end = min_val, max_val
            elif '-' in subparts[0]:
                start, end = map(int, subparts[0].split('-'))
            else:
                start = int(subparts[0])
                end = max_val
            result.update(range(start, end + 1, step))
        elif '-' in part:
            start, end = map(int, part.split('-'))
            result.update(range(start, end + 1))
        elif part == '*':
            result.update(range(min_val, max_val + 1))
        else:
            result.add(int(part))
    return result


def get_next_cron_time(cron_str: str, from_time: Optional[datetime] = None) -> datetime:
    """
    计算给定 5 位标准 Cron 表达式（分 时 日 月 周）的下一个触发时间。
    周字段支持 0-7，其中 0 和 7 均代表周日。
    """
    fields = cron_str.strip().split()
    if len(fields) != 5:
        raise ValueError("Cron 表达式必须包含 5 个字段（分 时 日 月 周），例如 '*/30 * * * *'")

    minutes = _parse_cron_field(fields[0], 0, 59)
    hours = _parse_cron_field(fields[1], 0, 23)
    doms = _parse_cron_field(fields[2], 1, 31)
    months = _parse_cron_field(fields[3], 1, 12)
    dows = _parse_cron_field(fields[4], 0, 7)
    if 7 in dows:
        dows.add(0)

    curr = (from_time or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)

    # 循环搜索未来匹配时间点（最多搜索 5 年，约 5*366*1440 次，遇非匹配月份或日期快速跳跃）
    for _ in range(5 * 366 * 1440):
        if curr.month not in months:
            # 快速跳至下月 1 日 0:00
            if curr.month == 12:
                curr = datetime(curr.year + 1, 1, 1, 0, 0)
            else:
                curr = datetime(curr.year, curr.month + 1, 1, 0, 0)
            continue
        if curr.day not in doms:
            # 快速跳至次日 0:00
            curr = (curr + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        # Python weekday: 0 是周一，6 是周日。Cron dow: 0 是周日，1 是周一，6 是周六
        cron_dow = (curr.weekday() + 1) % 7
        if cron_dow not in dows:
            curr = (curr + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if curr.hour not in hours:
            # 快速跳至下一个整点
            curr = (curr + timedelta(hours=1)).replace(minute=0)
            continue
        if curr.minute not in minutes:
            curr += timedelta(minutes=1)
            continue
        return curr
    raise ValueError("在未来 5 年内未找到符合该 Cron 表达式的触发时间")


@register(
    "astrbot_plugin_mc_update",
    "cyone123",
    "Minecraft 版本更新自动检测与 LLM 总结推送插件，支持 QQ 官方机器人",
    "1.0.0"
)
class MinecraftUpdatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._check_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def initialize(self):
        """插件初始化，启动后台轮询检测任务"""
        # 初始化存储的最新版本（避免初次启动把已有版本当更新全部推送）
        try:
            last_rel = await self._get_stored_version("mc_last_release")
            if not last_rel:
                manifest = await self._fetch_version_manifest()
                if manifest and "latest" in manifest:
                    cur_rel = manifest["latest"].get("release")
                    cur_snap = manifest["latest"].get("snapshot")
                    if cur_rel:
                        await self._set_stored_version("mc_last_release", cur_rel)
                    if cur_snap:
                        await self._set_stored_version("mc_last_snapshot", cur_snap)
                    logger.info(f"[MC Update] 初始化记录当前最新版本: Release={cur_rel}, Snapshot={cur_snap}")
        except Exception as e:
            logger.warning(f"[MC Update] 初始化版本记录失败: {e}")

        # 启动后台检测轮询任务
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("[MC Update] Minecraft 更新检测插件已初始化，后台轮询任务已启动")

    async def terminate(self):
        """插件卸载，优雅终止轮询任务与网络会话"""
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[MC Update] Minecraft 更新检测插件已卸载")

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 aiohttp 会话"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=25)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "AstrBot-MinecraftUpdatePlugin/1.0 (https://github.com/cyone123/astrbot_plugin)"}
            )
        return self._session

    # ==================== 版本持久化辅助 ====================

    async def _get_stored_version(self, key: str) -> Optional[str]:
        """优先使用 AstrBot KV 存储，兼容 config 回退"""
        try:
            val = await self.get_kv_data(key, None)
            if val:
                return str(val)
        except Exception:
            pass
        return self.config.get(key, None)

    async def _set_stored_version(self, key: str, val: str):
        """保存版本号到 KV 存储与 config"""
        try:
            await self.put_kv_data(key, val)
        except Exception:
            pass
        self.config[key] = val
        self.config.save_config()

    # ==================== 后台轮询与更新检测 ====================

    async def _check_loop(self):
        """后台轮询与 Cron 定时检测主循环"""
        while True:
            cron_expr = str(self.config.get("cron_expression", "")).strip()
            sleep_seconds = None

            if cron_expr:
                try:
                    now = datetime.now()
                    next_time = get_next_cron_time(cron_expr, now)
                    sleep_seconds = max(1.0, (next_time - now).total_seconds())
                    logger.info(f"[MC Update] 下次 Cron 检测时间: {next_time.strftime('%Y-%m-%d %H:%M:%S')} (等待 {int(sleep_seconds)} 秒)")
                except Exception as e:
                    logger.warning(f"[MC Update] Cron 表达式 '{cron_expr}' 解析失败: {e}，将回退至轮询间隔")

            if sleep_seconds is None:
                sleep_seconds = max(60, int(self.config.get("check_interval", 1800)))

            try:
                await asyncio.sleep(sleep_seconds)
                await self._check_updates()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MC Update] 检查更新时出现异常: {e}", exc_info=True)

    async def _fetch_version_manifest(self) -> Optional[dict]:
        """获取 Mojang 官方 Version Manifest V2"""
        session = await self._get_session()
        try:
            async with session.get(MANIFEST_URL) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                else:
                    logger.warning(f"[MC Update] 获取 Version Manifest 状态码非 200: {resp.status}")
        except Exception as e:
            logger.error(f"[MC Update] 请求 Version Manifest 失败: {e}")
        return None

    def _clean_html(self, raw_html: str) -> str:
        """清洗 HTML 内容为纯文本结构，去除冗余空行与孤立符号"""
        text = re.sub(r'<(script|style).*?</\1>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</?(h[1-6]|p|div|section|article)[^>]*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<li[^>]*>', '\n- ', text, flags=re.IGNORECASE)
        text = re.sub(r'</?li[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        text = html.unescape(text)
        lines = []
        for line in text.splitlines():
            line = line.strip()
            # 过滤空行或单独的破折号
            if not line or line == '-':
                continue
            lines.append(line)
        return '\n'.join(lines)

    @staticmethod
    def _match_version_title(version_id: str, title: str) -> bool:
        """
        匹配版本 ID 与文章标题。
        兼容以下命名规则:
        - 26.4-snapshot-1 <-> Minecraft Java Edition - 26.4 Snapshot 1
        - 26.3-rc-2 <-> Minecraft Java Edition - 26.3 Release Candidate 2
        - 26.3-pre-3 <-> Minecraft Java Edition - 26.3 Pre-release 3
        - 26.3 <-> Minecraft Java Edition - 26.3
        - 24w46a <-> Minecraft: Java Edition - Snapshot 24w46a
        """
        if version_id.lower() in title.lower():
            return True

        def normalize(s: str) -> list:
            s = s.lower()
            s = s.replace('release candidate', 'rc')
            s = s.replace('pre-release', 'pre').replace('pre release', 'pre')
            s = re.sub(r'[^a-z0-9.]+', ' ', s)
            return s.split()

        vt = normalize(version_id)
        tt = normalize(title)
        if not vt or not tt:
            return False

        n_v = len(vt)
        for i in range(len(tt) - n_v + 1):
            if tt[i:i + n_v] == vt:
                return True
        return False

    async def _fetch_changelog(self, version_id: str, version_type: str) -> Tuple[str, str, str]:
        """
        获取指定版本的更新日志与文章链接
        返回: (title, article_url, cleaned_content)
        """
        session = await self._get_session()
        section_url = ZENDESK_RELEASE_SECTION if version_type == "release" else ZENDESK_SNAPSHOT_SECTION

        # 1. 尝试从 Mojang 官方 Zendesk 获取
        try:
            async with session.get(section_url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    articles = data.get("articles", [])
                    # 匹配标题中含有版本号的文章
                    for art in articles:
                        title = art.get("title", "")
                        if self._match_version_title(version_id, title):
                            art_url = art.get("html_url", "")
                            raw_body = art.get("body", "")
                            cleaned = self._clean_html(raw_body)
                            logger.info(f"[MC Update] 成功在 Zendesk 找到文章: {title} ({art_url})")
                            return title, art_url, cleaned
        except Exception as e:
            logger.warning(f"[MC Update] 从 Zendesk 获取文章失败: {e}")

        # 2. 备选：尝试从 Minecraft.wiki API 获取
        try:
            wiki_page_title = f"Java_Edition_{version_id}"
            params = {
                "action": "query",
                "titles": wiki_page_title,
                "prop": "extracts",
                "explaintext": "1",
                "format": "json"
            }
            async with session.get(WIKI_API_URL, params=params) as resp:
                if resp.status == 200:
                    wiki_data = await resp.json(content_type=None)
                    pages = wiki_data.get("query", {}).get("pages", {})
                    for pid, pdata in pages.items():
                        if pid != "-1" and "extract" in pdata:
                            extract = pdata["extract"].strip()
                            if extract:
                                wiki_url = f"https://minecraft.wiki/w/{wiki_page_title}"
                                logger.info(f"[MC Update] 成功在 Minecraft Wiki 找到内容: {wiki_page_title}")
                                return f"Minecraft Java Edition {version_id}", wiki_url, extract
        except Exception as e:
            logger.warning(f"[MC Update] 从 Minecraft Wiki 获取失败: {e}")

        # 3. 缺省备用信息
        default_url = f"https://www.minecraft.net/article/minecraft-java-edition-{version_id.replace('.', '-')}"
        default_content = f"Minecraft Java Edition 发布了新版本 {version_id}（类型: {version_type}）。请前往官方网站查看详细更新日志。"
        return f"Minecraft Java Edition {version_id}", default_url, default_content

    async def _get_chat_provider(self) -> Tuple[Optional[str], Optional[Any]]:
        """
        获取用于总结的聊天模型 Provider ID 与 Provider 实例。
        解析顺序：
        1. 插件配置中显式指定的 chat_provider_id
        2. AstrBot 系统当前/默认聊天提供商 (get_using_provider_async(None))
        """
        # 1. 优先使用插件配置中显式指定的 Provider ID
        configured_id = str(self.config.get("chat_provider_id", "")).strip()
        if configured_id:
            prov = None
            if hasattr(self.context, "get_provider_by_id"):
                try:
                    prov = self.context.get_provider_by_id(configured_id)
                except Exception:
                    pass
            if prov:
                return configured_id, prov
            logger.warning(
                f"[MC Update] 配置的 chat_provider_id '{configured_id}' 未在 AstrBot 中找到或未激活，将尝试使用系统默认模型"
            )

        # 2. 获取 AstrBot 全局当前/默认聊天提供商
        try:
            if hasattr(self.context, "get_using_provider_async"):
                prov = await self.context.get_using_provider_async(None)
                if prov:
                    pid = prov.meta().id if hasattr(prov, "meta") else prov.provider_config.get("id")
                    if pid:
                        return pid, prov
        except Exception as e:
            logger.warning(f"[MC Update] 获取系统默认提供商失败: {e}")

        return None, None

    async def _get_chat_provider_id(self, umo: Optional[str] = None) -> Optional[str]:
        """获取用于总结的聊天模型 Provider ID（兼容旧接口）"""
        pid, _ = await self._get_chat_provider()
        return pid

    async def _summarize_changelog(
        self,
        version_id: str,
        version_type: str,
        changelog_text: str,
        article_url: str,
        umo: Optional[str] = None
    ) -> str:
        """调用 LLM 生成更新日志的中文精简总结"""
        # 智能截断过长日志以节省 Token（默认截取前 2500 字符，对齐至完整换行）
        max_chars = max(500, int(self.config.get("max_content_chars", 2500)))
        if len(changelog_text) > max_chars:
            last_nl = changelog_text.rfind('\n', 0, max_chars)
            cut_idx = last_nl if last_nl > max(300, max_chars // 2) else max_chars
            truncated_changelog = changelog_text[:cut_idx].rstrip()
            truncated_changelog += "\n\n...(为节省 Token，后续详细技术微调与 Bug 列表已自动省略)..."
        else:
            truncated_changelog = changelog_text

        default_prompt_tmpl = (
            "你是一个 Minecraft 资讯播报助手。请根据以下 Minecraft 更新日志内容，用生动友好、结构清晰的中文写一份版本更新速报。\n"
            "要求：\n"
            "1. 突出版本号与版本类型（正式版/快照版）。\n"
            "2. 提炼出 3-5 点最核心、玩家最关心的更新亮点或重要改动。\n"
            "3. 如果有重要 Bug 修复或技术调整，用简明语言概括 1-2 条。\n"
            "4. 总体字数控制在 400 字以内，排版精美，适当使用 Emoji。\n\n"
            "更新日志内容：\n{changelog}"
        )
        prompt_tmpl = self.config.get("custom_prompt", default_prompt_tmpl)
        try:
            prompt = prompt_tmpl.format(changelog=truncated_changelog, version=version_id, type=version_type)
        except Exception:
            prompt = prompt_tmpl.replace("{changelog}", truncated_changelog)

        provider_id, provider_inst = await self._get_chat_provider()

        # 若未检测到任何可用模型，避免传入 None 导致 Provider None not found 异常
        if not provider_id and not provider_inst:
            logger.warning("[MC Update] 未检测到任何可用的 LLM 提供商，跳过大模型总结，使用预置摘要。请在 AstrBot 设置中添加或激活模型提供商，或在插件配置中指定 chat_provider_id。")
            type_desc = "正式版 (Release)" if version_type == "release" else "快照/预览版 (Snapshot)"
            return f"🎮 Minecraft Java Edition 发布了新版本：{version_id} ({type_desc})！\n由于未检测到可用的大模型提供商，未能自动生成详细摘要，请点击下方链接查看完整日志。"

        # 尝试反向获取提供商实例
        if provider_id and not provider_inst and hasattr(self.context, "get_provider_by_id"):
            try:
                provider_inst = self.context.get_provider_by_id(provider_id)
            except Exception:
                pass

        try:
            llm_resp = None
            # 优先方式: 调用 context.llm_generate
            if provider_id and hasattr(self.context, "llm_generate"):
                try:
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt
                    )
                except Exception as e:
                    logger.warning(f"[MC Update] context.llm_generate 调用失败: {e}，尝试使用提供商实例直接调用")

            # 备选方式: 直接调用 provider_inst.text_chat
            if (not llm_resp or not getattr(llm_resp, "completion_text", None)) and provider_inst and hasattr(provider_inst, "text_chat"):
                llm_resp = await provider_inst.text_chat(prompt=prompt)

            if llm_resp and hasattr(llm_resp, "completion_text") and llm_resp.completion_text:
                return llm_resp.completion_text.strip()
        except Exception as e:
            logger.error(f"[MC Update] 调用 LLM 总结失败: {e}")

        # 回退默认简短总结
        type_desc = "正式版 (Release)" if version_type == "release" else "快照/预览版 (Snapshot)"
        return f"🎮 Minecraft Java Edition 发布了新版本：{version_id} ({type_desc})！\n由于大模型总结异常，未能自动生成详细摘要，请点击下方链接查看完整日志。"

    async def _broadcast_update(self, version_id: str, version_type: str, summary: str, article_url: str):
        """向所有订阅的群聊/会话广播更新（完美适配 QQ 官方机器人及多平台）"""
        subscribers = self.config.get("subscribers", [])
        if not subscribers:
            logger.info("[MC Update] 当前没有订阅的群聊或会话，跳过推送")
            return

        type_tag = "正式版" if version_type == "release" else "快照版"
        # 组织适合 QQ 官方机器人的纯文本排版，避免复杂 markdown 被官方平台丢弃
        msg_text = (
            f"📢【Minecraft 版本更新速报】\n"
            f"━━━━━━━━━━━━━━\n"
            f"📌 版本：Java Edition {version_id} ({type_tag})\n\n"
            f"{summary}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"🔗 官方更新日志：\n{article_url}"
        )

        chain = MessageChain().message(msg_text)

        logger.info(f"[MC Update] 开始向 {len(subscribers)} 个订阅目标推送版本 {version_id}...")
        for umo in subscribers:
            try:
                await self.context.send_message(umo, chain)
                logger.info(f"[MC Update] 成功向 {umo} 推送更新")
            except Exception as e:
                logger.warning(f"[MC Update] 向 {umo} 推送更新失败: {e}")
            # 适当等待以防速率限制
            await asyncio.sleep(0.5)

    async def _check_updates(self, force: bool = False) -> List[Dict[str, Any]]:
        """检查是否有新版本，如发现新版本则生成总结并推送"""
        manifest = await self._fetch_version_manifest()
        if not manifest or "latest" not in manifest:
            return []

        latest_rel = manifest["latest"].get("release")
        latest_snap = manifest["latest"].get("snapshot")
        notify_snapshot = bool(self.config.get("notify_snapshot", False))

        last_rel = await self._get_stored_version("mc_last_release")
        last_snap = await self._get_stored_version("mc_last_snapshot")

        detected_updates = []

        # 1. 检查正式版
        if latest_rel and (force or latest_rel != last_rel):
            logger.info(f"[MC Update] 发现新正式版: {latest_rel} (本地记录: {last_rel})")
            detected_updates.append({"id": latest_rel, "type": "release"})

        # 2. 检查快照版 (若已配置开启)
        if notify_snapshot and latest_snap and (force or latest_snap != last_snap):
            # 如果快照版版本号和正式版相同（发布正式版时 latest.snapshot 也会更新为该版本），避免重复推送
            if latest_snap != latest_rel:
                logger.info(f"[MC Update] 发现新快照版: {latest_snap} (本地记录: {last_snap})")
                detected_updates.append({"id": latest_snap, "type": "snapshot"})

        # 执行获取内容、总结与推送
        for update in detected_updates:
            vid = update["id"]
            vtype = update["type"]
            title, url, content = await self._fetch_changelog(vid, vtype)
            summary = await self._summarize_changelog(vid, vtype, content, url)
            await self._broadcast_update(vid, vtype, summary, url)

            # 更新持久化记录
            if vtype == "release":
                await self._set_stored_version("mc_last_release", vid)
            else:
                await self._set_stored_version("mc_last_snapshot", vid)

        return detected_updates

    # ==================== 指令注册与处理 ====================

    @filter.command("mc")
    async def mc_command(self, event: AstrMessageEvent, action: str = "", arg: str = ""):
        """Minecraft 更新推送管理指令
        用法：
        /mc help - 查看帮助与当前状态
        /mc sub - 订阅当前群的更新推送
        /mc unsub - 取消当前群的更新推送
        /mc check - 立即手动检查是否有新版本
        /mc latest [release/snapshot] - 查看当前最新版本的总结速报
        """
        action = action.strip().lower()
        umo = event.unified_msg_origin
        subscribers: List[str] = self.config.get("subscribers", [])

        if action == "sub":
            if umo in subscribers:
                yield event.plain_result("ℹ️ 当前群聊/会话已在订阅列表中，无需重复订阅。")
                return
            subscribers.append(umo)
            self.config["subscribers"] = subscribers
            self.config.save_config()
            logger.info(f"[MC Update] 会话 {umo} 成功订阅更新推送")
            yield event.plain_result("✅ 成功订阅 Minecraft 版本更新推送！新版本发布时将自动推送到本群。")

        elif action == "unsub":
            if umo not in subscribers:
                yield event.plain_result("ℹ️ 当前群聊/会话尚未订阅 Minecraft 版本更新。")
                return
            subscribers.remove(umo)
            self.config["subscribers"] = subscribers
            self.config.save_config()
            logger.info(f"[MC Update] 会话 {umo} 取消订阅更新推送")
            yield event.plain_result("✅ 已取消当前群聊/会话的 Minecraft 版本更新订阅。")

        elif action == "check":
            yield event.plain_result("🔍 正在检查 Minecraft 最新版本信息，请稍候...")
            manifest = await self._fetch_version_manifest()
            if not manifest or "latest" not in manifest:
                yield event.plain_result("❌ 获取 Minecraft 版本清单失败，请稍后重试。")
                return

            latest_rel = manifest["latest"].get("release")
            latest_snap = manifest["latest"].get("snapshot")
            last_rel = await self._get_stored_version("mc_last_release")
            last_snap = await self._get_stored_version("mc_last_snapshot")

            status_msg = (
                f"📊 【Minecraft 版本状态】\n"
                f"• 当前最新正式版: {latest_rel} (本地已记录: {last_rel})\n"
                f"• 当前最新快照版: {latest_snap} (本地已记录: {last_snap})\n"
                f"• 快照推送: {'已开启' if self.config.get('notify_snapshot', False) else '已关闭'}\n"
            )

            # 触发检测
            updates = await self._check_updates(force=False)
            if updates:
                new_vers = ", ".join([f"{u['id']}({u['type']})" for u in updates])
                status_msg += f"\n🎉 检测到新版本发布并已触发推送：{new_vers}"
            else:
                status_msg += "\n✅ 当前已是最新版本，无新更新发布。"

            yield event.plain_result(status_msg)

        elif action == "latest":
            vtype = "snapshot" if arg.strip().lower() in ["snapshot", "snap", "快照"] else "release"
            yield event.plain_result(f"⏳ 正在拉取 Minecraft 最新{ '快照版' if vtype == 'snapshot' else '正式版' }并由 AI 生成速报，请稍候...")

            manifest = await self._fetch_version_manifest()
            if not manifest or "latest" not in manifest:
                yield event.plain_result("❌ 获取 Minecraft 版本清单失败，请稍后重试。")
                return

            target_version = manifest["latest"].get(vtype)
            if not target_version:
                yield event.plain_result("❌ 未找到对应的版本信息。")
                return

            title, url, content = await self._fetch_changelog(target_version, vtype)
            summary = await self._summarize_changelog(target_version, vtype, content, url, umo=umo)

            type_tag = "正式版" if vtype == "release" else "快照版"
            msg_text = (
                f"📢【Minecraft 版本速报】\n"
                f"━━━━━━━━━━━━━━\n"
                f"📌 版本：Java Edition {target_version} ({type_tag})\n\n"
                f"{summary}\n\n"
                f"━━━━━━━━━━━━━━\n"
                f"🔗 官方更新日志：\n{url}"
            )
            yield event.plain_result(msg_text)

        else:
            # help
            is_sub = umo in subscribers
            cron_expr = str(self.config.get("cron_expression", "")).strip()
            interval = self.config.get("check_interval", 1800)
            notify_snap = self.config.get("notify_snapshot", False)

            schedule_desc = f"Cron 表达式: {cron_expr}" if cron_expr else f"轮询间隔: {interval} 秒 ({interval // 60} 分钟)"

            help_msg = (
                "⛏️【Minecraft 更新推送助手】\n"
                "------------------------------\n"
                "📌 支持指令：\n"
                "• /mc sub - 订阅本群的更新推送\n"
                "• /mc unsub - 取消本群的更新推送\n"
                "• /mc check - 立即检查是否有新版本\n"
                "• /mc latest - 查看最新正式版 AI 速报\n"
                "• /mc latest snapshot - 查看最新快照版 AI 速报\n"
                "• /mc help - 查看本帮助\n"
                "------------------------------\n"
                f"⚙️ 当前状态：\n"
                f"• 本群订阅状态: {'已订阅 ✅' if is_sub else '未订阅 ❌'}\n"
                f"• 定时检测设置: {schedule_desc}\n"
                f"• 快照推送: {'开启' if notify_snap else '关闭'}\n"
                f"• 订阅总数: {len(subscribers)} 个群聊/会话"
            )
            yield event.plain_result(help_msg)
