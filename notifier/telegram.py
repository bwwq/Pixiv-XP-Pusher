"""
Telegram 推送实现
"""
import asyncio
import logging
from io import BytesIO
from typing import Callable, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CallbackQueryHandler

from .base import BaseNotifier
from pixiv_client import Illust, PixivClient
from utils import get_pixiv_cat_url

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

logger = logging.getLogger(__name__)


async def _retry_on_flood(coro_func, max_retries=3):
    """
    Retry a coroutine on Flood Control errors and network errors.
    coro_func should be a callable that returns a coroutine (not the coroutine itself).
    """
    from telegram.error import RetryAfter, NetworkError, TimedOut
    
    # 网络错误关键词（httpx 错误）
    network_error_keywords = [
        "ConnectError", "RemoteProtocolError", "disconnected",
        "TimeoutException", "ConnectionResetError", "ConnectionRefusedError"
    ]
    
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except RetryAfter as e:
            wait_time = e.retry_after + 1  # Add 1 second buffer
            logger.info(f"Flood control: Sleeping for {wait_time}s to avoid conflict...")
            await asyncio.sleep(wait_time)
        except (NetworkError, TimedOut) as e:
            # Telegram 库的网络错误
            wait_time = 3 * (attempt + 1)  # 递增等待：3s, 6s, 9s
            logger.warning(f"网络错误 (尝试 {attempt+1}/{max_retries}): {e}，{wait_time}s 后重试...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            error_msg = str(e)
            # 检查是否为 Flood Control
            if "Flood control exceeded" in error_msg:
                import re
                match = re.search(r"Retry in (\d+)", error_msg)
                wait_time = int(match.group(1)) + 1 if match else 10
                logger.info(f"Flood control: Sleeping for {wait_time}s to avoid conflict...")
                await asyncio.sleep(wait_time)
            # 检查是否为网络错误
            elif any(kw in error_msg for kw in network_error_keywords):
                wait_time = 3 * (attempt + 1)
                logger.warning(f"网络错误 (尝试 {attempt+1}/{max_retries}): {type(e).__name__}，{wait_time}s 后重试...")
                await asyncio.sleep(wait_time)
            else:
                raise  # Re-raise non-retryable errors
    
    # Final attempt without catching
    return await coro_func()


class TelegramNotifier(BaseNotifier):
    """Telegram Bot 推送"""
    
    def __init__(
        self,
        bot_token: str,
        chat_ids: list[str] | str,           # 支持单个或多个 chat_id
        client: Optional[PixivClient] = None,
        multi_page_mode: str = "cover_link",
        allowed_users: list[str] | None = None,  # 允许发送反馈的用户 ID
        thread_id: int | None = None,          # Telegram Topic (Thread) ID (默认)
        on_feedback: Optional[Callable] = None,
        on_action: Optional[Callable] = None,
        proxy_url: str | None = None,             # HTTP 代理地址
        max_pages: int = 10,
        image_quality: int = 85,               # JPEG 压缩质量 (默认 85)
        max_image_size: int = 2000,            # 最大边长 (默认 2000px)
        topic_rules: dict | None = None,       # Topic 分流规则 {category: topic_id}
        topic_tag_mapping: dict | None = None, # 标签到分类的映射 {category: [tags]}
        # 批量模式配置
        batch_mode: str = "single",            # single / telegraph
        batch_show_title: bool = True,
        batch_show_artist: bool = True,
        batch_show_tags: bool = True,
    ):
        # Auto-detect proxy if not provided
        if not proxy_url:
            import urllib.request
            sys_proxies = urllib.request.getproxies()
            proxy_url = sys_proxies.get("https") or sys_proxies.get("http")
            if proxy_url:
                logger.info(f"TelegramNotifier using system proxy: {proxy_url}")

        from telegram.request import HTTPXRequest
        request = HTTPXRequest(proxy=proxy_url) if proxy_url else None
        self.bot = Bot(token=bot_token, request=request)
        
        # 支持单个或多个 chat_id，并去重防止重复发送
        if isinstance(chat_ids, str):
            self.chat_ids = [chat_ids] if chat_ids else []
        else:
            # 去重：转换为 set 再转回 list
            self.chat_ids = list(dict.fromkeys(str(c) for c in chat_ids if c))
        
        self.client = client
        self.multi_page_mode = multi_page_mode
        # 允许的用户（空=所有人）
        self.allowed_users = set(int(u) for u in allowed_users if u) if allowed_users else None
        self.on_feedback = on_feedback
        self.on_action = on_action
        self.proxy_url = proxy_url
        self.max_pages = max_pages
        self.image_quality = image_quality
        self.max_image_size = max_image_size
        self._app: Optional[Application] = None
        # 消息ID -> illust_id 映射（用于回复快捷反馈）
        self._message_illust_map: dict[int, int] = {}
        self.thread_id = thread_id  # 默认 Topic
        
        # Topic 智能分流
        self.topic_rules = topic_rules or {}
        self.topic_tag_mapping = topic_tag_mapping or {}
        
        # 批量模式
        self.batch_mode = batch_mode
        self.batch_show_title = batch_show_title
        self.batch_show_artist = batch_show_artist
        self.batch_show_tags = batch_show_tags
        self._telegraph = None  # Telegraph 客户端（延迟初始化）
        self._pending_input = None  # 等待用户输入的状态
        
        # 日志
        logger.info(f"Telegram 推送目标: {', '.join(self.chat_ids) or '无'}")
        if self.allowed_users:
            logger.info(f"允许反馈的用户: {self.allowed_users}")
        if self.topic_rules:
            logger.info(f"Topic 分流规则: {list(self.topic_rules.keys())}")
        if self.batch_mode == "telegraph":
            logger.info("批量模式: Telegraph")

    def _resolve_topic_id(self, illust: Illust) -> int | None:
        """根据作品标签匹配 Topic ID"""
        if not self.topic_rules:
            return self.thread_id  # 使用默认 topic
        
        illust_tags_lower = {t.lower() for t in illust.tags}
        
        # 优先检查 R18
        if illust.is_r18 and "r18" in self.topic_rules:
            return self.topic_rules["r18"]
        
        # 检查标签映射
        for category, tags in self.topic_tag_mapping.items():
            if category in self.topic_rules:
                for tag in tags:
                    if tag.lower() in illust_tags_lower:
                        return self.topic_rules[category]
        
        # 返回默认 topic
        return self.topic_rules.get("default", self.thread_id)

    def _build_main_menu(self) -> InlineKeyboardMarkup:
        """构建主菜单"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 推送", callback_data="menu:push"),
                InlineKeyboardButton("📊 统计", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton("🎯 XP画像", callback_data="menu:xp"),
                InlineKeyboardButton("📦 批量", callback_data="menu:batch"),
            ],
            [
                InlineKeyboardButton("🚫 屏蔽", callback_data="menu:block"),
                InlineKeyboardButton("⚙️ 设置", callback_data="menu:settings"),
            ],
        ])
    
    def _build_batch_menu(self) -> InlineKeyboardMarkup:
        """构建批量设置菜单"""
        mode_text = "📦 批量" if self.batch_mode == "telegraph" else "📄 逐条"
        title_icon = "✅" if self.batch_show_title else "❌"
        artist_icon = "✅" if self.batch_show_artist else "❌"
        tags_icon = "✅" if self.batch_show_tags else "❌"
        
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📄 逐条", callback_data="menu:batch:single"),
                InlineKeyboardButton(f"📦 批量", callback_data="menu:batch:telegraph"),
            ],
            [
                InlineKeyboardButton(f"标题{title_icon}", callback_data="menu:batch:title"),
                InlineKeyboardButton(f"画师{artist_icon}", callback_data="menu:batch:artist"),
                InlineKeyboardButton(f"标签{tags_icon}", callback_data="menu:batch:tags"),
            ],
            [InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")],
        ])
    
    def _build_settings_menu(self, config: dict) -> InlineKeyboardMarkup:
        """构建设置菜单"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🤖 AI过滤", callback_data="menu:set:ai"),
                InlineKeyboardButton("🔞 R18模式", callback_data="menu:set:r18"),
            ],
            [
                InlineKeyboardButton("📊 每日上限", callback_data="menu:set:limit"),
                InlineKeyboardButton("📅 推送时间", callback_data="menu:set:schedule"),
            ],
            [InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")],
        ])
    
    def _build_block_menu(self) -> InlineKeyboardMarkup:
        """构建屏蔽管理菜单"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 查看屏蔽列表", callback_data="menu:block:list")],
            [
                InlineKeyboardButton("🏷️ 标签屏蔽", callback_data="menu:block:tag"),
                InlineKeyboardButton("🎨 画师屏蔽", callback_data="menu:block:artist"),
            ],
            [InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")],
        ])

    def _read_config(self) -> dict:
        """读取配置文件"""
        import yaml
        import os
        config_path = "config.yaml"
        if not os.path.exists(config_path): return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except:
            return {}

    def _save_config_value(self, *args):
        """保存配置值 _save_config_value("filter", "daily_limit", 30)"""
        import yaml
        import os
        
        if len(args) < 2: return
        keys = args[:-1]
        value = args[-1]
        
        config_path = "config.yaml"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            
            # Navigate to leaf
            current = config
            for key in keys[:-1]:
                if key not in current: current[key] = {}
                current = current[key]
            current[keys[-1]] = value
            
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, sort_keys=False)
            logger.info(f"配置已更新: {keys} = {value}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _save_batch_config(self):
        """保存批量配置"""
        self._save_config_value("notifier", "telegram", "batch_mode", self.batch_mode)
        self._save_config_value("notifier", "telegram", "batch_show_title", self.batch_show_title)
        self._save_config_value("notifier", "telegram", "batch_show_artist", self.batch_show_artist)
        self._save_config_value("notifier", "telegram", "batch_show_tags", self.batch_show_tags)

    async def _handle_menu_callback(self, query, data: str):
        """处理菜单回调"""
        import database as db
        
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        sub_action = parts[2] if len(parts) > 2 else ""
        
        # 主菜单
        if action == "main":
            await query.edit_message_text(
                "🤖 *XP Pusher 控制面板*",
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )
        
        # 立即推送
        elif action == "push":
            if self.on_action:
                await query.edit_message_text("🚀 正在推送...", reply_markup=None)
                await self.on_action("push", None)
            else:
                await query.edit_message_text("❌ 未配置动作处理")
        
        # 统计
        elif action == "stats":
            stats = await db.get_all_strategy_stats()
            lines = ["📊 *策略表现*\n"]
            for strategy, data in stats.items():
                rate = f"{data['rate']:.1%}" if data['total'] > 0 else "N/A"
                lines.append(f"• {strategy}: {data['success']}/{data['total']} ({rate})")
            
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")
            ]])
            await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")
        
        # XP画像
        elif action == "xp":
            top_tags = await db.get_top_xp_tags(15)
            lines = ["🎯 *XP 画像 Top 15*\n"]
            for i, (tag, weight) in enumerate(top_tags, 1):
                lines.append(f"{i}. `{tag}` ({weight:.2f})")
            
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回", callback_data="menu:main")
            ]])
            await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")
        
        # 批量设置
        elif action == "batch":
            if not sub_action:
                mode_icon = "📦" if self.batch_mode == "telegraph" else "📄"
                text = f"📦 *批量模式设置*\n\n当前模式: {mode_icon} `{self.batch_mode}`"
                await query.edit_message_text(text, reply_markup=self._build_batch_menu(), parse_mode="Markdown")
            elif sub_action == "single":
                self.batch_mode = "single"
                self._save_batch_config()
                await query.edit_message_text("✅ 已切换为逐条发送模式 (已保存)", reply_markup=self._build_batch_menu())
            elif sub_action == "telegraph":
                self.batch_mode = "telegraph"
                self._save_batch_config()
                await query.edit_message_text("✅ 已切换为批量模式 (已保存)", reply_markup=self._build_batch_menu())
            elif sub_action == "title":
                self.batch_show_title = not self.batch_show_title
                self._save_batch_config()
                await query.edit_message_reply_markup(reply_markup=self._build_batch_menu())
            elif sub_action == "artist":
                self.batch_show_artist = not self.batch_show_artist
                self._save_batch_config()
                await query.edit_message_reply_markup(reply_markup=self._build_batch_menu())
            elif sub_action == "tags":
                self.batch_show_tags = not self.batch_show_tags
                self._save_batch_config()
                await query.edit_message_reply_markup(reply_markup=self._build_batch_menu())
        
        # 屏蔽管理
        elif action == "block":
            if not sub_action:
                await query.edit_message_text(
                    "🚫 *屏蔽管理*",
                    reply_markup=self._build_block_menu(),
                    parse_mode="Markdown"
                )
            elif sub_action == "list":
                blocked_tags = await db.get_blocked_tags()
                blocked_artists = await db.get_blocked_artists()
                
                lines = ["📋 *屏蔽列表*\n"]
                if blocked_tags:
                    lines.append("🏷️ 标签:")
                    for tag in blocked_tags[:10]:
                        lines.append(f"  • `{tag}`")
                if blocked_artists:
                    lines.append("\n🎨 画师:")
                    for artist_id, name in blocked_artists[:10]:
                        lines.append(f"  • {name} (`{artist_id}`)")
                if not blocked_tags and not blocked_artists:
                    lines.append("_暂无屏蔽_")
                
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ 返回", callback_data="menu:block")
                ]])
                await query.edit_message_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")
            elif sub_action == "tag":
                await query.edit_message_text(
                    "🏷️ 请回复要屏蔽的标签名称\n\n_直接发送标签名即可_",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 取消", callback_data="menu:block")
                    ]]),
                    parse_mode="Markdown"
                )
                # 设置状态等待输入
                self._pending_input = {"type": "block_tag", "chat_id": query.message.chat_id}
            elif sub_action == "artist":
                await query.edit_message_text(
                    "🎨 请回复要屏蔽的画师ID\n\n_发送画师ID (数字)_",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 取消", callback_data="menu:block")
                    ]]),
                    parse_mode="Markdown"
                )
                self._pending_input = {"type": "block_artist", "chat_id": query.message.chat_id}
        
        # 设置
        elif action == "settings" or action == "set":
            config = self._read_config()
            
            if not sub_action:
                await query.edit_message_text(
                    "⚙️ *设置*\n\n_部分设置修改后需重启生效_",
                    reply_markup=self._build_settings_menu(config),
                    parse_mode="Markdown"
                )
            elif sub_action == "ai":
                # 切换 AI 过滤 (filter.exclude_ai)
                current = config.get("filter", {}).get("exclude_ai", False)
                new_val = not current
                self._save_config_value("filter", "exclude_ai", new_val)
                # 刷新并重新读取
                config = self._read_config()
                await query.edit_message_text(
                    f"✅ AI 过滤已 {'开启' if new_val else '关闭'}",
                    reply_markup=self._build_settings_menu(config)
                )
            elif sub_action == "r18":
                # 循环切换 mixed -> r18_only -> safe
                current = config.get("filter", {}).get("r18_mode", "mixed")
                modes = ["mixed", "r18_only", "safe"]
                try:
                    next_mode = modes[(modes.index(current) + 1) % len(modes)]
                except:
                    next_mode = "mixed"
                
                self._save_config_value("filter", "r18_mode", next_mode)
                config = self._read_config()
                await query.edit_message_text(
                    f"✅ R18 模式已切换为: `{next_mode}`",
                    reply_markup=self._build_settings_menu(config),
                    parse_mode="Markdown"
                )
            elif sub_action == "limit":
                await query.edit_message_text(
                    "📊 请回复每日推送上限 (数字)\n\n_例如: 30_",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 取消", callback_data="menu:settings")
                    ]]),
                    parse_mode="Markdown"
                )
                self._pending_input = {"type": "set_limit", "chat_id": query.message.chat_id}
            elif sub_action == "schedule":
                if self.on_action:
                    await self.on_action("show_schedule", None)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ 返回", callback_data="menu:settings")
                ]])
                await query.edit_message_text(
                    "📅 推送时间设置请使用 `/schedule` 命令",
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )



    async def stop_polling(self):
        """停止Bot轮询"""
        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
                self._app = None  # 清理引用，允许重新初始化
                logger.info("Telegram Bot 轮询已停止")
            except Exception as e:
                logger.error(f"停止 Telegram 轮询时出错: {e}")
                self._app = None  # 即使出错也清理引用

    def _compress_image(self, image_data: bytes, max_size: int = 9 * 1024 * 1024) -> bytes:
        """智能压缩图片到指定大小以下 (默认 9MB)"""
        if not HAS_PILLOW:
            if len(image_data) > max_size:
                logger.warning(f"图片过大 ({len(image_data)} bytes) 且未安装 Pillow，无法压缩，发送可能失败。请 pip install Pillow")
            return image_data
            
        try:
            # 必须检查尺寸 (Telegram 限制 width + height <= 10000)
            # 即使文件大小很小，尺寸超标也会报 Photo_invalid_dimensions
            with Image.open(BytesIO(image_data)) as img:
                w, h = img.size
                need_resize = False
                
                # 检查尺寸 (优先使用配置的 max_image_size)
                max_dim = self.max_image_size
                if w > max_dim or h > max_dim:
                    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                    need_resize = True
                    logger.info(f"图片尺寸过大 ({w}x{h})，自动缩放到 {img.size[0]}x{img.size[1]}")
                elif w + h > 10000:
                    scale = 9500 / (w + h)
                    img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
                    need_resize = True
                    logger.info(f"图片尺寸超限 ({w}x{h})，缩放到 {img.size[0]}x{img.size[1]}")
                elif w / h > 20 or h / w > 20: # 比例过长
                    # 比例问题比较难搞，通常需要裁剪或填充，暂时简单缩放长边
                    max_side = 5000
                    if max(w, h) > max_side:
                        img.thumbnail((max_side, max_side))
                        need_resize = True
                        logger.info(f"图片比例极端 ({w}x{h})，缩放到 {img.size[0]}x{img.size[1]}")

                # 如果没有调整尺寸且文件大小也合格，直接返回原图
                if not need_resize and len(image_data) <= max_size:
                    return image_data
                
                # 开始压缩处理
                logger.info(f"正在处理图片 (原始大小: {len(image_data)/1024/1024:.2f}MB, 尺寸: {w}x{h})...")
                
                # 转换色彩空间
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode != 'RGBA':
                        img = img.convert('RGBA')
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                    
                output = BytesIO()
                
                # 策略1：降低 JPEG 质量 (从配置的 quality 到 50)
                quality = self.image_quality
                min_quality = 50
                while quality >= min_quality:
                    output.seek(0)
                    output.truncate()
                    img.save(output, format='JPEG', quality=quality)
                    size = output.tell()
                    if size <= max_size:
                        logger.info(f"压缩成功: 质量={quality}, 大小={size/1024/1024:.2f}MB")
                        return output.getvalue()
                    quality -= 10
                
                # 策略2：继续缩放 (质量已降到50但仍超标)
                scale = 0.8
                while scale >= 0.3:
                    new_size = (int(img.width * scale), int(img.height * scale))
                    resized = img.resize(new_size, Image.Resampling.LANCZOS)
                    output.seek(0)
                    output.truncate()
                    resized.save(output, format='JPEG', quality=60)
                    size = output.tell()
                    if size <= max_size:
                        logger.info(f"压缩成功: 缩放={scale:.1f}, 大小={size/1024/1024:.2f}MB")
                        return output.getvalue()
                    scale -= 0.2
                    
                logger.warning("压缩失败：图片实在太大了")
                return image_data

        except Exception as e:
            logger.error(f"处理图片出错: {e}")
            return image_data
    
    async def start_polling(self):
        """启动Bot轮询（用于接收反馈）"""
        from telegram.ext import MessageHandler, filters, CommandHandler
        from apscheduler.triggers.cron import CronTrigger
        
        from telegram.request import HTTPXRequest
        
        # 增加超时以减少 "Server disconnected" 错误
        # 长轮询需要更长的 read_timeout（Telegram 服务端默认最多等待 50 秒）
        request_kwargs = {
            "read_timeout": 60,
            "write_timeout": 30,
            "connect_timeout": 30,
            "pool_timeout": 30,
        }
        if self.proxy_url:
            request_kwargs["proxy"] = self.proxy_url
        
        request = HTTPXRequest(**request_kwargs)
        builder = Application.builder().token(self.bot.token).request(request)
        
        self._app = builder.build()
        
        # 处理按钮回调
        async def callback_handler(update, context):
            query = update.callback_query
            user_id = query.from_user.id
            
            # 权限验证
            # 权限验证
            if self.allowed_users and user_id not in self.allowed_users:
                await query.answer(f"❌ 无权限 (ID: {user_id})", show_alert=True)
                return
            
            # 检测回调是否过期（Telegram 限制回调查询必须在 48 秒内响应）
            is_query_expired = False
            try:
                await query.answer()
            except Exception as e:
                error_msg = str(e).lower()
                is_query_expired = "query is too old" in error_msg or "too old" in error_msg
                if is_query_expired:
                    logger.warning(f"回调查询已过期 (用户 {user_id})，将使用消息回复方式确认")
                else:
                    logger.debug(f"回调应答失败: {e}")
            
            data = query.data
            
            if data.startswith("retry_ai:"):
                # 处理重试动作
                if self.on_action:
                    error_id = int(data.split(":")[1])
                    await self.on_action("retry_ai", error_id)
                    await query.edit_message_text("🔄 已提交重试请求，请稍候...")
                else:
                    await query.message.reply_text("❌ 未配置动作处理")
                return
            
            # ===== 菜单回调处理 =====
            if data.startswith("menu:"):
                await self._handle_menu_callback(query, data)
                return
            
            if data == "batch_like":
                # 显示作品选择按钮
                import database as db
                illust_ids = await db.get_batch_all_illust_ids(
                    query.message.message_id, 
                    str(query.message.chat_id)
                )
                if illust_ids:
                    keyboard = self._build_batch_select_keyboard("like", len(illust_ids))
                    await query.edit_message_reply_markup(reply_markup=keyboard)
                return
            
            if data == "batch_dislike":
                import database as db
                illust_ids = await db.get_batch_all_illust_ids(
                    query.message.message_id, 
                    str(query.message.chat_id)
                )
                if illust_ids:
                    keyboard = self._build_batch_select_keyboard("dislike", len(illust_ids))
                    await query.edit_message_reply_markup(reply_markup=keyboard)
                return
            
            if data.startswith("batch_select:"):
                # 格式: batch_select:like:3
                import database as db
                parts = data.split(":")
                action = parts[1]  # like or dislike
                index = int(parts[2])  # 1-based
                
                illust_id = await db.get_batch_illust_id(
                    query.message.message_id,
                    str(query.message.chat_id),
                    index
                )
                if illust_id:
                    await self.handle_feedback(illust_id, action)
                    emoji = "❤️" if action == "like" else "👎"
                    await query.message.reply_text(f"{emoji} 已记录 #{index} 的反馈")
                
                # 恢复原始按钮
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❤️ 喜欢", callback_data="batch_like"),
                        InlineKeyboardButton("👎 不喜欢", callback_data="batch_dislike"),
                    ]
                ])
                await query.edit_message_reply_markup(reply_markup=keyboard)
                return
            
            if data.startswith("batch_all:"):
                # 格式: batch_all:like
                import database as db
                action = data.split(":")[1]
                
                illust_ids = await db.get_batch_all_illust_ids(
                    query.message.message_id,
                    str(query.message.chat_id)
                )
                for illust_id in illust_ids:
                    await self.handle_feedback(illust_id, action)
                
                emoji = "❤️" if action == "like" else "👎"
                await query.message.reply_text(f"{emoji} 已对全部 {len(illust_ids)} 个作品记录反馈")
                await query.edit_message_reply_markup(reply_markup=None)
                return
            
            if data == "batch_cancel":
                # 恢复原始按钮
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❤️ 喜欢", callback_data="batch_like"),
                        InlineKeyboardButton("👎 不喜欢", callback_data="batch_dislike"),
                    ]
                ])
                await query.edit_message_reply_markup(reply_markup=keyboard)
                return

            if ":" in data:
                action, illust_id = data.split(":")
                if action in ("like", "dislike"):
                    try:
                        await self.handle_feedback(int(illust_id), action)
                        
                        emoji = "❤️" if action == "like" else "👎"
                        # 发送反馈确认消息
                        try:
                            # 只有回调未过期时才尝试编辑消息按钮
                            if not is_query_expired:
                                await query.edit_message_reply_markup(reply_markup=None)
                            # 无论回调是否过期都发送确认消息
                            await query.message.reply_text(f"{emoji} 已记录反馈")
                        except Exception as e:
                            # 编辑消息失败时，尝试直接发送消息确认
                            logger.debug(f"更新消息失败: {e}")
                            try:
                                await self.bot.send_message(
                                    chat_id=query.message.chat_id,
                                    text=f"{emoji} 已记录反馈"
                                )
                            except Exception as e2:
                                logger.warning(f"发送确认消息失败: {e2}")
                    except Exception as e:
                        logger.error(f"处理反馈失败 ({action} {illust_id}): {e}")
                        try:
                            await query.message.reply_text(f"❌ 处理失败: {e}")
                        except:
                            # 最后尝试直接发送
                            try:
                                await self.bot.send_message(
                                    chat_id=query.message.chat_id,
                                    text=f"❌ 处理失败: {e}"
                                )
                            except:
                                pass
        
        # 处理回复消息（1=喜欢, 2=不喜欢, 或输入内容）
        async def reply_handler(update, context):
            message = update.message
            if not message:
                return
            
            user_id = message.from_user.id
            
            # 权限验证
            if self.allowed_users and user_id not in self.allowed_users:
                return
            
            text = message.text.strip()
            
            # ===== 处理等待输入 =====
            if self._pending_input and self._pending_input.get("chat_id") == message.chat_id:
                input_type = self._pending_input.get("type")
                self._pending_input = None  # 清除状态，避免死循环
                
                try:
                    if input_type == "block_tag":
                        from database import block_tag
                        await block_tag(text)
                        await message.reply_text(f"✅ 已屏蔽标签: `{text}`", parse_mode="Markdown")
                        
                    elif input_type == "block_artist":
                        if not text.isdigit():
                            await message.reply_text("❌ 画师ID必须是数字")
                            return
                        from database import block_artist
                        await block_artist(int(text))
                        await message.reply_text(f"✅ 已屏蔽画师: `{text}`", parse_mode="Markdown")
                        
                    elif input_type == "set_limit":
                        if not text.isdigit():
                            await message.reply_text("❌ 必须输入数字")
                            return
                        limit = int(text)
                        # 更新配置
                        self._save_config_value("filter", "daily_limit", limit)
                        await message.reply_text(f"✅ 每日推送上限已设置为: `{limit}`", parse_mode="Markdown")
                        
                except Exception as e:
                    await message.reply_text(f"❌ 操作失败: {e}")
                
                return

            if not message.reply_to_message:
                return
            
            reply_msg_id = message.reply_to_message.message_id
            
            # 查找对应的 illust_id
            illust_id = self._message_illust_map.get(reply_msg_id)
            if not illust_id:
                return
            
            if text == "1":
                await self.handle_feedback(illust_id, "like")
                await message.reply_text("❤️ 已记录喜欢")
            elif text == "2":
                await self.handle_feedback(illust_id, "dislike")
                await message.reply_text("👎 已记录不喜欢")
                
        # /push 指令 (支持 /push 或 /push <ID>)
        async def cmd_push(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                logger.warning(f"用户 {user_id} 尝试执行 /push 但被拒绝 (Allowed: {self.allowed_users})")
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            args = context.args
            if args and args[0].isdigit():
                # 推送指定作品
                illust_id = int(args[0])
                await update.message.reply_text(f"🔍 正在获取作品 {illust_id}...")
                
                try:
                    if self.client:
                        illust = await self.client.get_illust_detail(illust_id)
                        if illust:
                            await update.message.reply_text(f"📨 正在推送: {illust.title}...")
                            sent = await self.send([illust])
                            if sent:
                                await update.message.reply_text(f"✅ 推送成功: {illust.title}")
                            else:
                                await update.message.reply_text("❌ 推送失败")
                        else:
                            await update.message.reply_text(f"❌ 未找到作品 {illust_id}")
                    else:
                        await update.message.reply_text("⚠️ Pixiv 客户端未初始化")
                except Exception as e:
                    logger.error(f"手动推送 {illust_id} 失败: {e}")
                    await update.message.reply_text(f"❌ 推送失败: {e}")
            else:
                # 触发全量推送任务
                await update.message.reply_text("🚀 收到指令，正在启动推送任务...")
                if self.on_action:
                    await self.on_action("run_task", None)
                else:
                    await update.message.reply_text("⚠️ 内部错误: 未配置 Action 回调")
                
        # /schedule 指令
        async def cmd_schedule(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
                
            args = context.args
            if not args:
                await update.message.reply_text(
                    "用法: /schedule <时间>\n"
                    "例: `/schedule 9:30` (每天9:30)\n"
                    "例: `/schedule 9:30,21:00` (每天两次)\n"
                    "例: `/schedule 0 22 * * *` (Cron格式)", 
                    parse_mode="Markdown"
                )
                return
            
            input_str = " ".join(args)
            
            # 解析时间格式
            import re
            time_pattern = re.compile(r'^(\d{1,2}:\d{2})(,\d{1,2}:\d{2})*$')
            
            if time_pattern.match(input_str.replace(" ", "")):
                # 友好格式: 9:30 或 9:30,21:00
                times = [t.strip() for t in input_str.replace(" ", "").split(",")]
                cron_list = []
                for t in times:
                    h, m = t.split(":")
                    cron_list.append(f"{m} {h} * * *")
                    
                schedule_data = ",".join(cron_list)  # 多个 cron 用逗号分隔
                display_times = ", ".join(times)
            else:
                # 尝试作为 Cron 格式解析
                try:
                    CronTrigger.from_crontab(input_str)
                    schedule_data = input_str
                    display_times = input_str
                except ValueError:
                    await update.message.reply_text("❌ 格式错误，请使用 `9:30` 或 Cron 表达式", parse_mode="Markdown")
                    return
                    
            try:
                if self.on_action:
                    await self.on_action("update_schedule", schedule_data)
                    await update.message.reply_text(f"✅ 定时任务已更新为: `{display_times}`", parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ 内部错误: 未配置 Action 回调")
            except Exception as e:
                await update.message.reply_text(f"❌ 设置失败: {e}")
        
        # /xp 指令 - 查看 XP 画像
        async def cmd_xp(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            try:
                from database import get_top_xp_tags
                top_tags = await get_top_xp_tags(15)
                
                if not top_tags:
                    await update.message.reply_text("📊 暂无 XP 画像数据")
                    return
                
                lines = ["🎯 *您的 XP 画像 Top 15*\n"]
                for i, (tag, weight) in enumerate(top_tags, 1):
                    bar = "█" * min(int(weight), 10)
                    # Tag 用反引号包裹防止解析错误
                    lines.append(f"{i}. `{tag}` {bar} ({weight:.1f})")
                
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ 获取失败: {e}")
        
        # /stats 指令 - 查看 MAB 策略统计
        async def cmd_stats(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            try:
                from database import get_all_strategy_stats
                stats = await get_all_strategy_stats()
                
                if not stats:
                    await update.message.reply_text("📊 暂无策略统计数据")
                    return
                
                lines = ["📈 *MAB 策略表现*\n"]
                # 映射必须覆盖 fetcher.py 中所有的 key
                strategy_names = {
                    "xp_search": "XP搜索", 
                    "search": "XP搜索(旧)", 
                    "subscription": "订阅更新", 
                    "ranking": "排行榜"
                }
                
                for strategy, data in stats.items():
                    name = strategy_names.get(strategy, strategy)
                    # 如果 fallback 到原始 key，必须转义下划线以免 markdown 解析错误
                    if name == strategy and "_" in name:
                        name = name.replace("_", "\\_")
                        
                    rate_pct = data["rate"] * 100
                    lines.append(f"• *{name}*: {data['success']}/{data['total']} ({rate_pct:.1f}%)")
                
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ 获取失败: {e}")
        
        # /block 指令 - 快速屏蔽标签
        async def cmd_block(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            args = context.args
            if not args:
                # 无参数时显示当前屏蔽列表
                from database import get_blocked_tags
                blocked = await get_blocked_tags()
                if blocked:
                    await update.message.reply_text(f"🚫 当前屏蔽列表:\n`{', '.join(blocked)}`", parse_mode="Markdown")
                else:
                    await update.message.reply_text("🚫 屏蔽列表为空\n用法: `/block <tag>` 添加屏蔽", parse_mode="Markdown")
                return
            
            tag = " ".join(args).strip()
            
            try:
                from database import block_tag
                await block_tag(tag)
                await update.message.reply_text(f"✅ 已屏蔽标签: `{tag}`", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ 屏蔽失败: {e}")
        
        # /unblock 指令 - 取消屏蔽标签
        async def cmd_unblock(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            args = context.args
            if not args:
                await update.message.reply_text("用法: `/unblock <tag>`", parse_mode="Markdown")
                return
            
            tag = " ".join(args).strip()
            
            try:
                from database import unblock_tag
                result = await unblock_tag(tag)
                if result:
                    await update.message.reply_text(f"✅ 已取消屏蔽: `{tag}`", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"⚠️ 该标签未在屏蔽列表中: `{tag}`", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ 取消屏蔽失败: {e}")
        
        # /help 指令 - 帮助信息
        async def cmd_help(update, context):
            help_text = (
                "*🤖 Bot 指令帮助*\n\n"
                "`/menu` - 📋 打开控制面板\n"
                "`/push` - 🚀 立即触发推送\n"
                "`/xp` - 🎯 查看 XP 画像 (Top Tags)\n"
                "`/stats` - 📈 查看策略成功率\n"
                "`/schedule` - ⏰ 查看/修改定时时间\n"
                "`/block <tag>` - 🚫 屏蔽标签\n"
                "`/unblock <tag>` - ✅ 取消屏蔽标签\n"
                "`/block_artist <id>` - 🚫 屏蔽画师\n"
                "`/unblock_artist <id>` - ✅ 取消屏蔽画师\n"
                "`/batch` - 📦 批量模式设置\n"
                "`/help` - ℹ️ 显示此帮助\n\n"
                "*💡 推荐使用 /menu 菜单操作*"
            )
            await update.message.reply_text(help_text, parse_mode="Markdown")
        
        # /menu 和 /start 指令 - 打开控制面板
        async def cmd_menu(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            await update.message.reply_text(
                "🤖 *XP Pusher 控制面板*",
                reply_markup=self._build_main_menu(),
                parse_mode="Markdown"
            )
        
        # /batch 指令 - 批量模式设置
        async def cmd_batch(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            args = context.args
            
            if not args:
                # 显示当前状态
                mode_emoji = "📦" if self.batch_mode == "telegraph" else "📄"
                status = (
                    f"*📦 批量模式设置*\n\n"
                    f"{mode_emoji} 当前模式: `{self.batch_mode}`\n"
                    f"📝 显示标题: `{'✅' if self.batch_show_title else '❌'}`\n"
                    f"🎨 显示画师: `{'✅' if self.batch_show_artist else '❌'}`\n"
                    f"🏷️ 显示标签: `{'✅' if self.batch_show_tags else '❌'}`\n\n"
                    "*用法:*\n"
                    "`/batch on` - 开启 Telegraph 批量模式\n"
                    "`/batch off` - 关闭批量模式\n"
                    "`/batch title on|off` - 开关标题\n"
                    "`/batch artist on|off` - 开关画师\n"
                    "`/batch tags on|off` - 开关标签"
                )
                await update.message.reply_text(status, parse_mode="Markdown")
                return
            
            cmd = args[0].lower()
            
            if cmd == "on":
                self.batch_mode = "telegraph"
                await update.message.reply_text("✅ 批量模式已开启 (Telegraph)")
            elif cmd == "off":
                self.batch_mode = "single"
                await update.message.reply_text("✅ 批量模式已关闭 (逐条发送)")
            elif cmd in ("title", "artist", "tags"):
                if len(args) < 2:
                    await update.message.reply_text(f"❌ 用法: `/batch {cmd} on|off`", parse_mode="Markdown")
                    return
                value = args[1].lower() in ("on", "true", "1", "yes")
                if cmd == "title":
                    self.batch_show_title = value
                elif cmd == "artist":
                    self.batch_show_artist = value
                elif cmd == "tags":
                    self.batch_show_tags = value
                await update.message.reply_text(f"✅ {cmd} 显示已{'开启' if value else '关闭'}")
            else:
                await update.message.reply_text("❌ 未知参数，使用 `/batch` 查看帮助", parse_mode="Markdown")
        
        # /block_artist 指令 - 屏蔽画师
        async def cmd_block_artist(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            args = context.args
            if not args:
                # 无参数时显示当前屏蔽列表
                from database import get_blocked_artists
                blocked = await get_blocked_artists()
                if blocked:
                    lines = ["🚫 *当前屏蔽的画师:*"]
                    for artist_id, name in blocked:
                        lines.append(f"  • `{artist_id}` ({name})")
                    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
                else:
                    await update.message.reply_text("🚫 屏蔽列表为空\n用法: `/block_artist <画师ID>`", parse_mode="Markdown")
                return
            
            try:
                artist_id = int(args[0])
                artist_name = " ".join(args[1:]).strip() if len(args) > 1 else None
                
                from database import block_artist
                await block_artist(artist_id, artist_name)
                await update.message.reply_text(f"✅ 已屏蔽画师: `{artist_id}`" + (f" ({artist_name})" if artist_name else ""), parse_mode="Markdown")
            except ValueError:
                await update.message.reply_text("❌ 画师 ID 必须是数字")
            except Exception as e:
                await update.message.reply_text(f"❌ 屏蔽失败: {e}")
        
        # /unblock_artist 指令 - 取消屏蔽画师
        async def cmd_unblock_artist(update, context):
            user_id = update.message.from_user.id
            if self.allowed_users and user_id not in self.allowed_users:
                await update.message.reply_text(f"❌ 无权限 (ID: `{user_id}`)", parse_mode="Markdown")
                return
            
            args = context.args
            if not args:
                await update.message.reply_text("用法: `/unblock_artist <画师ID>`", parse_mode="Markdown")
                return
            
            try:
                artist_id = int(args[0])
                
                from database import unblock_artist
                result = await unblock_artist(artist_id)
                if result:
                    await update.message.reply_text(f"✅ 已取消屏蔽画师: `{artist_id}`", parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"⚠️ 该画师未在屏蔽列表中: `{artist_id}`", parse_mode="Markdown")
            except ValueError:
                await update.message.reply_text("❌ 画师 ID 必须是数字")
            except Exception as e:
                await update.message.reply_text(f"❌ 取消屏蔽失败: {e}")
        
        self._app.add_handler(CommandHandler("push", cmd_push))
        self._app.add_handler(CommandHandler("schedule", cmd_schedule))
        self._app.add_handler(CommandHandler("xp", cmd_xp))
        self._app.add_handler(CommandHandler("stats", cmd_stats))
        self._app.add_handler(CommandHandler("block", cmd_block))
        self._app.add_handler(CommandHandler("unblock", cmd_unblock))
        self._app.add_handler(CommandHandler("block_artist", cmd_block_artist))
        self._app.add_handler(CommandHandler("unblock_artist", cmd_unblock_artist))
        self._app.add_handler(CommandHandler("batch", cmd_batch))
        self._app.add_handler(CommandHandler("menu", cmd_menu))
        self._app.add_handler(CommandHandler("start", cmd_menu))  # /start 也打开菜单
        self._app.add_handler(CommandHandler("help", cmd_help))
        self._app.add_handler(CallbackQueryHandler(callback_handler))
        self._app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, reply_handler))
        
        # 添加错误处理器，捕获轮询过程中的错误
        async def error_handler(update, context):
            """处理 Bot 轮询过程中的错误"""
            logger.error(f"Telegram 轮询错误: {context.error}")
            # 对于网络错误，updater 会自动重试，这里只做记录
            
        self._app.add_error_handler(error_handler)
        
        # 真正启动 Bot (非阻塞模式)
        await self._app.initialize()
        await self._app.start()
        
        # 注册菜单指令 (需在启动后)
        try:
            from telegram import BotCommand
            commands = [
                BotCommand("menu", "📋 控制面板"),
                BotCommand("push", "🚀 立即推送"),
                BotCommand("xp", "🎯 查看XP画像"),
                BotCommand("stats", "📈 策略表现"),
                BotCommand("batch", "📦 批量模式"),
                BotCommand("help", "ℹ️ 帮助信息"),
            ]
            await self._app.bot.set_my_commands(commands)
            logger.info("✅ Telegram 指令菜单已注册")
        except Exception as e:
            logger.error(f"注册指令菜单失败: {e}")
        
        # 轮询级别的错误回调（非异步）
        self._consecutive_errors = 0
        
        def polling_error_callback(error):
            """处理轮询过程中的网络错误（updater 会自动重试）"""
            self._consecutive_errors += 1
            logger.warning(f"Telegram 轮询网络错误 (第 {self._consecutive_errors} 次): {error}")
        
        # 启动轮询，配置更健壮的参数
        await self._app.updater.start_polling(
            poll_interval=1.0,           # 轮询间隔（秒）
            timeout=30,                  # 长轮询超时（秒）
            drop_pending_updates=True,   # 启动时丢弃旧的待处理更新，避免处理过期消息
            error_callback=polling_error_callback,  # 轮询错误回调
        )
        logger.info("Telegram Bot 轮询已启动（已配置自动重连）")
        
        # 启动健康检查后台任务
        asyncio.create_task(self._polling_health_check())
    
    async def _polling_health_check(self):
        """后台健康检查：监控轮询状态，自动重启"""
        await asyncio.sleep(60)  # 启动后等待一分钟再开始检查
        
        while True:
            try:
                await asyncio.sleep(60)  # 每分钟检查一次
                
                if not self._app or not self._app.updater:
                    logger.warning("Telegram 应用实例不存在，跳过健康检查")
                    continue
                
                # 检查 updater 是否还在运行
                if not self._app.updater.running:
                    logger.error("🔄 检测到 Telegram 轮询已停止，正在尝试重启...")
                    
                    try:
                        # 重新启动轮询
                        await self._app.updater.start_polling(
                            poll_interval=1.0,
                            timeout=30,
                            drop_pending_updates=True,
                        )
                        self._consecutive_errors = 0
                        logger.info("✅ Telegram 轮询已成功重启")
                    except Exception as e:
                        logger.error(f"❌ 重启轮询失败: {e}")
                else:
                    # 轮询正常运行，重置错误计数
                    if self._consecutive_errors > 0:
                        logger.info(f"Telegram 轮询恢复正常 (之前累计 {self._consecutive_errors} 次错误)")
                        self._consecutive_errors = 0
                        
            except asyncio.CancelledError:
                logger.info("健康检查任务已取消")
                break
            except Exception as e:
                logger.error(f"健康检查异常: {e}")
    
    async def stop_polling(self):
        """停止 Bot 轮询（用于健康检查重启）"""
        try:
            if self._app:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                    logger.info("Telegram updater 已停止")
                
                # 停止 application
                if self._app.running:
                    await self._app.stop()
                    logger.info("Telegram application 已停止")
                
                # 关闭 application
                await self._app.shutdown()
                logger.info("Telegram application 已关闭")
                
                self._app = None
        except Exception as e:
            logger.error(f"停止 Telegram 轮询时出错: {e}")
    
    async def send(self, illusts: list[Illust]) -> list[int]:
        """发送推送"""
        if not illusts:
            return []
        
        # Telegraph 批量模式
        if self.batch_mode == "telegraph" and len(illusts) > 1:
            return await self._send_batch_telegraph(illusts)
        
        # 逐条发送模式
        success_ids = []
        
        for illust in illusts:
            try:
                is_sent = await self._send_single(illust)
                if is_sent:
                    success_ids.append(illust.id)
                await asyncio.sleep(1)  # 避免触发限流
            except Exception as e:
                logger.error(f"发送作品 {illust.id} 失败: {e}")
        
        return success_ids
    
    async def _init_telegraph(self):
        """延迟初始化 Telegraph 客户端"""
        if self._telegraph is None:
            try:
                from telegraph import Telegraph
                self._telegraph = Telegraph()
                self._telegraph.create_account(short_name='PixivXP')
                logger.info("Telegraph 客户端初始化成功")
            except Exception as e:
                logger.error(f"Telegraph 初始化失败: {e}")
                self._telegraph = False  # 标记为失败，避免重复尝试
    
    async def _send_batch_telegraph(self, illusts: list[Illust]) -> list[int]:
        """Telegraph 批量发送模式"""
        import database as db
        
        # 初始化 Telegraph
        await self._init_telegraph()
        if not self._telegraph:
            logger.warning("Telegraph 不可用，降级为逐条发送")
            return await self._send_batch_fallback(illusts)
        
        lines = [f"📚 今日推送 ({len(illusts)}张)\n"]
        import html
        
        lines = [f"📚 今日推送 ({len(illusts)}张)\n"]
        import html
        
        # 用户要求：无论设置如何，都不在 Telegram 消息正文中显示列表
        # 列表内容仅在 Telegraph 网页中展示
        
        # 创建 Telegraph 页面
        
        # 创建 Telegraph 页面
        telegraph_url = None
        try:
            content = await self._build_telegraph_content(illusts)
            response = self._telegraph.create_page(
                title=f"Pixiv 推送 - {len(illusts)}张",
                html_content=content
            )
            telegraph_url = f"https://telegra.ph/{response['path']}"
            lines.append(f"\n🔗 <a href='{telegraph_url}'>查看详情</a>")
        except Exception as e:
            logger.warning(f"创建 Telegraph 页面失败: {e}")
            lines.append(f"\n🔗 <i>(详情页创建失败)</i>")
        
        text = "\n".join(lines)
        
        # 构建反馈按钮
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❤️ 喜欢", callback_data="batch_like"),
                InlineKeyboardButton("👎 不喜欢", callback_data="batch_dislike"),
            ]
        ])
        
        # 发送消息
        success_ids = []
        for chat_id in self.chat_ids:
            try:
                msg = await _retry_on_flood(lambda: self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                    message_thread_id=self.thread_id,
                    disable_web_page_preview=False
                ))
                if msg:
                    # 保存映射
                    await db.save_batch_mapping(msg.message_id, chat_id, illusts)
                    success_ids = [i.id for i in illusts]  # 批量模式视为全部成功
                    logger.info(f"Telegraph 批量消息已发送: {len(illusts)} 个作品")
            except Exception as e:
                logger.error(f"发送批量消息到 {chat_id} 失败: {e}")
        
        return success_ids
    
    async def _upload_image(self, session, url: str) -> str | None:
        """下载并上传图片到 Telegraph"""
        try:
            from utils import download_image_with_referer
            import aiohttp
            from PIL import Image
            import io
            
            # 1. 下载
            image_data = await download_image_with_referer(session, url, proxy=self.proxy_url)
            if not image_data:
                logger.warning(f"下载失败: {url}")
                return None
            
            # 2. 转换与压缩 (Telegraph 限制 5MB，且要求格式正确)
            # 我们统一转换为 JPEG 以避免 PNG/WebP 兼容问题
            try:
                with Image.open(io.BytesIO(image_data)) as img:
                    # 修复透明度
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    if img.mode in ('RGBA', 'LA'):
                        bg = Image.new('RGB', img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[-1])
                        img = bg
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')
                    
                    # 尺寸限制 (Telegraph 虽无明确尺寸限制但过大会失败)
                    if max(img.size) > 2560: # 2K
                         img.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                    
                    output = io.BytesIO()
                    img.save(output, format="JPEG", quality=90, optimize=True)
                    
                    # 再次检查大小，确保 < 5MB
                    if output.tell() > 5 * 1024 * 1024:
                         output.seek(0)
                         output.truncate()
                         img.save(output, format="JPEG", quality=75, optimize=True)
                    
                    image_data = output.getvalue()
            except Exception as e:
                logger.warning(f"图片转换失败 {url}: {e}，尝试直接上传")
            
            # 3. 上传
            data = aiohttp.FormData()
            data.add_field('file', image_data, filename='image.jpeg', content_type='image/jpeg')
            
            async with session.post('https://telegra.ph/upload', data=data) as resp:
                if resp.status == 200:
                    json_resp = await resp.json()
                    if isinstance(json_resp, list) and len(json_resp) > 0:
                        src = json_resp[0].get('src')
                        # logger.info(f"Telegraph 上传成功: {src}")
                        return src
                    else:
                        logger.warning(f"Telegraph 响应格式异常: {json_resp}")
                else:
                    logger.warning(f"Telegraph 上传失败 {resp.status}: {await resp.text()}")
        except Exception as e:
            logger.warning(f"Telegraph 处理异常 {url}: {e}")
        return None

    async def _build_telegraph_content(self, illusts: list[Illust]) -> str:
        """构建 Telegraph 页面内容 (并发上传图片)"""
        import aiohttp
        import asyncio
        import html
        
        # 准备结果容器 (为了保持顺序)
        results = [None] * len(illusts)
        
        async def process_one(idx, illust, sem, session):
            async with sem:
                img_src = None
                # 尝试上传图片
                if illust.image_urls:
                    # 优先使用 medium 以减小体积和加快速度 (Telegraph 也不需要原图)
                    target_url = illust.image_urls[0].replace("original", "medium") if "original" in illust.image_urls[0] else illust.image_urls[0]
                    # 如果原图太大，Telegraph 也会拒收 (限制 5MB)
                    # 这里的 target_url 是 pixiv 的 url
                    
                    src_path = await self._upload_image(session, target_url)
                    if src_path:
                        img_src = f"https://telegra.ph{src_path}"
                    else:
                        # 失败回退到反代
                        img_src = get_pixiv_cat_url(illust.id)
                
                # 构建 HTML 片段
                parts = []
                if img_src:
                    parts.append(f'<img src="{img_src}"/>')
                
                safe_title = html.escape(illust.title)
                safe_user = html.escape(illust.user_name)
                
                parts.append(f'<h4>#{idx} {safe_title}</h4>')
                parts.append(f'<p>画师: <a href="https://pixiv.net/users/{illust.user_id}">{safe_user}</a></p>')
                parts.append(f'<p>❤️ {illust.bookmark_count} | 👁 {illust.view_count}</p>')
                parts.append(f'<p><a href="https://pixiv.net/i/{illust.id}">Pixiv 原图</a></p>')
                parts.append('<hr/>')
                
                results[idx-1] = "".join(parts)
        
        # 限制并发
        sem = asyncio.Semaphore(5)
        async with aiohttp.ClientSession() as session:
            tasks = [process_one(i, ill, sem, session) for i, ill in enumerate(illusts, 1)]
            await asyncio.gather(*tasks)
        
        return "".join([r for r in results if r])
    
    async def _send_batch_fallback(self, illusts: list[Illust]) -> list[int]:
        """批量模式降级：逐条发送"""
        success_ids = []
        for illust in illusts:
            try:
                if await self._send_single(illust):
                    success_ids.append(illust.id)
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"发送作品 {illust.id} 失败: {e}")
        return success_ids
    
    def _build_batch_select_keyboard(self, action: str, count: int) -> InlineKeyboardMarkup:
        """构建作品选择按钮"""
        rows = []
        # 每行最多 5 个按钮
        for i in range(0, count, 5):
            row = []
            for j in range(i, min(i + 5, count)):
                row.append(InlineKeyboardButton(
                    str(j + 1),
                    callback_data=f"batch_select:{action}:{j + 1}"
                ))
            rows.append(row)
        
        # 添加全选和取消按钮
        rows.append([
            InlineKeyboardButton("✅ 全部" + ("喜欢" if action == "like" else "不喜欢"), 
                               callback_data=f"batch_all:{action}"),
            InlineKeyboardButton("❌ 取消", callback_data="batch_cancel"),
        ])
        
        return InlineKeyboardMarkup(rows)
        
    async def send_text(self, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        """发送文本消息到所有目标"""
        markup = None
        if buttons:
            kb = [[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
            markup = InlineKeyboardMarkup(kb)
        
        success = True
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(chat_id, text, reply_markup=markup)
            except Exception as e:
                logger.error(f"Telegram 发送文本到 {chat_id} 失败: {e}")
                success = False
        return success
    
    async def push_illusts(
        self, 
        illusts: list, 
        message_prefix: str = "", 
        reply_to_message_id: int | None = None
    ) -> dict[int, int]:
        """
        推送作品列表（用于连锁推荐等场景）
        
        Args:
            illusts: 作品列表
            message_prefix: 消息前缀，会添加到 caption 开头
            reply_to_message_id: 要回复的消息 ID（用于形成消息链）
        
        Returns:
            dict[illust_id, message_id]: 成功发送的作品 ID 到消息 ID 的映射
        """
        if not illusts:
            return {}
        
        result_map = {}  # illust_id -> message_id
        
        for illust in illusts:
            try:
                # 构建 caption
                caption = self.format_message(illust)
                if message_prefix:
                    caption = f"{message_prefix}\n\n{caption}"
                
                keyboard = self._build_keyboard(illust.id)
                topic_id = self._resolve_topic_id(illust)
                
                # 下载图片
                image_data = None
                if self.client and illust.image_urls:
                    try:
                        image_data = await self.client.download_image(illust.image_urls[0])
                        if image_data:
                            image_data = self._compress_image(image_data)
                    except Exception as e:
                        logger.warning(f"下载图片失败: {e}")
                
                # 发送到第一个 chat_id（通常连锁推送只发给触发者所在的 chat）
                # 如果需要广播给所有 chat，可以改为遍历
                chat_id = self.chat_ids[0] if self.chat_ids else None
                if not chat_id:
                    continue
                
                sent_message = None
                try:
                    if image_data:
                        sent_message = await _retry_on_flood(lambda: self.bot.send_photo(
                            chat_id=chat_id,
                            photo=BytesIO(image_data),
                            caption=caption,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                            message_thread_id=topic_id,
                            reply_to_message_id=reply_to_message_id,
                            read_timeout=60,
                            write_timeout=60
                        ))
                    else:
                        from utils import get_pixiv_cat_url
                        proxy_url = get_pixiv_cat_url(illust.id)
                        sent_message = await _retry_on_flood(lambda: self.bot.send_photo(
                            chat_id=chat_id,
                            photo=proxy_url,
                            caption=caption,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                            message_thread_id=topic_id,
                            reply_to_message_id=reply_to_message_id,
                            read_timeout=60,
                            write_timeout=60
                        ))
                    
                    if sent_message:
                        self._message_illust_map[sent_message.message_id] = illust.id
                        result_map[illust.id] = sent_message.message_id
                        logger.info(f"🔗 连锁推送成功: {illust.id} -> msg_id={sent_message.message_id}")
                        
                except Exception as e:
                    logger.error(f"连锁推送到 {chat_id} 失败: {e}")
                
                await asyncio.sleep(1)  # 避免触发限流
                
            except Exception as e:
                logger.error(f"处理连锁作品 {illust.id} 失败: {e}")
        
        return result_map
    
    async def _send_single(self, illust: Illust) -> bool:
        """发送单个作品"""
        caption = self.format_message(illust)
        keyboard = self._build_keyboard(illust.id)
        
        # 动态 Topic ID
        topic_id = self._resolve_topic_id(illust)
        
        if getattr(illust, 'type', 'illust') == 'ugoira':
            return await self._send_video(illust, caption, keyboard, topic_id)
        
        # 多页逻辑
        if illust.page_count > self.max_pages:
            # 超过阈值：强制降级为封面模式
            # 在 caption 之后追加“长篇内容”提示
            long_caption = caption.replace("🎨", "📚 [长篇精选] 🎨")
            long_caption += f"\n\n<i>(本作品共 {illust.page_count} 页，仅展示封面)</i>"
            return await self._send_photo(illust, long_caption, keyboard, topic_id)

        if illust.page_count == 1 or self.multi_page_mode == "cover_link":
            # 单图或强制封面模式
            return await self._send_photo(illust, caption, keyboard, topic_id)
        else:
            # 多图打包模式 (2 到 max_pages 页)
            return await self._send_media_group(illust, caption, keyboard, topic_id)
    
    async def _send_photo(self, illust: Illust, caption: str, keyboard: InlineKeyboardMarkup, topic_id: int | None = None) -> bool:
        """发送单张图片到所有目标"""
        any_success = False
        # 先下载图片（如果可以）
        image_data = None
        if self.client and illust.image_urls:
            try:
                image_data = await self.client.download_image(illust.image_urls[0])
                if image_data:
                    image_data = self._compress_image(image_data)
            except Exception as e:
                logger.warning(f"下载图片失败: {e}")
        
        # 发送到所有 chat_id
        for chat_id in self.chat_ids:
            sent_message = None
            try:
                if image_data:
                    sent_message = await _retry_on_flood(lambda: self.bot.send_photo(
                        chat_id=chat_id,
                        photo=BytesIO(image_data),
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=60,
                        write_timeout=60
                    ))
                else:
                    # Fallback: 使用反代链接
                    proxy_url = get_pixiv_cat_url(illust.id)
                    sent_message = await _retry_on_flood(lambda: self.bot.send_photo(
                        chat_id=chat_id,
                        photo=proxy_url,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=self.thread_id,
                        read_timeout=60,
                        write_timeout=60
                    ))
                
                if sent_message:
                    self._message_illust_map[sent_message.message_id] = illust.id
                    any_success = True
            except Exception as e:
                logger.error(f"发送到 {chat_id} 失败: {e}")
        
        # 限制映射大小，避免内存泄漏
        if len(self._message_illust_map) > 200:
            oldest_keys = list(self._message_illust_map.keys())[:100]
            for k in oldest_keys:
                del self._message_illust_map[k]
        
        return any_success

    async def _send_video(self, illust: Illust, caption: str, keyboard: InlineKeyboardMarkup, topic_id: int | None = None) -> bool:
        """发送动图视频 (优先PixivCat，失败则尝试本地转码)"""
        any_success = False
        video_url = f"https://pixiv.cat/{illust.id}.mp4"
        
        # 缓存本地转码结果，避免重复下载转换
        local_mp4_bytes = None
        
        for chat_id in self.chat_ids:
            try:
                # 1. 如果已有本地数据，直接发送
                if local_mp4_bytes:
                    video_file = BytesIO(local_mp4_bytes)
                    video_file.name = f"{illust.id}.mp4"
                    
                    await _retry_on_flood(lambda: self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_file,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=60,
                        write_timeout=60
                    ))
                    any_success = True
                    continue

                # 2. 尝试反代 URL
                try:
                    sent = await _retry_on_flood(lambda: self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_url,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=60,
                        write_timeout=60
                    ))
                    if sent:
                        self._message_illust_map[sent.message_id] = illust.id
                        any_success = True
                        continue
                except Exception:
                    # 如果 URL 发送失败，进入转码流程
                    pass
                
                # 3. 尝试本地转码 (仅当反代失败且尚未转码时)
                if not local_mp4_bytes and self.client:
                    logger.info(f"反代链接不可用，尝试本地转码作品 {illust.id}...")
                    try:
                        meta = await self.client.get_ugoira_metadata(illust.id)
                        if meta and meta.get('ugoira_metadata'):
                            u_meta = meta['ugoira_metadata']
                            zip_url = u_meta['zip_urls']['medium']
                            frames = u_meta['frames']
                            
                            logger.info(f"正在下载动图包: {zip_url}")
                            zip_data = await self.client.download_image(zip_url)
                            if zip_data:
                                from utils import convert_ugoira_to_mp4
                                logger.info(f"正在转换 MP4 ({len(zip_data)} bytes)...")
                                local_mp4_bytes = convert_ugoira_to_mp4(zip_data, frames)
                    except Exception as exc:
                        logger.error(f"本地转码失败: {exc}")

                # 4. 如果转码成功，重试发送
                if local_mp4_bytes:
                    video_file = BytesIO(local_mp4_bytes)
                    video_file.name = f"{illust.id}.mp4"
                    
                    sent = await _retry_on_flood(lambda: self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_file,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=120,
                        write_timeout=120
                    ))
                    if sent:
                        self._message_illust_map[sent.message_id] = illust.id
                        any_success = True
                    continue
                    
                # 5. 最终降级：发送封面
                raise Exception("所有动图发送方式均失败")

            except Exception as e:
                logger.warning(f"发送动图到 {chat_id} 失败: {e}")
                # 降级尝试发送封面
                try:
                   fallback_cap = caption + f"\n(⚠️ 动图发送失败，<a href='{video_url}'>点击观看</a>)"
                   await self._send_photo(illust, fallback_cap, keyboard)
                   any_success = True
                except:
                   pass
        return any_success
    
    async def _send_media_group(self, illust: Illust, caption: str, keyboard: InlineKeyboardMarkup, topic_id: int | None = None) -> bool:
        """发送多图到所有目标"""
        media = []
        any_success = False
        
        # 限制在 max_pages 以内 (且不能超过 TG API 的 10 张限制)
        limit = min(self.max_pages, 10, len(illust.image_urls))
        for i, url in enumerate(illust.image_urls[:limit]):
            try:
                if self.client:
                    image_data = await self.client.download_image(url)
                    if image_data:
                        image_data = self._compress_image(image_data)
                    photo = BytesIO(image_data)
                else:
                    photo = get_pixiv_cat_url(illust.id, i)
                
                media.append(InputMediaPhoto(
                    media=photo,
                    caption=caption if i == 0 else None,
                    parse_mode="HTML" if i == 0 else None
                ))
            except Exception as e:
                logger.warning(f"获取第{i+1}页失败: {e}")
        
        if media:
            for chat_id in self.chat_ids:
                try:
                    await _retry_on_flood(lambda: self.bot.send_media_group(
                        chat_id=chat_id,
                        media=media,
                        message_thread_id=self.thread_id,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=60
                    ))
                    any_success = True  # 图片发送成功即视为成功
                    
                    # MediaGroup不支持按钮，单独发送 (允许失败)
                    try:
                        await _retry_on_flood(lambda: self.bot.send_message(
                            chat_id=chat_id,
                            text=f"作品 #{illust.id} 的操作：",
                            reply_markup=keyboard,
                            message_thread_id=self.thread_id
                        ))
                    except Exception as e:
                        logger.warning(f"发送操作按钮到 {chat_id} 失败: {e}")
                        
                except Exception as e:
                    logger.error(f"发送 MediaGroup 到 {chat_id} 失败: {e}")
        return any_success
    
    def format_message(self, illust: Illust) -> str:
        """格式化消息"""
        tags = " ".join(f"#{t}" for t in illust.tags[:5])
        r18_mark = "🔞 " if illust.is_r18 else ""
        ugoira_mark = "🎞️ " if getattr(illust, 'type', 'illust') == 'ugoira' else ""
        
        # 获取匹配度（如果有）
        match_score = getattr(illust, 'match_score', None)
        match_line = f"🎯 匹配度: {match_score*100:.0f}%\n" if match_score is not None else ""
        
        return (
            f"{r18_mark}{ugoira_mark}🎨 <b>{illust.title}</b>\n"
            f"👤 {illust.user_name} (ID: {illust.user_id})\n"
            f"❤️ {illust.bookmark_count} | 👀 {illust.view_count}\n"
            f"{match_line}"
            f"🏷️ {tags}\n"
            f"🔗 <a href=\"https://pixiv.net/i/{illust.id}\">原图链接</a>"
        )
    
    def _build_keyboard(self, illust_id: int) -> InlineKeyboardMarkup:
        """构建反馈按钮"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❤️ 喜欢", callback_data=f"like:{illust_id}"),
                InlineKeyboardButton("👎 不喜欢", callback_data=f"dislike:{illust_id}"),
            ],
            [
                InlineKeyboardButton("🔗 查看原图", url=f"https://pixiv.net/i/{illust_id}"),
            ]
        ])
    
    async def handle_feedback(self, illust_id: int, action: str) -> bool:
        """处理反馈回调"""
        if self.on_feedback:
            await self.on_feedback(illust_id, action)
        return True
    

