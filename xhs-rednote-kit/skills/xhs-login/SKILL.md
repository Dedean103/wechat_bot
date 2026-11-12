---
name: xhs-login
description: |
  管理小红书登录状态：检查是否已登录、二维码扫码登录、重置登录切换账号。
  当用户提到登录、扫码、账号、切换账号、退出登录、登录状态检查，或其他 skill 报告"未登录"需要先登录时使用。
---

## 执行流程

### 0. 先确认账号所属站点

当用户是海外账号、网页会跳转到 `rednote.com`，或多次扫码后仍显示未登录时，先确认账号和 MCP 服务访问的是同一站点：

- 中国大陆版账号通常使用 `xiaohongshu.com`，登录 Cookie 属于 `.xiaohongshu.com`
- 海外注册账号可能属于 RedNote，登录 Cookie 属于 `.rednote.com`
- 两个平台的 session 不通用；修改 Cookie 的 domain 不能把一个平台的 session 转成另一个平台

检查当前 MCP 版本是否明确支持 RedNote 或可配置站点。只有服务器实现了相应能力时，才使用该版本文档提供的站点参数；本机定制版使用 `XHS_BACKEND=rednote`。若服务器仅支持 `xiaohongshu.com`，更新 skill 本身不能让 RedNote 登录生效，应先升级或修补 MCP 服务。参考上游 [issue #699](https://github.com/xpzouying/xiaohongshu-mcp/issues/699) 和 [issue #609](https://github.com/xpzouying/xiaohongshu-mcp/issues/609)。

### 1. 检查登录状态

调用 `check_login_status`（无参数），返回是否已登录及用户名。

- 已登录 → 告知用户当前登录账号
- 未登录 → 进入步骤 2

### 2. 扫码登录

调用 `get_login_qrcode`（无参数）。MCP 工具返回两部分内容：
- 文本：超时提示（含截止时间）
- 图片：PNG 格式二维码（MCP image content type，Base64 编码）

**展示二维码**：MCP 返回的图片会通过客户端渲染给用户。如果客户端无法直接展示图片（如纯文本终端），则将 Base64 数据保存为临时 PNG 文件，告知用户文件路径让其手动打开：
```bash
# fallback: 保存二维码到临时文件
echo "<base64_data>" | base64 -d > /tmp/xhs-qrcode.png
open /tmp/xhs-qrcode.png   # macOS
xdg-open /tmp/xhs-qrcode.png  # Linux
```

提示用户：
- 打开小红书 App 扫描二维码
- 二维码有效期有限，过期需重新获取

扫码完成后，调用 `check_login_status` 确认登录成功。

若扫码后仍显示未登录，不要立即反复生成二维码或删除 Cookie。先检查服务实际使用的 Cookie 文件，只读取文件时间、Cookie 数量、名称和 domain，不输出 Cookie value：

- 文件未更新：扫码可能未完成 App 端确认、二维码已过期，或等待登录的浏览器页已退出
- 文件已更新且出现与当前站点匹配的 `web_session` / `id_token`：扫码和持久化已经成功，问题位于站点选择、登录检测选择器或页面兼容性
- Cookie 只属于另一个域：账号站点与 MCP 后端不匹配

每次只运行一个浏览器型 MCP 调用，并在调用前后查看服务日志；并发检查会混淆日志，也可能加剧超时。

#### RedNote Cookie 已写入但登录状态误判

若 Cookie 文件已更新、`.rednote.com` 下存在 `web_session` / `id_token`，但 `check_login_status` 仍返回未登录，这通常不是扫码失败，而是 MCP 仍按旧版小红书页面结构判断登录。先用一个只读业务调用（如 `search_feeds`）交叉验证：若它能返回真实内容，会话有效，故障集中在登录检测；若它也失败，再排查导航、网络、风控或 Cookie 加载。

当用户要求修复 MCP 实现时，按以下兼容方案处理；仅诊断时只报告结论，不修改服务：

1. RedNote 获取二维码时导航到 `https://www.rednote.com/login`，不要依赖首页自动弹出登录框；小红书国内站继续使用其原有入口。
2. 登录判断保留旧版侧栏/个人入口选择器，同时增加对页面 `window.__INITIAL_STATE__` 用户状态的安全检查。应以“已认证状态或稳定的用户身份字段存在，且不是 guest”为登录证据，不依赖单一 CSS 选择器。
3. 前端状态字段可能变化或缺失；读取逻辑必须容错，不能因 JavaScript 字段不存在、结构变化或序列化失败而让整个工具报错。旧选择器与状态检测任一给出可靠登录证据即可。
4. 诊断日志只记录 URL、布尔判断（如 legacy selector、authenticated、guest、identity、login button/modal），不得记录 Cookie value、token 或用户标识原值。
5. 修改后运行相关单元测试和完整测试，构建新二进制并保留旧二进制与 Cookie 备份，再重启服务。先确认 `check_login_status` 返回账号，再执行一次只读业务调用；两者都成功才算修复完成。

已验证的故障链是：RedNote 扫码成功 → `.rednote.com` Cookie 正常持久化 → 旧版单选择器产生假阴性。修复目标是登录页入口和状态检测兼容性；不要删除有效 Cookie，也不要把 `.rednote.com` Cookie 改写成 `.xiaohongshu.com`。

### 3. 重新登录 / 切换账号

当用户要求重新登录或切换账号时：

1. 调用 `delete_cookies`（⚠️ 需用户确认）— 清除当前登录状态
2. 调用 `get_login_qrcode` — 获取新二维码
3. 引导用户扫码

## 约束

- `delete_cookies` 会清除登录状态，执行前必须确认
- 登录需要用户手动用手机 App 扫码，无法自动完成

## 失败处理

| 场景 | 处理 |
|---|---|
| MCP 工具不可用 | 引导用户使用 `/setup-xhs-mcp` 完成部署和连接配置 |
| 二维码超时 | 重新调用 `get_login_qrcode` |
| 海外账号扫码后始终未登录 | 检查是否为 `.rednote.com` 会话以及服务是否支持 RedNote；不要改 Cookie domain 冒充兼容 |
| Cookie 已更新但仍判定未登录 | 先用只读业务调用区分“会话无效”和“状态误判”；RedNote 会话有效时按上面的登录页与双重检测方案修复 |
| VPS 调用超时 | 先升级到最新 MCP 版本；仍复现时保留完整文本日志，检查导航、页面等待、出口 IP/代理和风控 |
