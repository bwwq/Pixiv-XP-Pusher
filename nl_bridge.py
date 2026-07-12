#!/usr/bin/env python3
"""
Pixiv Natural Language Bridge
通过 Agent 的自然语言理解，操作 Pixiv-XP-Pusher 的各项能力。

用法:
  python nl_bridge.py profile             查看XP画像
  python nl_bridge.py history [N]          查看推送历史
  python nl_bridge.py stats                查看收藏统计
  python nl_bridge.py status               查看系统状态
  python nl_bridge.py blacklist            查看黑名单
  python nl_bridge.py blacklist --add TAG  添加黑名单标签
  python nl_bridge.py blacklist --remove TAG  移除黑名单标签
  python nl_bridge.py search KEYWORD       搜索作品
  python nl_bridge.py search KEYWORD --limit N  指定数量
  python nl_bridge.py push                 触发推送
  python nl_bridge.py bookmark ID          收藏作品
  python nl_bridge.py unbookmark ID        取消收藏
  python nl_bridge.py illust ID            查看作品详情
  python nl_bridge.py schedule             查看调度
  python nl_bridge.py schedule --cron ...  修改调度
  python nl_bridge.py config               查看配置概览
  python nl_bridge.py config --set key=val 修改配置
  python nl_bridge.py ranking              每日排行榜
  python nl_bridge.py ranking --mode weekly  周榜
  python nl_bridge.py related ID           相关作品
  python nl_bridge.py following            关注画师新作
  python nl_bridge.py mybookmarks          我的收藏列表
  python nl_bridge.py download ID          下载原图
"""
import argparse
import asyncio
import json
import os
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

# 确保项目根目录在路径中
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# 禁用日志（避免干扰 CLI 输出）
logging.disable(logging.CRITICAL)

# ---- 导入项目模块 ----
from config import load_config, CONFIG_PATH
import database as db
from pixiv_client import PixivClient, Illust
from profiler import XPProfiler
from utils import normalize_tag

# 切换到项目根目录
os.chdir(str(PROJECT_ROOT))

CONFIG = load_config(CONFIG_PATH)


# ============ 工具函数 ============

async def get_pixiv_client() -> PixivClient:
    """创建并登录 PixivClient"""
    pixiv_cfg = CONFIG.get("pixiv", {})
    refresh_token = pixiv_cfg.get("refresh_token", "")
    if not refresh_token:
        print("错误: config.yaml 中未配置 refresh_token")
        sys.exit(1)
    proxy = pixiv_cfg.get("proxy", CONFIG.get("notifier", {}).get("qwenpaw", {}).get("proxy_url", ""))
    client = PixivClient(refresh_token=refresh_token, proxy_url=proxy)
    ok = await client.login()
    if not ok:
        print("错误: Pixiv 登录失败，token 可能已过期")
        sys.exit(1)
    return client


async def init_database():
    """初始化数据库"""
    await db.init_db()


def format_illust(ill: Illust) -> str:
    """格式化作品信息为 Markdown"""
    tags = ", ".join(f"`#{t}`" for t in (ill.tags or [])[:8])
    return (
        f"**{ill.title}** (ID: {ill.id})\n"
        f"画师: {ill.user_name} (ID: {ill.user_id})\n"
        f"收藏: {ill.bookmark_count} | 页数: {ill.page_count}\n"
        f"标签: {tags}\n"
    )


# ============ 子命令实现 ============

async def cmd_profile(args):
    """查看XP画像"""
    await init_database()
    profile = await db.get_xp_profile()
    if not profile:
        print("❌ XP 画像为空，尚未生成画像数据")
        return

    # 按权重排序，取前20
    sorted_tags = sorted(profile.items(), key=lambda x: x[1], reverse=True)

    print(f"## 🎯 XP 画像\n")
    print(f"共 {len(sorted_tags)} 个标签 | 更新时间: 最近一次推送后更新\n")

    # 按权重分段显示
    weights = [w for _, w in sorted_tags]
    if weights:
        max_w = max(weights)
        for tag, weight in sorted_tags[:20]:
            bar_len = int((weight / max_w) * 20)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            print(f"  `{tag}` {bar} {weight:.2f}")

    # 统计信息
    if len(sorted_tags) > 20:
        print(f"\n  ... 及其他 {len(sorted_tags) - 20} 个标签")


