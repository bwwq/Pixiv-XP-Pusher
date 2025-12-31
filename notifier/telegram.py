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
    Retry a coroutine on Flood Control errors.
    coro_func should be a callable that returns a coroutine (not the coroutine itself).
    """
    from telegram.error import RetryAfter
    
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except RetryAfter as e:
            wait_time = e.retry_after + 1  # Add 1 second buffer
            logger.warning(f"Flood control exceeded. Sleeping for {wait_time} seconds...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            error_msg = str(e)
            if "Flood control exceeded" in error_msg:
                # Parse retry time from error message
                import re
                match = re.search(r"Retry in (\d+)", error_msg)
                wait_time = int(match.group(1)) + 1 if match else 10
                logger.warning(f"Flood control exceeded. Sleeping for {wait_time} seconds...")
                await asyncio.sleep(wait_time)
            else:
                raise  # Re-raise non-flood errors
    
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
        topic_tag_mapping: dict | None = None  # 标签到分类的映射 {category: [tags]}
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
        
        # 日志
        logger.info(f"Telegram 推送目标: {', '.join(self.chat_ids) or '无'}")
        if self.allowed_users:
            logger.info(f"允许反馈的用户: {self.allowed_users}")
        if self.topic_rules:
            logger.info(f"Topic 分流规则: {list(self.topic_rules.keys())}")

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


    async def stop_polling(self):
        """停止Bot轮询"""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

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
        
        builder = Application.builder().token(self.bot.token)
        if self.proxy_url:
            request = HTTPXRequest(proxy=self.proxy_url)
            builder = builder.request(request)
        
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
            
            try:
                await query.answer()
            except Exception as e:
                # 忽略 "Query is too old" 等错误
                pass
            
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

            if ":" in data:
                action, illust_id = data.split(":")
                if action in ("like", "dislike"):
                    await self.handle_feedback(int(illust_id), action)
                    
                    emoji = "❤️" if action == "like" else "👎"
                    try:
                        await query.edit_message_reply_markup(reply_markup=None)
                        await query.message.reply_text(f"{emoji} 已记录反馈")
                    except Exception:
                        pass
        
        # 处理回复消息（1=喜欢, 2=不喜欢）
        async def reply_handler(update, context):
            message = update.message
            if not message or not message.reply_to_message:
                return
            
            user_id = message.from_user.id
            
            # 权限验证
            if self.allowed_users and user_id not in self.allowed_users:
                return
            
            text = message.text.strip()
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
                "`/push` - 🚀 立即触发推送\n"
                "`/xp` - 🎯 查看 XP 画像 (Top Tags)\n"
                "`/stats` - 📈 查看策略成功率\n"
                "`/schedule` - ⏰ 查看/修改定时时间\n"
                "`/block <tag>` - 🚫 屏蔽标签\n"
                "`/unblock <tag>` - ✅ 取消屏蔽标签\n"
                "`/block_artist <id>` - 🚫 屏蔽画师\n"
                "`/unblock_artist <id>` - ✅ 取消屏蔽画师\n"
                "`/help` - ℹ️ 显示此帮助\n\n"
                "*💡 Tips:*\n"
                "• 回复作品消息发送 `1` = 喜欢\n"
                "• 回复作品消息发送 `2` = 不喜欢"
            )
            await update.message.reply_text(help_text, parse_mode="Markdown")
        
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
        self._app.add_handler(CommandHandler("help", cmd_help))
        self._app.add_handler(CallbackQueryHandler(callback_handler))
        self._app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, reply_handler))
        
        # 真正启动 Bot (非阻塞模式)
        await self._app.initialize()
        await self._app.start()
        
        # 注册菜单指令 (需在启动后)
        try:
            from telegram import BotCommand
            commands = [
                BotCommand("push", "🚀 立即推送"),
                BotCommand("xp", "🎯 查看XP画像"),
                BotCommand("stats", "📈 策略表现"),
                BotCommand("schedule", "⏰ 调整时间"),
                BotCommand("block", "🚫 屏蔽标签"),
                BotCommand("unblock", "✅ 取消屏蔽"),
                BotCommand("help", "ℹ️ 帮助信息"),
            ]
            await self._app.bot.set_my_commands(commands)
            logger.info("✅ Telegram 指令菜单已注册")
        except Exception as e:
            logger.error(f"注册指令菜单失败: {e}")
            
        await self._app.updater.start_polling()
        logger.info("Telegram Bot 轮询已启动")
    
    async def send(self, illusts: list[Illust]) -> list[int]:
        """发送推送"""
        if not illusts:
            return []
        
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
                    
                    await self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_file,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=60,
                        write_timeout=60
                    )
                    any_success = True
                    continue

                # 2. 尝试反代 URL
                try:
                    sent = await self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_url,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=60,
                        write_timeout=60
                    )
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
                    
                    sent = await self.bot.send_animation(
                        chat_id=chat_id,
                        animation=video_file,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        message_thread_id=topic_id,
                        read_timeout=120,
                        write_timeout=120
                    )
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
    

