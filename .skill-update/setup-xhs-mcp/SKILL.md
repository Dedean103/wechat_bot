---
name: setup-xhs-mcp
description: |
  安装部署 xiaohongshu-mcp 服务并配置 MCP 连接，引导用户完成从零到可用的全流程。
  当用户第一次使用小红书功能、提到安装/部署/配置小红书、环境搭建、MCP 服务连接失败、或 check_login_status 等 MCP 工具不可用时使用。
---

项目仓库：https://github.com/xpzouying/xiaohongshu-mcp

## 执行流程

### 1. 检测服务状态

先看当前会话是否已经加载 `mcp__xiaohongshu_mcp__check_login_status`。如果已加载，优先直接调用该工具；工具调用的真实结果是本客户端连接状态的最高优先级证据。成功返回或明确的工具业务错误都表示 MCP 传输层已连通。

检查 xiaohongshu-mcp 是否已在运行（注意：MCP 端点只接受 POST，GET 会返回 405，不能用 `-f` 判断）：

```bash
curl -so /dev/null http://localhost:18060/mcp && echo "running" || echo "not running"
```

- 已运行 → 记录地址 `http://localhost:18060/mcp`，跳到步骤 3
- 未运行 → 询问用户：服务是否部署在其他地址/端口？
  - 用户提供地址 → 验证可达后跳到步骤 3
  - 未部署 → 进入步骤 2

若客户端报错 `failed when send initialize request`、`connection refused` 或 `error sending request`，先按“未运行”处理：这通常表示 TCP 端口没有监听，不是 MCP 工具或协议本身错误。用 `ss -ltnp | grep 18060` 和 `ps` 检查监听进程；不要只检查配置文件。

如果 shell 命令是在受限沙箱、容器或与服务不同的网络命名空间中执行，`curl 127.0.0.1`、`ss`、`ps` 和 tmux 可能分别显示连接失败、权限不足或没有进程。这些结果只能说明“当前探测环境看不到宿主机”，不能证明 MCP 客户端连接失败；不得用它们覆盖已经成功的 MCP 工具调用。仅当直接 MCP 调用也失败时，才继续按端口/进程/日志排查。

诊断结论按以下顺序记录：

1. 工具是否已加载；
2. 直接 MCP 调用是否成功；
3. 登录状态或只读业务调用是否成功；
4. 只有前三项不足以定位时，才使用同一宿主机命名空间中的端口、进程和服务日志。

同时记录当前 MCP 版本，并与上游最新 release 比较。VPS 上出现浏览器导航或页面等待超时时，先升级到最新稳定版再复现；若仍超时，保留一次完整调用对应的文本日志，不要用大量并发重试代替诊断。

#### 账号站点兼容性

部署或排查登录前确认账号所属站点：

- `xiaohongshu.com` 与 `rednote.com` 是不同的 Web 产品，Cookie session 不通用
- 海外注册账号若登录后跳转到 `rednote.com`，必须使用明确支持 RedNote 的 MCP 构建和对应站点配置
- 仅修改 Cookie domain 无效；登录、状态检查、搜索和发布必须统一访问同一站点
- 不要假定任意版本都支持某个环境变量或启动参数，应先核对该版本的 `--help`、release notes 或源码