async def cmd_history(args):
    """查看推送历史"""
    await init_database()
    limit = args.limit if args.limit else 10
    records, total = await db.get_push_history_paginated(limit=limit)

    if not records:
        print("暂无推送历史记录")
        return

    print(f"## 📋 推送历史 (最近 {len(records)}/{total} 条)\n")
    for i, r in enumerate(records, 1):
        pushed_at = r.get("pushed_at", "")
        if pushed_at:
            try:
                dt = datetime.fromisoformat(pushed_at)
                pushed_at = dt.strftime("%m-%d %H:%M")
            except:
                pass
        print(f"**{i}.** ID: `{r['illust_id']}` | {pushed_at}")
        if r.get("title"):
            print(f"  标题: {r['title']}")
        if r.get("tags"):
            tags = ", ".join(f"`#{t}`" for t in r["tags"][:5])
            print(f"  标签: {tags}")
        print(f"  来源: {r.get('source', '未知')}")
        print()


async def cmd_stats(args):
    """查看收藏统计"""
    await init_database()

    # 获取画像数据做标签统计
    profile = await db.get_xp_profile()

    # 获取收藏总数
    pixiv_cfg = CONFIG.get("pixiv", {})
    user_id = pixiv_cfg.get("user_id", 0)

    client = await get_pixiv_client()
    try:
        bookmarks = await client.get_bookmarks(user_id, limit=1)
        total_bookmarks = bookmarks.total_count if hasattr(bookmarks, 'total_count') else "?"
    except:
        total_bookmarks = "?"
    finally:
        await client.close()

    print(f"## 📊 收藏统计\n")
    print(f"| 项目 | 数值 |")
    print(f"|------|------|")
    print(f"| Pixiv 收藏总数 | {total_bookmarks} |")

    if profile:
        print(f"| 画像标签数 | {len(profile)} |")
        top_tags = sorted(profile.items(), key=lambda x: x[1], reverse=True)[:10]
        top_str = ", ".join(f"`{t}`" for t, _ in top_tags)
        print(f"| 热门标签 | {top_str} |")

    # 数据库统计
    blacklist = await db.get_blacklisted_tags()
    if blacklist:
        bl_str = ", ".join(f"`{t}`" for t in sorted(blacklist))
        print(f"| 黑名单标签 | {bl_str} |")

    push_count = await db.get_push_history_paginated(limit=1)
    print(f"| 推送历史总数 | {push_count[1]} |")


async def cmd_status(args):
    """查看系统状态"""
    print(f"## 🔧 系统状态\n")

    # Pixiv 登录状态
    client = await get_pixiv_client()
    user_id = CONFIG.get("pixiv", {}).get("user_id", "?")
    print(f"**Pixiv**")
    print(f"- User ID: {user_id}")
    print(f"- Token: {'✓ 有效' if client else '✗ 无效'}")
    await client.close()

    # 数据库
    await init_database()
    db_path = PROJECT_ROOT / "data" / "pixiv_xp.db"
    if db_path.exists():
        size_mb = db_path.stat().st_size / 1024 / 1024
        print(f"\n**数据库**")
        print(f"- 路径: `{db_path}`")
        print(f"- 大小: {size_mb:.1f} MB")
    else:
        print(f"\n**数据库**: ✗ 未找到")

    # 推送配置
    notifier_cfg = CONFIG.get("notifier", {})
    notifier_types = notifier_cfg.get("types") or [notifier_cfg.get("type", "telegram")]
    print(f"\n**推送配置**")
    print(f"- 渠道: {', '.join(notifier_types)}")

    cron = CONFIG.get("scheduler", {}).get("cron", "未配置")
    print(f"- 调度: `{cron}`")

    daily_limit = CONFIG.get("filter", {}).get("daily_limit", 15)
    print(f"- 单次上限: {daily_limit} 幅")

    exclude_ai = CONFIG.get("filter", {}).get("exclude_ai", True)
    r18_mode = CONFIG.get("filter", {}).get("r18_mode", "safe")
    print(f"- AI过滤: {'开' if exclude_ai else '关'} | R18: {r18_mode}")

    # 推送数据目录
    push_dir = PROJECT_ROOT / "data" / "push_data" / "latest"
    if push_dir.exists():
        files = list(push_dir.iterdir())
        md_files = [f for f in files if f.suffix == ".md"]
        img_files = [f for f in files if f.suffix in (".png", ".jpg", ".jpeg", ".gif")]
        print(f"\n**待推送缓存**")
        print(f"- 报告: {'有' if md_files else '无'}")
        print(f"- 图片: {len(img_files)} 张")


