---
name: pixiv-nl-bridge
description: "通过自然语言与 Pixiv-XP-Pusher 交互，支持 Pixiv 插画搜索、收藏管理、排行榜、画师关注动态、下载原图、查看XP画像等功能。所有操作通过 nl_bridge.py 桥接脚本执行。"
metadata:
  emoji: "🎨"
---

# 🎨 Pixiv NL Bridge 技能

通过自然语言操作 Pixiv-XP-Pusher，Agent 将用户的意图映射到 `nl_bridge.py` 的命令行参数，通过执行脚本并返回结果。

---

## 环境要求

- Python 3.10+
- Pixiv-XP-Pusher 项目已部署且依赖已安装
- `nl_bridge.py` 位于项目根目录

**调用方式**：

```bash
# 进入项目目录后执行
python nl_bridge.py <command> [args]
```

> 实际使用时请替换 `python` 为项目对应的 Python 解释器路径（如 conda 环境的 python 绝对路径）。

---

## 命令一览

### 查询类

| 命令 | 说明 | 示例用法 |
|------|------|---------|
| `profile` | 查看XP画像（收藏统计、标签偏好、活跃时间等） | `nl_bridge.py profile` |
| `history` | 查看推送历史记录 | `nl_bridge.py history [N]` |
| `stats` | 查看收藏统计（总数、每日/每周/每月新增） | `nl_bridge.py stats` |
| `status` | 查看系统状态（token 有效性、调度配置、配额） | `nl_bridge.py status` |
| `blacklist` | 查看/管理黑名单标签 | `nl_bridge.py blacklist` |
| `search` | 搜索作品（支持指定收藏数阈值、过滤 R18） | `nl_bridge.py search "初音ミク" --limit 15 --bookmarks 50` |
| `illust` | 查看作品详情 | `nl_bridge.py illust <ID>` |
| `config` | 查看/修改配置 | `nl_bridge.py config --set key=val` |
| `ranking` | 查看排行榜（支持日/周/月/新人/原创） | `nl_bridge.py ranking [--mode daily\|weekly\|monthly\|rookie\|original]` |
| `related` | 查看相关作品推荐 | `nl_bridge.py related <ID>` |
| `following` | 查看关注画师最新作品 | `nl_bridge.py following` |
| `mybookmarks` | 查看已收藏作品列表 | `nl_bridge.py mybookmarks [--limit N]` |
| `download` | 下载作品原图 | `nl_bridge.py download <ID>` |

### 操作类

| 命令 | 说明 | 示例用法 |
|------|------|---------|
| `bookmark` | 收藏指定作品 | `nl_bridge.py bookmark <ID>` |
| `unbookmark` | 取消收藏指定作品 | `nl_bridge.py unbookmark <ID>` |
| `blacklist` | 添加/移除黑名单标签 | `nl_bridge.py blacklist --add <TAG>` / `--remove <TAG>` |
| `schedule` | 查看/修改推送调度 | `nl_bridge.py schedule [--cron "0 */6 * * *"]` |
| `config --set` | 修改配置项 | `nl_bridge.py config --set notifier.qwenpaw.cronid="xxx"` |
| `push` | 触发手动推送 | `nl_bridge.py push` |

---

## Agent 使用指南

### 搜索作品

当用户要求搜索 Pixiv 插画时：

1. 确定用户意向的关键词（日文/中文/英文均可）
2. 调用 search 命令，必要时添加 `--bookmarks` 阈值过滤低质量结果
3. 默认不过滤 R18，若用户需要屏蔽 R18 内容则添加 `--r18` 参数
4. 呈现结果给用户：作品标题、画师、收藏数、页数、标签

```bash
# 基础搜索
nl_bridge.py search "初音ミク" --limit 10

# 设置收藏数阈值
nl_bridge.py search "初音ミク" --limit 15 --bookmarks 50

# 过滤掉 R18 作品
nl_bridge.py search "初音ミク" --limit 15 --r18

# 查看搜索结果中的某幅作品详情
nl_bridge.py illust 147062953
```

### 展示图片

用户要求看某幅/某几幅作品时：

1. 先用 `download <ID>` 下载原图到项目 `data/downloads/` 目录
2. 通过文件发送能力将图片发送给用户
3. 先发文字介绍，再发图片（部分平台限制图文无法合并到同一消息）
4. 注意图片大小限制 ~10MB，超限需压缩
5. 单次响应中图片数量限制约 14 张

```bash
# 下载作品
nl_bridge.py download 147062953
```

### 收藏管理

用户要求收藏/取消收藏时：

```bash
# 收藏
nl_bridge.py bookmark 147058704

# 取消收藏
nl_bridge.py unbookmark 147058704
```

### 排行榜

用户问"今天有什么热门"时，优先使用 ranking：

```bash
# 默认日榜
nl_bridge.py ranking

# 周榜
nl_bridge.py ranking --mode weekly

# 月榜
nl_bridge.py ranking --mode monthly

# 新人榜
nl_bridge.py ranking --mode rookie

# 原创榜
nl_bridge.py ranking --mode original
```

### 画师相关

```bash
# 关注画师新作
nl_bridge.py following

# 相关作品推荐
nl_bridge.py related <ID>
```

### 我的收藏

```bash
# 查看收藏列表（默认最近20个）
nl_bridge.py mybookmarks

# 指定数量
nl_bridge.py mybookmarks --limit 50
```

### XP 画像与统计

```bash
# XP 画像（标签偏好、画师偏好、活跃时间分布）
nl_bridge.py profile

# 收藏统计
nl_bridge.py stats

# 系统状态
nl_bridge.py status
```

---

## 输出说明

- 所有命令输出为 Markdown 格式文本，Agent 可直接转发给用户
- 搜索结果中标注 R18 作品数量，可通过 `--r18` 参数过滤掉 R18
- 错误信息（token 过期、网络错误等）以中文输出

---

## 常见问题

### 超时
下载图片或搜索时若超时，可：
- 单次只下载 1 幅作品
- 增大命令执行的超时时间

### token 过期
若返回 `Pixiv 登录失败`，需要用户提供新的 refresh_token 更新到 config.yaml。