本机定制构建支持 `XHS_BACKEND=rednote`；上游版本应按其当前文档选择站点参数。相关背景与维护者建议见 [issue #699](https://github.com/xpzouying/xiaohongshu-mcp/issues/699) 和 [issue #609](https://github.com/xpzouying/xiaohongshu-mcp/issues/609)。

### 2. 部署服务

确认操作系统（macOS / Linux / Windows）和是否已安装 Docker。

#### 方式一：Docker Compose（推荐）

Docker 镜像内置 Chrome 和中文字体，无需额外配置。

```bash
# 下载 docker-compose.yml
wget https://raw.githubusercontent.com/xpzouying/xiaohongshu-mcp/main/docker/docker-compose.yml

# 启动服务
docker compose up -d

# 查看日志
docker compose logs -f
```

镜像源：
- Docker Hub（默认）：`xpzouying/xiaohongshu-mcp`
- 阿里云（国内推荐）：`crpi-hocnvtkomt7w9v8t.cn-beijing.personal.cr.aliyuncs.com/xpzouying/xiaohongshu-mcp`

切换方法：编辑 docker-compose.yml，注释默认 image 行，取消注释阿里云 image 行。

数据持久化：
- `./data` — cookies 登录状态
- `./images` — 发布图片时的挂载目录

#### 方式二：下载二进制

从 GitHub Releases 下载：https://github.com/xpzouying/xiaohongshu-mcp/releases/latest

```bash
curl -s https://api.github.com/repos/xpzouying/xiaohongshu-mcp/releases/latest | grep browser_download_url
```

注意：二进制方式需要本机已安装 Chrome 或 Chromium。

#### 方式三：源码编译

不推荐，仅适合 Go 开发者。参考项目仓库 README。

部署完成后用 curl 确认服务已启动，然后进入步骤 3。

#### 已有二进制的启动与持久化

如果本机已有二进制（例如 `/home/<user>/xhs/xiaohongshu-mcp/bin/xiaohongshu-mcp`），从其工作目录启动，并把日志写到部署目录。不要仅依赖一次性的前台命令或普通后台进程；可使用 tmux 持久运行：

```bash
tmux has-session -t xhs-mcp 2>/dev/null && tmux kill-session -t xhs-mcp 2>/dev/null || true
tmux new-session -d -s xhs-mcp \
  'cd /path/to/xhs && exec /path/to/xiaohongshu-mcp >> /path/to/xiaohongshu-mcp/server.log 2>&1'
```

启动后必须从独立命令验证端点仍然存活，避免进程在启动命令结束后立即被清理：

```bash
curl -sS -D /tmp/xhs-init-headers.txt -o /tmp/xhs-init-body.txt \
  --max-time 10 -X POST http://127.0.0.1:18060/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"codex-check","version":"1.0"}}}'
```

成功标准是 HTTP 200、响应中包含 `Mcp-Session-Id`，且 JSON-RPC 结果包含 `serverInfo`。随后用该 session 发送 `notifications/initialized` 和 `tools/list`；若独立验证失败，检查 `server.log` 和 tmux 会话，不要直接修改 MCP URL。

### 3. 检测 MCP 连接配置

检查当前客户端是否已配置 xiaohongshu MCP 连接。

**Claude Code**：读取 `~/.claude/settings.json` 和项目级 `.claude/settings.json`，查找 `mcpServers` 中是否有 `xiaohongshu` 配置。

- 已配置且地址正确 → 跳到步骤 5
- 已配置但地址不匹配 → 修正地址
- 未配置 → 进入步骤 4

### 4. 配置 MCP 连接

询问用户：

**连接地址**：如果步骤 1 已确认可达的地址，用该地址作为默认值，否则默认 `http://localhost:18060/mcp`。

**使用的客户端**：

**Claude Code**：

```bash
claude mcp add xiaohongshu --transport http <地址>
```

或写入配置文件：
- 全局：`~/.claude/settings.json`
- 项目级：`.claude/settings.json`

```json
{
  "mcpServers": {
    "xiaohongshu": {
      "url": "http://localhost:18060/mcp"
    }
  }
}
```

**Cursor**（`.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "xiaohongshu": {
      "url": "http://localhost:18060/mcp"
    }
  }
}
```

**其他客户端**：告知用户 MCP 服务地址，让用户按客户端文档自行配置。

### 5. 验证与提示

1. **提示用户重启当前会话** — MCP 配置变更后需重启客户端才能加载新的 MCP 工具
2. 重启后调用 `check_login_status` 验证连接正常
3. 若已登录，再执行一次只读业务调用（例如 `search_feeds` 或 `list_feeds`）确认业务链路
4. 只有工具未加载或直接调用失败时，才报告 MCP 不可用；不要用隔离 shell 的本地探测替代 MCP 验证
5. 验证成功 → 引导用户使用 `/xhs-login` 完成扫码登录

## 环境变量（可选）

- `XHS_PROXY` — HTTP/HTTPS/SOCKS5 代理地址
- `ROD_BROWSER_BIN` — 自定义 Chromium 路径
- `HEADLESS` — 无头模式开关

## 失败处理

| 场景 | 处理 |
|---|---|
| Docker 未安装 | 建议安装 Docker 或改用二进制方式 |
| 国内拉取镜像慢 | 切换到阿里云镜像源 |
| 端口 18060 被占用 | 检查已有进程，或更换端口 |
| Chrome 未安装（二进制方式） | 引导安装 Chrome 或改用 Docker 方式 |
| 配置写入后工具仍不可用 | 提示重启客户端会话 |
| 已有配置但地址错误 | 修正地址并重启 |
| `initialize` 时 connection refused / error sending request | 确认 18060 有监听进程；若已有二进制，用 tmux 持久启动并从独立命令重新发送 `initialize` |
| 启动命令成功但随后端口消失 | 说明后台进程随 shell/执行环境退出；检查 tmux/systemd 持久托管状态，并查看 `server.log` |
| 海外账号扫码后始终未登录 | 检查 Cookie domain 与服务目标站点；确认运行版本确实支持 RedNote |
| VPS 页面操作超时 | 先升级最新稳定版，再用单次调用的完整日志定位导航、等待、出口 IP/代理或风控问题 |