async def cmd_blacklist(args):
    """查看/编辑黑名单"""
    await init_database()

    if args.add:
        tag = normalize_tag(args.add)
        # 将标签写入 config 的 blacklist_tags
        cfg_path = PROJECT_ROOT / "config.yaml"
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        bl = cfg.get("filter", {}).get("blacklist_tags", [])
        if tag not in bl:
            bl.append(tag)
            cfg.setdefault("filter", {})["blacklist_tags"] = bl
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            print(f"✅ 已将 `{tag}` 加入黑名单")
        else:
            print(f"`{tag}` 已在黑名单中")
        return

    if args.remove:
        tag = normalize_tag(args.remove)
        cfg_path = PROJECT_ROOT / "config.yaml"
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        bl = cfg.get("filter", {}).get("blacklist_tags", [])
        if tag in bl:
            bl.remove(tag)
            cfg.setdefault("filter", {})["blacklist_tags"] = bl
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            print(f"✅ 已将 `{tag}` 移出黑名单")
        else:
            print(f"`{tag}` 不在黑名单中")
        return

    # 查看黑名单
    bl = await db.get_blacklisted_tags()
    cfg_bl = CONFIG.get("filter", {}).get("blacklist_tags", [])
    all_bl = set(bl) | set(cfg_bl)

    if not all_bl:
        print("📭 黑名单为空")
        return

    print(f"## 🚫 黑名单标签 ({len(all_bl)} 个)\n")
    for tag in sorted(all_bl):
        source = "配置" if tag in cfg_bl else "数据库"
        print(f"- `{tag}` ({source})")


async def cmd_search(args):
    """搜索作品"""
    if not args.keyword:
        print("错误: 请指定搜索关键词")
        return

    client = await get_pixiv_client()
    try:
        results = await client.search_illusts(
            tags=[args.keyword],
            bookmark_threshold=args.bookmarks,
            limit=min(args.limit, 30)
        )
    finally:
        await client.close()

    if not results:
        print(f"未找到与「{args.keyword}」相关的作品")
        return

    # 仅在 --r18 时过滤 R18
    if args.r18:
        results = [ill for ill in results if not ill.is_r18]

    display = results[:args.limit]

    r18_count = sum(1 for ill in results if ill.is_r18)
    r18_hint = f"，含 {r18_count} 个 R18" if r18_count else ""
    print(f"## 🔍 搜索「{args.keyword}」 ({len(display)} 个结果{r18_hint})\n")
    for i, ill in enumerate(display, 1):
        print(f"**{i}.** {format_illust(ill)}")
        print()


async def cmd_push(args):
    """触发推送"""
    print("🔄 正在触发推送任务...")
    print("   (运行中，请耐心等待 Pixiv 数据获取)\n")

    # 动态导入 main 模块
    import importlib
    main_mod = importlib.import_module("main")

    try:
        await main_mod.run_once(CONFIG)
        print(f"\n✅ 推送任务执行完毕")
    except Exception as e:
        print(f"\n❌ 推送失败: {e}")
        import traceback
        traceback.print_exc()


async def cmd_bookmark(args):
    """收藏作品"""
    if not args.id:
        print("错误: 请指定作品 ID")
        return

    client = await get_pixiv_client()
    try:
        # 先查看作品信息
        ill = await client.get_illust_detail(args.id)
        if not ill:
            print(f"❌ 作品 {args.id} 不存在或已删除")
            return

        print(f"📌 即将收藏：\n")
        print(format_illust(ill))

        private = args.private
        tags = args.tags.split(",") if args.tags else None
        ok = await client.add_bookmark(args.id, private=private, tags=tags)

        if ok:
            print(f"✅ 已收藏作品 {args.id}")
        else:
            print(f"❌ 收藏失败")
    finally:
        await client.close()


