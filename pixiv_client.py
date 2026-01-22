"""
Pixiv API 异步客户端
基于 pixivpy-async
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
from pixivpy_async import AppPixivAPI

from utils import AsyncRateLimiter, retry_async, download_image_with_referer

logger = logging.getLogger(__name__)


@dataclass
class Illust:
    """作品数据结构"""
    id: int
    title: str
    user_id: int
    user_name: str
    tags: list[str]
    bookmark_count: int
    view_count: int
    page_count: int
    image_urls: list[str]  # 所有页的原图URL
    is_r18: bool
    ai_type: int  # 0=非AI, 1=辅助AI, 2=纯AI
    create_date: datetime
    type: str = "illust" # illust, manga, ugoira
    source: str = "xp_search"  # 来源策略 (用于归因)


class PixivClient:
    """Pixiv API 异步封装"""
    
    def __init__(
        self,
        refresh_token: Optional[str] = None,
        requests_per_minute: int = 60,
        random_delay: tuple[float, float] = (1.0, 3.0),
        max_concurrency: int = 5,
        proxy_url: Optional[str] = None
    ):
        self.refresh_token = refresh_token
        
        # Auto-detect proxy if not provided
        if not proxy_url:
            import urllib.request
            sys_proxies = urllib.request.getproxies()
            # Prioritize https, then http
            proxy_url = sys_proxies.get("https") or sys_proxies.get("http")
            if proxy_url:
                logger.info(f"Using system proxy: {proxy_url}")
                # Ensure scheme if missing (though getproxies usually includes it)
                if not proxy_url.startswith("http"):
                    proxy_url = f"http://{proxy_url}"

        # 传递代理给 AppPixivAPI (注意: pixivpy 可能需要特定的代理传参方式，通常是在 login 或 api 调用时)
        # 但 AppPixivAPI 构造函数可以直接接收 env 覆盖，或者我们在 login 时传递 proxy
        self.api = AppPixivAPI(proxy=proxy_url) 
        self.rate_limiter = AsyncRateLimiter(requests_per_minute, random_delay)
        self.download_semaphore = asyncio.Semaphore(max_concurrency)
        self._session: Optional[aiohttp.ClientSession] = None
        self._logged_in = False
        self.proxy_url = proxy_url
    
    async def login(self) -> bool:
        """
        登录（可选）
        无token时使用游客模式（功能受限）
        """
        if not self.refresh_token:
            logger.warning("未配置Token，使用游客模式（仅支持公开内容）")
            return False
        
        try:
            async with self.rate_limiter:
                # login 返回 json 数据
                auth_info = await self.api.login(refresh_token=self.refresh_token)
            
            self._logged_in = True
            
            # 从响应中获取用户信息
            user_data = auth_info.get("response", {}).get("user", {})
            uid = user_data.get("id", "Unknown")
            name = user_data.get("name", "Unknown")
            
            logger.info(f"Pixiv 登录成功: UserID={uid}, Name={name}")
            return True
        except Exception as e:
            logger.error(f"Pixiv 登录失败: {e}")
            return False
    
    @retry_async(max_retries=3)
    async def get_bookmarks(
        self,
        user_id: int,
        limit: int = 500,
        private: bool = False,
        stop_ids: Optional[set[int]] = None,
        skip_ids: Optional[set[int]] = None,
        on_batch: Optional[callable] = None,
        start_url: Optional[str] = None
    ) -> list[Illust]:
        """
        获取用户收藏
        
        Args:
            user_id: 用户ID
            limit: 获取数量
            private: 是否获取私密收藏
            stop_ids: 遇到这些ID时停止获取（用于增量更新）
            skip_ids: 遇到这些ID时跳过但继续（用于笨拙的断点续传）
            on_batch: 每批次回调 (items, next_url)
            start_url: 起始URL（用于高效断点续传）
        """
        if private and not self._logged_in:
            logger.warning("获取私密收藏需要登录")
            return []
        
        illusts = []
        restrict = "private" if private else "public"
        next_qs = None
        
        if start_url:
            next_qs = self.api.parse_qs(start_url)
        
        stop_triggered = False
        skipped_count = 0
        page_count = 0
        
        while len(illusts) < limit:
            async with self.rate_limiter:
                try:
                    if next_qs:
                        result = await self.api.user_bookmarks_illust(**next_qs)
                    else:
                        result = await self.api.user_bookmarks_illust(
                            user_id=user_id,
                            restrict=restrict
                        )
                except Exception as e:
                    logger.error(f"API Request Failed: {e}")
                    await asyncio.sleep(5)
                    break
            
            page_count += 1
            
            current_next_url = result.get("next_url")
            
            if not result.get("illusts"):
                if "error" in result:
                    logger.error(f"API错误: {result['error']}")
                elif len(illusts) == 0 and skipped_count == 0 and not start_url:
                    logger.warning(f"用户 {user_id} 未返回任何收藏 (restrict={restrict})。请确认账号是否有对应权限或收藏。")
                break
            
            new_items = []
            for item in result["illusts"]:
                try:
                    illust = self._parse_illust(item)
                    
                    # Logics
                    if stop_ids and illust.id in stop_ids:
                        stop_triggered = True
                        break
                    
                    if skip_ids and illust.id in skip_ids:
                        skipped_count += 1
                        continue
                    
                    if len(illusts) + len(new_items) >= limit:
                        break
                    
                    new_items.append(illust)
                except Exception as parse_e:
                    logger.warning(f"解析作品失败: {parse_e}")
                    continue
            
            # Batch Callback with Cursor
            if on_batch:
                try:
                    # 将本次获取后的 next_url 传出去，以便在保存这批数据的同时保存"下一次该从哪读"
                    await on_batch(new_items, current_next_url)
                except Exception as cb_e:
                    logger.error(f"Batch callback failed: {cb_e}")

            illusts.extend(new_items)
            
            if stop_triggered:
                break
            
            # 进度日志
            if page_count % 5 == 0:
                 logger.info(f"进度: 已获取 {len(illusts)} 新收藏 (已跳过 {skipped_count} 旧收藏)...")
                 await asyncio.sleep(1)
            
            next_qs = self.api.parse_qs(current_next_url)
            if not next_qs:
                break
        
        logger.info(f"获取结束: 共获取 {len(illusts)} 个新收藏 (跳过 {skipped_count} 个)")
        return illusts
    
    @retry_async(max_retries=3)
    async def search_illusts(
        self,
        tags: list[str],
        bookmark_threshold: int = 0,
        date_range_days: int = 30,  # 默认扩大到 30 天，增加命中率
        limit: int = 50
    ) -> list[Illust]:
        """
        搜索作品
        
        Args:
            tags: 搜索Tag（AND关系）
            bookmark_threshold: 收藏数阈值
            date_range_days: 日期范围
            limit: 返回数量
        """
        if not self._logged_in:
            logger.warning("搜索功能需要登录")
            return []
        
        query = " ".join(tags)
        illusts = []
        next_qs = None
        
        # 计算过滤统计
        total_fetched = 0
        filtered_count = 0
        
        while len(illusts) < limit:
            async with self.rate_limiter:
                if next_qs:
                    result = await self.api.search_illust(**next_qs)
                else:
                    # 动态计算日期范围
                    if date_range_days > 0:
                         start_date = (datetime.now() - timedelta(days=date_range_days)).strftime("%Y-%m-%d")
                    
                    result = await self.api.search_illust(
                        word=query,
                        search_target="partial_match_for_tags",
                        sort="popular_desc",
                        start_date=start_date
                    )
            
            if not result.get("illusts"):
                break
            
            batch = result["illusts"]
            total_fetched += len(batch)
            
            for item in batch:
                if len(illusts) >= limit:
                    break
                illust = self._parse_illust(item)
                if illust.bookmark_count >= bookmark_threshold:
                    illusts.append(illust)
                else:
                    filtered_count += 1
            
            next_qs = self.api.parse_qs(result.get("next_url"))
            if not next_qs:
                break
        
        logger.info(f"搜索 '{query}' (近{date_range_days}天): 获取 {total_fetched} -> 过滤 {filtered_count} -> 保留 {len(illusts)}")
        return illusts
    
    @retry_async(max_retries=3)
    async def get_user_illusts(
        self,
        user_id: int,
        since: Optional[datetime] = None,
        limit: int = 30
    ) -> list[Illust]:
        """
        获取画师作品
        
        Args:
            user_id: 画师ID
            since: 仅获取此时间之后的作品
            limit: 返回数量
        """
        illusts = []
        next_qs = None
        
        while len(illusts) < limit:
            async with self.rate_limiter:
                if next_qs:
                    result = await self.api.user_illusts(**next_qs)
                else:
                    result = await self.api.user_illusts(user_id=user_id)
            
            if not result.get("illusts"):
                break
            
            for item in result["illusts"]:
                if len(illusts) >= limit:
                    break
                illust = self._parse_illust(item)
                if since and illust.create_date < since:
                    # 作品按时间倒序，早于since则停止
                    return illusts
                illusts.append(illust)
            
            next_qs = self.api.parse_qs(result.get("next_url"))
            if not next_qs:
                break
        
        return illusts
    
    @retry_async(max_retries=3)
    async def get_related_illusts(
        self,
        illust_id: int,
        limit: int = 30
    ) -> list[Illust]:
        """
        获取相关作品 (Related Works)
        
        Args:
            illust_id: 种子作品ID
            limit: 返回数量
        """
        illusts = []
        next_qs = None
        
        # 限制只抓取前几页，防止过深
        max_pages = max(1, limit // 30 + 1)
        page = 0
        
        while len(illusts) < limit and page < max_pages:
            async with self.rate_limiter:
                if next_qs:
                    # 过滤掉不支持的参数 (如 viewed)
                    supported_keys = {'illust_id', 'filter', 'offset'}
                    filtered_qs = {k: v for k, v in next_qs.items() if k in supported_keys}
                    result = await self.api.illust_related(**filtered_qs)
                else:
                    result = await self.api.illust_related(illust_id=illust_id)
            
            if not result.get("illusts"):
                break
            
            for item in result["illusts"]:
                if len(illusts) >= limit:
                    break
                illusts.append(self._parse_illust(item))
            
            next_qs = self.api.parse_qs(result.get("next_url"))
            if not next_qs:
                break
            page += 1
            
        return illusts
    
    @retry_async(max_retries=3)
    async def get_ranking(
        self,
        mode: str = "day",
        date: str | None = None,
        limit: int = 50
    ) -> list[Illust]:
        """
        获取排行榜
        
        Args:
            mode: 排行榜类型
                - day: 日榜
                - week: 周榜
                - month: 月榜
                - day_male: 男性向日榜
                - day_female: 女性向日榜
                - day_r18: R18日榜 (需登录)
            date: 日期 (YYYY-MM-DD)，None 表示昨天
            limit: 返回数量
        """
        if not self._logged_in:
            logger.warning("排行榜功能需要登录")
            return []
        
        illusts = []
        next_qs = None
        
        while len(illusts) < limit:
            async with self.rate_limiter:
                if next_qs:
                    result = await self.api.illust_ranking(**next_qs)
                else:
                    params = {"mode": mode}
                    if date:
                        params["date"] = date
                    result = await self.api.illust_ranking(**params)
            
            if not result.get("illusts"):
                if "error" in result:
                    logger.error(f"排行榜 API 错误: {result['error']}")
                break
            
            for item in result["illusts"]:
                if len(illusts) >= limit:
                    break
                try:
                    illust = self._parse_illust(item)
                    illusts.append(illust)
                except Exception as e:
                    logger.warning(f"解析排行榜作品失败: {e}")
                    continue
            
            next_qs = self.api.parse_qs(result.get("next_url"))
            if not next_qs:
                break
        
        logger.info(f"获取 {mode} 排行榜 {len(illusts)} 个作品")
        return illusts
    
    async def fetch_following(self, user_id: int, public: bool = True) -> set[int]:
        """获取关注的用户ID列表"""
        if not self._logged_in:
            return set()
            
        following_ids = set()
        restrict = "public" if public else "private"
        next_qs = None
        
        try:
            while True:
                async with self.rate_limiter:
                    if next_qs:
                        result = await self.api.user_following(**next_qs)
                    else:
                        result = await self.api.user_following(user_id, restrict=restrict)
                
                users = result.get("user_previews") or []
                if not users:
                    break
                    
                for u in users:
                    if u.get("user"):
                        following_ids.add(u["user"]["id"])
                        
                next_qs = self.api.parse_qs(result.get("next_url"))
                if not next_qs:
                    break
                    
                # 限制最大获取数防止风控
                if len(following_ids) > 5000:
                    break
            
            logger.info(f"获取关注列表 ({restrict}): {len(following_ids)} 人")
            return following_ids
            
        except Exception as e:
            logger.error(f"获取关注列表失败: {e}")
            return following_ids

    async def fetch_follow_latest(self, limit: int = 50) -> list[Illust]:
        """获取关注用户的最新作品"""
        if not self._logged_in:
            return []
            
        illusts = []
        next_qs = None
        
        try:
            while len(illusts) < limit:
                async with self.rate_limiter:
                    if next_qs:
                        result = await self.api.illust_follow(**next_qs)
                    else:
                        result = await self.api.illust_follow(restrict="public")
                
                items = result.get("illusts")
                if not items:
                    break
                    
                for item in items:
                    if len(illusts) >= limit:
                        break
                    try:
                        illusts.append(self._parse_illust(item))
                    except:
                        pass
                
                next_qs = self.api.parse_qs(result.get("next_url"))
                if not next_qs:
                    break
                    
            logger.info(f"获取关注者新作: {len(illusts)} 个")
            return illusts
        except Exception as e:
            logger.error(f"获取关注者新作失败: {e}")
            return []
    
    async def download_image(self, url: str) -> bytes:
        """
        下载图片（带Referer）
        """
        if not self._session:
            self._session = aiohttp.ClientSession()
        
        return await download_image_with_referer(
            self._session,
            url,
            self.download_semaphore,
            proxy=self.proxy_url
        )
    
    async def close(self):
        """关闭会话"""
        if self._session:
            await self._session.close()
            self._session = None
    
    def _parse_illust(self, data: dict) -> Illust:
        """解析API返回的作品数据"""
        tags = [t["name"] for t in data.get("tags", [])]
        
        # 获取所有页的原图URL
        image_urls = []
        if data.get("meta_single_page", {}).get("original_image_url"):
            image_urls.append(data["meta_single_page"]["original_image_url"])
        for page in data.get("meta_pages", []):
            url = page.get("image_urls", {}).get("original")
            if url:
                image_urls.append(url)
        
        # 没有原图URL时使用大图
        if not image_urls:
            large_url = data.get("image_urls", {}).get("large")
            if large_url:
                image_urls.append(large_url)
        
        # 解析创建时间
        create_date_str = data.get("create_date", "")
        try:
            create_date = datetime.fromisoformat(create_date_str.replace("Z", "+00:00"))
        except:
            create_date = datetime.now()
        
        return Illust(
            id=data["id"],
            title=data.get("title", ""),
            user_id=data.get("user", {}).get("id", 0),
            user_name=data.get("user", {}).get("name", ""),
            tags=tags,
            bookmark_count=data.get("total_bookmarks", 0),
            view_count=data.get("total_view", 0),
            page_count=data.get("page_count", 1),
            image_urls=image_urls,
            is_r18="R-18" in tags,
            ai_type=data.get("illust_ai_type", 0),
            create_date=create_date,
            type=data.get("type", "illust")
        )

    async def get_ugoira_metadata(self, illust_id: int) -> dict:
        """获取动图元数据"""
        if not self._logged_in: 
            return {}
        async with self.rate_limiter:
            return await self.api.ugoira_metadata(illust_id)

    async def add_bookmark(self, illust_id: int, private: bool = False, tags: Optional[list[str]] = None) -> bool:
        """添加收藏"""
        restrict = "private" if private else "public"
        try:
            async with self.rate_limiter:
                await self.api.illust_bookmark_add(
                    illust_id=illust_id,
                    restrict=restrict,
                    tags=tags
                )
            logger.info(f"已添加收藏: {illust_id}")
            return True
        except Exception as e:
            logger.error(f"添加收藏失败 {illust_id}: {e}")
            return False
    @retry_async(max_retries=3)
    async def get_illust_detail(self, illust_id: int) -> Optional[Illust]:
        """获取作品详情"""
        if not self._logged_in:
            logger.warning("获取详情需要登录")
            return None
            
        async with self.rate_limiter:
            result = await self.api.illust_detail(illust_id)
            
        if result and result.get("illust"):
            return self._parse_illust(result["illust"])
        return None
