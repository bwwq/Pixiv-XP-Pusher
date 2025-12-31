# Pixiv-XP-Pusher

> 🎨 **智能 XP 捕获与推送系统**
> 
> 基于用户收藏自动分析 XP（性癖/兴趣偏好）画像，全网搜索并智能推送最懂你的 Pixiv 插画。支持 Telegram / QQ (OneBot) 多渠道推送。

---

## ✨ 核心特性

- 🧠 **XP 画像构建** - 深度分析你的收藏夹，提取核心 Tag 权重，比你更懂你的口味
- 🤖 **AI 智能清洗** - (可选) 集成 LLM (OpenAI/DeepSeek) 过滤无意义标签、归并同义词，构建精准画像
- 🔍 **混合搜索策略** - 
  - **XP 搜索**: 基于画像权重的多标签组合搜索
  - **关联推荐**: [New!] 自动发掘高分作品的"相似作品"，发现潜在 XP
  - **画师订阅**: 自动追踪关注画师的最新作品
  - **排行榜**: 每日/每周排行榜筛选
- 🧬 **反馈闭环** - [New!]
  - **连锁反应**: 点击 Like ❤️ 立即推送关联作品，自动回复形成消息链 (单图深度可控)，好图停不下来
  - **画师权重**: 反馈直接影响画师评分，喜欢的画师会更常出现
  - **智能屏蔽**: 厌恶达到阈值仅提示确认，尊重用户选择
- 🎭 **智能过滤** - 
  - **R-18 混合模式**: 支持 纯R18 / 净网 / 混合模式 三档调节
  - 多维度去重（ID/图片指纹）
  - AI 作画检测与过滤
  - 动态阈值（热门 Tag 需高收藏，冷门 Tag 宽容度高）
- 📱 **多渠道推送** - 
  - **Telegram**: 
    - 支持 MediaGroup 图集、直传图片（防防盗链/被墙）
    - **交互式指令**:
      - `/push` - 立即触发推送
      - `/push <ID>` - 手动推送指定作品 ID [New!]
      - `/xp` - 查看您的 XP 画像 (Top Tags)
      - `/stats` - 查看各策略 (XP搜索/订阅/榜单) 的成功率
      - `/schedule` - 查看或修改定时任务时间
      - `/block` - 快速屏蔽讨厌的标签
      - `/block_artist` - 快速屏蔽画师 ID
  - **OneBot (QQ)**: 支持 Go-CQHTTP/Lagrange，链接卡片或图文消息，多图并发下载 [New!]
- ⚙️ **完全自动化** 
  - 智能调度器，支持多时间点运行
  - **每日日报**: 每天生成 XP 变化报告与策略统计
  - **健康检查**: Web API `/health` 端点，易于接入外部监控 [New!]
- 🛠️ **懒人配置** - 提供交互式引导脚本 `launcher.py`，一键完成环境与参数配置

---

## 🚀 快速开始

### 方式一：Docker 部署 (推荐)

最简单、最稳定的运行方式。

```bash
# 1. 下载项目
git clone https://github.com/bwwq/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher

# 2. 启动服务 (自动构建并运行)
chmod +x deploy.sh
./deploy.sh start

# 3. 查看日志
./deploy.sh logs
```

- **初次启动**：容器会自动执行一次“Run Once”任务，然后进入定时调度模式。
- **管理命令**：
  - `./deploy.sh stop` - 停止服务
  - `./deploy.sh once` - 手动触发一次推送
  - `./deploy.sh reset-xp` - 清空 XP 缓存（保留收藏数据）

### 方式二：本地直接运行 (Windows/Linux)

#### 1. 环境准备
确保已安装 Python 3.10+。

```bash
# 安装依赖
pip install -r requirements.txt
```

#### 2. 交互式配置 (小白推荐)
运行引导脚本，跟随提示完成 Token 获取、账号设置和推送配置。

```bash
# Windows
start.bat

# Linux/macOS
python launcher.py
```