async def cmd_illust(args):
    """查看作品详情"""
    if not args.id:
        print("错误: 请指定作品 ID")
        return

    client = await get_pixiv_client()
    try:
        ill = await client.get_illust_detail(args.id)
        if not ill:
            print(f"❌ 作品 {args.id} 不存在或已删除")
            return

        print(f"## 🖼 作品详情\n")
        print(format_illust(ill))

        # 图片链接
        print(f"**图片链接:**")
        for i, url in enumerate(ill.image_urls or []):
            print(f"- [Page {i+1}]({url})")

        # 创建日期
        if ill.create_date:
            print(f"\n**创建日期:** {ill.create_date.strftime('%Y-%m-%d')}")

        if ill.ai_type == 2:
            print(f"\n🤖 **AI生成作品**")
        elif ill.ai_type == 1:
            print(f"\n🎨 **辅助AI作品**")

        if ill.is_r18:
            print(f"\n🔞 **R-18 作品**")
    finally:
        await client.close()


async def cmd_ranking(args):
    """查看排行榜"""
    # 用户友好名→API名
    mode_map = {
        "daily": "day", "weekly": "week", "monthly": "month",
        "rookie": "rookie", "original": "original",
        "day": "day", "week": "week", "month": "month",
    }
    api_mode = mode_map.get(args.mode, "day")

    client = await get_pixiv_client()
    try:
        results = await client.get_ranking(
            mode=api_mode,
            limit=min(args.limit, 30)
        )
    finally:
        await client.close()

    if not results:
        print(f"排行榜为空")
        return

    mode_names = {"day": "每日", "week": "每周", "month": "每月", "rookie": "新人", "original": "原创"}
    mode_cn = mode_names.get(api_mode, args.mode)

    print(f"## 🏆 Pixiv {mode_cn}排行榜 (Top {len(results)})\n")
    for i, ill in enumerate(results[:args.limit], 1):
        r18 = "🔞 " if ill.is_r18 else ""
        ai = "🤖 " if ill.ai_type == 2 else "🎨 " if ill.ai_type == 1 else ""
        print(f"**{i}.** {r18}{ai}**{ill.title}** (ID: {ill.id})")
        print(f"   画师: {ill.user_name} | 收藏: {ill.bookmark_count}")
        tags = ", ".join(f"`#{t}`" for t in (ill.tags or [])[:4])
        if tags:
            print(f"   标签: {tags}")
        print()


async def cmd_related(args):
    """查看相关作品"""
    if not args.id:
        print("错误: 请指定作品 ID")
        return

    client = await get_pixiv_client()
    try:
        results = await client.get_related_illusts(args.id)
    finally:
        await client.close()

    if not results:
        print(f"作品 {args.id} 没有相关推荐")
        return

    print(f"## 🔗 「{results[0].title}」的相关作品 ({len(results)} 个)\n")
    for i, ill in enumerate(results[:args.limit], 1):
        r18 = "🔞 " if ill.is_r18 else ""
        print(f"**{i}.** {r18}**{ill.title}** (ID: {ill.id})")
        print(f"   画师: {ill.user_name} | 收藏: {ill.bookmark_count}")
        tags = ", ".join(f"`#{t}`" for t in (ill.tags or [])[:4])
        if tags:
            print(f"   标签: {tags}")
        print()


async def cmd_following(args):
    """查看关注画师的最新作品"""
    client = await get_pixiv_client()
    try:
        results = await client.fetch_follow_latest(limit=min(args.limit, 50))
    finally:
        await client.close()

    if not results:
        print("关注的画师暂无新作品")
        return

    print(f"## 📡 关注画师最新作品 ({len(results)} 个)\n")
    for i, ill in enumerate(results[:args.limit], 1):
        r18 = "🔞 " if ill.is_r18 else ""
        ai = "🤖 " if ill.ai_type == 2 else ""
        print(f"**{i}.** {r18}{ai}**{ill.title}** (ID: {ill.id})")
        print(f"   画师: {ill.user_name} | 收藏: {ill.bookmark_count}")
        tags = ", ".join(f"`#{t}`" for t in (ill.tags or [])[:4])
        if tags:
            print(f"   标签: {tags}")
        print()


