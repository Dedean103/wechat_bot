# WeChat → Codex VPS Setup

## Current status

- OS: Debian 13.6 (trixie)
- User: `dedeanzheng`
- Bridge repository: `/home/dedeanzheng/memo/wechat-time/`
- Codex: `/usr/bin/codex`, version `0.150.1`
- Model: `gpt-5.6-luna`, reasoning effort `medium`
- WeChat registration: completed
- Bridge: running under `wechat-bridge.service`
- End-to-end WeChat → Codex → WeChat test: passed
- Calendar reminders: active; verified against the iCloud `AI助手` calendar
- Notion long tasks: active; live resolver verified against the current weekly page

## Layout

```text
/home/dedeanzheng/memo/
├── ai/memo/
│   ├── VPS_DEPLOY_PLAN.md
│   ├── DEPLOY_REMOTE.md
│   ├── investigation/
│   │   └── YYMMDD-<subject>.md   # completed research reports
│   └── qrcode.png
├── wechat-time/
│   ├── .env                       # sensitive Notion runtime credential; mode 600, Git-ignored
│   ├── scripts/wechat_bridge.py
│   ├── scripts/codex_ws_helper.mjs
│   ├── scripts/weixin_bridge/
│   │   ├── account.json       # sensitive runtime state
│   │   ├── backend.json       # active bridge config
│   │   ├── bridge.log
│   │   ├── state.json
│   │   ├── calendar_reminder.json  # non-secret iCloud calendar selection
│   │   ├── calendar_reminders.sqlite3  # durable local WeChat timers
│   │   └── qrcode.png         # symlink to ai/memo/qrcode.png
│   └── wechat_work/
│       ├── AGENTS.md
│       ├── wechat_memory.md   # generated
│       └── inbox/             # received files
└── wechat-bridge.service       # local unit template
```

The installed unit is `/etc/systemd/system/wechat-bridge.service`.
The QR image is actually stored at `/home/dedeanzheng/memo/ai/memo/qrcode.png`.

## Important configuration

Codex config: `/home/dedeanzheng/.codex/config.toml`

```toml
model = "gpt-5.6-luna"
model_reasoning_effort = "medium"
```

Codex login state: `/home/dedeanzheng/.codex/auth.json`.
Never print or commit this file or `wechat-time/scripts/weixin_bridge/account.json`.

Bridge config: `wechat-time/scripts/weixin_bridge/backend.json`

```json
{
  "backend": "codex",
  "work_dir": "/home/dedeanzheng/memo/wechat-time/wechat_work",
  "chat_model": "gpt-5.6-luna",
  "chat_effort": "medium",
  "casual_effort": "low",
  "task_model": "gpt-5.6-luna",
  "task_effort": "medium"
}
```

The model fields must be explicit. Empty fields fall back to the bridge's
hard-coded `deepseek-v4-flash`; they do not inherit `config.toml`.
Since this VPS has no DeepSeek proxy, empty fields cause “暂时连不上模型服务”.

## Service operations

```bash
sudo systemctl status wechat-bridge.service
sudo systemctl restart wechat-bridge.service
sudo journalctl -u wechat-bridge.service -n 100 --no-pager
sudo journalctl -u wechat-bridge.service -f
```

The service runs as user `dedeanzheng` with working directory
`/home/dedeanzheng/memo/wechat-time/` and `Restart=always`.
Do not run a foreground bridge while systemd is running.

## Validation

```bash
cd /home/dedeanzheng/memo/wechat-time
python3 scripts/wechat_bridge.py doctor
tail -n 100 scripts/weixin_bridge/bridge.log
```

Independent Codex test:

```bash
cd /home/dedeanzheng/memo/wechat-time/wechat_work
codex exec --json '只回复：1+1=2，不要调用工具'
```

## Long-message behavior

`wechat-time/scripts/wechat_bridge.py` currently uses:

```python
SPLIT_TARGET = 600
MAX_MSG_LEN = 1000
MAX_REPLY_PARTS = 5
```

Normal text is split by sentence or paragraph after 600 characters. A single
message still has a 1000-character hard limit, and one reply can contain at
most 5 parts. Tables, code blocks, and very long content can still be split.
The threshold was increased from 100 after one reply arrived as three messages.

## Research requests and reports

The bridge routes research requests by the source explicitly named by the user:

- `帮我调研一下…` (without 小红书/XHS/红书) uses normal web research only.
  It must not invoke any 小红书 skill, MCP tool, or source.
- `用小红书帮我调研一下…`, or another research request that explicitly names
  小红书/XHS/红书, uses the installed `xhs-research` / 小红书 skill flow. The
  skill checks MCP availability and login before researching.

For either route, the bridge immediately replies exactly:

```text
Okay, I'm starting to do the investigation.
```

This acknowledgement is sent before the request enters a worker queue or starts
the actual investigation. The completed report is Markdown and follows the
preferred research presentation: conclusion first, scope, comparison table,
key evidence/cost or configuration breakdown, risks, recommendation,
limitations, and sources (小红书 reports label their sources as `参考笔记`).

The bridge saves the final report only in `ai/memo/investigation/` and sends
that same file back through WeChat. Names use China-local date and a safe
subject slug:

```text
YYMMDD-<subject>.md
```

For example, on 2026-08-29: `260829-显示器支架.md`. A repeated same-subject
report is preserved with `-2`, `-3`, and so on; it never overwrites an earlier
report. The legacy `wechat_work/research/` directory is no longer used for new
reports.

## Calendar reminders

The bridge contains a Codex-interpreted calendar-reminder flow. Every incoming
WeChat message first gets a small, isolated Codex intent pass; reminder wording
is not matched by a hard-coded `几点提醒我` command. Codex only turns the request
into a validated time and task; all subsequent scheduling, calendar access,
delivery, retries, and cancellation are ordinary bridge code. When Codex can
resolve a future time and task, the bridge immediately:

1. stores a durable WeChat reminder in
   `scripts/weixin_bridge/calendar_reminders.sqlite3`;
2. creates a 30-minute iCloud Calendar event with a **10-minute display alarm**;
3. sends `⏰ 提醒：…` at the event start time. The scheduler runs inside the
   systemd bridge and sends overdue reminders after a restart.

Time is interpreted as `Asia/Shanghai`. The local timer is saved first: an
iCloud failure is recorded but never prevents the WeChat reminder. An immediate
natural-language cancellation is interpreted by Codex, then cancelled by code
locally and, when available, in iCloud.

### One-time iCloud configuration

The CalDAV skill keeps secrets outside the bridge. Create
`/home/dedeanzheng/.codex/skills/caldav-icloud/credentials.env`, mode `600`,
with `APPLE_ID` and an Apple **app-specific password**. Do not paste either
value into WeChat, chat, source files, or bridge configuration.

Discover the destination calendar with:

```bash
cd /home/dedeanzheng/.codex/skills/caldav-icloud
./caldav.sh calendars
```

The existing `AI助手` calendar has ID `ai-calendar`. Its non-secret bridge configuration is at
`wechat-time/scripts/weixin_bridge/calendar_reminder.json`:

```json
{
  "calendar_id": "ai-calendar"
}
```

Restart `wechat-bridge.service` after adding this configuration. Until both the
credentials and `calendar_id` exist, iCloud sync will fail but local WeChat
timers will still be created and delivered.

On this VPS, invoke the helper through Bash because its file mode is not
executable:

```bash
cd /home/dedeanzheng/.codex/skills/caldav-icloud
bash ./caldav.sh calendars
```

The bridge converts UTC+8 timestamps to CalDAV's required
`YYYYMMDDTHHMMSS` format. The helper uses portable Python date arithmetic, so
the 30-minute event duration works on Debian as well as macOS.

## Notion long tasks

Send a WeChat message containing `长任务`, for example `整理产品方案，长任务`.
The bridge consumes that message (it does not send it to the normal Codex task
queue), finds the active week beneath `Habit Tracker`, then inserts an
unchecked Notion to-do directly below today's `周一`–`周日` marker in its
assigned column. It uses `Asia/Shanghai` for the current day. An identical task
already present in that day's section is rejected and reported back in WeChat.

For the current layout, the resolver found the `0824-0830` weekly page and its
`周六` marker. It resolves those IDs dynamically on every request, so it does
not depend on this week's IDs continuing to exist.

The Notion credential is stored only in `wechat-time/.env` as
`NOTION_ACCESS_TOKEN`, which must remain mode `600` and is ignored by the
`wechat-time` Git repository. Do not add this credential to documentation,
source code, or Git.

## Security and capacity

- Codex app-server uses `approvalPolicy=never` and `danger-full-access`; this
  was explicitly accepted for this deployment. Only trusted users should use
  the bound bot account.
- Do not commit `account.json`, `auth.json`, `state.json`, logs, QR images,
  `calendar_reminders.sqlite3`, `calendar_reminder.json`, `.env`, or generated
  work files.
- Root disk is about 9.7 GB, with roughly 695 MB free after dependencies.
  Check `df -h /` before installing packages or generating large files.

## References

- Repository: `https://github.com/li1bai0/wechat-channel.git`
- Deployment plan: `ai/memo/VPS_DEPLOY_PLAN.md`
- Manual: `ai/memo/DEPLOY_REMOTE.md`
- Unit template: `wechat-bridge.service`
