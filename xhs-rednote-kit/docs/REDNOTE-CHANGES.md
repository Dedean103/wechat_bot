# RedNote 改造清单（xiaohongshu.com → rednote.com）

上游：`xpzouying/xiaohongshu-mcp` @ `f451866`
本地：分支 `fix/rednote-session-restore`，备份提交 `27b1111`，补丁见 `patches/01-rednote-backend.patch`

## 为什么要改

`xiaohongshu.com`（中国大陆版）和 `rednote.com`（海外版）是两个独立 Web 产品：

- 登录 Cookie 分别属于 `.xiaohongshu.com` 和 `.rednote.com`
- 两边的 session 不通用，改 Cookie 的 domain 也换不过去
- 上游把页面地址硬编码成 `https://www.xiaohongshu.com/...`，RedNote 账号扫码成功后，后续每次导航都会落到错误站点，于是出现「Cookie 有了，还是未登录」

## 站点映射

| 用途 | 国内站 | RedNote |
|---|---|---|
| Web 首页 / explore | https://www.xiaohongshu.com | https://www.rednote.com |
| 搜索 | https://www.xiaohongshu.com/search_result | https://www.rednote.com/search_result |
| 笔记详情 | /explore/{feed_id} | /explore/{feed_id} |
| 用户主页 | /user/profile/{user_id} | /user/profile/{user_id} |
| 登录页 | /explore（首页弹窗） | /login（显式入口） |
| 发布后台 | https://creator.xiaohongshu.com/publish/publish | https://creator.rednote.com/publish/publish |

## 开关

```bash
XHS_BACKEND=rednote       # 走 www.rednote.com / creator.rednote.com
XHS_BACKEND=xiaohongshu   # 走国内站（默认，变量为空时也是它）
```

实现见 `server/configs/backend.go`：

```go
func WebBaseURL() string {
    if BackendFromEnv() == BackendRednote {
        return "https://www.rednote.com"
    }
    return "https://www.xiaohongshu.com"
}

func CreatorBaseURL() string {
    if BackendFromEnv() == BackendRednote {
        return "https://creator.rednote.com"
    }
    return "https://creator.xiaohongshu.com"
}
```

非法取值会 `Warnf` 后回落到 `xiaohongshu`，不会静默失败。

## 改动文件（12 个，+212 / −21）

| 文件 | 改动 |
|---|---|
| `configs/backend.go` | 新增：站点开关与 URL 构造函数 |
| `configs/backend_test.go` | 新增：三种取值的单测 |
| `xiaohongshu/login.go` | 登录页/二维码入口改走 `WebBaseURL()`；RedNote 用 `/login` 显式入口；登录检测改为「选择器 OR 前端状态」双通道 |
| `xiaohongshu/search.go` | 搜索 URL 前缀；等待逻辑从「`__INITIAL_STATE__` 存在」改成「feed 数组非空」；`feeds` 可能是数组也可能被包成 `{value}`，两种都兼容 |
| `xiaohongshu/feeds.go` | 首页推荐流前缀 |
| `xiaohongshu/navigate.go` | explore 前缀 |
| `xiaohongshu/feed_detail.go` | 详情 URL 前缀 |
| `xiaohongshu/user_profile.go` | 主页 URL 前缀 |
| `xiaohongshu/publish.go` / `publish_video.go` | 发布后台前缀 `CreatorBaseURL()` |
| `xiaohongshu/backend_urls_test.go` | 新增：URL 前缀单测 |
| `xiaohongshu/login_test.go` | 新增：登录态解析单测 |

## 登录检测的具体逻辑

```go
// 老逻辑：只看侧栏元素，命中不了就判定未登录
// 新逻辑：
state, stateErr := readLoginPageState(pp)   // 读 window.__INITIAL_STATE__.user
return exists || (stateErr == nil && state.Authenticated), nil
```

`readLoginPageState` 兼容 go-rod 对 `Eval` 返回值可能的 `{value}` / `{_value}` 包装，字段缺失一律当 false，不会让工具报错。诊断日志只输出布尔值：

```
登录诊断: url=... sidebar=false authenticated_state=true guest=false user_identity=true login_button=false login_modal=false
```

**不要**在日志里打印 Cookie、token、用户标识原值。

## 数据解析的兼容点

RedNote 的 `window.__INITIAL_STATE__.search.feeds` 有时直接是数组，有时是 `{value: [...]}` / `{_value: [...]}`。上游只处理后者，取不到数据会一直等到超时。三处（等待条件、取数据、翻页判重）都加了 `Array.isArray` 分支。

## 浏览器并发（本地额外修复）

`patches/02-browser-slots.patch`，未提交上游：

- `browser_slots.go`：包级 channel 信号量，`maxResearchBrowsers = 3`，`researchBrowserTimeout = 120s`；取消与拿到槽位竞争时不会启动 Chromium
- `service.go`：`newBrowser()` → `newBrowser(ctx)`；`GetLoginQrcode` 把 page/browser 的清理权交给后台等待扫码的 goroutine（`background = true`），否则二维码刚返回页面就被关掉

## 维护者建议（来自上游 issue #699 / #609）

- 先确认账号属于哪个站点，再排查代码
- 不要通过改 Cookie domain 伪造兼容
- 页面结构变了先看选择器，再看前端状态字段
- VPS 上导航超时优先升级到最新稳定版，再抓单次调用完整日志