async def cmd_bookmarks_list(args):
    """查看我的收藏列表"""
    user_id = CONFIG.get("pixiv", {}).get("user_id", 0)
    if not user_id:
        print("错误: config.yaml 中未配置 user_id")
        return

    client = await get_pixiv_client()
    try:
        result = await client.get_bookmarks(
            user_id=user_id,
            limit=min(args.limit, 50),
            tag=args.tag or None
        )
        illusts = result.illusts if hasattr(result, 'illusts') else result
        total = result.total_count if hasattr(result, 'total_count') else len(illusts)
    finally:
        await client.close()

    if not illusts:
        print("收藏列表为空")
        return

    tag_hint = f" (标签: {args.tag})" if args.tag else ""
    print(f"## 📚 我的收藏 ({len(illusts)}/{total} 页){tag_hint}\n")
    for i, ill in enumerate(illusts[:args.limit], 1):
        r18 = "🔞 " if ill.is_r18 else ""
        ai = "🤖 " if ill.ai_type == 2 else ""
        print(f"**{i}.** {r18}{ai}**{ill.title}** (ID: {ill.id})")
        print(f"   画师: {ill.user_name} | 收藏: {ill.bookmark_count}")
        tags = ", ".join(f"`#{t}`" for t in (ill.tags or [])[:4])
        if tags:
            print(f"   标签: {tags}")
        print()


async def cmd_download(args):
    """下载作品原图"""
    if not args.id:
        print("错误: 请指定作品 ID")
        return

    out_dir = PROJECT_ROOT / "data" / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)

    client = await get_pixiv_client()
    try:
        ill = await client.get_illust_detail(args.id)
        if not ill:
            print(f"❌ 作品 {args.id} 不存在或已删除")
            return

        print(f"📥 正在下载: {ill.title}\n")

        # 下载所有页面
        downloaded = []
        for i, url in enumerate(ill.image_urls or []):
            ext = os.path.splitext(url.split("?")[0])[1] or ".png"
            out_path = out_dir / f"{args.id}_{i}{ext}"

            data = await client.download_image(url)
            with open(out_path, "wb") as f:
                f.write(data)
            downloaded.append(str(out_path))
            print(f"  [{i+1}/{len(ill.image_urls)}] {out_path.name} ({len(data)/1024:.0f} KB)")

        print(f"\n✅ 下载完成: {len(downloaded)} 张图片")
        print(f"   保存到: {out_dir}")
    finally:
        await client.close()


async def cmd_unbookmark(args):
    """取消收藏"""
    if not args.id:
        print("错误: 请指定作品 ID")
        return

    client = await get_pixiv_client()
    try:
        await client.api.illust_bookmark_delete(illust_id=args.id)
        print(f"✅ 已取消收藏作品 {args.id}")
    except Exception as e:
        print(f"❌ 取消收藏失败: {e}")
    finally:
        await client.close()


async def cmd_schedule(args):
    """查看/修改调度"""
    import yaml

    cfg_path = PROJECT_ROOT / "config.yaml"

    if args.cron:
        # 修改调度
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg.setdefault("scheduler", {})["cron"] = args.cron
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        print(f"✅ 调度已更新为: `{args.cron}`")
        print(f"   (下次定时任务将按新时间运行)")
        return

    # 查看调度
    cron_str = CONFIG.get("scheduler", {}).get("cron", "未配置")
    next_run = "(暂停中，仅手动触发)"
    print(f"## ⏰ 推送调度\n")
    print(f"| 项 | 值 |")
    print(f"|----|-----|")
    print(f"| 表达式 | `{cron_str}` |")
    print(f"| 含义 | {_explain_cron(cron_str)} |")
    print(f"| 下次执行 | {next_run} |")


