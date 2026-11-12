# 小红书（RedNote）MCP + Skills 工具包

一套可以整体拷走的「小红书自动化」运行包：让 AI 助手（Codex / Claude Code / Cursor 等支持 MCP 的客户端）直接登录、搜索、阅读、发布小红书内容。

本包为 **RedNote（rednote.com）分支**：服务端所有页面入口从 `www.xiaohongshu.com` 切换为 `www.rednote.com`，用于海外账号（Cookie 属于 `.rednote.com`）。两个站点的登录态不通用。

## 1. 包内容

```
xhs-rednote-kit/
├── README.md                     ← 本文档
├── install.sh                    ← 把 skills 装到客户端目录
├── skills/                       ← 10 个 agent skill（可直接拷进 ~/.codex/skills 或 ~/.claude/skills）
│   ├── xiaohongshu/              ← 统一入口，负责意图路由 + MCP 前置检查
│   ├── setup-xhs-mcp/            ← 安装部署、连接排障
│   ├── xhs-login/                ← 登录 / 扫码 / 切换账号
│   ├── xhs-search/               ← 关键词搜索 + 筛选
│   ├── xhs-explore/              ← 推荐流、笔记详情、评论
│   ├── xhs-interact/             ← 点赞 / 收藏 / 评论 / 回复
│   ├── xhs-profile/              ← 博主主页
│   ├── xhs-content-plan/         ← 选题、竞品分析
│   ├── xhs-research/             ← 系统化调研（多关键词矩阵 + 结构化结论）
│   └── post-to-xhs/              ← ⚠️ 另一条链路：CDP + Python 脚本（脚本里的路径是 Windows，Linux 需改）
├── server/                       ← 打过补丁的 xiaohongshu-mcp Go 源码
├── bin/
│   └── xiaohongshu-mcp-linux-amd64   ← 预编译二进制（linux/amd64，可直接跑）
├── deploy/
│   ├── start.sh                  ← 简易启动脚本（默认 XHS_BACKEND=rednote）
│   ├── xhs-mcp.service           ← systemd --user 单元
│   └── codex-config.toml.example ← Codex MCP 配置片段
├── patches/
│   ├── 01-rednote-backend.patch  ← 相对上游的 RedNote 站点改造
│   └── 02-browser-slots.patch    ← 浏览器并发/登录页生命周期的本地修复
└── docs/
    └── REDNOTE-CHANGES.md        ← API 节点替换清单（xiaohongshu.com → rednote.com）
```

不含任何 Cookie / 二维码 / 账号数据。`cookies.json` 由服务自己在运行目录生成，不要打包外发。

## 2. 它是怎么工作的

四层，逐层收窄：

```
用户一句话（"帮我搜一下人体工学椅"）
        │
        ▼
[Skill 层]  skills/xiaohongshu/SKILL.md
        │   1) 先看 MCP 工具里有没有 check_login_status —— 没有就让你跑 /setup-xhs-mcp
        │   2) 按意图把请求路由到子 skill（登录/搜索/浏览/互动…）
        ▼
[MCP 层]    http://127.0.0.1:18060/mcp    （JSON-RPC over HTTP，streamable）
        │   工具名形如 mcp__xiaohongshu_mcp__search_feeds
        ▼
[服务层]    server/ (Go) + go-rod 控制 Chromium，headless
        │   站点由环境变量 XHS_BACKEND 决定：rednote | xiaohongshu
        ▼
[站点层]    www.rednote.com / creator.rednote.com
```

要点：

- **技能本身不碰浏览器**，只负责判断意图、校验登录、组织参数、解释结果。真正的动作都由 MCP 工具完成。
- 技能里明确禁止用 Playwright / WebFetch 代替 MCP；MCP 不可用时只提示部署，不做降级。
- 登录态是浏览器 Cookie，持久化在服务的工作目录（本机是 `~/xhs/cookies.json`）。
- 浏览器型调用要串行；本地构建额外加了全局信号量，最多 3 个浏览器并发、排队超时 120s。

## 3. 部署

### 3.1 直接用预编译二进制

```bash
mkdir -p ~/xhs/xiaohongshu-mcp
cp -r bin/xiaohongshu-mcp-linux-amd64 ~/xhs/xiaohongshu-mcp/bin/xiaohongshu-mcp
chmod +x ~/xhs/xiaohongshu-mcp/bin/xiaohongshu-mcp
cp deploy/start.sh ~/xhs/xiaohongshu-mcp/../start.sh   # 可选
```

前提：机器上有 Chrome / Chromium（`ROD_BROWSER_BIN` 可指定路径）。

### 3.2 用 systemd --user 托管（推荐）