#### 3. 手动运行

```bash
# 获取 Pixiv Token (需要浏览器交互)
python get_token.py

# 立即执行一次推送
python main.py --once

# 启动定时调度模式 (守护进程)
python main.py --now
```

---

## ⚙️ 配置文件 (config.yaml)

如果跳过引导脚本手动配置，请参考以下结构。

> **注意**：推荐使用 `launcher.py` 自动生成配置。

```yaml
pixiv:
  user_id: 12345678       # 你的 Pixiv 用户 ID（用于分析 XP）
  refresh_token: "..."    # 必填，用于搜索/排行榜等操作

  # [New!] 可选：同步专用 Token (仅用于获取收藏和关注动态)
  # 使用独立 Token 可降低主号因搜索等操作被封禁的风险
  sync_token: ""          # 留空则使用主 Token

profiler:
  ai:
    enabled: true
    provider: "openai"    # 支持 openai 格式接口
    api_key: "sk-..."
    base_url: "https://api.openai.com/v1"
    model: "gpt-4o-mini"
  
  scan_limit: 1000        # 每次分析收藏的数量
  discovery_rate: 0.1     # 探索新 Tag 的概率

fetcher:
  # MAB 策略配额限制 (防止某一策略独占)
  mab_limits:
    min_quota: 0.2
    max_quota: 0.6
    
  bookmark_threshold:
    search: 1000          # 搜索结果的最低收藏数要求
  subscribed_artists: []  # 额外关注的画师 ID 列表

filter:
  daily_limit: 20         # 每日推送上限
  exclude_ai: true        # 过滤 AI 生成作品
  r18_mode: false         # 是否允许 R18 (需 Token 权限)

scheduler:
  # 定时任务配置 (Cron 表达式: 分 时 日 月 周)
  # 支持多个时间点，用逗号分隔
  cron: "0 12 * * *, 0 21 * * *"
  # 每日维护任务 (发送日报 + 清理数据)
  daily_report_cron: "0 0 * * *"

notifier:
  # 启用的推送通道列表
  types: [telegram] 
  
  telegram:
    bot_token: "123456:ABC..."
    # 你的 Telegram User ID (必须配置，否则无法使用指令)
    allowed_users: 
      - '123456789'
    chat_ids: [123456789]
  
  onebot:
    ws_url: "ws://127.0.0.1:3001"
    private_id: 12345678

# 进阶配置 (可选)
web:
  password: ""  # 留空表示首次访问时设置
  # 启动后访问 http://localhost:8080 查看管理面板
```

### 进阶配置项 [New!]

```yaml
notifier:
  multi_page_mode: "media_group"  # cover_link | media_group
  max_pages: 10                   # 多图模式最大页数 (1-10)

  telegram:
    image_quality: 85     # JPEG 压缩质量 (50-95)，越低越快
    max_image_size: 2000  # 最大边长 (px)，越小越快

    # Topic 智能分流 [New!]
    topic_rules:
      r18: 12345        # R18 作品自动发到此 Topic
      wallpaper: 67890  # "wallpaper" 标签发到此 Topic
    topic_tag_mapping:
      wallpaper: ["風景", "背景", "scenery"]
```

## 🎨 Web 管理后台 [New!]

启动服务后，访问 `http://localhost:8000` (默认端口) 即可进入管理后台。

- **Dashboard**: 查看 XP 画像词云、近期推送统计。
- **Gallery**: 浏览推送历史，提供无限滚动画廊。
  - **画廊代理**: 内置本地反代服务，**无需梯子**即可在画廊中浏览 Pixiv 图片（需配置 `proxy_url`）。
- **设置**: 首次访问需设置管理密码，之后凭密码登录。

---

## 📂 项目结构

