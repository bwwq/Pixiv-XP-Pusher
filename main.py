
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Ensure project root in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import load_config, get_proxy_url, CONFIG_PATH
from database import init_db, cache_illust, get_cached_illust_tags, get_cached_illust, mark_pushed
from pixiv_client import PixivClient
from profiler import XPProfiler
from fetcher import ContentFetcher
from filter import ContentFilter
from notifier.telegram import TelegramNotifier
from notifier.onebot import OneBotNotifier
from utils import setup_logging

logger = logging.getLogger(__name__)


async def retry_async(coro_func, *args, max_retries: int = 3, delay: float = 5.0, backoff: float = 2.0, **kwargs):
    """
    通用异步重试函数

    Args:
        coro_func: 要执行的异步函数
        max_retries: 最大重试次数
        delay: 初始延迟秒数
        backoff: 延迟倍增系数

    Returns:
        函数返回值，或在所有重试失败后返回 None
    """
    last_error = None
    current_delay = delay

    for attempt in range(max_retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(f"操作失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}，{current_delay:.1f}s 后重试...")
                await asyncio.sleep(current_delay)
                current_delay *= backoff
            else:
                logger.error(f"操作最终失败 (已重试 {max_retries} 次): {e}")

    return None


# 全局运行锁，防止任务并发
_task_lock = asyncio.Lock()

async def setup_notifiers(config: dict, client: PixivClient, profiler: XPProfiler, sync_client: PixivClient = None):
    """创建并配置推送器（支持多推送渠道）"""
    # sync_client 用于 on_action 回调中的 main_task 调用
    if sync_client is None:
        sync_client = client
    notifier_cfg = config.get("notifier", {})
    proxy_url = get_proxy_url(config)
    # 支持单个 type 字符串或 types 列表
    notifier_types = notifier_cfg.get("types") or [notifier_cfg.get("type", "telegram")]
    if isinstance(notifier_types, str):
        notifier_types = [notifier_types]

    # 延迟引用避免因为 notifiers 列表未初始化完成导致的问题
    # 但 on_feedback 需要访问 notifiers 列表
    # 我们可以把 notifiers 定义在外部列表，然后引用它
    notifiers_list = []
    max_pages = notifier_cfg.get("max_pages", 10)

    async def push_related_task(seed_illust, parent_msg_id: int = None, current_depth: int = 1):
        """
        异步：推送关联作品

        Args:
            seed_illust: 触发连锁的作品
            parent_msg_id: 父消息 ID（用于回复形成消息链）
            current_depth: 当前连锁深度（从 1 开始）
        """
        try:
            logger.info(f"🔗 触发连锁反应 (深度={current_depth}): 正在获取 {seed_illust.id} 的关联作品...")
            # 1. 获取关联
            related = await client.get_related_illusts(seed_illust.id, limit=20)
            if not related:
                logger.info(f"🔗 作品 {seed_illust.id} 无关联推荐，连锁结束")
                return

            # 2. 过滤 (复用 ContentFilter 逻辑，但简化参数)
            from filter import ContentFilter
            # 临时构造 filter 配置
            filter_cfg = config.get("filter", {})
            c_filter = ContentFilter(
                blacklist_tags=list(profiler.stop_words), # 使用实时黑名单
                exclude_ai=filter_cfg.get("exclude_ai", True),
                r18_mode=filter_cfg.get("r18_mode", False),
                min_create_days=filter_cfg.get("min_create_days", 0)
            )

            # 使用简单的过滤逻辑 (不去重 SENT_HISTORY，因为这是用户主动要求的)
            # 但我们要去重 "已收藏" 和 "画师屏蔽"
            filtered = []
            import database as db_mod
            xp_profile = await db_mod.get_xp_profile()

            for ill in related:
                # 严格去重 (ID 类型统一)
                if int(ill.id) == int(seed_illust.id): continue

                # 过滤已推送过的作品 (响应用户需求: 不推老图)
                if await db_mod.is_pushed(ill.id):
                    logger.debug(f"🔗 作品 {ill.id} 已推送过，跳过推荐")
                    continue
                # 检查屏蔽
                if not c_filter.check_illust(ill): continue
                if ill.user_id in profiler._blocked_artist_ids: continue

                # 计算分数
                score = 0
                for t in ill.tags:
                     norm = t.lower().replace(" ", "_")
                     if norm in xp_profile: score += xp_profile[norm]

                # Artist Boost
                artist_score = await db_mod.get_artist_score(ill.user_id)
                score += artist_score

                filtered.append((ill, score))

            # 排序取前 N
            filtered.sort(key=lambda x: x[1], reverse=True)

            push_limit = config.get("feedback", {}).get("related_push_limit", 1)
            top_results = [x[0] for x in filtered[:push_limit]]

            if top_results:
                # 构建消息前缀（包含源作品信息）
                source_title = getattr(seed_illust, 'title', f'#{seed_illust.id}')
                message_prefix = f"🔗 连锁推荐 (源自: {source_title})"

                logger.info(f"🔗 连锁推送: {len(top_results)} 个关联作品")
                for n in notifiers_list:
                    if hasattr(n, 'push_illusts'):
                        # 使用 push_illusts 带回复功能
                        sent_map = await n.push_illusts(
                            top_results,
                            message_prefix=message_prefix,
                            reply_to_message_id=parent_msg_id
                        )

                        # 缓存连锁作品信息（包含链深度）
                        for ill in top_results:
                            # 获取该作品对应的消息 ID
                            msg_id = sent_map.get(ill.id)
                            # 缓存作品信息 + 链元数据
                            await db_mod.cache_illust(
                                illust_id=ill.id,
                                tags=ill.tags,
                                user_id=ill.user_id,
                                user_name=ill.user_name,
                                source='related',  # 连锁推送来源
                                chain_depth=current_depth,
                                chain_parent_id=seed_illust.id,
                                chain_msg_id=msg_id
                            )
                            # 记录推送来源
                            await db_mod.mark_pushed(ill.id, 'related')
            else:
                logger.info("🔗 关联作品过滤后为空")

        except Exception as e:
            logger.error(f"连锁推送失败: {e}")

    async def on_feedback(illust_id: int, action: str):
        """反馈回调 (优化版：使用缓存避免 API 调用)"""
        illust = None

        # 1. 尝试从缓存获取
        cached = await get_cached_illust(illust_id)
        if cached:
            from pixiv_client import Illust
            from datetime import datetime
            illust = Illust(
                id=cached["id"],
                title="",
                user_id=cached.get("user_id", 0),
                user_name=cached.get("user_name", ""),
                tags=cached.get("tags", []),
                bookmark_count=0,
                view_count=0,
                page_count=1,
                image_urls=[],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now()
            )
            # 是否需要完整信息（如点赞时不知道画家ID）
            if (action in ("like", "1") and illust.user_id == 0):
                try:
                    full = await client.get_illust_detail(illust_id)
                    if full: illust = full
                except Exception as e:
                    logger.warning(f"补充详情失败: {e}")

        # 2. 缓存未命中，回退到 API
        if not illust:
            logger.warning(f"未找到作品缓存: {illust_id}，尝试从 API 获取...")
            try:
                illust = await client.get_illust_detail(illust_id)
                if illust:
                    # 补充写入缓存
                    await cache_illust(illust.id, illust.tags, illust.user_id, illust.user_name)
                    logger.info(f"API 获取成功并已缓存: {illust.title}")
            except Exception as e:
                logger.error(f"API 回退获取失败: {e}")

        if not illust:
            logger.error(f"无法获取作品信息: {illust_id}，反馈处理中止")
            return

        # 3. 执行核心反馈逻辑
        suggested_block_tag = await profiler.apply_feedback(
            illust=illust,
            action=action,
            config=config.get("feedback", {})
        )

        # 如果 profiler 建议屏蔽
        if suggested_block_tag:
             msg = f"🚫 Tag `{suggested_block_tag}` 累计不喜欢已达阈值。\n是否屏蔽？\n发送 `/block {suggested_block_tag}` 确认屏蔽。"
             for n in notifiers_list:
                 if hasattr(n, 'send_text'):
                     await n.send_text(msg)

        # 如果是"喜欢"，同步添加到 Pixiv 收藏
        if action in ("like", "1"):
             try:
                 await sync_client.add_bookmark(illust_id)

                 # 更新 MAB 策略反馈 (排除连锁推荐，连锁只计入 Tag 统计)
                 from database import get_push_source, update_strategy_stats
                 source = await get_push_source(illust_id)
                 if source and source != 'related':
                     await update_strategy_stats(source, is_success=True)
                     logger.info(f"MAB策略 '{source}' 获得正反馈")

                 # === Chain Reaction Logic (Per-Image Depth) ===
                 if "related" in config.get("strategies", ["related"]):
                     max_depth = config.get("feedback", {}).get("max_chain_depth", 3)

                     # 从缓存中获取当前作品的链深度和消息 ID
                     chain_depth = cached.get("chain_depth", 0) if cached else 0
                     chain_msg_id = cached.get("chain_msg_id") if cached else None

                     # Fallback: 从 notifier 的消息映射中查找（用于非连锁推送的原图）
                     if chain_msg_id is None:
                         for n in notifiers_list:
                             if hasattr(n, '_message_illust_map'):
                                 # 反查：illust_id -> message_id
                                 for msg_id, ill_id in n._message_illust_map.items():
                                     if ill_id == illust_id:
                                         chain_msg_id = msg_id
                                         break
                             if chain_msg_id:
                                 break

                     # 如果深度未超限，触发新一层连锁
                     next_depth = chain_depth + 1
                     if next_depth <= max_depth:
                         logger.info(f"🔗 触发连锁 (当前深度={chain_depth}, 下一层={next_depth})")
                         asyncio.create_task(push_related_task(
                             illust,
                             parent_msg_id=chain_msg_id,
                             current_depth=next_depth
                         ))
                     else:
                         logger.info(f"🔗 作品 {illust_id} 连锁深度已达上限 ({chain_depth}/{max_depth})，跳过")

             except Exception as e:
                 logger.error(f"同步收藏/连锁处理失败: {e}")

        logger.info(f"反馈处理完成: illust_id={illust_id}, action={action}")

    # ... (rest of setup_notifiers) ...


    async def on_action(action: str, data: any):
        """通用动作回调"""
        if action == "retry_ai":
            error_id = int(data)
            logger.info(f"收到重试请求: error_id={error_id}")

            try:
                from database import get_ai_error, update_ai_error_status
                import json

                # 1. 获取错误记录
                error_record = await get_ai_error(error_id)
                if not error_record:
                    logger.error("错误记录不存在")
                    return

                if error_record["status"] == "resolved":
                    logger.info("该错误已修复")
                    return

                tags = json.loads(error_record["tags_content"])

                # 2. 重新尝试 AI 处理
                logger.info(f"正在重试 AI 处理 {len(tags)} 个标签...")
                valid, mapping = await profiler.ai_processor.process_tags(tags)

                await update_ai_error_status(error_id, "resolved")

                # 通知用户（使用第一个可用的 notifier）
                msg = f"✅ 修复成功！\n已验证 AI 配置可用。\n({len(tags)} 个标签已正确处理)"
                for n in notifiers:
                    if hasattr(n, 'send_text'):
                        await n.send_text(msg)
                        break

            except Exception as e:
                logger.error(f"重试失败: {e}")

        elif action == "run_task":
             # 手动触发推送任务
             logger.info("🤖 收到 Bot 手动推送指令")
             # 确保 config, client, profiler, notifiers 可用
             # 这里是一个闭包，可以直接访问外部变量
             # 使用 create_task 异步执行，避免阻塞 Bot 响应
             asyncio.create_task(main_task(config, client, profiler, notifiers, sync_client))

        elif action == "update_schedule":
            # 更新调度计划 (支持多个时间)
            schedule_str = str(data)
            logger.info(f"📅 收到调度更新请求: {schedule_str}")
            try:
                # 1. 持久化
                from database import set_state
                await set_state("schedule_cron", schedule_str)

                # 2. 如果 scheduler 实例存在，重新调度
                if 'scheduler' in config:
                    sched = config['scheduler']

                    # 移除所有旧的 push_job
                    for job in sched.get_jobs():
                        if job.id.startswith('push_job'):
                            sched.remove_job(job.id)

                    # 添加新的任务
                    cron_list = [c.strip() for c in schedule_str.split(",") if c.strip()]
                    for i, cron_expr in enumerate(cron_list):
                        try:
                            sched.add_job(
                                main_task,
                                CronTrigger.from_crontab(cron_expr),
                                args=[config, client, profiler, notifiers, sync_client],
                                id=f'push_job_{i}'
                            )
                        except Exception as e:
                            logger.error(f"添加任务失败 ({cron_expr}): {e}")

                    logger.info(f"✅ 调度任务已更新，共 {len(cron_list)} 个时间点")
            except Exception as e:
                logger.error(f"更新调度失败: {e}")

    notifiers = []

    if "telegram" in notifier_types:
        tg_cfg = notifier_cfg.get("telegram", {})
        # 支持旧配置 chat_id 或新配置 chat_ids
        chat_ids = tg_cfg.get("chat_ids") or tg_cfg.get("chat_id")
        if tg_cfg.get("bot_token") and chat_ids:
            notifiers.append(TelegramNotifier(
                bot_token=tg_cfg["bot_token"],
                chat_ids=chat_ids,
                client=client,
                multi_page_mode=notifier_cfg.get("multi_page_mode", "cover_link"),
                allowed_users=tg_cfg.get("allowed_users"),
                thread_id=tg_cfg.get("thread_id"),
                on_feedback=on_feedback,
                on_action=on_action,
                proxy_url=proxy_url,
                max_pages=max_pages,
                image_quality=tg_cfg.get("image_quality", 85),
                max_image_size=tg_cfg.get("max_image_size", 2000),
                topic_rules=tg_cfg.get("topic_rules"),
                topic_tag_mapping=tg_cfg.get("topic_tag_mapping"),
                # 批量模式配置
                batch_mode=tg_cfg.get("batch_mode", "single"),
                batch_show_title=tg_cfg.get("batch_show_title", True),
                batch_show_artist=tg_cfg.get("batch_show_artist", True),
                batch_show_tags=tg_cfg.get("batch_show_tags", True),
            ))
            logger.info("已启用 Telegram 推送")

    if "onebot" in notifier_types:
        ob_cfg = notifier_cfg.get("onebot", {})
        ob_mode = str(ob_cfg.get("mode", "forward")).lower().strip()
        if ob_mode == "reverse" or ob_cfg.get("ws_url"):
            ob_notifier = None
            try:
                ob_notifier = OneBotNotifier(
                    ws_url=ob_cfg.get("ws_url"),
                    mode=ob_mode,
                    reverse_config=ob_cfg.get("reverse"),
                    private_id=ob_cfg.get("private_id"),
                    group_id=ob_cfg.get("group_id"),
                    push_to_private=ob_cfg.get("push_to_private", True),
                    push_to_group=ob_cfg.get("push_to_group", False),
                    master_id=ob_cfg.get("master_id"),
                    on_feedback=on_feedback,
                    on_action=on_action,
                    client=client,
                    max_pages=max_pages,
                    proxy_url=proxy_url
                )
                if ob_mode == "reverse":
                    await ob_notifier.start_reverse_server()
                else:
                    await ob_notifier.connect()
                notifiers.append(ob_notifier)
                logger.info(f"已启用 OneBot 推送 ({ob_mode})")
            except Exception as e:
                logger.error(f"OneBot 连接失败: {e}")
                if ob_notifier:
                    await ob_notifier.close()

    if "astrbot" in notifier_types:
        ab_cfg = notifier_cfg.get("astrbot", {})
        if ab_cfg.get("http_url") and ab_cfg.get("unified_msg_origin"):
            from notifier.astrbot import AstrBotNotifier
            ab_notifier = AstrBotNotifier(
                http_url=ab_cfg["http_url"],
                unified_msg_origin=ab_cfg["unified_msg_origin"],
                api_key=ab_cfg.get("api_key"),
                on_feedback=on_feedback,
                on_action=on_action,
                client=client,
                max_pages=max_pages,
                image_quality=ab_cfg.get("image_quality", 85),
                max_image_size=ab_cfg.get("max_image_size", 1500)
            )
            notifiers.append(ab_notifier)
            logger.info("已启用 AstrBot 推送")

    if "qwenpaw" in notifier_types:
        qp_cfg = notifier_cfg.get("qwenpaw", {})
        if qp_cfg.get("cronid"):
            from notifier.qwenpaw import QwenPawNotifier
            qp_notifier = QwenPawNotifier(
                cronid=qp_cfg["cronid"],
                keep_history=qp_cfg.get("keep_history", False),
                client=client,
                max_pages=max_pages,
                max_image_size_mb=qp_cfg.get("max_image_size_mb", 9.5),
                qwenpaw_bin=qp_cfg.get("qwenpaw_bin"),
            )
            notifiers.append(qp_notifier)
            logger.info(f"已启用 QwenPaw 推送")

    # 将创建的 notifiers 填充到 notifiers_list (供 push_related_task 等闭包使用)
    notifiers_list.extend(notifiers)

    return notifiers if notifiers else None


async def setup_services(config: dict):
    """初始化全局服务 (DB, Client, Profiler, Notifiers)"""
    await init_db()

    # 公共网络配置
    network_cfg = config.get("network", {})
    pixiv_cfg = config.get("pixiv", {})
    proxy_url = get_proxy_url(config)

    client_kwargs = {
        "requests_per_minute": network_cfg.get("requests_per_minute", 60),
        "random_delay": tuple(network_cfg.get("random_delay", [1.0, 3.0])),
        "max_concurrency": network_cfg.get("max_concurrency", 5),
        "proxy_url": proxy_url
    }

    # 主客户端 (用于搜索、排行榜等高风险操作)
    main_client = PixivClient(
        refresh_token=pixiv_cfg.get("refresh_token"),
        **client_kwargs
    )
    await main_client.login()

    # 同步客户端 (用于获取收藏、关注动态等低风险操作)
    sync_token = pixiv_cfg.get("sync_token")
    if sync_token:
        sync_client = PixivClient(
            refresh_token=sync_token,
            **client_kwargs
        )
        await sync_client.login()
        logger.info("✅ 已启用同步专用 Token (sync_token)")
    else:
        sync_client = main_client  # 回退到主客户端
        logger.info("未配置 sync_token，收藏同步将使用主 Token")

    # Init Profiler (使用 sync_client，只读操作)
    profiler_cfg = config.get("profiler", {})
    profiler = XPProfiler(
        client=sync_client,  # 使用同步客户端获取收藏
        stop_words=profiler_cfg.get("stop_words"),
        discovery_rate=profiler_cfg.get("discovery_rate", 0.1),
        time_decay_days=profiler_cfg.get("time_decay_days", 180),
        ai_config=profiler_cfg.get("ai"),
        saturation_threshold=profiler_cfg.get("saturation_threshold", 0.5)
    )

    # Init Notifiers (使用 main_client 用于下载图片等，sync_client 用于 on_action 回调)
    notifiers = await setup_notifiers(config, main_client, profiler, sync_client)

    # 返回双客户端
    return main_client, sync_client, profiler, notifiers


async def main_task(config: dict, client: PixivClient, profiler: XPProfiler, notifiers: list, sync_client: PixivClient = None):
    """
    执行一次完整的推送任务 (依赖外部服务)

    Args:
        client: 主客户端 (用于搜索、排行榜、下载)
        sync_client: 同步客户端 (用于获取关注动态，可选)
    """
    # 如果未传入 sync_client，使用 main_client
    if sync_client is None:
        sync_client = client

    if _task_lock.locked():
        logger.info("⏳ 推送任务正在运行中，本次触发已跳过或排队")

    async with _task_lock:
        logger.info("=== 开始推送任务 ===")

        # 刷新 Access Token (防止长时间运行后过期)
        try:
            await client.login()
            if sync_client and sync_client is not client:
                await sync_client.login()
        except Exception as e:
            logger.warning(f"Token 刷新失败: {e}")

    try:
        # 1. 构建/更新 XP 画像
        profiler_cfg = config.get("profiler", {})

        await profiler.build_profile(
            user_id=config["pixiv"]["user_id"],
            scan_limit=profiler_cfg.get("scan_limit", 500),
            include_private=profiler_cfg.get("include_private", True)
        )

        top_tags = await profiler.get_top_tags(profiler_cfg.get("top_n", 20))
        logger.info(f"Top XP Tags: {[t[0] for t in top_tags[:10]]}")

        if config.get("test"): # Test mode skip heavy DB load if possible, but we need it for xp_profile
             pass

        # 获取完整的 XP Profile 用于匹配度计算
        import database as db_module
        xp_profile = await db_module.get_xp_profile()

        # 2. 获取内容
        fetcher_cfg = config.get("fetcher", {})

        # 1.5 获取关注列表（使用 sync_client，低风险操作）
        following_ids = set()
        pixiv_uid = config.get("pixiv", {}).get("user_id", 0)
        if pixiv_uid:
            try:
                following_ids = await sync_client.fetch_following(user_id=pixiv_uid)
            except Exception as e:
                logger.warning(f"获取关注列表失败: {e}")

        manual_subs = set(fetcher_cfg.get("subscribed_artists") or [])
        all_subs = list(following_ids | manual_subs)
        logger.info(f"有效关注画师数: {len(all_subs)} (API获取: {len(following_ids)}, 手动: {len(manual_subs)})")

        # ContentFetcher: 搜索/排行榜用 client，订阅检查用 sync_client
        fetcher = ContentFetcher(
            client=client,
            sync_client=sync_client,  # 新增：同步客户端
            bookmark_threshold=fetcher_cfg.get("bookmark_threshold", {"search": 1000, "subscription": 0}),
            date_range_days=fetcher_cfg.get("date_range_days", 7),
            subscribed_artists=list(manual_subs),
            discovery_rate=profiler_cfg.get("discovery_rate", 0.1),
            ranking_config=fetcher_cfg.get("ranking"),
            dynamic_threshold_config=fetcher_cfg.get("dynamic_threshold"),  # 动态阈值配置
            search_limit=fetcher_cfg.get("search_limit", 50)  # 搜索数量限制 (默认50)
        )

        # 执行 Discovery (Search + Ranking + Subs)
        top_tags = await profiler.get_top_tags(profiler_cfg.get("top_n", 20)) # Re-get is cheap

        # 执行 Discovery (Search + Ranking + Subs) -> MAB Scheduled
        top_tags = await profiler.get_top_tags(profiler_cfg.get("top_n", 20)) # Re-get is cheap

        all_illusts = await fetcher.fetch_content(
             xp_tags=top_tags,
             total_limit=fetcher_cfg.get("discovery_limit", 200)
        )
        logger.info(f"共获取 {len(all_illusts)} 个候选作品")

        # 3. 过滤
        filter_cfg = config.get("filter", {})
        match_cfg = fetcher_cfg.get("match_score", {})

        # 初始化可选的 Embedder (AI 语义匹配)
        embedder = None
        ai_cfg = config.get("ai", {})
        embedding_cfg = ai_cfg.get("embedding", {})
        if embedding_cfg.get("enabled", False):
            try:
                from embedder import Embedder
                embedder = Embedder(embedding_cfg)
                if embedder.enabled:
                    logger.info(f"已启用 AI 语义匹配 (model={embedder.model})")
            except Exception as e:
                logger.warning(f"Embedder 初始化失败: {e}")

        # 初始化可选的 AIScorer (LLM 精排)
        ai_scorer = None
        scorer_cfg = ai_cfg.get("scorer", {})
        if scorer_cfg.get("enabled", False):
            try:
                from ai_scorer import AIScorer

                # 支持复用 profiler.ai 的 API 配置
                if scorer_cfg.get("use_profiler_api", True):
                    profiler_ai_cfg = config.get("profiler", {}).get("ai", {})
                    # 合并配置：scorer 优先，缺失的从 profiler.ai 继承
                    merged_cfg = {
                        "enabled": scorer_cfg.get("enabled", False),
                        "provider": scorer_cfg.get("provider") or profiler_ai_cfg.get("provider", "openai"),
                        "api_key": scorer_cfg.get("api_key") or profiler_ai_cfg.get("api_key", ""),
                        "base_url": scorer_cfg.get("base_url") or profiler_ai_cfg.get("base_url", ""),
                        "model": scorer_cfg.get("model") or profiler_ai_cfg.get("model", "gpt-4o-mini"),
                        "max_candidates": scorer_cfg.get("max_candidates", 50),
                        "score_weight": scorer_cfg.get("score_weight", 0.3),
                    }
                    ai_scorer = AIScorer(merged_cfg)
                else:
                    ai_scorer = AIScorer(scorer_cfg)

                if ai_scorer.enabled:
                    logger.info(f"已启用 AI 精排评分 (model={ai_scorer.model})")
            except Exception as e:
                logger.warning(f"AIScorer 初始化失败: {e}")

        content_filter = ContentFilter(
            blacklist_tags=filter_cfg.get("blacklist_tags"),
            daily_limit=filter_cfg.get("daily_limit", 20),
            exclude_ai=filter_cfg.get("exclude_ai", True),
            min_match_score=match_cfg.get("min_threshold", 0.0),
            match_weight=match_cfg.get("weight_in_sort", 0.5),
            max_per_artist=filter_cfg.get("max_per_artist", 3),
            subscribed_artists=all_subs,
            artist_boost=filter_cfg.get("artist_boost", 0.3),
            min_create_days=filter_cfg.get("min_create_days", 0),
            r18_mode=filter_cfg.get("r18_mode", False),
            allowed_types=filter_cfg.get("allowed_types", ["illust"]),
            # 新增：借鉴 X 算法的增强选项
            author_diversity=filter_cfg.get("author_diversity"),
            source_boost=filter_cfg.get("source_boost"),
            embedder=embedder,  # 可选的语义匹配
            ai_scorer=ai_scorer,  # 可选的 LLM 精排
            # 多样性增强
            shuffle_factor=filter_cfg.get("shuffle_factor", 0.0),
            exploration_ratio=filter_cfg.get("exploration_ratio", 0.0)
        )

        pixiv_uid = config.get("pixiv", {}).get("user_id", 0)
        filtered = await content_filter.filter(all_illusts, xp_profile=xp_profile, user_id=pixiv_uid)
        logger.info(f"过滤后 {len(filtered)} 个作品")

        # 4. 推送
        if notifiers and filtered:
            try:
                # 缓存作品信息 (包含来源归因)
                for illust in filtered:
                    await cache_illust(illust.id, illust.tags, illust.user_id, illust.user_name, source=illust.source)

                all_sent_ids = set()
                for notifier in notifiers:
                    try:
                        sent_ids = await notifier.send(filtered)
                        all_sent_ids.update(sent_ids)
                    except Exception as e:
                        logger.error(f"推送器 {type(notifier).__name__} 发送失败: {e}")

                if all_sent_ids:
                    # 记录推送历史
                    filtered_map = {ill.id: ill for ill in filtered}
                    for pid in all_sent_ids:

                        if pid in filtered_map:
                            illust = filtered_map[pid]
                            source = getattr(illust, 'source', 'unknown')
                            await mark_pushed(pid, source)

                            # 更新 MAB 策略统计 (Total Count)
                            if source in ['xp_search', 'subscription', 'ranking', 'related', 'engagement_artists']:
                                await db_module.update_strategy_stats(source, is_success=False)

                    # 将消息 ID 写入数据库缓存（用于连锁推送引用）
                    for notifier in notifiers:
                        if hasattr(notifier, '_message_illust_map'):
                            for msg_id, illust_id in notifier._message_illust_map.items():
                                if illust_id in all_sent_ids:
                                    await db_module.set_chain_meta(illust_id, chain_depth=0, chain_msg_id=msg_id)

                    logger.info(f"推送完成: {len(all_sent_ids)}/{len(filtered)} 个作品成功")
                else:
                    logger.error("没有任何作品被成功推送")

                # 5. AI 错误报警
                ai_errors = profiler.ai_processor.occurred_errors
                if ai_errors:
                    err_count = len(ai_errors)
                    err_id = ai_errors[0]
                    msg = f"⚠️ 警告：本次任务有 {err_count} 批 Tag AI 优化失败。\n已自动记录并降级处理。"
                    buttons = [("🔄 重试修复", f"retry_ai:{err_id}")]
                    logger.warning(f"AI 优化失败 {err_count} 次，发送警告")

                    for notifier in notifiers:
                        if hasattr(notifier, 'send_text'):
                            try:
                                await notifier.send_text(msg, buttons)
                            except:
                                pass
            except Exception as e:
                logger.error(f"推送过程出错: {e}")
        elif not filtered:
             logger.info("无新作品可推送")
        else:
            logger.warning("未配置推送器")

    except Exception as e:
        logger.error(f"任务执行出错: {e}", exc_info=True)

    logger.info("=== 推送任务结束 ===")


async def run_once(config: dict):
    """立即执行一次"""
    main_client, sync_client, profiler, notifiers = await setup_services(config)

    # 即使是 Run Once，如果用于测试，可能也需要 Feedback?
    # 但 cli --once 通常是脚本调用，跑完即走。
    # 这里我们还是启动监听 (如果是 Test 模式也许不需要?)
    # 如果是 --test, 我们不启动监听?
    # 如果用户想测试反馈，OneBot/TG 需要跑。
    # 但 script ends immediately. Feedback needs loop.
    # 所以 --once 真的就是 "Fire and Forget".

    try:
        await main_task(config, main_client, profiler, notifiers, sync_client)
    finally:
        await main_client.close()
        # 如果 sync_client 是独立实例，也需要关闭
        if sync_client is not main_client:
            await sync_client.close()
        for n in (notifiers or []):
            if hasattr(n, 'close'):
                try:
                    await n.close()
                except:
                    pass

async def daily_report_task(config: dict, notifiers: list, profiler=None):
    """每日维护任务：生成日报 + 数据清理 + AI 标签刷新

    设计原则：
    - 每个步骤独立 try/except，即使某一步失败，其他步骤仍可继续
    - 网络相关操作（AI、发送）使用 retry_async 自动重试
    """
    logger.info("📊 开始执行每日维护任务...")

    maintenance_summary = []
    lines = ["📊 **每日 XP 日报**\n"]

    # ========== 1. 生成日报 (Top Tags + MAB Stats) ==========
    try:
        from database import get_top_xp_tags, get_all_strategy_stats

        top_tags = await get_top_xp_tags(10)
        stats = await get_all_strategy_stats()

        if top_tags:
            lines.append("🎯 **Top 10 XP 标签**")
            for i, (tag, weight) in enumerate(top_tags[:10], 1):
                lines.append(f"  {i}. `{tag}` ({weight:.1f})")
            lines.append("")

        if stats:
            lines.append("📈 **MAB 策略表现**")
            strategy_names = {"search": "XP搜索", "xp_search": "XP搜索", "subscription": "订阅", "ranking": "排行榜"}
            for strategy, data in stats.items():
                name = strategy_names.get(strategy, strategy)
                rate_pct = data["rate"] * 100
                lines.append(f"  • {name}: {data['success']}/{data['total']} ({rate_pct:.1f}%)")
    except Exception as e:
        logger.error(f"生成日报统计失败: {e}")
        maintenance_summary.append(f"⚠️ 日报统计失败: {e}")

    # ========== 2. 同步屏蔽标签到 XP 画像 ==========
    try:
        from database import sync_blocked_tags_to_xp
        blocked_removed = await sync_blocked_tags_to_xp()
        if blocked_removed > 0:
            maintenance_summary.append(f"🚫 从画像中移除 {blocked_removed} 个已屏蔽标签")
            logger.info(f"已从 XP 画像中移除 {blocked_removed} 个屏蔽标签")
    except Exception as e:
        logger.error(f"同步屏蔽标签失败: {e}")
        maintenance_summary.append(f"⚠️ 同步屏蔽标签失败: {e}")

    # ========== 3. AI 标签增量处理 (带重试) ==========
    if profiler and hasattr(profiler, 'ai_processor') and profiler.ai_processor.enabled:
        try:
            from database import get_uncached_tags
            uncached_tags = await get_uncached_tags(limit=200)
            if uncached_tags:
                logger.info(f"发现 {len(uncached_tags)} 个未处理标签，启动 AI 清洗...")

                async def _ai_process():
                    return await profiler.ai_processor.process_tags(uncached_tags)

                result = await retry_async(_ai_process, max_retries=3, delay=10.0)
                if result:
                    valid_tags, mapping = result
                    maintenance_summary.append(f"🤖 AI 清洗 {len(uncached_tags)} 个标签 → {len(valid_tags)} 个有效")
                    logger.info(f"AI 清洗完成: {len(valid_tags)}/{len(uncached_tags)} 有效")
                else:
                    maintenance_summary.append(f"⚠️ AI 清洗失败 (已重试)")
        except Exception as e:
            logger.error(f"AI 清洗失败: {e}")
            maintenance_summary.append(f"⚠️ AI 清洗失败: {e}")

    # ========== 4. 清理旧推送历史 ==========
    try:
        from database import cleanup_old_sent_history
        old_removed = await cleanup_old_sent_history(days=30)
        if old_removed > 0:
            maintenance_summary.append(f"🗑️ 清理 {old_removed} 条过期推送记录")
            logger.info(f"已清理 {old_removed} 条 30 天前的推送历史")
    except Exception as e:
        logger.error(f"清理推送历史失败: {e}")
        maintenance_summary.append(f"⚠️ 清理推送历史失败: {e}")

    # ========== 5. 清理旧作品缓存 ==========
    try:
        from database import cleanup_old_illust_cache
        cache_removed = await cleanup_old_illust_cache(days=60)
        if cache_removed > 0:
            maintenance_summary.append(f"🗑️ 清理 {cache_removed} 条过期作品缓存")
            logger.info(f"已清理 {cache_removed} 条 60 天前的作品缓存")
    except Exception as e:
        logger.error(f"清理作品缓存失败: {e}")
        maintenance_summary.append(f"⚠️ 清理作品缓存失败: {e}")

    # ========== 6. 添加维护摘要到日报 ==========
    if maintenance_summary:
        lines.append("")
        lines.append("🛠️ **维护记录**")
        for item in maintenance_summary:
            lines.append(f"  {item}")

    report_msg = "\n".join(lines)

    # ========== 7. 发送日报 (带重试) ==========
    async def _send_report():
        for n in notifiers:
            if hasattr(n, 'send_text'):
                await n.send_text(report_msg)
                return True
        return False

    result = await retry_async(_send_report, max_retries=5, delay=30.0, backoff=2.0)
    if not result:
        logger.error("发送日报最终失败")

    logger.info("✅ 每日维护任务完成")


async def run_scheduler(config: dict, run_immediately: bool = False):
    """启动调度器 (Daemon Mode)"""
    main_client, sync_client, profiler, notifiers = await setup_services(config)

    # Start Listeners (Background)
    if notifiers:
        for n in notifiers:
            if isinstance(n, TelegramNotifier):
                 # TelegramNotifier.start_polling is async but handles its own background tasks (updater.start_polling)
                 await n.start_polling()
            elif isinstance(n, OneBotNotifier):
                 # OneBot loop needs to be scheduled
                 asyncio.create_task(n.start_listening())

    if run_immediately:
        logger.info("🚀 正在立即执行首次任务...")
        # Run main_task as a background task so it doesn't block scheduler start?
        # Or await it? Since it's "Now", usually await is fine, or create task to allow listener to process concurrently?
        # If we await, listener logic (OneBot) runs in background task ok.
        # BUT if main_task crashes, we still want scheduler.
        asyncio.create_task(main_task(config, main_client, profiler, notifiers, sync_client))

    scheduler = AsyncIOScheduler()
    scheduler_cfg = config.get("scheduler", {})
    coalesce = scheduler_cfg.get("coalesce", True)

    # 获取调度配置 (优先读取数据库)
    from database import get_state
    db_cron = await get_state("schedule_cron")
    config_cron = config.get("scheduler", {}).get("cron", "0 20 * * *")

    schedule_str = db_cron if db_cron else config_cron

    # 将 scheduler 注入到 config 中以便 callback 访问
    config['scheduler'] = scheduler

    # 支持多个时间点
    # 逻辑优化：
    # 1. 先尝试将整个字符串作为一个 Cron，如果成功则认为是一个任务 (解决 "0 12,21 * * *" 被误拆的问题)
    # 2. 如果失败，再尝试用逗号分割 (兼容旧的多任务写法 "0 12 * * *, 0 21 * * *")

    cron_list = []

    # 尝试解析整体
    try:
        CronTrigger.from_crontab(schedule_str.strip())
        cron_list = [schedule_str.strip()]
        logger.info(f"识别为单一定时任务: {schedule_str}")
    except ValueError:
        # 整体解析失败，尝试分割
        potential_crons = [c.strip() for c in schedule_str.split(",") if c.strip()]
        valid_crons = []
        for c in potential_crons:
            try:
                CronTrigger.from_crontab(c)
                valid_crons.append(c)
            except ValueError:
                logger.warning(f"忽略无效的 Cron 表达式片段: {c}")

        if valid_crons:
            cron_list = valid_crons
            logger.info(f"识别为 {len(cron_list)} 个独立定时任务")
        else:
            # 如果分割也全错，那可能就是整体写错了，保留整体让后面报错
            cron_list = [schedule_str]

    for i, cron_expr in enumerate(cron_list):
        try:
            scheduler.add_job(
                main_task,
                CronTrigger.from_crontab(cron_expr),
                args=[config, main_client, profiler, notifiers, sync_client],
                id=f'push_job_{i}',
                coalesce=coalesce,
                misfire_grace_time=3600
            )
            logger.info(f"已添加定时任务 #{i+1}: {cron_expr}")
        except Exception as e:
            logger.error(f"添加定时任务失败 ({cron_expr}): {e}")

    # 每日维护任务 (日报 + 清理)
    daily_cron = scheduler_cfg.get("daily_report_cron", "0 0 * * *")  # 默认每天00:00
    try:
        scheduler.add_job(
            daily_report_task,
            CronTrigger.from_crontab(daily_cron),
            args=[config, notifiers, profiler],  # 传入 profiler 以支持 AI 清洗
            id='daily_report_job',
            coalesce=True,
            misfire_grace_time=3600
        )
        logger.info(f"已添加每日维护任务: {daily_cron}")
    except Exception as e:
        logger.error(f"添加每日维护任务失败: {e}")

    scheduler.start()
    logger.info(f"调度器已启动，共 {len(cron_list)} 个推送任务 + 1 个每日维护任务")

    try:
        while True:
            await asyncio.sleep(1800)  # 每 30 分钟检查一次

            # Telegram 连接健康检查
            for n in notifiers:
                if isinstance(n, TelegramNotifier):
                    need_restart = False

                    # 检查 _app 是否存在且 updater 是否在运行
                    if not n._app or not n._app.updater or not n._app.updater.running:
                        logger.warning("Telegram updater 未运行，需要重启...")
                        need_restart = True
                    else:
                        # updater 在运行，检查实际连接
                        try:
                            await n._app.bot.get_me()
                            logger.debug("Telegram 连接健康检查通过")
                        except Exception as e:
                            logger.warning(f"Telegram 健康检查失败: {e}，需要重启轮询...")
                            need_restart = True

                    if need_restart:
                        try:
                            await n.stop_polling()
                            await asyncio.sleep(5)
                            await n.start_polling()
                            logger.info("✅ Telegram 轮询已重启")
                        except Exception as restart_err:
                            logger.error(f"重启 Telegram 轮询失败: {restart_err}")
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
    finally:
        await main_client.close()
        # 如果 sync_client 是独立实例，也需要关闭
        if sync_client is not main_client:
            await sync_client.close()
        for n in (notifiers or []):
            if hasattr(n, 'close'):
                try:
                    await n.close()
                except:
                    pass


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(description="Pixiv-XP-Pusher")
    parser.add_argument("--once", action="store_true", help="立即执行一次并退出")
    parser.add_argument("--now", action="store_true", help="启动时立即执行一次，然后保持后台运行（调度模式）")
    parser.add_argument("--reset-xp", action="store_true", help="重置 XP 数据")
    parser.add_argument("--test", action="store_true", help="快速测试模式")
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH), help="配置文件路径")
    args = parser.parse_args()

    setup_logging()

    if args.reset_xp:
        from database import reset_xp_data, init_db
        logger.info("正在清除 XP 数据...")
        asyncio.run(init_db())
        asyncio.run(reset_xp_data())
        logger.info("✅ XP 数据已清除。")
        return

    config = load_config()

    # 测试模式 override
    if args.test:
        logger.info("🔧 启用测试模式：参数最小化")
        config.setdefault("profiler", {})["scan_limit"] = 10
        config["profiler"]["discovery_rate"] = 0
        config.setdefault("fetcher", {})["bookmark_threshold"] = {"search": 0, "subscription": 0}
        config["fetcher"]["discovery_limit"] = 1
        config["fetcher"]["ranking"] = {"modes": ["day"], "limit": 1}
        # Force once for test
        args.once = True

    if args.once:
        asyncio.run(run_once(config))
    else:
        # If --now is set, run_scheduler will handle immediate run
        asyncio.run(run_scheduler(config, run_immediately=args.now))


if __name__ == "__main__":
    main()