```bash
mkdir -p ~/.config/systemd/user
cp deploy/xhs-mcp.service ~/.config/systemd/user/
# 按需修改其中 WorkingDirectory / ExecStart 的绝对路径
systemctl --user daemon-reload
systemctl --user enable --now xhs-mcp.service
systemctl --user status xhs-mcp.service
```

关键项：

```ini
Environment=XHS_BACKEND=rednote
ExecStart=/home/<user>/xhs/xiaohongshu-mcp/bin/xiaohongshu-mcp -headless=true -port 127.0.0.1:18060
```

换回国内站只需 `XHS_BACKEND=xiaohongshu`（或删掉该变量，默认就是国内站）。

### 3.3 验证服务

```bash
ss -ltnp | grep 18060
curl -sS -D - -o /dev/null --max-time 5 \
  -X POST http://127.0.0.1:18060/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"check","version":"1.0"}}}'
```

成功标准：HTTP 200，响应头带 `Mcp-Session-Id`，body 里有 `serverInfo`。
注意：MCP 端点只接受 POST，GET 返回 405，不能用 `curl -f` 判断存活。

### 3.4 安装 skills

```bash
./install.sh                      # 默认装到 ~/.codex/skills
./install.sh ~/.claude/skills     # Claude Code
```

装完必须**重启会话**，MCP 工具和 skill 都是启动时加载的。

### 3.5 配置客户端

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.xiaohongshu-mcp]
url = "http://127.0.0.1:18060/mcp"

# 清 Cookie 属于破坏性操作，单独要求确认
[mcp_servers.xiaohongshu-mcp.tools.delete_cookies]
approval_mode = "approve"
```

Claude Code：

```bash
claude mcp add xiaohongshu --transport http http://127.0.0.1:18060/mcp
```

Cursor（`.cursor/mcp.json`）：

```json
{ "mcpServers": { "xiaohongshu": { "url": "http://127.0.0.1:18060/mcp" } } }
```

## 4. 登录

登录 = 人拿手机扫码，AI 只负责把二维码递到你面前并确认结果。

### 4.1 标准流程

1. `check_login_status`（无参数）
   - 已登录 → 返回账号信息，结束
   - 未登录 → 下一步
2. `get_login_qrcode`（无参数）
   - 返回两段内容：文本（超时截止时间）+ PNG 图片（Base64 的 image content）
   - 客户端能渲染就直接显示；纯终端环境把 Base64 落盘再打开：
     ```bash
     echo "<base64>" | base64 -d > /tmp/xhs-qrcode.png
     xdg-open /tmp/xhs-qrcode.png
     ```
3. 用户用小红书 App 扫码，并在 App 内确认
4. 再调 `check_login_status` 验证

RedNote 分支的二维码入口是 `https://www.rednote.com/login`（不是靠首页自动弹窗）；国内站仍走 `/explore` 首页弹窗。

### 4.2 登录判定规则（本包的关键修复）

老版本只看一个 CSS 选择器，RedNote 改版后会「Cookie 明明写进去了，却报未登录」。现在的判定是：

```
已登录 = 侧栏/个人入口选择器命中
       OR (window.__INITIAL_STATE__.user 里存在非 guest 且带身份的 userInfo)
```

任一为真即视为已登录；读取前端状态的过程全部容错，字段缺失不会让工具直接报错。诊断日志只打印布尔值（sidebar / authenticated / guest / hasIdentity / login_button / login_modal），不打印 Cookie、token、用户标识。

**交叉验证**：如果 `check_login_status` 说未登录，但 `search_feeds` 能返回真实笔记，说明会话是好的，问题只在登录检测——不要删 Cookie、不要重新扫码。

### 4.3 切换账号 / 重新登录

1. `delete_cookies` —— 会清掉当前登录态，**必须**先经用户确认（Codex 配置里已设为 `approval_mode = "approve"`）
2. `get_login_qrcode`
3. 重新扫码

### 4.4 登录常见故障

| 现象 | 判断 | 处理 |
|---|---|---|
| 扫码后仍显示未登录 | 先看 Cookie 文件有没有更新：时间、条数、名称、domain（不要输出 value） | 未更新 → 扫码没在 App 内确认 / 二维码过期 / 等待登录的页面已退出 |
| Cookie 已更新且是 `.rednote.com` 的 `web_session` / `id_token` | 会话其实成功了 | 用 `search_feeds` 交叉验证；问题在登录检测，不是扫码 |
| Cookie 全属于另一个域 | 站点不匹配 | 检查 `XHS_BACKEND`；不要改 Cookie domain 冒充兼容 |
| `delete_cookies` 后仍未登录 | 正常 | 需要重新扫码 |

## 5. 搜索

### 5.1 工具与参数

`search_feeds`

| 参数 | 类型 | 说明 |
|---|---|---|
| `keyword` | string，必填 | 搜索词 |
| `filters` | object，可选 | 见下表 |

