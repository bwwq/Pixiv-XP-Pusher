"""
QwenPaw 推送实现
将作品图片下载到本地后生成 MD 推送清单，通过 cron 任务 触发 dispatch 发送图片+文字
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import aiohttp

from .base import BaseNotifier
from utils import get_pixiv_cat_url

if TYPE_CHECKING:
    from pixiv_client import Illust

logger = logging.getLogger(__name__)


class QwenPawNotifier(BaseNotifier):
    """QwenPaw 推送器（图片下载 + MD 清单 + agents chat 中转）"""

    def __init__(
        self,
        cronid: str,
        keep_history: bool = False,
        client: Optional['PixivClient'] = None,
        max_pages: int = 10,
        max_image_size_mb: float = 9.5,
        qwenpaw_bin: Optional[str] = None,
    ):
        """
        初始化 QwenPaw 推送器

        Args:
            cronid: QwenPaw cron 任务 ID（用于触发 dispatch 发送图片+文字）
            client: PixivClient 实例 (用于下载原图)
            max_pages: 多图作品最大页数
            max_image_size_mb: 图片最大大小（MB），超过此大小的图片会被自动压缩
            qwenpaw_bin: qwenpaw 可执行文件路径 (留空则自动从 PATH 查找)
        """
        self.cronid = cronid
        # 相对于项目根目录的 data/ 目录
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.download_dir = os.path.join(_project_root, "data")
        self.keep_history = keep_history
        self.client = client
        self.max_pages = max_pages
        # QQ Bot 图片大小限制 ~10MB，由配置参数控制
        self.max_image_size = max_image_size_mb * 1024 * 1024
        # qwenpaw 可执行文件路径：优先配置 → PATH 查找 → 默认 conda 环境
        self.qwenpaw_bin = qwenpaw_bin or \
            shutil.which("qwenpaw") or \
            os.path.join(os.path.expanduser("~"), "miniconda3", "envs", "QwenPaw", "bin", "qwenpaw")

        self._session: Optional[aiohttp.ClientSession] = None

        os.makedirs(self.download_dir, exist_ok=True)

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── 图片下载 ───────────────────────────────────────────────

    async def _download_image(self, url: str, save_path: str) -> bool:
        """下载单张图片到本地，下载后立即检查并压缩"""
        try:
            from utils import download_image_with_referer
            await self._ensure_session()
            logger.debug(f"开始下载: {url}")
            data = await download_image_with_referer(
                self._session, url
            )
            if data:
                with open(save_path, "wb") as f:
                    f.write(data)
                logger.info(f"下载完成: {os.path.basename(save_path)} ({len(data)/1024:.0f}KB)")
                # 下载后立即检查是否需要压缩
                self._compress_image(save_path)
                return True
            logger.warning(f"下载返回空数据: {url}")
            return False
        except Exception as e:
            logger.warning(f"下载图片失败 {url}: {e}")
            return False

    async def _download_illust_images(self, illust: 'Illust', batch_dir: str) -> list[str]:
        """下载作品的所有图片，返回本地路径列表"""
        paths = []

        # ── 动图 (ugoira) 特殊处理 ──────────────────────────
        if getattr(illust, 'type', 'illust') == 'ugoira' and illust.image_urls:
            logger.info(f"作品 #{illust.id} 为动图，开始下载并转换 GIF")
            return await self._download_ugoira(illust, batch_dir)

        # ── 普通图片 ─────────────────────────────────────────
        urls = illust.image_urls[:self.max_pages]
        logger.info(f"作品 #{illust.id} 开始下载 {len(urls)} 张图片")

        for i, url in enumerate(urls):
            ext = ".jpg"
            if ".png" in url:
                ext = ".png"
            elif ".webp" in url:
                ext = ".webp"
            filename = f"{illust.id}_{i}{ext}"
            save_path = os.path.join(batch_dir, filename)

            if await self._download_image(url, save_path):
                paths.append(save_path)

        # 若无原图 URL，尝试 pixiv.cat 反代
        if not paths:
            logger.info(f"作品 #{illust.id} 原图下载失败，尝试 pixiv.cat 反代")
            cat_url = get_pixiv_cat_url(illust.id)
            save_path = os.path.join(batch_dir, f"{illust.id}_0.jpg")
            if await self._download_image(cat_url, save_path):
                paths.append(save_path)

        logger.info(f"作品 #{illust.id} 下载完成: {len(paths)}/{len(urls)} 张")
        return paths

    async def _download_ugoira(self, illust: 'Illust', batch_dir: str) -> list[str]:
        """下载动图并转换为 GIF"""
        zip_url = illust.image_urls[0]
        zip_path = os.path.join(batch_dir, f"{illust.id}.zip")
        gif_path = os.path.join(batch_dir, f"{illust.id}_0.gif")

        # 1. 下载 zip
        if not await self._download_image(zip_url, zip_path):
            logger.warning(f"动图 #{illust.id} zip 下载失败")
            return []

        # 2. 获取帧延迟
        delays = []
        if self.client:
            try:
                meta = await self.client.api.ugoira_metadata(illust.id)
                if isinstance(meta, dict):
                    frames = meta.get("frames", [])
                    delays = [f.get("delay", 50) for f in frames]
            except Exception as e:
                logger.debug(f"获取动图元数据失败: {e}")

        if not delays:
            delays = [50]  # 默认 50ms

        # 3. 解压并合成 GIF
        import subprocess, tempfile
        extract_dir = os.path.join(batch_dir, f"{illust.id}_frames")
        os.makedirs(extract_dir, exist_ok=True)

        try:
            subprocess.run(
                ["unzip", "-q", "-o", zip_path, "-d", extract_dir],
                capture_output=True, timeout=30
            )
        except Exception as e:
            logger.warning(f"解压动图失败: {e}")
            shutil.rmtree(extract_dir, ignore_errors=True)
            return []

        # 按文件名排序帧
        frame_files = sorted([
            f for f in os.listdir(extract_dir)
            if f.lower().endswith((".png", ".jpg", ".jpeg"))
        ])

        if not frame_files:
            logger.warning("动图内无有效帧")
            shutil.rmtree(extract_dir, ignore_errors=True)
            return []

        # 用 ffmpeg 合成 GIF (使用第一帧的延迟作为全局 fps)
        first_delay = delays[0] if delays else 50
        fps = 1000 / first_delay
        input_pattern = os.path.join(extract_dir, frame_files[0].replace(
            frame_files[0].rsplit("_", 1)[-1] if "_" in frame_files[0] else "",
            "%d" + os.path.splitext(frame_files[0])[1]
        ))

        # 简化：直接用 concat 合成所有帧
        try:
            # 生成 ffmpeg concat 文件
            concat_file = os.path.join(batch_dir, "concat.txt")
            with open(concat_file, "w") as f:
                for i, (frame, delay) in enumerate(zip(frame_files, delays)):
                    f.write(f"file '{os.path.join(extract_dir, frame)}'\n")
                    f.write(f"duration {delay / 1000:.3f}\n")
                # 最后一帧需要重复一次来指定时长
                f.write(f"file '{os.path.join(extract_dir, frame_files[-1])}'\n")

            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", concat_file,
                 "-filter_complex", f"[0:v] fps={fps},scale=800:-1:flags=lanczos,split [a][b];[a] palettegen [p];[b][p] paletteuse",
                 gif_path],
                capture_output=True, timeout=120
            )

            if os.path.isfile(gif_path) and os.path.getsize(gif_path) > 1024:
                logger.info(f"动图 #{illust.id} GIF 合成成功: {os.path.getsize(gif_path)} bytes")
                return [gif_path]
        except Exception as e:
            logger.warning(f"动图 GIF 合成失败: {e}")

        # 清理临时文件
        shutil.rmtree(extract_dir, ignore_errors=True)
        os.remove(zip_path)
        return [gif_path] if os.path.isfile(gif_path) else []

    # ─── 图片压缩 ───────────────────────────────────────────────

    def _compress_image(self, file_path: str) -> bool:
        """压缩图片至小于 max_image_size，使用二分查找最大化画质。无需压缩时静默返回"""
        size = os.path.getsize(file_path)
        if size <= self.max_image_size:
            return True

        logger.info(f"开始压缩: {os.path.basename(file_path)} ({size/1024/1024:.1f}MB > {self.max_image_size/1024/1024:.0f}MB)")
        try:
            from PIL import Image
            import io

            img = Image.open(file_path)
            orig_w, orig_h = img.size

            # PNG → 转 JPEG 压缩（PNG 无损，降 quality 无效）
            save_fmt = 'JPEG'
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            def _try_save(image, quality):
                """以指定质量保存到内存，返回 (是否满足大小, 字节大小)"""
                buf = io.BytesIO()
                image.save(buf, save_fmt, quality=quality, optimize=True)
                buf_size = buf.tell()
                return buf, buf_size

            def _binary_search_quality(image, lo=20, hi=95):
                """二分查找最高质量使得文件 <= max_image_size"""
                best_buf = None
                best_q = lo
                while lo <= hi:
                    mid = (lo + hi) // 2
                    buf, buf_size = _try_save(image, mid)
                    if buf_size <= self.max_image_size:
                        best_buf = buf
                        best_q = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                return best_buf, best_q

            # 阶段 1：不缩尺寸，二分查找最高质量
            buf, q = _binary_search_quality(img)
            if buf is not None:
                with open(file_path, 'wb') as f:
                    f.write(buf.getvalue())
                logger.info(f"压缩完成: {os.path.basename(file_path)} quality={q}, {os.path.getsize(file_path)/1024/1024:.1f}MB (原图尺寸)")
                return True

            # 阶段 2：逐步缩尺寸，每次在当前尺寸下二分查找最高质量
            w, h = orig_w, orig_h
            min_dim = 400  # 最小边长下限
            while min(w, h) > min_dim:
                w = int(w * 0.9)
                h = int(h * 0.9)
                img_resized = img.resize((w, h), Image.LANCZOS)
                buf, q = _binary_search_quality(img_resized)
                if buf is not None:
                    # 如果文件扩展名不是 .jpg，需要改名为 .jpg
                    if not file_path.lower().endswith(('.jpg', '.jpeg')):
                        os.remove(file_path)
                        file_path = os.path.splitext(file_path)[0] + '.jpg'
                    with open(file_path, 'wb') as f:
                        f.write(buf.getvalue())
                    logger.info(f"压缩完成: {os.path.basename(file_path)} {w}x{h} quality={q}, {os.path.getsize(file_path)/1024/1024:.1f}MB")
                    return True

            logger.warning(f"压缩后仍超限: {os.path.basename(file_path)} {os.path.getsize(file_path)/1024/1024:.1f}MB")
            return False
        except Exception as e:
            logger.warning(f"压缩失败 {file_path}: {e}")
            return False

    # ─── MD 文档生成 ───────────────────────────────────────────

    def _generate_md(self, illusts: list['Illust'], batch_dir: str, image_paths: dict[int, list[str]]) -> str:
        """生成 MD 推送清单"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 统计来源
        source_counts = {}
        for ill in illusts:
            src = getattr(ill, 'source', 'unknown')
            source_counts[src] = source_counts.get(src, 0) + 1
        source_str = " + ".join(f"{name}({count})" for name, count in source_counts.items())

        lines = [
            "# Pixiv 每日推荐",
            f"- 推送时间: {now_str}",
            f"- 来源: {source_str}",
            "",
        ]

        for ill in illusts:
            paths = image_paths.get(ill.id, [])
            r18_mark = " 🔞" if ill.is_r18 else ""
            lines.append("---")
            lines.append("")
            lines.append(f"## {ill.title}{r18_mark}")
            lines.append("")
            lines.append(f"**作品ID:** {ill.id}")
            lines.append(f"**画师:** {ill.user_name} (ID: {ill.user_id})")
            lines.append(f"**收藏:** {ill.bookmark_count:,}")
            tags_str = " ".join(f"`#{t}`" for t in ill.tags[:8])
            lines.append(f"**标签:** {tags_str}")
            lines.append("")
            lines.append("**图片:**")
            for i, p in enumerate(paths, 1):
                lines.append(f"{i}. `{p}`")
            lines.append("")

        return "\n".join(lines)

    # ─── cron dispatch ────────────────────────────────────────
    # ─── cron dispatch ────────────────────────────────────────
    # 归档 latest/ 到 history/（可选），然后触发 cron run

    def _submit_to_agent(self, md_path: str) -> bool:
        """归档 latest/ → 触发 cron dispatch"""
        push_data = os.path.join(self.download_dir, "push_data")
        latest_dir = os.path.join(push_data, "latest")

        # 保留历史记录（从 latest/ 复制到 history/）
        if self.keep_history:
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                history_dir = os.path.join(push_data, "history", today)
                os.makedirs(history_dir, exist_ok=True)
                target = os.path.join(history_dir, ts)
                shutil.copytree(latest_dir, target)
                logger.info(f"归档到: {target}")
            except Exception as e:
                logger.warning(f"归档失败: {e}")

        # 触发 cron dispatch
        try:
            result = subprocess.run(
                [self.qwenpaw_bin, "cron", "run",
                 "--agent-id", "default",
                 self.cronid],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.error(f"cron run 失败: {result.stderr.strip()}")

        except Exception as e:
            logger.warning(f"cron run 异常: {e}")

        return True

    # ─── BaseNotifier 接口实现 ────────────────────────────────

    def format_message(self, illust: 'Illust') -> str:
        """单作品格式化（用于 send_text 等场景）"""
        r18_mark = "🔞 " if illust.is_r18 else ""
        tags = " ".join(f"#{t}" for t in illust.tags[:5])
        return (
            f"{r18_mark}{illust.title}\n"
            f"👤 {illust.user_name}\n"
            f"❤️ {illust.bookmark_count:,}\n"
            f"🏷️ {tags}\n"
            f"🔗 https://pixiv.net/i/{illust.id}"
        )

    async def send(self, illusts: list['Illust']) -> list[int]:
        """发送推送：下载图片 → 生成 MD → 提交给 Agent"""
        if not illusts:
            return []

        total_works = len(illusts)
        logger.info(f"开始推送 {total_works} 个作品")

        latest_dir = os.path.join(self.download_dir, "push_data", "latest")
        if os.path.exists(latest_dir):
            shutil.rmtree(latest_dir)
        os.makedirs(latest_dir, exist_ok=True)

        image_paths: dict[int, list[str]] = {}
        download_tasks = {}
        for ill in illusts:
            download_tasks[ill.id] = self._download_illust_images(ill, latest_dir)

        results = await asyncio.gather(*download_tasks.values())
        for ill_id, paths in zip(download_tasks.keys(), results):
            if paths:
                image_paths[ill_id] = paths

        md_content = self._generate_md(illusts, latest_dir, image_paths)
        md_path = os.path.join(latest_dir, "push_report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        success = self._submit_to_agent(md_path)

        if success:
            success_ids = [ill.id for ill in illusts]
            logger.info(f"QwenPaw 推送完成: {len(success_ids)}/{total_works}")
            return success_ids
        else:
            logger.warning(f"推送失败，目录保留: {latest_dir}")
            logger.error("QwenPaw 推送失败：cron dispatch 调用出错")
            return []

    async def clear_data(self) -> bool:
        """清空所有推送数据（临时批次 + 历史记录）"""
        try:
            if not os.path.exists(self.download_dir):
                logger.info("推送目录不存在，无需清理")
                return True

            removed = 0
            for entry in os.listdir(self.download_dir):
                entry_path = os.path.join(self.download_dir, entry)
                if os.path.isdir(entry_path):
                    shutil.rmtree(entry_path, ignore_errors=True)
                    removed += 1
                else:
                    os.remove(entry_path)
                    removed += 1

            logger.info(f"已清空推送数据: 共删除 {removed} 项")
            return True
        except Exception as e:
            logger.error(f"清空推送数据失败: {e}")
            return False

    async def send_text(self, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        """发送文本消息（走 qwenpaw channels send CLI，仅文字）"""
        try:
            full_text = text
            if buttons:
                btn_text = "\n".join(f"• {label}" for label, _ in buttons)
                full_text += f"\n\n{btn_text}"

            result = subprocess.run(
                [
                    "qwenpaw", "channels", "send",
                    "--agent-id", self.agent_id,
                    "--channel", "qq",
                    "--target-user", self.target_user,
                    "--target-session", self.target_session,
                    "--text", full_text,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"send_text 失败: {e}")
            return False
