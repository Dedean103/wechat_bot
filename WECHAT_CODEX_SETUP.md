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
- Notion week lookup: checks both the week-start and week-end month pages, including year-crossing weeks
- Investigation completion records: active; successful reports are recorded in `ai/memo/knowledge.md`
- Research-report archival guard: active; final reports are stored in AI Memo, never retained in `wechat_work/research/`
- Question source precedence: active; question messages use `knowledge.md` as authoritative context, then WeChat memory, then web search
- Knowledge recall: active; records are numbered and list-style questions scan the complete `knowledge.md` store
- Diary capture: active; `diary`/`ddd`/`日记` entries are saved newest-first to `ai/memo/diary.md` and synchronized to the Notion diary page

## Layout

```text
/home/dedeanzheng/memo/
├── ai/memo/
│   ├── diary.md               # newest-first diary entries
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
  "task_effort": "medium",
  "notion_diary_page_id": "39571e16-c3e7-81e9-a980-e9583a3ed66d"
}
```

The model fields must be explicit. Empty fields fall back to the bridge's
hard-coded `deepseek-v4-flash`; they do not inherit `config.toml`.
Since this VPS has no DeepSeek proxy, empty fields cause “暂时连不上模型服务”.

The bridge source uses the repository root (`/home/dedeanzheng/memo`) as the
base for durable AI data. Therefore research reports are saved to
`/home/dedeanzheng/memo/ai/memo/investigation/`, completion records to
`/home/dedeanzheng/memo/ai/memo/knowledge.md`, and diary entries to
`/home/dedeanzheng/memo/ai/memo/diary.md`. The agent work directory remains
`/home/dedeanzheng/memo/wechat-time/wechat_work/` for temporary work, memory,
and received inbox files; it is not the archive location for new research
reports.

## Knowledge store recall

The durable Markdown knowledge store is
`/home/dedeanzheng/memo/ai/memo/knowledge.md`. It is loaded for questions,
list-style requests, and requests that explicitly mention the knowledge store,
`knowledge`, or `MDS`.

The bridge presents blank-line-separated notes as numbered knowledge records.
For requests containing words such as `哪些`, `有哪些`, `全部`, `所有`, `列出`,
or `分别`, the prompt requires an exhaustive scan from the first record through
the last record and requires every directly relevant record to be covered. The
bridge no longer drops the beginning of the file with a fixed tail-only limit.
Research requests that explicitly require web or 小红书 sources remain on their
research source route and do not receive the knowledge store as a competing
source.

Useful local checks after changing the bridge:

```bash
cd /home/dedeanzheng/memo
python3 -m py_compile wechat-time/scripts/wechat_bridge.py
python3 wechat-time/scripts/test_diary.py
```

The regression scenario is `日本有哪些清酒店？`. Its answer context must retain
the Kurashiki, Tokyo/Yokohama, Kyoto, and Obuse-related records from
`knowledge.md`, rather than returning only a representative subset.

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

The bridge routes research and report-producing requests by the source explicitly named by the user:

- `帮我调研一下…` (without 小红书/XHS/红书) uses normal web research only.
  It must not invoke any 小红书 skill, MCP tool, or source.
- `用小红书帮我调研一下…`, or another research request that explicitly names
  小红书/XHS/红书, uses the installed `xhs-research` / 小红书 skill flow. The
  skill checks MCP availability and login before researching.
- Requests such as `分析一下…并产出报告`, `生成报告`, `写报告`, and `做报告`
  use the same durable report-archive flow even when they do not literally
  contain `调研` or `研究`.

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
reports. `wechat_work/` is strictly a temporary agent-work area for memory,
received files, and disposable intermediate artifacts.

As a defensive fallback, if an agent still creates a Markdown report under
`wechat_work/research/`, the bridge copies its full content into
`ai/memo/investigation/`, sends the archived copy, and removes the temporary
file. This prevents a short final chat message such as
`报告已生成：research/example.md` from being mistakenly saved as the report.

After a report is successfully saved, the bridge appends a completion record to
`ai/memo/knowledge.md` in the form
`{title} completed, reported {filename}`. If the report cannot be saved, no
completion record is added.

## Notion cross-month weeks