```
pixiv-xp/
├── config.yaml          # 配置文件
├── main.py              # 程序主入口 (调度/执行)
├── launcher.py          # 交互式引导/管理菜单
├── start.bat            # Windows 启动脚本
├── deploy.sh            # Docker 管理脚本
├── get_token.py         # Pixiv Token 获取工具
├── requirements.txt     # Python 依赖
├── docker-compose.yml   # Docker 编排
├── pixiv_client.py      # Pixiv API 封装
├── profiler.py          # XP 画像分析核心
├── fetcher.py           # 内容搜索与抓取
├── filter.py            # 过滤与去重逻辑
├── database.py          # SQLite 数据存储
└── notifier/            # 推送适配器 (Telegram, OneBot)
```

---

## 📖 新手完整教程 (Step-by-Step)

> 从零开始，一步步教你跑起来！

### Step 1: 环境准备

**必需软件：**
- **Python 3.10+**: [下载地址](https://www.python.org/downloads/)
- **Git** (可选): 用于克隆项目
- **代理软件** (国内必需): v2rayN / Clash 等，用于连接 Telegram

**安装依赖：**
```bash
# 克隆项目（或直接下载 ZIP）
git clone https://github.com/bwwq/Pixiv-XP-Pusher.git
cd Pixiv-XP-Pusher

# 安装 Python 依赖
pip install -r requirements.txt
```

---

### Step 2: 获取 Pixiv Token

> ⚠️ **这是最关键的一步！** Token 是访问 Pixiv API 的钥匙。

```bash
python get_token.py
```

脚本会自动打开浏览器窗口，请登录您的 Pixiv 账号。登录成功后，Token 会自动保存到 `config.yaml`。

**无法打开浏览器？** （服务器环境）
1. 在本地电脑运行 `get_token.py` 获取 Token
2. 将获取到的 `refresh_token` 手动复制到服务器的 `config.yaml` 中

---

### Step 3: 配置 Telegram Bot

#### 3.1 创建 Bot
1. 在 Telegram 中搜索 `@BotFather`
2. 发送 `/newbot` 创建机器人
3. 按提示设置名称，获得 `bot_token`（形如 `123456789:ABCdefGHIjklMNO...`）

#### 3.2 获取 Chat ID
1. 将你的 Bot 拉入目标群组（或直接私聊）
2. 访问 `https://api.telegram.org/bot<你的token>/getUpdates`
3. 发送任意消息给 Bot，刷新页面，找到 `"chat": {"id": -100xxxxx}` 就是 Chat ID

#### 3.3 获取 User ID
1. 在 Telegram 中搜索 `@userinfobot`
2. 发送 `/start`，它会回复你的 User ID

#### 3.4 填写配置
打开 `config.yaml`，填入获取到的信息：

```yaml
notifier:
  types: [telegram]
  
  telegram:
    bot_token: "123456789:ABCdefGHIjklMNO..."  # BotFather 给你的
    chat_ids:
      - '-1001234567890'  # 你的群组 ID（或个人 Chat ID）
    allowed_users:
      - '987654321'       # 你的 User ID（用于权限控制）
    proxy_url: "http://127.0.0.1:7890"  # 代理地址（国内必填！）
```

> **🔴 国内用户必看：** `proxy_url` 必须填写你的代理软件地址，否则 Bot 无法连接 Telegram！

#### 3.5 (可选) 配置 OneBot (QQ)

如果您使用 QQ 而非 Telegram，请配置 OneBot：

1. 安装 [Lagrange.OneBot](https://github.com/LagrangeDev/Lagrange.Core) 或 [Go-CQHTTP](https://github.com/Mrs4s/go-cqhttp)
2. 启动后获取 WebSocket 地址（默认 `ws://127.0.0.1:3001`）
3. 填写配置：

```yaml
notifier:
  types: [onebot]
  
  onebot:
    ws_url: "ws://127.0.0.1:3001"
    private_id: 12345678      # 你的 QQ 号（推送目标）
    master_id: 12345678       # 主人 QQ（只有主人能使用指令）
    push_to_private: true
```

OneBot 支持与 Telegram 相同的指令：`/push`, `/xp`, `/stats`, `/block`, `/unblock`, `/schedule`, `/help`

---

### Step 4: 配置 AI 清洗 (可选但推荐)

AI 功能可以智能合并同义词（如"白发"="White Hair"），提升画像精准度。

```yaml
profiler:
  ai:
    enabled: true
    provider: "openai"
    api_key: "sk-..."              # 你的 OpenAI API Key
    base_url: "https://api.openai.com/v1"  # 或中转地址
    model: "gpt-4o-mini"           # 推荐，便宜又好用
```

**没有 API？** 设置 `enabled: false`，系统会用纯统计模式运行。

---

### Step 5: 启动程序

```bash
# 方式A: 立即执行一次推送（测试用）
python main.py --once

# 方式B: 启动定时任务 + 立即执行一次
python main.py --now

# 方式C: 仅启动定时任务（后台守护）
python main.py
```

**Windows 用户：** 直接双击 `start.bat` 启动交互菜单。

---

### Step 6: 使用 Bot 指令

Bot 启动后，在 Telegram 聊天框输入 `/` 可看到所有指令：

| 指令 | 功能 |
|------|------|
| `/push` | 🚀 立即触发一次推送 |
| `/push <ID>` | 📌 手动推送指定作品 |
| `/xp` | 🎯 查看你的 XP 画像（Top 标签） |
| `/stats` | 📈 查看各策略的成功率 |
| `/schedule` | ⏰ 查看/修改定时时间 |
| `/block <tag>` | 🚫 屏蔽讨厌的标签 |
| `/unblock <tag>` | ✅ 取消屏蔽标签 |
| `/help` | ℹ️ 显示帮助 |

**修改定时时间示例：**
```
/schedule 9:30,21:00   # 每天 9:30 和 21:00 推送
```

---

### Step 7: 日常维护

- **重置 XP 画像：** `python main.py --reset-xp`
- **查看日志：** 日志保存在 `logs/` 目录
- **更新项目：** `git pull && pip install -r requirements.txt`

---

## 🙋 常见问题

<details>
<summary><b>Q: telegram.error.NetworkError / httpx.ConnectError</b></summary>

**原因：** 无法连接 Telegram 服务器（国内被墙）

**解决：** 在 `config.yaml` 中配置代理：
```yaml
telegram:
  proxy_url: "http://127.0.0.1:7890"  # 改成你的代理地址
```
</details>

<details>
<summary><b>Q: 点击 喜欢/不喜欢 按钮提示"无权限"</b></summary>

**原因：** 你的 User ID 没有加入 `allowed_users` 列表

**解决：** 
1. 通过 `@userinfobot` 获取你的 User ID
2. 将 ID 添加到 `config.yaml` 的 `allowed_users` 列表中
</details>

<details>
<summary><b>Q: 如何获取 Pixiv Refresh Token?</b></summary>
运行 <code>python get_token.py</code>，脚本会启动一个 Selenium 浏览器窗口引导登录，登录成功后会自动捕获并保存 Token。
</details>

<details>
<summary><b>Q: 部署在服务器，无法打开浏览器获取 Token 怎么办?</b></summary>
请在本地电脑运行 <code>python get_token.py</code> 获取 Token 后，将其复制到服务器的 <code>config.yaml</code> 中。
</details>

<details>
<summary><b>Q: XP 画像是如何生成的?</b></summary>
系统会拉取你的 Pixiv 收藏夹（公开+私密），统计所有作品的标签频率。结合 AI (如果启用) 清洗同义词（如 "白发" = "White Hair"），计算出这一标签的权重。推送时会根据这些权重组合搜索关键词。
</details>

<details>
<summary><b>Q: 为什么 AI 模式报错 401/502?</b></summary>
请检查 `config.yaml` 中 LLM 的 `api_key` 和 `base_url` 是否正确。如果 API 不稳定，可以在配置中关闭 AI 功能 (`enabled: false`)，系统将回退到纯统计模式运行。
</details>

---

## 📜 许可证

MIT License