`filters`：

| 字段 | 可选值 |
|---|---|
| `sort_by` | 综合 / 最新 / 最多点赞 / 最多评论 / 最多收藏 |
| `note_type` | 不限 / 视频 / 图文 |
| `publish_time` | 不限 / 一天内 / 一周内 / 半年内 |
| `search_scope` | 不限 / 已看过 / 未看过 / 已关注 |
| `location` | 不限 / 同城 / 附近 |

### 5.2 返回结果怎么用

每条结果都要留住两个字段：

- `feed_id` —— 笔记 ID
- `xsec_token` —— 访问凭证

这两个值**必须**从搜索结果里抄，不能编。后续的 `get_feed_detail`（看正文和评论）、点赞、收藏、评论都依赖它们。

搜索 URL 的站点前缀同样跟随 `XHS_BACKEND`，RedNote 下是 `https://www.rednote.com/search_result?keyword=...&source=web_search_result_notes`。

### 5.3 典型用法

```
用户：搜一下小红书上的"人体工学椅 500 元以内"
→ xhs-search：search_feeds(keyword="人体工学椅 500元以内", filters={sort_by:"最多点赞"})
→ 列表展示标题/作者/点赞/评论/收藏 + feed_id + xsec_token
→ 用户挑一条 → xhs-explore 用 get_feed_detail(feed_id, xsec_token) 读正文和评论
```

研究型需求（"用小红书帮我调研一下…"）走 `xhs-research`：生成 3–6 组互补关键词（核心方案 / 配置价格 / 对比项 / 避坑翻车），逐组搜索、筛笔记、读详情与评论，最后给结构化结论，来源统一标成「参考笔记」。

### 5.4 搜索相关的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 返回空列表 | 关键词太窄 / 被风控 | 换词、减少筛选条件、放慢节奏 |
| 搜索超时 | 同时开了多个浏览器 | 线程串行；本包最多 3 个并发，排队 120s |
| `feed_id` 有效但详情打不开 | 用了别的笔记的 `xsec_token` | 两个值必须成对来自同一条结果 |
| 一直跳登录 | 会话失效或站点不匹配 | 见第 4 节 |

## 6. 排障顺序（别弄反）

1. 客户端的 MCP 工具列表里有没有 `check_login_status`？（没有 = 未加载，重启会话）
2. 直接调用 MCP 工具成功了吗？（成功就说明传输层没问题）
3. 只读业务调用（`search_feeds`）成功了吗？（区分「会话失效」和「登录误判」）
4. 以上都不足以定位，才去看端口、进程、日志

沙箱/容器里的 `curl 127.0.0.1`、`ss`、`ps` 可能看不到宿主机回环，**不能**用它们推翻一次成功的 MCP 调用。

日志：`~/xhs/xiaohongshu-mcp/server.log`（或 systemd 的 `journalctl --user -u xhs-mcp`）。

## 7. 与上游的差异

基于 `xpzouying/xiaohongshu-mcp` 的上游提交 `f451866`，加了三块改动：

1. **RedNote 站点支持**（`patches/01-rednote-backend.patch`，对应源码仓 commit `27b1111`）
   - 新增 `configs/backend.go`：`XHS_BACKEND` → `WebBaseURL()` / `CreatorBaseURL()`
   - 登录、搜索、详情、主页、发布、导航全部改走这组函数
   - 详见 `docs/REDNOTE-CHANGES.md`
2. **登录态双通道检测**（同上补丁里的 `xiaohongshu/login.go`）
   - 选择器 + `window.__INITIAL_STATE__` 双重判定，修 RedNote 假阴性
3. **浏览器并发与生命周期**（`patches/02-browser-slots.patch`，未提交上游）
   - `browser_slots.go`：全局最多 3 个浏览器，排队 120s
   - `service.go`：`newBrowser(ctx)` 走信号量；取二维码时把 page/browser 的所有权交给后台等待登录的 goroutine，避免页面被提前关掉

`server/skills/post-to-xhs/` 是随源码一起发布的上游版本，走 MCP 发布工具（`publish_content` / `publish_with_video`），与本包 `skills/post-to-xhs` 的 CDP 版本不是同一套实现：前者跨平台、依赖 MCP；后者依赖本机 Chrome + 调试端口。要发布内容时优先选前者，或确认 CDP 脚本里的 Chrome 路径后再用后者。

源码重建：

```bash
cd server
go build -o ../bin/xiaohongshu-mcp-linux-amd64 .
```

## 8. 安全与合规

- 包里没有 Cookie、二维码、账号信息；`cookies.json` 是运行期产物。
- 发布、评论、点赞、删 Cookie 都是对外可见/不可逆的操作，执行前必须经用户确认。
- 只用于自己账号的自动化。高频调用会触发风控，保持串行、加间隔。