def _explain_cron(cron_str: str) -> str:
    """解析 cron 表达式为中文说明"""
    parts = cron_str.strip().split()
    if len(parts) != 5:
        return cron_str
    minute, hour, dom, month, dow = parts
    parts_desc = []
    if minute != "*":
        parts_desc.append(f"第{minute}分")
    if hour != "*":
        parts_desc.append(f"第{hour}时")
    if dom != "*" and month == "*" and dow == "*":
        parts_desc.append(f"每月{dom}日")
    elif dom == "*" and month == "*" and dow != "*":
        days = {"0": "周日","1": "周一","2": "周二","3": "周三","4": "周四","5": "周五","6": "周六"}
        parts_desc.append(f"每{days.get(dow, dow)}")
    if month != "*":
        parts_desc.append(f"{month}月")
    if all(p == "*" for p in parts):
        return "每分钟"
    return " ".join(parts_desc) if parts_desc else cron_str


async def cmd_config(args):
    """查看/修改配置"""
    import yaml

    if args.set:
        key = args.set
        if "=" not in key:
            print("错误: 格式应为 key=value，如 filter.daily_limit=20")
            return
        k, v = key.split("=", 1)
        parts = k.strip().split(".")

        cfg_path = PROJECT_ROOT / "config.yaml"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        target = cfg
        for p in parts[:-1]:
            if p not in target:
                target[p] = {}
            target = target[p]

        last_key = parts[-1]
        old_val = target.get(last_key)
        if isinstance(old_val, bool):
            v = v.lower() in ("true", "1", "yes")
        elif isinstance(old_val, int):
            try:
                v = int(v)
            except:
                pass
        elif isinstance(old_val, float):
            try:
                v = float(v)
            except:
                pass

        target[last_key] = v

        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

        print(f"✅ 已更新 `{k.strip()}`: `{old_val}` → `{v}`")
        return

    section = args.section
    if section:
        parts = section.split(".")
        val = CONFIG
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p, {})
            else:
                val = "N/A"
                break
        print(json.dumps(val, ensure_ascii=False, indent=2))
    else:
        print(f"## ⚙️ 配置概览\n")
        print(f"| 项 | 值 |")
        print(f"|----|-----|")
        print(f"| Pixiv User ID | {CONFIG.get('pixiv', {}).get('user_id', '?')} |")
        scheduler = CONFIG.get("scheduler", {})
        print(f"| 推送调度 | `{scheduler.get('cron', '?')}` |")
        filter_cfg = CONFIG.get("filter", {})
        print(f"| 单次上限 | {filter_cfg.get('daily_limit', 15)} |")
        print(f"| 模式 | {filter_cfg.get('r18_mode', 'safe')} |")
        print(f"| AI 过滤 | {'开' if filter_cfg.get('exclude_ai', True) else '关'} |")
        notifier = CONFIG.get("notifier", {})
        types = notifier.get('types', ['?'])
        print(f"| 推送渠道 | {', '.join(types)} |")
        print(f"| 画像 AI | {CONFIG.get('profiler', {}).get('ai', {}).get('model', '?')} |")


# ============ 主入口 ============

