"""
内容获取模块
双策略：XP搜索 + 画师订阅 + 排行榜
"""
import logging
import random
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from pixiv_client import Illust, PixivClient
import database as db
from utils import expand_search_query

logger = logging.getLogger(__name__)


class ContentFetcher:
    """内容获取器"""
    
    def __init__(
        self,
        client: PixivClient,
        bookmark_threshold: dict[str, int] = None,
        date_range_days: int = 7,
        subscribed_artists: Optional[list[int]] = None,
        discovery_rate: float = 0.1,
        ranking_config: Optional[dict] = None,
        mab_limits: Optional[dict] = None,
        sync_client: PixivClient = None,
        dynamic_threshold_config: Optional[dict] = None,  # 动态阈值配置
        search_limit: int = 50  # 默认搜索数量
    ):
        self.client = client  # 主客户端 (搜索、排行榜)
        self.sync_client = sync_client or client  # 同步客户端 (订阅、关注)
        self.bookmark_threshold = bookmark_threshold or {"search": 1000, "subscription": 0}
        self.date_range_days = date_range_days
        self.subscribed_artists = subscribed_artists or []
        self.discovery_rate = discovery_rate
        self.mab_limits = mab_limits or {"min_quota": 0.2, "max_quota": 0.6}
        
        # 排行榜配置
        self.ranking_config = ranking_config or {}
        self.ranking_enabled = self.ranking_config.get("enabled", False)
        self.ranking_modes = self.ranking_config.get("modes", ["day"])
        self.ranking_limit = self.ranking_config.get("limit", 100)
        
        # 动态阈值配置 (冷门标签保底)
        dt_cfg = dynamic_threshold_config or {}
        dt_cfg = dynamic_threshold_config or {}
        self.dynamic_threshold_min = dt_cfg.get("min", 100)
        self.dynamic_threshold_rate = dt_cfg.get("rate", 0.05)
        
        # 搜索数量限制
        self.search_limit = search_limit

        # 缓存 Tag 的最高热度，避免重复查询 (Session Valid)
        self._search_max_bookmarks_cache = {}
    
    def _adaptive_threshold(self, base: int, tag_weight: float, is_combination: bool = False) -> int:
        """
        自适应收藏阈值
        
        根据 Tag 权重动态调整阈值：
        - 高权重 Tag（用户热爱）：保持高阈值，确保质量
        - 低权重 Tag（尝试发现）：降低阈值，扩大搜索范围
        - 组合搜索：额外降低阈值（更精准匹配）
        """
        multiplier = max(0.3, tag_weight)
        
        if is_combination:
            multiplier *= 0.5
        
        return max(100, int(base * multiplier))
    
    async def discover(
        self,
        xp_tags: list[tuple[str, float]],
        limit: int = 50
    ) -> list[Illust]:
        """
        策略A：基于XP的广泛搜索 (并发优化版)
        """
        if not xp_tags:
            logger.warning("无XP标签，跳过搜索")
            return []
        
        all_illusts = []
        
        # 获取高权重组合 (Smart Search)
        top_pairs = await db.get_top_tag_pairs(limit=50)
        used_tags = set()
        
        tasks = []
        
        # 1. 构建组合搜索任务
        max_combo_tasks = 20 # 限制并发数
        combo_count = 0
        
        for t1, t2, _ in top_pairs:
            if combo_count >= max_combo_tasks:
                break
            # 简化：如果不通过 limit 控制，而是全量并发，可能会太多
            # 我们先收集任务，回头再看是否需要分批
            
            pair_key = tuple(sorted([t1, t2]))
            if pair_key in used_tags:
                continue
            used_tags.add(pair_key)
            
            q1 = expand_search_query(t1)
            q2 = expand_search_query(t2)
            
            if q1 == q2 or t1 in q2 or t2 in q1:
                continue
            
            combo_count += 1
            
            # 使用闭包或独立方法来封装单个搜索逻辑以便并发
            tasks.append(self._search_pair(t1, t2))

        # 执行组合搜索
        # 为了避免瞬间过高并发，我们可以切分 tasks
        # 但 PixivClient 内部有 RateLimiter，所以 gather all 也是安全的，只是会排队。
        # 不过为了更早拿到结果并判断数量，我们还是分批好？
        # 全量 gather 也行，代码最简单。
        
        if tasks:
            logger.info(f"启动 {len(tasks)} 个组合搜索任务...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, list):
                    all_illusts.extend(res)
                elif isinstance(res, Exception):
                    logger.error(f"组合搜索任务异常: {res}")

        # 检查数量是否达标
        remaining = limit - len(all_illusts)
        
        # 2. 如果不够，补充单 Tag 搜索
        if remaining > 0:
            fallback_tasks = []
            # 尝试多次采样补充
            for _ in range(3):
                tags_to_search = self._weighted_sample(xp_tags, k=1)
                if not tags_to_search: continue
                
                tag = tags_to_search[0]
                if tag in [t for pair in used_tags for t in pair]:
                    continue
                
                fallback_tasks.append(self._search_single(tag, remaining // 2))
                
            if fallback_tasks:
                logger.info(f"启动 {len(fallback_tasks)} 个单Tag补充任务...")
                res_list = await asyncio.gather(*fallback_tasks, return_exceptions=True)
                for res in res_list:
                    if isinstance(res, list):
                        all_illusts.extend(res)

        # 去重
        MAX_PER_ARTIST = 3
        artist_counts = {}
        filtered_illusts = []
        
        for illust in all_illusts:
            artist_id = illust.user_id
            if artist_counts.get(artist_id, 0) < MAX_PER_ARTIST:
                filtered_illusts.append(illust)
                artist_counts[artist_id] = artist_counts.get(artist_id, 0) + 1
        
        logger.info(f"XP搜索获取 {len(filtered_illusts)} 个作品 (原始 {len(all_illusts)})")
        return filtered_illusts[:limit]

    async def _search_pair(self, t1: str, t2: str) -> list[Illust]:
        """单个组合搜索任务"""
        base_threshold = self.bookmark_threshold["search"]
        
        # 并发获取动态阈值
        t1_thresh, t2_thresh = await asyncio.gather(
            self._get_dynamic_threshold(t1, base_threshold),
            self._get_dynamic_threshold(t2, base_threshold)
        )
        
        threshold = int(min(t1_thresh, t2_thresh) * 0.3)  # 组合搜索降低阈值(0.3)，增加命中率
        
        # 获取搜索词
        raw_t1 = await db.get_best_search_tag(t1)
        raw_t2 = await db.get_best_search_tag(t2)
        
        final_q1 = self._build_query(t1, raw_t1)
        final_q2 = self._build_query(t2, raw_t2)
        
        return await self.client.search_illusts(
            tags=[final_q1, final_q2],
            bookmark_threshold=threshold,
            date_range_days=self.date_range_days,
            limit=self.search_limit
        )

    async def _search_single(self, tag: str, limit: int) -> list[Illust]:
        """单个Tag搜索任务"""
        dynamic = await self._get_dynamic_threshold(tag, self.bookmark_threshold["search"])
        # 如果是单Tag搜索，也给与一定折扣(0.5)，防止动态阈值过高
        threshold = int(min(dynamic, self.bookmark_threshold["search"] * 0.5))
        
        raw_tag = await db.get_best_search_tag(tag)
        final_q = self._build_query(tag, raw_tag)
        
        return await self.client.search_illusts(
            tags=[final_q],
            bookmark_threshold=threshold,
            date_range_days=self.date_range_days,
            limit=limit
        )

    def _build_query(self, tag: str, raw_tag: str) -> str:
        base_q = expand_search_query(tag)
        if raw_tag != tag and raw_tag not in base_q:
            if "(" in base_q:
                return base_q[:-1] + f" OR {raw_tag})"
            else:
                return f"({base_q} OR {raw_tag})"
        return base_q

    
    async def check_subscriptions(self) -> list[Illust]:
        """
        策略B：检查订阅画师更新 + 关注者新作
        使用 sync_client 进行低风险操作
        """
        all_illusts = []
        seen_ids = set()
        
        # 1. 获取关注者时间轴 (高效) - 使用 sync_client
        try:
            feed_illusts = await self.sync_client.fetch_follow_latest(limit=100)
            for illust in feed_illusts:
                if illust.id not in seen_ids:
                    all_illusts.append(illust)
                    seen_ids.add(illust.id)
        except Exception as e:
            logger.error(f"获取关注时间轴失败: {e}")
            
        # 2. 检查配置中的特定订阅 (补充) - 使用 sync_client
        # 如果订阅列表只有几个，检查一下也无妨；如果是空的则跳过
        if self.subscribed_artists:
            since = datetime.now().astimezone() - timedelta(days=self.date_range_days)
            for artist_id in self.subscribed_artists:
                # 如果刚才的 feed 里已经有了很多该画师的图，或许可以跳过？
                # 简单起见，还是查一下，但限制数量
                try:
                    illusts = await self.sync_client.get_user_illusts(
                        user_id=artist_id,
                        since=since,
                        limit=5
                    )
                    for illust in illusts:
                        if illust.id not in seen_ids:
                            all_illusts.append(illust)
                            seen_ids.add(illust.id)
                except Exception as e:
                    logger.error(f"获取画师 {artist_id} 作品失败: {e}")
        
        logger.info(f"订阅/关注更新获取 {len(all_illusts)} 个作品")
        return all_illusts
    
    async def fetch_ranking(self) -> list[Illust]:
        """
        策略C：排行榜抓取
        
        Returns:
            排行榜作品列表
        """
        if not self.ranking_enabled:
            logger.debug("排行榜功能未启用")
            return []
        
        all_illusts = []
        
        for mode in self.ranking_modes:
            try:
                illusts = await self.client.get_ranking(
                    mode=mode,
                    limit=self.ranking_limit // len(self.ranking_modes)
                )
                all_illusts.extend(illusts)
                logger.info(f"排行榜 [{mode}] 获取 {len(illusts)} 个作品")
            except Exception as e:
                logger.error(f"获取 {mode} 排行榜失败: {e}")
        
        logger.info(f"排行榜总计获取 {len(all_illusts)} 个作品")
        return all_illusts
    
    def _weighted_sample(
        self,
        weighted_tags: list[tuple[str, float]],
        k: int
    ) -> list[str]:
        """根据权重随机采样Tag"""
        if len(weighted_tags) <= k:
            return [t[0] for t in weighted_tags]
        
        tags = [t[0] for t in weighted_tags]
        weights = [t[1] for t in weighted_tags]
        
        # 使用权重作为选择概率
        total = sum(weights)
        probs = [w / total for w in weights]
        
        selected = []
        available = list(range(len(tags)))
        
        for _ in range(k):
            if not available:
                break
            
            r = random.random()
            cumsum = 0
            for i in available:
                cumsum += probs[i]
                if r <= cumsum:
                    selected.append(tags[i])
                    available.remove(i)
                    break
        
        return selected
    
    async def _get_dynamic_threshold(self, tag: str, base: int) -> int:
        """
        基于热度的动态阈值 (Dynamic Threshold) - Cached
        """
        # 1. Check Cache
        if tag in self._search_max_bookmarks_cache:
            max_bookmarks = self._search_max_bookmarks_cache[tag]
            # logger.debug(f"动态阈值 Cache Hit: {tag} -> {max_bookmarks}")
        else:
            try:
                # 搜索该 Tag 按收藏数降序，获取第一张作为参考
                top_illusts = await self.client.search_illusts(
                    tags=[tag], 
                    limit=1,
                    # search_illusts 内部默认是 popular_desc，所以取第1个就是 Max Bookmarks 左右
                )
                if top_illusts:
                    max_bookmarks = top_illusts[0].bookmark_count
                else:
                    max_bookmarks = 1000  # Fallback
            except Exception as e:
                logger.debug(f"获取 Tag '{tag}' 热度失败: {e}")
                max_bookmarks = 1000  # Fallback
            
            # Save Cache
            self._search_max_bookmarks_cache[tag] = max_bookmarks

        # 相对阈值：使用配置的比例和保底值
        relative_threshold = max(self.dynamic_threshold_min, int(max_bookmarks * self.dynamic_threshold_rate))
        
        final_threshold = min(base, relative_threshold)
        # logger.debug(f"动态阈值 '{tag}': Max={max_bookmarks} -> Threshold={final_threshold} (Base={base})")
        
        return final_threshold

    async def select_strategies(self, total_limit: int) -> dict[str, int]:
        """
        MAB 策略调度 (Thompson Sampling)
        
        基于历史反馈动态分配配额：
        - XP搜索 (search)
        - 订阅 (subscription)
        - 排行榜 (ranking)
        """
        strategies = ['xp_search', 'subscription', 'ranking']
        scores = {}
        
        for strategy in strategies:
            s, total = await db.get_strategy_stats(strategy)
            f = total - s
            # Beta分布采样 (alpha=s+1, beta=f+1)
            # 给予一定的先验信心 (alpha+=2, beta+=2) 避免初期剧烈波动
            scores[strategy] = random.betavariate(s + 2, f + 2)
            
        total_score = sum(scores.values())
        ratios = {k: v/total_score for k, v in scores.items()}
        
        # 应用配额限制 (Quota Limits)
        min_q = self.mab_limits.get("min_quota", 0.1)
        max_q = self.mab_limits.get("max_quota", 0.8)
        
        final_quotas = {}
        remaining = total_limit
        
        # 1. 先分配最小保底
        for s in ratios:
            min_count = int(total_limit * min_q)
            final_quotas[s] = min_count
            remaining -= min_count
            
        # 2. 剩下的按比例分配，但不超过 max
        if remaining > 0:
            for s, r in ratios.items():
                if remaining <= 0: break
                
                # 当前已分配
                current = final_quotas[s]
                # 最大允许
                max_allowed = int(total_limit * max_q)
                
                # 目标分配 (按比例应得的总数)
                target = int(total_limit * r)
                
                # 还可以加多少
                can_add = max(0, max_allowed - current)
                needed = max(0, target - current)
                
                actual_add = min(needed, can_add, remaining)
                final_quotas[s] += actual_add
                remaining -= actual_add
                
        # 3. 如果还有剩余 (由于 max 限制导致未分完)，均分给未达上限的
        if remaining > 0:
             # 为了简单，随机分给没满的，或者直接分给第一名
             # 这里简单平铺
             for s in strategies:
                 if remaining <= 0: break
                 max_allowed = int(total_limit * max_q)
                 if final_quotas[s] < max_allowed:
                     add = 1
                     final_quotas[s] += add
                     remaining -= add
        
        logger.info(f"MAB 分配: {final_quotas} (Scores: { {k: f'{v:.2f}' for k, v in scores.items()} })")
        return final_quotas

    async def discover_related(self, xp_tags: list[tuple[str, float]], limit: int = 50) -> list[Illust]:
        """
        策略D：基于关联作品 (Related Works) 的发现
        
        1. 从高权重 XP Tag 中随机选一个 "Seed Tag"
        2. 从数据库中找出包含该 Tag 的高分收藏作为 "Seed Illust"
        3. 调用 API 获取 Related Works
        4. 结合 XP Tag 和 Artist Profile 进行二次筛选
        """
        if not xp_tags:
            return []
            
        # 1. 选取 Seed (加权随机)
        # 取 Top 20 Tags
        top_tags = xp_tags[:20]
        if not top_tags:
            return []
            
        tags, weights = zip(*top_tags)
        seed_tag = random.choices(tags, weights=weights, k=1)[0]
        
        # 2. 找 Seed Illust
        # 这里需要一个 DB 方法：get_top_illusts_by_tag(tag, limit=10)
        # 暂时用 get_xp_bookmarks 替代，然后在内存筛选
        # 优化：为了性能，我们可以随机选一个 Recently Liked Illust
        try:
            liked_ids = await db.get_liked_illusts()
            if not liked_ids:
                return []
            
            seed_illust_id = random.choice(list(liked_ids))
            logger.info(f"关联策略: 选中种子作品 {seed_illust_id} (Tag: {seed_tag})")
        except Exception as e:
            logger.warning(f"关联策略选种失败: {e}")
            return []
            
        # 3. Fetch Related
        try:
            raw_related = await self.client.get_related_illusts(seed_illust_id, limit=limit * 2) # 多抓点备选
        except Exception as e:
            logger.error(f"获取关联作品失败: {e}")
            return []
            
        # 4. Filter & Score
        scored_candidates = []
        xp_dict = dict(xp_tags)
        
        for illust in raw_related:
            # 基础分
            score = 0.0
            
            # Tag 分
            for tag in illust.tags:
                norm = tag.lower().replace(" ", "_") # 简单归一化
                if norm in xp_dict:
                    score += xp_dict[norm]
            
            # 画师分 (Artist Boost)
            artist_score = await db.get_artist_score(illust.user.id)
            score += artist_score
            
            scored_candidates.append((illust, score))
            
        # 按分数排序
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        
        # 返回 Top N
        details = [f"{ill.id}({sc:.1f})" for ill, sc in scored_candidates[:5]]
        logger.info(f"关联推荐结果: {details}...")
        
        return [x[0] for x in scored_candidates[:limit]]



    async def fetch_content(self, xp_tags: list[tuple[str, float]], total_limit: int = 200) -> list[Illust]:
        """
        统一内容获取入口 (MAB 调度)
        """
        quotas = await self.select_strategies(total_limit)
        
        tasks = []
        
        # 1. XP 搜索
        if quotas['xp_search'] > 0:
            tasks.append(self.discover(xp_tags, limit=quotas['xp_search']))
            
        # 2. 订阅
        tasks.append(self.check_subscriptions())
        
        # 3. 排行榜
        if quotas['ranking'] > 0 and self.ranking_enabled:
            tasks.append(self.fetch_ranking_with_limit(quotas['ranking']))
        else:
            tasks.append(asyncio.sleep(0))
            
        # 4. 关联推荐 (New)
        if quotas.get('related', 0) > 0:
            tasks.append(self.discover_related(xp_tags, limit=quotas['related']))
        else:
            tasks.append(asyncio.sleep(0)) # Placeholder
            
        # 执行获取
        results = await asyncio.gather(*tasks)
        
        # 展平并按顺序解析结果
        idx = 0
        search_res = []
        if quotas['xp_search'] > 0:
            search_res = results[idx] if isinstance(results[idx], list) else []
            idx += 1
            
        sub_res = results[idx] if isinstance(results[idx], list) else []
        idx += 1
        
        rank_res = results[idx] if isinstance(results[idx], list) else []
        idx += 1
        
        related_res = results[idx] if isinstance(results[idx], list) else []
        
        # 记录来源 (Strategy Attribution)
        for ill in search_res: ill.source = "xp_search"
        for ill in sub_res: ill.source = "subscription"
        for ill in rank_res: ill.source = "ranking"
        for ill in related_res: ill.source = "related"
        
        # 合并并去重
        all_illusts = search_res + sub_res + rank_res + related_res
        
        logger.info(f"策略获取汇总: Search={len(search_res)}, Sub={len(sub_res)}, Rank={len(rank_res)}, Related={len(related_res)}")
        
        return all_illusts


    async def fetch_ranking_with_limit(self, limit: int) -> list[Illust]:
        """支持自定义 Limit 的排行榜抓取"""
        if not self.ranking_enabled or limit <= 0:
            return []
            
        all_illusts = []
        for mode in self.ranking_modes:
            try:
                # 平均分配 limit
                mode_limit = max(1, limit // len(self.ranking_modes))
                illusts = await self.client.get_ranking(mode=mode, limit=mode_limit)
                all_illusts.extend(illusts)
            except Exception as e:
                logger.error(f"获取 {mode} 排行榜失败: {e}")
        return all_illusts