Notion weekly pages are titled `MMDD-MMDD` and may be filed under either the
month containing Monday or the month containing Sunday. For example, the week
`0831-0906` may be under `2026-09` even when the current date is August 31.
The resolver calculates the current Monday–Sunday range, checks both candidate
month pages beneath `Habit Tracker`, and matches the exact weekly-page title.
This also handles weeks crossing December and January.

## Calendar reminders

The bridge contains a Codex-interpreted calendar-reminder flow. Every incoming
WeChat message first gets a small, isolated Codex intent pass; reminder wording
is not matched by a hard-coded `几点提醒我` command. Codex only turns the request
into a validated time and task; all subsequent scheduling, calendar access,
delivery, retries, and cancellation are ordinary bridge code. When Codex can
resolve a future time and task, the bridge immediately:

1. stores a durable WeChat reminder in
   `scripts/weixin_bridge/calendar_reminders.sqlite3` and immediately confirms
   it in WeChat;
2. then attempts to create a 30-minute iCloud Calendar event with a **10-minute
   display alarm**;
3. sends `⏰ 提醒：…` at the event start time. The scheduler runs inside the
   systemd bridge and sends overdue reminders after a restart.

Time is interpreted as `Asia/Shanghai`. The local timer is saved and confirmed
first: an iCloud failure is recorded but never prevents the WeChat reminder.
When a time is given without an AM/PM qualifier, the bridge uses the next
future occurrence in that timezone: for example, “两点提醒我……” at 13:00
means 14:00 the same day, while at 23:00 it means 02:00 the next day.
The configured calendar must be a CalDAV resource ID (for example `ai-calendar`),
not a visible calendar name such as `提醒`. An immediate natural-language
cancellation is interpreted by Codex, then cancelled by code locally and, when
available, in iCloud.

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

The bridge is intentionally pinned to the existing `AI助手` calendar, whose
CalDAV resource ID is `ai-calendar`; it rejects any other configured value. Its
non-secret bridge configuration is at
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

## Knowledge capture

Send a WeChat message containing the standalone English keyword `knowledge`
(case-insensitive) or the Chinese keyword `知识`. The bridge removes every
occurrence of the keyword and appends the remaining text to
`ai/memo/knowledge.md`, then confirms the saved text in WeChat. For example,
`knowledge PostgreSQL backups run nightly` appends
`PostgreSQL backups run nightly`.

The keyword is a reserved bridge command: after a successful capture, that
message is not sent to the normal Codex conversation or reminder flow. A
keyword without any remaining content is rejected.

For an incoming question, the bridge detects question wording rather than
requiring a final `?` or `？`. It reads `ai/memo/knowledge.md` and passes it to
Codex as additional context. The source order for a response is strict:

1. A relevant `knowledge.md` entry is the authoritative fact source. It wins
   over WeChat memory, prior conversation, model general knowledge, and web
   results; the agent must not add conflicting advice or search online.
2. If `knowledge.md` has no relevant information, the agent may use
   `wechat-time/wechat_work/wechat_memory.md`. This is secondary context and
   can contain earlier inaccurate answers.
3. If neither local source has relevant information, the agent searches the
   web before answering rather than presenting unverified model knowledge as a
   researched fact.

The bridge injects WeChat memory on every normal chat turn. Knowledge context
is capped at the most recent 16 KB to keep question handling bounded. Restart
`wechat-bridge.service` after changing this logic.

## Diary capture

Send a WeChat message containing a standalone English keyword `diary` or `ddd`
(case-insensitive), or the Chinese keyword `日记`, followed by a thought. The
bridge removes the keyword and saves the remaining text to
`ai/memo/diary.md`. Entries use the format below and are inserted newest first,
with the newest entry directly below the matching date heading:

```md
## 2026-08-31

- 14:30: 今天的想法
```

The same thought is also written to the configured Notion diary page through
the shared `NotionDiaryManager` used by the scheduler. The Notion page ID is a
non-secret `notion_diary_page_id` value in
`wechat-time/scripts/weixin_bridge/backend.json`; the Notion access token
remains only in `wechat-time/.env`.

The local diary is saved first. If Notion is temporarily unavailable, the
local entry is retained and WeChat reports that the Notion write failed. A
message containing only `diary`, `ddd`, or `日记` is rejected.

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