def main():
    parser = argparse.ArgumentParser(
        description="Pixiv Natural Language Bridge - 通过自然语言操作 Pixiv-XP-Pusher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python nl_bridge.py profile                       查看XP画像
  python nl_bridge.py history 5                     最近5条推送
  python nl_bridge.py stats                         收藏统计
  python nl_bridge.py status                        系统状态
  python nl_bridge.py blacklist                     查看黑名单
  python nl_bridge.py blacklist --add R-18G         屏蔽标签
  python nl_bridge.py search "初音ミク"              搜索作品
  python nl_bridge.py illust 123456                  查看作品详情
  python nl_bridge.py bookmark 123456                收藏作品
  python nl_bridge.py unbookmark 123456              取消收藏
  python nl_bridge.py schedule                      查看调度
  python nl_bridge.py schedule --cron "0 8 * * *"   修改调度
  python nl_bridge.py config                         查看配置概览
  python nl_bridge.py config --set filter.daily_limit=20  修改配置
  python nl_bridge.py ranking                       每日排行榜
  python nl_bridge.py ranking --mode weekly         周榜
  python nl_bridge.py related 123456                相关作品
  python nl_bridge.py following                     关注画师新作
  python nl_bridge.py mybookmarks                   我的收藏列表
  python nl_bridge.py mybookmarks --tag "初音ミク"  按标签筛选
  python nl_bridge.py download 123456               下载原图
        """
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("profile", help="查看XP画像")

    p_history = sub.add_parser("history", help="查看推送历史")
    p_history.add_argument("limit", nargs="?", type=int, default=10, help="显示条数")

    sub.add_parser("stats", help="查看收藏统计")
    sub.add_parser("status", help="查看系统状态")

    p_bl = sub.add_parser("blacklist", help="查看/编辑黑名单")
    p_bl.add_argument("--add", type=str, default="", help="添加标签到黑名单")
    p_bl.add_argument("--remove", type=str, default="", help="从黑名单移除标签")

    p_search = sub.add_parser("search", help="搜索作品")
    p_search.add_argument("keyword", type=str, help="搜索关键词")
    p_search.add_argument("--limit", type=int, default=10, help="返回数量")
    p_search.add_argument("--bookmarks", type=int, default=0, help="最低收藏数")
    p_search.add_argument("--r18", action="store_true", help="过滤掉 R18 作品")

    sub.add_parser("push", help="触发推送")

    p_bm = sub.add_parser("bookmark", help="收藏作品")
    p_bm.add_argument("id", type=int, help="作品ID")
    p_bm.add_argument("--private", action="store_true", help="私密收藏")
    p_bm.add_argument("--tags", type=str, default="", help="添加标签（逗号分隔）")

    p_ub = sub.add_parser("unbookmark", help="取消收藏")
    p_ub.add_argument("id", type=int, help="作品ID")

    p_ill = sub.add_parser("illust", help="查看作品详情")
    p_ill.add_argument("id", type=int, help="作品ID")

    p_sch = sub.add_parser("schedule", help="查看/修改调度")
    p_sch.add_argument("--cron", type=str, default="", help="设置 cron 表达式，如 '0 8 * * *'")

    p_cfg = sub.add_parser("config", help="查看/修改配置")
    p_cfg.add_argument("section", nargs="?", type=str, default="", help="配置段落，如 filter.daily_limit")
    p_cfg.add_argument("--set", type=str, default="", help="设置配置项，如 filter.daily_limit=20")

    p_rank = sub.add_parser("ranking", help="查看排行榜")
    p_rank.add_argument("--mode", type=str, default="daily", choices=["daily", "weekly", "monthly", "rookie", "original", "day", "week", "month"], help="排行模式")
    p_rank.add_argument("--limit", type=int, default=10, help="返回数量")

    p_rel = sub.add_parser("related", help="查看相关作品")
    p_rel.add_argument("id", type=int, help="作品ID")
    p_rel.add_argument("--limit", type=int, default=10, help="返回数量")

    p_fol = sub.add_parser("following", help="关注画师最新作品")
    p_fol.add_argument("--limit", type=int, default=10, help="返回数量")

    p_my = sub.add_parser("mybookmarks", help="我的收藏列表")
    p_my.add_argument("--limit", type=int, default=10, help="返回数量")
    p_my.add_argument("--tag", type=str, default="", help="按标签筛选")

    p_dl = sub.add_parser("download", help="下载原图")
    p_dl.add_argument("id", type=int, help="作品ID")

    args = parser.parse_args()

    cmd_map = {
        "profile": cmd_profile,
        "history": cmd_history,
        "stats": cmd_stats,
        "status": cmd_status,
        "blacklist": cmd_blacklist,
        "search": cmd_search,
        "push": cmd_push,
        "bookmark": cmd_bookmark,
        "unbookmark": cmd_unbookmark,
        "illust": cmd_illust,
        "ranking": cmd_ranking,
        "related": cmd_related,
        "following": cmd_following,
        "mybookmarks": cmd_bookmarks_list,
        "download": cmd_download,
        "schedule": cmd_schedule,
        "config": cmd_config,
    }

    async def run():
        await cmd_map[args.command](args)

    asyncio.run(run())


if __name__ == "__main__":
    main()
