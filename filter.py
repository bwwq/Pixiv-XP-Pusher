"""
内容过滤模块
去重、黑名单、质量过滤、匹配度评分
"""
import logging
from typing import Optional

from pixiv_client import Illust
import database as db

logger = logging.getLogger(__name__)


def calculate_match_score(
    illust: Illust, 
    xp_profile: dict[str, float],
    negative_profile: dict[str, float] = None  # 负向画像
) -> float:
    """
    计算作品与 XP 画像的匹配度（改进版）
    
    算法:
    1. 累加匹配 tag 的权重
    2. 按最高权重归一化
    3. 奖励高权重匹配（Top 20% 的 tag 匹配额外 +20%）
    4. 使用对数平滑匹配数量影响
    5. 负向画像惩罚（匹配到不喜欢的 Tag 时扣分）
    
    Returns:
        0.0 ~ 1.0 归一化分数
    """
    import math
    
    if not illust.tags or not xp_profile:
        return 0.0
    
    # 获取 XP 中的最大权重和 Top 20% 阈值
    sorted_weights = sorted(xp_profile.values(), reverse=True)
    max_weight = sorted_weights[0] if sorted_weights else 1.0
    top_threshold = sorted_weights[len(sorted_weights) // 5] if len(sorted_weights) >= 5 else max_weight * 0.8
    
    total_score = 0.0
    matched_count = 0
    high_weight_matches = 0
    negative_penalty = 0.0  # 负向惩罚累计
    
    for tag in illust.tags:
        # 使用统一的归一化逻辑
        from utils import normalize_tag
        normalized_tag = normalize_tag(tag)
        
        # 正向匹配
        weight = None
        if normalized_tag in xp_profile:
            weight = xp_profile[normalized_tag]
        # Fallback: 尝试原始Tag的小写 (有些特例可能未被归一化覆盖)
        elif tag.lower() in xp_profile:
            weight = xp_profile[tag.lower()]
        
        if weight is not None:
            total_score += weight
            matched_count += 1
            if weight >= top_threshold:
                high_weight_matches += 1
        
        # 负向匹配（匹配到不喜欢的 Tag）
        if negative_profile:
            neg_weight = negative_profile.get(normalized_tag, 0)
            if neg_weight > 0:
                # 惩罚系数 0.5，避免过度惩罚
                negative_penalty += neg_weight * 0.5
    
    if matched_count == 0:
        # 即使没有正向匹配，如果有负向匹配也要惩罚
        if negative_penalty > 0:
            return max(-negative_penalty / (max_weight + 1), -0.5)  # 最多负 -0.5
        return 0.0
    
    # 基础分：权重总和 / (匹配数 × 最大权重)
    base_score = total_score / (matched_count * max_weight) if max_weight > 0 else 0.0
    
    # 匹配数量奖励：log(1 + n) / log(6) → 匹配5个以上趋于饱和
    quantity_bonus = min(math.log(1 + matched_count) / math.log(6), 0.3)
    
    # 高权重匹配奖励：每匹配一个 Top 20% 的 tag +5%，最多 +20%
    quality_bonus = min(high_weight_matches * 0.05, 0.2)
    
    # 负向惩罚（归一化后扣除）
    penalty_normalized = negative_penalty / (max_weight + 1) if max_weight > 0 else 0
    
    final_score = base_score + quantity_bonus + quality_bonus - penalty_normalized
    return max(min(final_score, 1.0), 0.0)  # 限制在 0~1


class ContentFilter:
    """内容过滤器"""
    
    def __init__(
        self,
        blacklist_tags: Optional[list[str]] = None,
        daily_limit: int = 20,
        exclude_ai: bool = True,
        min_match_score: float = 0.0,
        match_weight: float = 0.5,
        max_per_artist: int = 3,
        subscribed_artists: Optional[list[int]] = None,  # 关注的画师 ID
        artist_boost: float = 0.3,  # 关注画师的匹配度加成
        min_create_days: int = 0,  # 过滤 N 天前的老图 (0=不过滤)
        r18_mode: bool = False,  # 涩涩模式：只推送 R-18
        # === 新增：借鉴 X 算法的增强选项 ===
        author_diversity: Optional[dict] = None,  # 画师多样性衰减配置
        source_boost: Optional[dict] = None,  # 来源加成配置
        embedder = None,  # 可选的 Embedder 实例 (用于语义匹配)
        ai_scorer = None,  # 可选的 AIScorer 实例 (用于 LLM 精排)
        # 多样性增强
        shuffle_factor: float = 0.0,  # 随机打散因子 (0-0.5)
        exploration_ratio: float = 0.0  # 探索比例 (0-0.5)
    ):
        self.blacklist_tags = set(t.lower() for t in (blacklist_tags or []))
        self.daily_limit = daily_limit
        self.exclude_ai = exclude_ai
        self.min_match_score = min_match_score
        self.match_weight = match_weight
        self.max_per_artist = max_per_artist
        self.subscribed_artists = set(subscribed_artists or [])
        self.artist_boost = artist_boost
        self.min_create_days = min_create_days
        self.r18_mode = r18_mode
        
        # 画师多样性衰减 (借鉴 X 算法 AuthorDiversityScorer)
        # 公式: multiplier(position) = (1.0 - floor) × decay^position + floor
        diversity_cfg = author_diversity or {}
        self.diversity_enabled = diversity_cfg.get("enabled", False)
        self.diversity_decay = diversity_cfg.get("decay_factor", 0.7)
        self.diversity_floor = diversity_cfg.get("floor", 0.1)
        
        # 来源加成 (借鉴 X 算法 OON Scorer)
        self.source_boost = source_boost or {
            "xp_search": 1.0,
            "subscription": 1.1,
            "ranking": 0.9,
            "related": 1.15
        }
        
        # AI Embedding 语义匹配 (可选)
        self.embedder = embedder
        
        # AI Scorer LLM 精排 (可选)
        self.ai_scorer = ai_scorer
        
        # 多样性增强
        self.shuffle_factor = min(0.5, max(0.0, shuffle_factor))  # 限制在 0-0.5
        self.exploration_ratio = min(0.5, max(0.0, exploration_ratio))
        
        # 硬性过滤Tag
        self.blacklist_tags.update({"r-18g", "guro", "gore"})
    
    async def filter(
        self,
        illusts: list[Illust],
        xp_profile: Optional[dict[str, float]] = None,
        user_id: int = 0  # 用于 Embedding 缓存
    ) -> list[Illust]:
        """
        过滤管道
        
        1. 去重（已推送）
        2. 时间过滤（老图片）
        3. 硬性过滤（R-18G、AI）
        4. 黑名单Tag
        5. 匹配度过滤 + 画师权重加成 + 语义匹配(可选)
        6. 综合排序
        7. 多样性控制
        8. 每日上限
        """
        from datetime import datetime, timedelta
        
        if not illusts:
            return []
        
        # 计算时间阈值
        time_threshold = None
        if self.min_create_days > 0:
            time_threshold = datetime.now(illusts[0].create_date.tzinfo if illusts else None) - timedelta(days=self.min_create_days)
        
        # 批量预加载已推送 ID (性能优化: O(n) -> O(1) 数据库查询)
        all_ids = [illust.id for illust in illusts]
        pushed_ids = await db.get_pushed_ids_batch(all_ids)
        
        result = []
        filtered_by_time = 0
        
        for illust in illusts:
            # 1. 去重 (使用预加载的集合)
            if illust.id in pushed_ids:
                continue
            
            # 2. 时间过滤
            if time_threshold and illust.create_date < time_threshold:
                filtered_by_time += 1
                continue
            
            # 3. R-18G 排除
            if self._has_blacklisted_tag(illust):
                continue
            
            # 4. AI 生成排除
            if self.exclude_ai and illust.ai_type == 2:
                continue
            
            # 4.1 涩涩模式 (R-18 Mode Control)
            # 支持 bool (旧配置) 和 str (新配置: safe, mixed, r18_only)
            mode_str = str(self.r18_mode).lower()
            
            if mode_str in ("true", "r18_only", "pure"):
                # 纯 18+ 模式：只允许 R-18
                if not illust.is_r18:
                    continue
            elif mode_str in ("safe", "18-", "clean"):
                # 净网模式：禁止 R-18
                if illust.is_r18:
                    continue
            else:
                # 默认/mixed/neutral：不因 R-18 属性过滤，全凭匹配度
                pass
            
            result.append(illust)
        
        if filtered_by_time > 0:
            logger.debug(f"过滤 {filtered_by_time} 个超过 {self.min_create_days} 天的老图")
        
        # 去重（同批次内）
        seen_ids = set()
        unique_result = []
        for illust in result:
            if illust.id not in seen_ids:
                seen_ids.add(illust.id)
                unique_result.append(illust)
        
        # 5. 计算匹配度并过滤 + 画师权重加成 + 负向画像惩罚 + 语义匹配(可选)
        negative_profile = await db.get_negative_profile()  # 加载负向画像
        
        # 准备语义匹配 (如果启用)
        user_embedding = None
        illust_embeddings_cache = {}
        
        if self.embedder and self.embedder.enabled and xp_profile and user_id > 0:
            try:
                import hashlib
                # 计算 XP Profile 哈希，判断是否需要更新用户 Embedding
                profile_str = str(sorted(list(xp_profile.items())[:20]))  # 只取 Top 20
                profile_hash = hashlib.md5(profile_str.encode()).hexdigest()[:16]
                
                # 获取或更新用户 Embedding
                cached = await db.get_user_embedding(user_id)
                if cached and cached[1] == profile_hash:
                    user_embedding = cached[0]
                    logger.debug("使用缓存的用户 Embedding")
                else:
                    # 重新计算
                    top_tags = [t for t, _ in sorted(xp_profile.items(), key=lambda x: x[1], reverse=True)[:15]]
                    user_embedding = await self.embedder.embed_tags(top_tags)
                    if user_embedding:
                        await db.save_user_embedding(user_id, user_embedding, self.embedder.model, profile_hash)
                        logger.info("已更新用户画像 Embedding")
                
                # 批量获取作品 Embedding 缓存
                illust_ids = [ill.id for ill in unique_result]
                illust_embeddings_cache = await db.get_illust_embeddings_batch(illust_ids)
                logger.debug(f"Embedding 缓存命中: {len(illust_embeddings_cache)}/{len(illust_ids)}")
                
            except Exception as e:
                logger.warning(f"语义匹配初始化失败: {e}")
                user_embedding = None
        
        scored_result = []
        uncached_embeddings = []  # 待计算的作品 Embedding
        
        for illust in unique_result:
            if xp_profile:
                score = calculate_match_score(illust, xp_profile, negative_profile)
                
                # 画师权重加成：关注画师的作品额外加成
                if illust.user_id in self.subscribed_artists:
                    score = min(score + self.artist_boost, 1.0)
                
                if score < self.min_match_score:
                    continue
            else:
                score = 0.0
                # 无 XP 时，关注画师也给予基础分
                if illust.user_id in self.subscribed_artists:
                    score = self.artist_boost
            
            # 语义匹配加成 (可选)
            semantic_score = 0.0
            if user_embedding and self.embedder:
                illust_emb = illust_embeddings_cache.get(illust.id)
                if illust_emb:
                    # 使用缓存
                    similarity = self.embedder.cosine_similarity(user_embedding, illust_emb)
                    semantic_score = self.embedder.normalize_similarity(similarity)
                else:
                    # 记录需要计算的作品
                    uncached_embeddings.append((illust, score))
                    continue  # 跳过，后面批量处理
                
                # 加权组合: (1-semantic_weight)*tag_score + semantic_weight*semantic_score
                semantic_weight = self.embedder.semantic_weight
                score = (1 - semantic_weight) * score + semantic_weight * semantic_score
            
            # 来源加成 (借鉴 X 算法 OON Scorer)
            source = getattr(illust, 'source', 'xp_search')
            source_multiplier = self.source_boost.get(source, 1.0)
            score *= source_multiplier
            
            scored_result.append((illust, score))
        
        # 批量计算未缓存的作品 Embedding
        if uncached_embeddings and user_embedding and self.embedder:
            try:
                texts = [", ".join(ill.tags[:10]) for ill, _ in uncached_embeddings]
                embeddings = await self.embedder.embed_batch(texts)
                
                to_save = []
                for i, (illust, tag_score) in enumerate(uncached_embeddings):
                    emb = embeddings[i]
                    if emb:
                        to_save.append((illust.id, emb, self.embedder.model))
                        similarity = self.embedder.cosine_similarity(user_embedding, emb)
                        semantic_score = self.embedder.normalize_similarity(similarity)
                        semantic_weight = self.embedder.semantic_weight
                        score = (1 - semantic_weight) * tag_score + semantic_weight * semantic_score
                    else:
                        score = tag_score
                    
                    # 来源加成
                    source = getattr(illust, 'source', 'xp_search')
                    source_multiplier = self.source_boost.get(source, 1.0)
                    score *= source_multiplier
                    
                    scored_result.append((illust, score))
                
                # 保存新计算的 Embedding
                if to_save:
                    await db.save_illust_embeddings_batch(to_save)
                    logger.info(f"已缓存 {len(to_save)} 个作品 Embedding")
                    
            except Exception as e:
                logger.error(f"批量 Embedding 计算失败: {e}")
                # Fallback: 只用 Tag 分数
                for illust, tag_score in uncached_embeddings:
                    source = getattr(illust, 'source', 'xp_search')
                    source_multiplier = self.source_boost.get(source, 1.0)
                    scored_result.append((illust, tag_score * source_multiplier))
        
        # 6. 综合排序：match_score * weight + normalized_bookmark * (1-weight) + 随机打散
        if scored_result:
            import random
            max_bookmark = max(item[0].bookmark_count for item in scored_result) or 1
            
            def sort_key(item):
                illust, score = item
                normalized_bookmark = illust.bookmark_count / max_bookmark
                base_score = score * self.match_weight + normalized_bookmark * (1 - self.match_weight)
                # 随机打散：添加随机噪声使每次排序结果不同
                if self.shuffle_factor > 0:
                    noise = random.uniform(-self.shuffle_factor, self.shuffle_factor)
                    return base_score + noise
                return base_score
            
            scored_result.sort(key=sort_key, reverse=True)
        
        # 构建 illust -> score 的映射
        score_map = {item[0].id: item[1] for item in scored_result}
        sorted_illusts = [item[0] for item in scored_result]
        
        # 6.1 AI 精排 (可选) - 使用 LLM 对候选作品进行二次评分
        if self.ai_scorer and self.ai_scorer.enabled and xp_profile:
            try:
                candidates_for_ai = sorted_illusts[:self.ai_scorer.max_candidates]
                if len(candidates_for_ai) > 5:  # 至少需要一定数量才有意义
                    # 获取近期反馈
                    recent_likes = await db.get_recent_liked_tags(limit=5)
                    recent_dislikes = await db.get_recent_disliked_tags(limit=5)
                    
                    ai_scores = await self.ai_scorer.score_candidates(
                        candidates_for_ai,
                        xp_profile,
                        recent_likes,
                        recent_dislikes
                    )
                    
                    if ai_scores:
                        # 混合 AI 分数和基础分数
                        score_map = self.ai_scorer.blend_scores(score_map, ai_scores)
                        # 重新排序
                        scored_result = [(ill, score_map.get(ill.id, 0)) for ill in sorted_illusts]
                        scored_result.sort(key=lambda x: x[1], reverse=True)
                        sorted_illusts = [item[0] for item in scored_result]
                        logger.info("AI 精排已应用")
            except Exception as e:
                logger.warning(f"AI 精排失败: {e}")
        
        # 7. 多样性控制：画师多样性衰减 + 硬性限制
        # 借鉴 X 算法 AuthorDiversityScorer: 同一画师后续作品分数递减
        if self.diversity_enabled:
            # 应用画师多样性衰减
            artist_position = {}  # 记录每个画师已出现的位置
            for illust, score in scored_result:
                pos = artist_position.get(illust.user_id, 0)
                # 衰减公式: (1.0 - floor) × decay^position + floor
                multiplier = (1.0 - self.diversity_floor) * (self.diversity_decay ** pos) + self.diversity_floor
                new_score = score * multiplier
                score_map[illust.id] = new_score
                artist_position[illust.user_id] = pos + 1
            
            # 重新排序
            scored_result = [(ill, score_map[ill.id]) for ill, _ in scored_result]
            scored_result.sort(key=lambda x: x[1], reverse=True)
            sorted_illusts = [item[0] for item in scored_result]
        
        # 硬性限制每个画师的作品数
        artist_count = {}
        diverse_result = []
        for illust in sorted_illusts:
            count = artist_count.get(illust.user_id, 0)
            if count < self.max_per_artist:
                # 将匹配度附加到对象上（动态属性）
                illust.match_score = score_map.get(illust.id, 0.0)
                diverse_result.append(illust)
                artist_count[illust.user_id] = count + 1
        
        # 7. 探索比例：从后半部分随机抽取一些"潜力股"
        if self.exploration_ratio > 0 and len(diverse_result) > self.daily_limit:
            import random
            explore_count = int(self.daily_limit * self.exploration_ratio)
            main_count = self.daily_limit - explore_count
            
            # 前 main_count 个是高分作品
            main_result = diverse_result[:main_count]
            
            # 从后半部分随机抽取 explore_count 个
            candidate_pool = diverse_result[main_count:main_count * 3]  # 后续 2 倍候选
            if len(candidate_pool) >= explore_count:
                explore_picks = random.sample(candidate_pool, explore_count)
            else:
                explore_picks = candidate_pool
            
            # 混合并打散
            final_result = main_result + explore_picks
            random.shuffle(final_result)  # 打散顺序，避免探索推荐集中在末尾
            logger.info(f"探索推荐: 混入 {len(explore_picks)} 个潜力作品")
        else:
            # 8. 每日上限
            final_result = diverse_result[:self.daily_limit]
        
        # 记录匹配度日志
        if xp_profile and scored_result:
            top_3 = scored_result[:3]
            log_items = [f"{i[0].title[:10]}(score={i[1]:.2f})" for i in top_3]
            logger.info(f"匹配度 Top3: {', '.join(log_items)}")
        
        logger.info(f"过滤后剩余 {len(final_result)} 个作品 (涉及 {len(artist_count)} 个画师)")
        return final_result
    
    def check_illust(self, illust: Illust) -> bool:
        """检查单个作品是否满足基本过滤条件 (Blacklist, AI, R18, Time)"""
        # 1. 基础有效性
        if not illust.id: return False
        
        # 2. 时间过滤 (如果配置)
        if self.min_create_days > 0:
            from datetime import datetime, timedelta
            time_threshold = datetime.now(illust.create_date.tzinfo) - timedelta(days=self.min_create_days)
            if illust.create_date < time_threshold:
                return False

        # 3. R-18G / Blacklist
        if self._has_blacklisted_tag(illust):
            return False
            
        # 4. AI 排除
        if self.exclude_ai and illust.ai_type == 2:
            return False
            
        # 5. R-18 Mode
        mode_str = str(self.r18_mode).lower()
        if mode_str in ("true", "r18_only", "pure"):
            if not illust.is_r18: return False
        elif mode_str in ("safe", "18-", "clean"):
            if illust.is_r18: return False
            
        return True

    def _has_blacklisted_tag(self, illust: Illust) -> bool:
        """检查是否包含黑名单Tag"""
        for tag in illust.tags:
            if tag.lower() in self.blacklist_tags:
                return True
        return False
    
    async def add_to_blacklist(self, tag: str):
        """动态添加黑名单Tag"""
        self.blacklist_tags.add(tag.lower())
        logger.info(f"Tag '{tag}' 已加入黑名单")
