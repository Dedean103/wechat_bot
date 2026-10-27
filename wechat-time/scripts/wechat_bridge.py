#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex 微信桥（专线版 v2 — 稳定性加固）— iLink Bot API ↔ Codex CLI

用户微信专线：微信 bot 只服务扫码绑定的那个用户，回复由本机 Agent CLI 生成。
按长期稳定运行的单一 bot 连接经验设计：单一 bot 稳定连接、无自动轮换：
  1. 熔断器 + 指数退避 + 抖动：瞬时错误不再线性傻等，连续失败跳闸 OPEN
  2. 会话过期(-14 / ret=-2+"unknown error")：
     - 暂停 10 分钟 → 重置熔断 → 自动重试（不再无限 300s 干等）
     - 发送时先去掉 context_token 重试一次
     - 自动生成新二维码并保存 PNG + 总线通告，等你扫码；不自动切换其他账号
     - 检测到 account.json 被重新注册覆盖后自动热加载新账号
  3. 发送失败持久化 retry_queue.jsonl，启动自动排空重发（防丢消息）
  4. 单实例锁（防双进程并发轮询同一 bot 被服务端踢）
  5. 轮询悬挂看门狗：同步轮询整段卡死超时后自杀，交给看护拉起
  6. 重绑后自动清空旧账号的 codex_session/context_token 残留

用法：
  python wechat_bridge.py register   # 首次注册：出二维码，微信扫码绑定
  python wechat_bridge.py run        # 常驻桥（配合守护脚本保活）
  python wechat_bridge.py status     # 查看当前账号/队列/熔断状态
"""
import ctypes
import base64
import hashlib
import importlib.util
import json
import os
import queue
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import uuid
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from research_diagnostics import capture_diagnostics
from Crypto.Cipher import AES

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent
DATA_DIR = HERE / "weixin_bridge"
ENV_FILE = HERE.parent / ".env"
ACCOUNT_FILE = DATA_DIR / "account.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "bridge.log"
LOCK_FILE = DATA_DIR / "bridge.lock"
RETRY_QUEUE_FILE = DATA_DIR / "retry_queue.jsonl"
REMINDER_DB_FILE = DATA_DIR / "calendar_reminders.sqlite3"
REMINDER_CONFIG_FILE = DATA_DIR / "calendar_reminder.json"
# This bridge's reminder destination is intentionally fixed.  CalDAV uses
# resource IDs, not visible calendar names (the “AI助手” calendar is `ai-calendar`).
ICLOUD_REMINDER_CALENDAR_ID = "ai-calendar"
QR_PNG = DATA_DIR / "qrcode.png"
ACCOUNTS_DIR = DATA_DIR / "accounts"

ILINK_DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"

BUS_SEND = ""  # 可选：桥状态通告地址，默认关闭（仅状态通告，不传私聊内容）

ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MSG_TYPE_USER, MSG_TYPE_BOT, MSG_STATE_FINISH = 1, 2, 2
ERRCODE_SESSION_EXPIRED = -14
ERRCODE_RATE_LIMIT = -2
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
BASH = _GIT_BASH if os.path.exists(_GIT_BASH) else "bash"
_DEFAULT_NODE = r"C:\Program Files\nodejs\node.exe"
_DEFAULT_CODEX_JS = ""  # 留空则自动探测（见 _resolve_tool_paths）
_DEFAULT_CLAUDE_EXE = ""
_DEFAULT_WORK_DIR = ""  # 留空则用仓库根目录下的 wechat_work/
REPLY_TIMEOUT = 600
TURN_HANG_TIMEOUT = 90    # 常驻连接单轮无任何事件超过此秒数视为卡死，静默自愈重试
RESEARCH_HANG_TIMEOUT = 360  # Browser queue + operation must finish before inactivity interruption.
RESEARCH_TOTAL_TIMEOUT = 20 * 60  # Shared across retries for one research execution.
RESEARCH_MAX_ATTEMPTS = 3
RESEARCH_MAX_DELIVERY_ATTEMPTS = 3
RESEARCH_RETRY_BASE = 30
RESEARCH_RETRY_MAX = 15 * 60
RESEARCH_RETRY_POLL = 2
TURN_HANG_TIMEOUT_TASK = 300  # 任务：执行命令时可能长时间无文本事件，放宽到 300s
# 模型分工（2026-08-14 稳定版）：对话用快速档保证秒回，任务用质量档；可在 backend.json 覆盖
CHAT_MODEL = "deepseek-v4-flash"
CHAT_EFFORT = "medium"
CASUAL_EFFORT = "low"              # 简单消息（问候/确认）用低推理档，省 token（backend.json 可覆盖）
TASK_MODEL = "deepseek-v4-flash"
TASK_EFFORT = "medium"
ALIVE_NUDGE_S = 25                 # 代码级保活：单轮超 N 秒无任何输出，主动发“还在处理中”
ALIVE_NUDGE_INTERVAL = 60          # 保活提示最小间隔（秒），防刷屏
BACKEND_FILE = DATA_DIR / "backend.json"
DEFAULT_BACKEND = "codex"
BACKEND_IDENTITY = {"codex": "李白的助手", "claude": "李白的助手", "generic": "AI 助手"}
_RESEARCH_STAGE_LABELS = {
    "backend": "模型后端",
    "model_routing": "模型路由",
    "browser_page": "浏览器页面",
    "archive_processing": "报告归档",
    "model_execution": "模型执行",
    "helper_startup": "助手启动",
    "deadline": "总时限",
    "search": "搜索/页面",
    "archive": "报告归档",
    "wechat_delivery": "微信发送",
    "restart_recovery": "桥重启恢复",
    "retry_limit": "重试上限",
    "bridge": "桥处理",
}


class ResearchDeliveryOutcome(dict):
    """Structured archive/delivery result with legacy truthiness.

    Older callers only checked whether the outcome was handled.  New callers
    inspect ``success``/``stage`` so an archive failure cannot be mistaken for
    a delivered report while the compatibility truth value remains useful in
    existing tests and integrations.
    """

    def __init__(self, *, success=False, handled=True, stage="", reason="",
                 archive_paths=(), summary_sent=False, file_sent=False,
                 notification_sent=False):
        super().__init__(success=bool(success), handled=bool(handled), stage=stage,
                         reason=reason, archive_paths=[str(p) for p in archive_paths if p],
                         summary_sent=bool(summary_sent), file_sent=bool(file_sent),
                         notification_sent=bool(notification_sent))

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __bool__(self):
        # Preserve the old "handled" contract for direct callers.  Research
        # lifecycle code must use ``outcome.success`` explicitly.
        return bool(self.get("handled"))


def _resolve_tool_paths():
    """解析 agent 可执行文件路径：backend.json / 环境变量 / 常见安装位置。"""
    cfg = {}
    try:
        if BACKEND_FILE.exists():
            cfg = json.loads(BACKEND_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    codex_js = cfg.get("codex_js") or os.environ.get("CODEX_JS") or ""
    claude_exe = cfg.get("claude_exe") or os.environ.get("CLAUDE_EXE") or ""
    work_dir = cfg.get("work_dir") or os.environ.get("AGENT_WORK_DIR") or ""
    node_exe = cfg.get("node_exe") or os.environ.get("CODEX_NODE") or shutil.which("node") or _DEFAULT_NODE
    if not codex_js or not os.path.exists(codex_js):
        apd = os.environ.get("APPDATA", "")
        home = os.path.expanduser("~")
        cands = []
        w = shutil.which("codex")
        if w:
            cands.append(w)
        cands += [
            _DEFAULT_CODEX_JS,
            os.path.join(home, ".codex", "bin", "codex"),        # 原生 CLI（macOS/Linux）
            os.path.join(home, ".codex", "bin", "codex.exe"),    # 原生 CLI（Windows）
            os.path.join(home, ".local", "bin", "codex"),
            os.path.join(apd, "npm", "node_modules", "@openai", "codex", "bin", "codex.js"),
            os.path.join(home, "node_modules", "@openai", "codex", "bin", "codex.js"),
        ]
        for cand in cands:
            if cand and os.path.exists(cand):
                codex_js = cand
                break
    if not claude_exe or not os.path.exists(claude_exe):
        apd = os.environ.get("APPDATA", "")
        home = os.path.expanduser("~")
        cands = []
        w = shutil.which("claude")
        if w:
            cands.append(w)
        cands += [
            _DEFAULT_CLAUDE_EXE,
            os.path.join(apd, "npm", "claude.cmd"),          # Windows: npm 全局 .cmd 壳
            os.path.join(apd, "npm", "claude"),
            os.path.join(home, ".local", "bin", "claude.exe"),  # 原生安装（Windows/macOS）
            os.path.join(home, ".local", "bin", "claude"),
            os.path.join(apd, "npm", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.js"),
        ]
        for cand in cands:
            if cand and os.path.exists(cand):
                claude_exe = cand
                break
    if not work_dir:
        work_dir = str(HERE.parent / "wechat_work")  # 仓库根下 wechat_work（兑现注释承诺）
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    return node_exe, codex_js, claude_exe, work_dir


CODEX_NODE, CODEX_JS, CLAUDE_EXE, CODEX_WORK_DIR = _resolve_tool_paths()
CODEX_NATIVE = bool(CODEX_JS) and not CODEX_JS.lower().endswith(".js")  # 原生 codex 二进制 vs npm codex.js
INBOX_DIR = Path(CODEX_WORK_DIR) / "inbox"
MEMORY_FILE = Path(CODEX_WORK_DIR) / "wechat_memory.md"  # 每轮注入 + 对话后写回摘要
MEMORY_MAX_ENTRIES = 30
RESEARCH_ARCHIVE_DIR = REPO_ROOT / "ai" / "memo" / "investigation"
AI_MEMO_DIR = REPO_ROOT / "ai" / "memo"
KNOWLEDGE_FILE = REPO_ROOT / "ai" / "memo" / "knowledge.md"
DIARY_FILE = REPO_ROOT / "ai" / "memo" / "diary.md"
NOTION_DIARY_MANAGER_FILE = Path("/home/dedeanzheng/test_env/schduler/notion/notion_diary.py")
_NOTION_DIARY_MANAGER_CLASS = None

# ── 稳定性参数 ──────────────────────────────────────────────────────
CB_FAILURE_THRESHOLD = 6          # 连续失败次数 → 跳闸 OPEN
CB_OPEN_COOLDOWN = 60             # OPEN 后冷却秒数
CB_BASE_DELAY = 5.0               # 指数退避基数
CB_MAX_DELAY = 60.0               # 退避上限
CB_JITTER = 0.5                   # ±50% 抖动
SESSION_EXPIRED_PAUSE = 600       # 会话过期暂停秒数（10 分钟）
HANG_TIMEOUT_S = 180               # 轮询悬挂看门狗阈值（更快的卡死检测）
FLUSH_RETRY_INTERVAL = 30          # 重试队列排空周期
REBIND_ATTEMPT_COOLDOWN = 3600     # 自动重绑二维码触发冷却
EXPIRY_REBIND_THRESHOLD = 6        # 连续 -14 轮数（约 1 小时）后才判定真失效，触发重绑提醒
SEND_RATE_LIMIT = 8                # 微信侧约 10 条/分钟上限，留余量主动限流
SEND_RATE_WINDOW = 60              # 限流窗口秒数
MAX_MSG_LEN = 1000                 # 单条微信消息长度上限：代码块等整体单元的硬切上限
SPLIT_TARGET = 600                 # 普通文本切片目标：回复超 600 字才按句子切片，减少条数
MAX_REPLY_PARTS = 5                # 单次回复最多发5条：iLink 对单条入站消息回复有约10条硬限制，超出会被吞
FINGERPRINT_TTL_S = 2 * 60         # 无消息 ID 时仅短期按内容去重
POLL_TIMEOUT = 45                  # getupdates 长轮询默认超时秒数（跟随服务端 longpolling_timeout_ms 调整）
REMINDER_POLL_INTERVAL = 5         # 到期提醒检查频率；状态保存在 SQLite，重启不会丢
REMINDER_TIMEZONE = "Asia/Shanghai"
CALDAV_SKILL_DIR = Path.home() / ".codex" / "skills" / "caldav-icloud"
CALDAV_HELPER = CALDAV_SKILL_DIR / "caldav.sh"
CALDAV_CREDENTIALS = CALDAV_SKILL_DIR / "credentials.env"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"
NOTION_HABIT_TRACKER_ID = "9d7abbcb-c7f0-4e30-9e9b-86d15763f27e"
NOTION_TIMEOUT = 20


def _load_dotenv(path):
    """Load simple KEY=VALUE entries without adding a dependency."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value
    except FileNotFoundError:
        pass
    except Exception:
        # Startup must stay available even if an optional local secrets file is malformed.
        pass


_load_dotenv(ENV_FILE)

# 提醒意图由 Codex 理解，而非关键词/正则命令。这里仅定义其受限 JSON 协议。
REMINDER_INTERPRETER_PROMPT = """你是微信日历提醒的意图解析器。不要调用工具，只输出一行 JSON，不要 Markdown 或解释。
当前本地时间（{timezone}）：{now}。时区固定为 {timezone}。
根据用户消息判断 action：
- create：用户希望在某个时间提醒自己做某事；请把自然语言时间换成 future 的 YYYY-MM-DDTHH:MM:SS，title 为简短提醒内容。
- 用户只说“几点”而没有明确上午/下午时，理解为最近的那个未来时间：生成今天和明天的上午、下午候选，选择严格晚于当前时间的最早候选。例：当前 18:14，“九点半”是今天 21:30；当前 13:00，“两点”是今天 14:00；当前 23:00，“两点”是明天 02:00。“十二点”对应 00:00 和 12:00。明确日期或时段必须遵守，不得自动改到其它日期或时段。
- cancel：用户取消一个尚未送达的提醒；title 可为空，datetime 可为空。
- list：用户要查看提醒。
- none：其它所有消息。
- clarify：用户希望创建提醒，但时间不确定、限定冲突或指定时间已过去，需要用户补充时间。
只有时间明确、在未来且提醒内容明确时才可 create；提醒时间不确定时 action=clarify。不要猜测。
严格输出：{{\"action\":\"create|cancel|list|none|clarify\",\"datetime\":\"YYYY-MM-DDTHH:MM:SS 或空字符串\",\"title\":\"字符串\"}}\n用户消息："""

SYSTEM_HINT = ("你是{identity}，正在微信里和用户私聊。像日常聊天一样自然回复："
               "口语化、直接、一两句话（内容需要时可以稍长），不要markdown列表、"
               "不要项目符号、不要客套前缀。这是微信后台通道：除非用户明确要求，"
               "不要执行任何语音播报，也不要提起自己是后台进程。\n"
               "回复规矩：绝对不要一大段文字堆在一起——先说结论、再补关键细节，"
               "一条回复尽量控制在几条短消息内（总长 ~600 字内）；一行一个意思，"
               "需要多条时每条说一个要点，宁可分条也别堆成整段；"
               "能用一句话说清绝不多写，长内容只挑重点；只讲结论和必要细节，"
               "删掉客套、过程描述和无关铺垫。用户的消息：\n")

PERMISSION_POLICY_VERSION = 2
PERMISSION_POLICY = (
    "【读取与修改权限】你可读取本机当前账号有权访问的文件来完成用户任务；"
    "读取本身不需要另行确认。不要把凭据、令牌或私密文件内容发到聊天或外部服务，除非用户明确要求。"
    "用户要求的普通、可撤销文件修改可以直接完成。"
    "修改系统或服务配置、定时任务、机器人自身代码或配置、数据库结构或大量记录，"
    "以及删除重要数据、重启或停止服务前，必须获得用户对该具体操作的明确授权。"
    "用户在当前或此前对话中已明确授权的，不要重复索取授权；先完成必要的只读检查和准备，"
    "再执行获授权的操作并报告结果。没有授权时，说明拟做的具体改动并询问一次。"
    "不要把一次授权扩大到其他敏感操作。若实际工具或系统权限不足，说明真实错误，"
    "不要把它说成永久禁止，也不要声称已完成。\n\n"
)

PRIMER = (
    "【本体底稿】你是 {identity}，微信主人是李白。你走的是李白开的微信专线，这个微信 bot 只服务扫码绑定的那个用户。"
    "你和用户在微信私聊，消息直达你的本体会话，不走任何群。你跑在用户的 VPS 上。"
    "用户说话直接，不喜欢废话和客套。你是 AI 不是真人：做不到的事（发文件、发邮箱、打电话、"
    "线下操作）一律不许假装能做，直接说实话。按实际工具权限和用户授权做事，"
    "干完把结果简洁地告诉用户。\n\n"
    + PERMISSION_POLICY +
    "微信提醒由桥接程序统一创建和发送；你绝不直接调用 iCloud/CalDAV、绝不尝试写入"
    "日历。时间明确的提醒会在你回复前由桥接程序保存为微信定时提醒，再同步到固定的"
    "iCloud ‘AI助手’ 日历。时间不明确时，只向用户问清时间和提醒内容。\n\n"
    "干活不许闷着头：只要是在做事（不是纯聊天），动手前先一句话告诉用户你要做什么；"
    "过程中每完成一个关键步骤或节点，主动发一句进展（这一步做了什么、结果如何）；"
    "卡住或需要长时间等待时，主动说明当前状态；全部完成后给一句总结。"
    "宁可多报不可少报，绝不长时间无声无息。\n"
    "回复一律精简：微信里说话简短直接、先结论后细节，长内容拆成几条短消息分开发，"
    "绝不把一大段文字堆在一起。\n\n"
    "遇到复杂任务（大型开发、系统级改动、长流程工程），先做能完成的检查和准备；"
    "确实受工具、权限或能力限制时，说明限制和已有进度。\n\n"
)


# ── 消息分级：仅用于决定是否注入任务执行协议（复杂任务实时报进度）──

def _norm_msg(text):
    """归一化：去空白与常见标点，转小写，用于快速匹配。"""
    return re.sub(r"[\s，。？！!?、,.~～]+", "", str(text or "")).lower()


STOP_PHRASES = ("停", "停止", "停下", "暂停", "先停一下", "等等", "等一下", "等会", "等会儿",
                "别做了", "别干了", "算了", "不做了", "取消", "/stop")
STATUS_PHRASES = ("/status", "状态", "通道状态", "通道正常吗", "线路通吗", "桥正常吗")
NEW_PHRASES = ("/new", "新任务", "新会话", "开新会话", "换个话题", "重开", "从头开始", "清空会话")
SESSIONS_PHRASES = ("/sessions", "会话", "历史", "历史会话", "看历史", "会话列表", "有哪些会话", "看看历史")

COMPLEX_KEYWORDS = (
    "创建", "生成", "分析", "整理", "统计", "处理", "下载", "上传",
    "翻译", "总结", "执行", "脚本", "代码", "程序", "文件", "数据",
    "表格", "文档", "图片", "视频", "网页", "项目", "部署", "修复", "检查", "排查",
    "调研", "设计", "实现", "发文件", "发图片",
)

# 强指令信号：明确要求动手做事（即便以问号/行不行收尾，也是真任务，不是闲聊）
STRONG_TASK_HINTS = (
    "帮我", "请你", "麻烦你", "给我", "写一个", "写份", "写篇", "写个", "写一下",
    "做个", "做一个", "改一下", "改个", "改下", "跑一下", "跑个", "跑下",
    "加上", "添加", "补充", "创建", "生成", "部署", "修复", "排查", "调研", "实现",
    "发文件", "发图片", "下载", "上传", "翻译", "执行", "报告一下",
)

# 问句/状态确认信号：这类消息再长、再带任务词也是对话，不能升级成任务
QUESTION_HINTS = re.compile(
    r"[?？]|吗|么|呢|啥|咋|在干嘛|在吗|在不在|是不是|有没有|能不能|"
    r"可不可以|行不行|什么|怎么|为什么|哪个|几点|多少|如何|多久"
)

# Requests using these words are asking for a complete set, not a few examples.
KNOWLEDGE_EXHAUSTIVE_HINTS = (
    "有哪些", "哪一些", "哪些", "全部", "所有", "每个", "逐个", "分别",
    "列出", "完整", "都有什么", "相关的都",
)

# 纯确认/问候短词：整条消息命中即 casual（秒回、低推理省 token）
CASUAL_PHRASES = (
    "好的", "好", "可以", "没问题", "收到", "谢谢", "嗯", "嗯嗯", "行",
    "行吧", "ok", "OK", "知道了", "明白", "在", "在的", "好的好的",
)


def classify_message(text):
    """语义化粗分流（2026-08-17 正式版）：只做防误判兜底，轻重交给模型理解。
    - 问句/状态确认 → 对话通道（含强任务词的问句也是对话，模型自判）；
    - 超长纯指令（无问号、>80 字）→ 后台任务队列兜底；
    - 其余短消息 → 对话通道，由模型读内容判断轻重：简单直接答、复杂才分步报。"""
    t = str(text or "").strip()
    # 0) 纯确认/问候：秒回档，不进任务队列
    if t in CASUAL_PHRASES:
        return "casual"
    # 1) 问句/状态确认/闲聊：对话，绝不进任务队列
    if QUESTION_HINTS.search(t):
        has_task = (any(kw in t for kw in STRONG_TASK_HINTS)
                    or any(kw in t for kw in COMPLEX_KEYWORDS))
        return "medium" if has_task else "casual"
    # 2) 超长纯指令描述（无问号的长任务要求）→ 后台队列兜底
    if len(t) > 80:
        return "complex"
    # 3) 短指令（帮我/写/做个…）：交模型自己判断轻重，走对话通道
    return "medium"


# ── 调研路由与报告归档 ───────────────────────────────────────────────

_RESEARCH_HINTS = (
    "调研", "研究一下", "研究研究", "帮我研究", "做研究",
    "产出报告", "生成报告", "写报告", "做报告",
)
_XHS_SOURCE_RE = re.compile(r"小红书|红书|\bxhs\b", re.I)
_RESEARCH_NOISE_RE = re.compile(
    r"(?:请|帮我|帮忙|麻烦|用|通过|在|一下|一下下|给我|关于|针对|做个|做一份|"
    r"调研|研究一下|研究研究|研究|小红书|红书|xhs|XHS)", re.I)


def _research_request(text):
    """Return a source route only for an actual research request, never for casual XHS mentions."""
    raw = str(text or "").strip()
    if not raw or not any(hint in raw for hint in _RESEARCH_HINTS):
        return None
    return "xhs" if _XHS_SOURCE_RE.search(raw) else "web"


def _research_subject(text):
    """Derive a safe, readable report subject from a short natural-language request."""
    subject = _RESEARCH_NOISE_RE.sub(" ", str(text or ""))
    subject = re.sub(r"[，。！？!?、,:：;；()（）\[\]【】]", " ", subject)
    subject = re.sub(r"\s+", "-", subject).strip("-_. ")
    subject = re.sub(r"[^\w\-\u4e00-\u9fff]", "-", subject, flags=re.UNICODE)
    subject = re.sub(r"-+", "-", subject).strip("-_.")
    return (subject[:60] or "调研报告")


def _research_instruction(route):
    """Per-turn source boundary; it must survive existing and resumed Codex sessions."""
    common = f"""\n\n【本次调研报告要求】
这是一次调研任务。完成研究后必须输出完整 Markdown 报告，不要只给聊天摘要；桥会把最终报告保存为 .md，并生成内容完全相同的 .txt 文件发送到微信。
最终报告只能交给桥归档到 AI Memo 的 `{RESEARCH_ARCHIVE_DIR}`；不得在微信临时工作目录 `{CODEX_WORK_DIR}` 或其 `research/` 子目录创建最终报告。临时下载和中间文件可以留在工作目录，但最终成果不可以。
研究过程中的进度可简短汇报，最终报告使用中文并按以下顺序：
1. 结论先行
2. 调研范围
3. 方案对比表
4. 关键依据或成本/配置拆解
5. 潜在短板
6. 综合建议
7. 局限性
8. 参考来源
允许使用 Markdown 标题、表格和可点击链接；这条报告格式要求优先于微信的简短聊天偏好。

在完整报告前，必须另起两行严格输出以下结构（摘要不属于 Markdown 报告正文）：
【微信调研摘要】
用 50–100 个中文字符写出可独立阅读的结论摘要：回答推荐/不推荐什么、最重要的依据、适用条件和关键风险；不要用“详见报告”等代指。
【报告正文】
随后输出完整 Markdown 报告；不要在报告外追加【总结】，微信摘要已经单独提供。桥只会把“报告正文”归档到本地 Markdown/TXT 文件，并将摘要作为微信文字消息发送。
"""
    if route == "xhs":
        return common + """【来源路由：小红书】
浏览器调用每批最多 3 个，上一批完成后再开始下一批。
用户明确要求用小红书调研。必须使用当前设备已安装的 xhs-research / xiaohongshu 技能流程：先检查 MCP 可用性与登录状态，再用小红书 MCP 检索、阅读详情和评论。严格遵守该技能的固定报告格式；参考来源列为“参考笔记”，不得用普通网页搜索替代小红书调研。
"""
    return common + """【来源路由：普通网页】
用户没有明确要求小红书。必须仅使用普通网页搜索和网页来源完成调研。绝对不得调用、读取或遵循任何小红书/红书/XHS 相关技能、MCP 工具或网站内容，也不得将小红书笔记或评论作为来源。第 8 节写“参考来源”，提供可点击的网页链接。
"""


def _research_archive_target(request_text, task_id=None):
    """Return a stable per-task archive target to make crash recovery idempotent."""
    from zoneinfo import ZoneInfo
    stem = f"{datetime.now(ZoneInfo('Asia/Shanghai')):%y%m%d}-{_research_subject(request_text)}"
    if task_id:
        stem += "-" + re.sub(r"[^A-Za-z0-9_-]", "", str(task_id))[:40]
    return RESEARCH_ARCHIVE_DIR / f"{stem}.md"


def _save_research_report(text, request_text, target_path=None):
    """Save the final report in the durable ai/memo/investigation archive."""
    from zoneinfo import ZoneInfo
    _, text = _split_research_reply(text)
    if not _is_complete_research_report(text):
        log("调研报告保存失败: 缺少完整报告正文，拒绝归档摘要")
        return None
    try:
        RESEARCH_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        path = Path(target_path) if target_path else _research_archive_target(request_text)
        if not target_path:
            stem = path.stem
            sequence = 2
            while path.exists():
                path = RESEARCH_ARCHIVE_DIR / f"{stem}-{sequence}.md"
                sequence += 1
        report = str(text).strip() + "\n"
        path.write_text(report, encoding="utf-8")
        if path.read_bytes() != report.encode("utf-8"):
            path.unlink()
            raise OSError("归档内容与报告正文不一致")
        return path
    except Exception as e:
        log(f"调研报告保存失败: {str(e)[:120]}")
        return None


_RESEARCH_SUMMARY_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?【微信调研摘要】[ \t]*", re.M)
_RESEARCH_BODY_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?【报告正文】[ \t]*\r?$", re.M)


def _split_research_reply(reply):
    """Return the optional WeChat summary and the Markdown-only report body."""
    text = str(reply or "").strip()
    summary_marker = _RESEARCH_SUMMARY_RE.search(text)
    body_marker = _RESEARCH_BODY_RE.search(text)
    if body_marker:
        summary = (text[summary_marker.end():body_marker.start()].strip()
                   if summary_marker and summary_marker.start() < body_marker.start() else "")
        return summary, text[body_marker.end():].strip()
    if summary_marker:
        return text[summary_marker.end():].strip(), ""
    return "", text


def _is_complete_research_report(text):
    """Reject chat summaries and empty report shells using required report sections."""
    text = str(text or "")
    heading_re = re.compile(r"^(#{1,6})[ \t]+([^\r\n]+)\r?$", re.M)
    headings = list(heading_re.finditer(text))
    populated = []
    for i, heading in enumerate(headings):
        end = next((other.start() for other in headings[i + 1:]
                    if len(other.group(1)) <= len(heading.group(1))), len(text))
        if heading_re.sub("", text[heading.end():end]).strip():
            populated.append(heading.group(2))
    return all(any(re.search(pattern, heading) for heading in populated)
               for pattern in (r"结论", r"调研范围", r"参考(?:来源|笔记)"))


def _fallback_research_summary(report_body):
    """Produce a readable conclusion when an older agent misses the summary marker."""
    text = str(report_body or "")
    conclusion = re.split(r"\n\s*#{1,3}\s*(?:调研范围|方案对比|关键依据|成本|配置|潜在短板|综合建议|局限性|参考来源|参考笔记)", text, maxsplit=1)[0]
    conclusion = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", conclusion)
    conclusion = re.sub(r"[`#>*_|]", "", conclusion)
    conclusion = re.sub(r"\s+", " ", conclusion).strip()
    # A safe excerpt is preferable to padding a thin report with invented claims.
    return conclusion[:100] or "调研报告已完成，请查看随附的 TXT 文件。"


def _save_research_text_copy(markdown_path):
    """Create the WeChat-readable TXT sibling with exactly the Markdown file bytes."""
    try:
        markdown_path = Path(markdown_path)
        txt_path = markdown_path.with_suffix(".txt")
        shutil.copyfile(markdown_path, txt_path)
        if txt_path.read_bytes() != markdown_path.read_bytes():
            txt_path.unlink()
            raise OSError("TXT 副本与 Markdown 正文不一致")
        return txt_path
    except (OSError, shutil.Error) as e:
        log(f"调研 TXT 副本保存失败: {str(e)[:120]}")
        return None


def _report_body_from_reply(reply, file_paths=()):
    """Prefer a complete inline report, then recover from validated local report files."""
    _, reply = _split_research_reply(reply)
    if _is_complete_research_report(reply):
        return reply, None
    work_dir = Path(CODEX_WORK_DIR).resolve()
    allowed_roots = (work_dir, RESEARCH_ARCHIVE_DIR.resolve(), AI_MEMO_DIR.resolve())
    candidates = [str(p) for p in file_paths]
    candidates += re.findall(r"`([^`\r\n]+\.md)`", reply or "")
    candidates += re.findall(r"(?<![\w/])((?:[\w.-]+/)*research/[^\s`<>()[\]]+\.md)", reply or "")
    for candidate in reversed(candidates):
        source = Path(candidate).expanduser()
        if not source.is_absolute():
            source = work_dir / source
        try:
            source = source.resolve()
            if not any(_is_relative_to(source, root) for root in allowed_roots):
                continue
            if source.is_file():
                _, body = _split_research_reply(source.read_text(encoding="utf-8"))
                if _is_complete_research_report(body):
                    return body, source
        except (OSError, UnicodeError, ValueError):
            continue
    return str(reply or "").strip(), None


def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _archive_temp_report_file(file_path, request_text):
    """Move an agent-created temporary research Markdown file into the durable archive."""
    source = Path(file_path).expanduser()
    if not source.is_absolute():
        source = Path(CODEX_WORK_DIR) / source
    try:
        source = source.resolve()
        temp_research_dir = (Path(CODEX_WORK_DIR) / "research").resolve()
        if source.suffix.lower() != ".md" or not source.is_file() or not _is_relative_to(source, temp_research_dir):
            return None
        archived = _save_research_report(source.read_text(encoding="utf-8"), request_text)
        if archived:
            source.unlink()
            log(f"临时调研报告已归档并清理: {source} -> {archived}")
        return archived
    except (OSError, UnicodeError) as e:
        log(f"临时调研报告归档失败: {str(e)[:120]}")
        return None


# ── 微信媒体收发（iLink CDN + AES-128-ECB） ──

def _norm_aes_key(key):
    """把 iLink 的 aes_key 统一成 16 字节：hex32 / base64(hex32) / base64(16字节)。"""
    if isinstance(key, bytes) and len(key) == 16:
        return key
    s = str(key or "").strip()
    try:
        decoded = base64.b64decode(s)
    except Exception:
        decoded = b""
    if len(decoded) == 16:
        return decoded
    text = decoded.decode("utf-8", "replace")
    if len(decoded) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return bytes.fromhex(text)
    if re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return bytes.fromhex(s)
    return decoded


def _sanitize_fname(name):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")
    return (safe or "file")[:120]


def _download_media(item):
    """下载并解密 iLink 消息里的媒体，返回本地路径；失败返回 None。"""
    try:
        sub = item.get("image_item") or item.get("file_item") or item.get("video_item") or item.get("voice_item") or item
        media = sub.get("media") or {}
        cdn_url = media.get("full_url") or sub.get("cdn_url") or sub.get("full_url") or sub.get("url")
        if not cdn_url and media.get("encrypt_query_param"):
            cdn_url = f"{CDN_BASE}/download?encrypted_query_param={urllib.parse.quote(str(media['encrypt_query_param']))}"
        aes_key = media.get("aes_key") or sub.get("aes_key")
        if not aes_key and sub.get("aeskey"):
            aes_key = base64.b64encode(bytes.fromhex(str(sub["aeskey"]))).decode()
        fname = sub.get("file_name") or media.get("file_name") or ("image.jpg" if item.get("type") == ITEM_IMAGE else "file")
        if not cdn_url:
            log("媒体项无 CDN 地址，跳过")
            return None
        with urllib.request.urlopen(cdn_url, timeout=30) as r:
            data = r.read()
        if aes_key:
            key = _norm_aes_key(aes_key)
            if len(key) != 16:
                log(f"媒体 AES key 异常({len(key)}字节)，跳过")
                return None
            data = AES.new(key, AES.MODE_ECB).decrypt(data)
            pad = data[-1] if data else 0
            if 1 <= pad <= 16 and all(b == pad for b in data[-pad:]):
                data = data[:-pad]
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        path = INBOX_DIR / _sanitize_fname(fname)
        path.write_bytes(data)
        log(f"📎 已收媒体: {path.name} ({len(data)} bytes)")
        return str(path)
    except Exception as e:
        log(f"媒体下载失败: {str(e)[:120]}")
        return None


def _upload_media(base_url, token, path, media_type, to_user_id):
    """AES 加密上传文件到微信 CDN，返回 sendmessage 需要的引用参数。"""
    data = Path(path).read_bytes()
    if len(data) > 50 * 1024 * 1024:
        raise ValueError(f"文件过大：{len(data) // 1024 // 1024}MB（上限 50MB）")
    filekey = os.urandom(16).hex()
    aeskey_hex = os.urandom(16).hex()
    key = bytes.fromhex(aeskey_hex)
    pad = 16 - len(data) % 16
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(data + bytes([pad]) * pad)
    body = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data).hexdigest(),
        "filesize": len(encrypted),
        "no_need_thumb": True,
        "aeskey": aeskey_hex,
        "base_info": {"channel_version": "1.0.2"},
    }
    resp = _req(f"{base_url}/{EP_GET_UPLOAD_URL}", body, token=token, timeout=30)
    upload_param = resp.get("upload_param") or resp.get("uploadParam") or ""
    if not upload_param:
        raise ValueError(f"getuploadurl 无 upload_param: {json.dumps(resp, ensure_ascii=False)[:200]}")
    upload_url = f"{CDN_BASE}/upload?encrypted_query_param={urllib.parse.quote(upload_param)}&filekey={urllib.parse.quote(filekey)}"
    req = urllib.request.Request(upload_url, data=encrypted,
                                 headers={"Content-Type": "application/octet-stream"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        download_param = r.headers.get("x-encrypted-param") or upload_param
    return {
        "aes_key": aeskey_hex,
        "file_key": filekey,
        "file_size": len(data),
        "file_size_encrypted": len(encrypted),
        "file_name": Path(path).name,
        "encrypt_query_param": download_param,
    }


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _safe_print(s=""):
    try:
        print(s, flush=True)
    except Exception:
        pass


def _req(url, payload=None, token=None, timeout=20, method=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def bus_notice(content):
    """仅桥状态通告（不含私聊内容）"""
    try:
        _req(BUS_SEND, {"agent": "Codex桥", "content": content})
    except Exception:
        pass


def _toast(title, text):
    """右下角 Windows Toast 通知（不阻塞、不影响任务）。"""
    if os.name != "nt":
        return  # 非 Windows 无 Toast，静默跳过
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-File",
                          _TOAST_PS1,
                          "-Title", title, "-Text", text],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"Toast 提醒失败: {str(e)[:80]}")


def _is_stale_session_ret(ret, errcode, errmsg):
    """ret=-2 + 'unknown error' = stale session 信号（同 errcode=-14）"""
    if ret != ERRCODE_RATE_LIMIT and errcode != ERRCODE_RATE_LIMIT:
        return False
    return bool(errmsg) and "unknown error" in str(errmsg).lower()


# ───────────────────────── 熔断器 ─────────────────────────────────────

class _CircuitBreaker:
    """三态 CLOSED/OPEN/HALF_OPEN + 指数退避 + 抖动，防止错误雪崩。"""

    def __init__(self, name, failure_threshold=CB_FAILURE_THRESHOLD,
                 open_cooldown=CB_OPEN_COOLDOWN, base_delay=CB_BASE_DELAY,
                 max_delay=CB_MAX_DELAY, jitter_factor=CB_JITTER):
        self._name = name
        self._threshold = failure_threshold
        self._cooldown = open_cooldown
        self._base = base_delay
        self._max = max_delay
        self._jitter = jitter_factor
        self._state = "CLOSED"
        self._failures = 0
        self._opened_at = 0.0
        self._last_delay = 0.0

    @property
    def state(self):
        return self._state

    @property
    def consecutive_failures(self):
        return self._failures

    @property
    def last_delay(self):
        return self._last_delay

    def before_request(self):
        """CLOSED 放行；OPEN 冷却到期后转 HALF_OPEN 放行。"""
        if self._state != "OPEN":
            return True
        if time.time() - self._opened_at >= self._cooldown:
            self._state = "HALF_OPEN"
            return True
        return False

    def remaining_cooldown(self):
        if self._state != "OPEN":
            return 0.0
        return max(0.0, self._cooldown - (time.time() - self._opened_at))

    def on_success(self):
        self._state = "CLOSED"
        self._failures = 0
        self._last_delay = 0.0

    def on_failure(self, is_rate_limit=False):
        """记录失败并返回本次应等待秒数（指数退避 + 抖动；限流 ×2）。"""
        self._failures += 1
        mult = 2.0 if is_rate_limit else 1.0
        delay = min(self._base * (2 ** (self._failures - 1)), self._max) * mult
        jitter_range = delay * self._jitter
        delay = max(1.0, delay + random.uniform(-jitter_range, jitter_range))
        self._last_delay = delay
        if self._failures >= self._threshold:
            self._state = "OPEN"
            self._opened_at = time.time()
            self._failures = 0
        return delay


# ───────────────────────── 发送重试队列（防丢消息） ─────────────────────────

def _save_to_retry_queue(to, text, context_token=None, research_task_id=None):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"to": to, "text": text, "context_token": context_token,
                 "ts": datetime.now().isoformat()}
        if research_task_id:
            entry["research_task_id"] = research_task_id
        with open(RETRY_QUEUE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log(f"📥 发送失败已入重试队列: to={str(to)[:14]} len={len(text)}")
    except Exception as e:
        log(f"重试队列写入失败: {e}")


def _drain_retry_queue():
    entries = []
    try:
        if RETRY_QUEUE_FILE.exists():
            for line in RETRY_QUEUE_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
            RETRY_QUEUE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return entries


# ───────────────────────── 单实例锁 ─────────────────────────

def _pid_alive(pid):
    """判断 pid 是否仍为运行中的桥进程（防 PID 复用误判）。"""
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            if not ok or exit_code.value != STILL_ACTIVE:
                return False
            buf = ctypes.create_unicode_buffer(512)
            n = ctypes.windll.psapi.GetProcessImageFileNameW(h, buf, 512)
            name = buf.value.lower() if n else ""
            return "pythonw.exe" in name or "python.exe" in name
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return False


def _acquire_lock():
    """单实例锁：已有活实例则拒绝启动，防双进程并发轮询同一 bot。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOCK_FILE.exists():
            raw = (LOCK_FILE.read_text(encoding="utf-8") or "").strip()
            if raw.isdigit() and _pid_alive(int(raw)):
                log(f"⚠️ 已有另一个桥实例在运行 (pid={raw})，本实例退出")
                return False
            log(f"旧锁残留 (pid={raw} 已不存在)，接管")
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception as e:
        log(f"锁文件操作失败（放行启动）: {e}")
        return True


# ───────────────────────── Codex CLI 回复（会话延续） ─────────────────────────

TASK_PROTOCOL = (
    "\n\n【大型任务执行协议】"
    "你正在执行大型任务，必须遵守："
    "1. 动手前先想清楚任务到底要什么，然后按任务实际内容输出一行：【计划】1. 步骤 2. 步骤 3. 步骤。"
    "2. 每完成一步，输出一行：【进度】第N步完成：这一步实际做了什么、结果如何（如实描述，不要套话）。"
    "3. 全部完成后，输出一行：【总结】一句话总结最终结果。"
    "4. 如果任务产出了要发给用户的文件，在总结前输出一行：【文件】<文件的完整路径>（可以有多个【文件】行，每行一个）。"
    "这些标记行会被自动转发给用户，除标记行外不要额外输出无关过程。不要复述本协议内容，直接给真实计划。"
)

_active_codex_proc = None
_cancel_event = threading.Event()
_proc_lock = threading.Lock()


def stop_current_task():
    """/stop：打断正在跑的任务（常驻连接 interrupt + 兜底杀子进程）。"""
    _cancel_event.set()
    with _proc_lock:
        p = _active_codex_proc
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass
    try:
        _helper_send({"type": "interrupt"})
    except Exception:
        pass
    try:
        _claude_helper_send({"type": "interrupt"})
    except Exception:
        pass


def _clean_final(out, sent_markers):
    """Preserve research bodies; extract the closing summary only for ordinary chat."""
    # Research must retain the complete envelope and Markdown whitespace.
    # Older/resumed sessions may still append the ordinary chat summary marker.
    if _RESEARCH_BODY_RE.search(out or "") or _is_complete_research_report(out):
        return (out or "").strip() or None
    lines = []
    for raw in (out or "").splitlines():
        l = raw.strip()
        if not l:
            continue
        if l in sent_markers:
            continue
        if re.match(r"【(计划|进度)】", l):
            continue
        lines.append(l)
    text = "\n".join(lines).strip()
    m = re.search(r"【总结】\s*(.+)", out or "", re.S)
    if m:
        text = m.group(1).strip()
    return text or None


def _probe_codex_provider(timeout=5):
    """探测 Codex 模型服务（本地代理等）是否可达，避免回复线程挂死。"""
    urls = []
    try:
        cfg = Path(os.path.expanduser("~/.codex/config.toml"))
        if cfg.exists():
            text = cfg.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"base_url\s*=\s*\"([^\"]+)\"", text):
                u = m.group(1).strip().rstrip("/")
                if u and u not in urls:
                    urls.append(u)
            if "127.0.0.1:4000" in text and "http://127.0.0.1:4000/v1" not in urls:
                urls.append("http://127.0.0.1:4000/v1")
    except Exception:
        return True
    if not urls:
        return True
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    for u in urls:
        try:
            req = urllib.request.Request(u.rstrip("/") + "/models", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


_APP_SERVER_PORT = 38123
_APP_SERVER_URL = f"ws://127.0.0.1:{_APP_SERVER_PORT}"
_HELPER_JS = str(Path(__file__).resolve().parent / "codex_ws_helper.mjs")
_CLAUDE_HELPER_PY = str(Path(__file__).resolve().parent / "claude_helper.py")
_TOAST_PS1 = str(Path(__file__).resolve().parent / "show_toast.ps1")
_app_server_proc = None
_helper_proc = None
_helper_in = None
_helper_out = None
_helper_q = queue.Queue()
_helper_reader = None
_helper_lock = threading.Lock()
_turn_queues = {}                 # req_id -> queue.Queue：事件按 turn 路由，防并发互抢饿死
_turn_queues_lock = threading.Lock()
_claude_proc = None
_claude_in = None
_claude_out = None
_claude_q = queue.Queue()
_claude_reader = None
_claude_lock = threading.Lock()
_claude_turn_queues = {}          # claude 常驻：req_id -> queue.Queue
_claude_turn_queues_lock = threading.Lock()
_state_lock = threading.Lock()
_active_turns = set()
_active_turns_lock = threading.Lock()


def _port_listening(port, host="127.0.0.1"):
    import socket
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except Exception:
        return False


def _ensure_app_server():
    """确保 Codex app-server 常驻服务在跑（常驻进程）。"""
    global _app_server_proc
    if _port_listening(_APP_SERVER_PORT):
        return True
    log("🔄 启动 Codex app-server 常驻服务…")
    if CODEX_NATIVE:
        cmd = [CODEX_JS, "app-server", "--listen", _APP_SERVER_URL]
    else:
        cmd = [CODEX_NODE, CODEX_JS, "app-server", "--listen", _APP_SERVER_URL]
    _app_server_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    capture_diagnostics(_app_server_proc.stdout, DATA_DIR / "diagnostics", "app-server")
    for _ in range(20):
        time.sleep(1)
        if _port_listening(_APP_SERVER_PORT):
            return True
    return False


def _helper_restart():
    """卡死自愈：杀掉助手与 app-server，整体重拉（静默，不打扰用户）。"""
    global _helper_proc, _helper_in, _helper_out, _helper_q, _helper_reader, _app_server_proc
    with _helper_lock:
        if _helper_proc:
            try:
                _helper_proc.kill()
            except Exception:
                pass
            _helper_proc = None
            _helper_in = None
            _helper_out = None
            _helper_q = queue.Queue()
            with _turn_queues_lock:
                _turn_queues.clear()
            _helper_reader = None
        if _app_server_proc and _app_server_proc.poll() is None:
            try:
                _app_server_proc.kill()
            except Exception:
                pass
            _app_server_proc = None
    time.sleep(3)
    return _helper_ensure()


def _helper_start_reader():
    """常驻读线程：把助手 stdout 事件推入队列（消除阻塞 readline 导致超时失效的问题）。"""
    global _helper_reader

    def _read():
        for line in iter(_helper_out.readline, ""):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            rid = obj.get("id")
            if rid:
                with _turn_queues_lock:
                    q = _turn_queues.get(rid)
                if q is not None:
                    q.put(obj)
                    continue
            _helper_q.put(obj)  # ready/无 id 事件：进共享队列
        _helper_q.put({"type": "exit"})
        with _turn_queues_lock:
            qs = list(_turn_queues.values())
        for q in qs:
            q.put({"type": "exit"})

    _helper_reader = threading.Thread(target=_read, daemon=True)
    _helper_reader.start()


def _helper_ensure():
    """确保 WS 连接助手常驻（等待 ready）。"""
    global _helper_proc, _helper_in, _helper_out, _helper_q
    if _helper_proc and _helper_proc.poll() is None:
        return True
    if not _ensure_app_server():
        log("⚠️ app-server 启动失败")
        return False
    log("🔄 启动 Codex 常驻连接助手…")
    try:
        _helper_proc = subprocess.Popen([CODEX_NODE, _HELPER_JS],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="replace",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _helper_in = _helper_proc.stdin
        capture_diagnostics(_helper_proc.stderr, DATA_DIR / "diagnostics", "codex-helper")
        _helper_out = _helper_proc.stdout
        _helper_q = queue.Queue()
        with _turn_queues_lock:
            _turn_queues.clear()
    except Exception as e:
        log(f"助手启动失败: {str(e)[:120]}")
        return False
    _helper_start_reader()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            obj = _helper_q.get(timeout=5)
        except queue.Empty:
            continue
        t = obj.get("type")
        if t == "ready":
            log("✅ Codex 常驻连接就绪")
            return True
        if t == "exit":
            log("⚠️ 助手进程退出")
            return False
        if t == "error":
            log(f"助手初始化错误: {obj.get('message')}")
            return False
    log("⚠️ 助手 ready 超时，已终止")
    try:
        _helper_proc.kill()
    except Exception:
        pass
    return False


def _helper_send(cmd):
    with _helper_lock:
        if not _helper_in:
            return False
        _helper_in.write(json.dumps(cmd, ensure_ascii=False) + "\n")
        _helper_in.flush()
        return True


# ───────────────────────── Claude Code 常驻连接（对等 codex_ws_helper） ─────────────────────────

def _claude_helper_start_reader():
    """常驻读线程：把 claude 助手 stdout 事件推入队列（按 req_id 路由）。"""
    global _claude_reader

    def _read():
        for line in iter(_claude_out.readline, ""):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            rid = obj.get("id")
            if rid:
                with _claude_turn_queues_lock:
                    q = _claude_turn_queues.get(rid)
                if q is not None:
                    q.put(obj)
                    continue
            _claude_q.put(obj)  # ready/无 id 事件：进共享队列
        _claude_q.put({"type": "exit"})
        with _claude_turn_queues_lock:
            qs = list(_claude_turn_queues.values())
        for q in qs:
            q.put({"type": "exit"})

    _claude_reader = threading.Thread(target=_read, daemon=True)
    _claude_reader.start()


def _kill_stray_claude_helpers():
    """清理残留的 claude_helper 进程（防孤儿堆积）。只清本助手，不动用户 claude.exe。"""
    if os.name != "nt":
        try:
            subprocess.run(["pkill", "-f", "claude_helper.py"], capture_output=True, timeout=10)
        except Exception:
            pass
        return
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and "
             "$_.CommandLine -match 'claude_helper.py' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _claude_helper_ensure():
    """确保 Claude 常驻子进程就绪（等待 ready）。"""
    global _claude_proc, _claude_in, _claude_out, _claude_q
    if _claude_proc and _claude_proc.poll() is None:
        return True
    if not CLAUDE_EXE or not os.path.exists(CLAUDE_EXE):
        log("⚠️ claude 可执行文件未找到，跳过常驻助手")
        return False
    log("🔄 启动 Claude 常驻连接助手…")
    try:
        _kill_stray_claude_helpers()
        env = dict(os.environ)
        env["CLAUDE_WORK_DIR"] = str(CODEX_WORK_DIR)
        env["CLAUDE_CLI"] = CLAUDE_EXE
        _claude_proc = subprocess.Popen([sys.executable, "-u", _CLAUDE_HELPER_PY],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="replace", env=env,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _claude_in = _claude_proc.stdin
        capture_diagnostics(_claude_proc.stderr, DATA_DIR / "diagnostics", "claude-helper")
        _claude_out = _claude_proc.stdout
        _claude_q = queue.Queue()
        with _claude_turn_queues_lock:
            _claude_turn_queues.clear()
    except Exception as e:
        log(f"Claude 助手启动失败: {str(e)[:120]}")
        return False
    _claude_helper_start_reader()
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            obj = _claude_q.get(timeout=5)
        except queue.Empty:
            continue
        t = obj.get("type")
        if t == "ready":
            log("✅ Claude 常驻连接就绪")
            return True
        if t == "exit":
            log("⚠️ Claude 助手进程退出")
            return False
        if t == "error":
            log(f"Claude 助手初始化错误: {obj.get('message')}")
            return False
    log("⚠️ Claude 助手 ready 超时，已终止")
    try:
        _claude_proc.kill()
    except Exception:
        pass
    return False


def _claude_helper_restart():
    """卡死自愈：杀掉 claude 助手，整体重拉（静默）。"""
    global _claude_proc, _claude_in, _claude_out, _claude_q, _claude_reader
    with _claude_lock:
        if _claude_proc:
            try:
                _claude_proc.kill()
            except Exception:
                pass
            _claude_proc = None
            _claude_in = None
            _claude_out = None
            _claude_q = queue.Queue()
            with _claude_turn_queues_lock:
                _claude_turn_queues.clear()
            _claude_reader = None
    time.sleep(3)
    return _claude_helper_ensure()


def _claude_helper_send(cmd):
    with _claude_lock:
        if not _claude_in:
            return False
        _claude_in.write(json.dumps(cmd, ensure_ascii=False) + "\n")
        _claude_in.flush()
        return True


def _extract_files(reply):
    """从最终回复里取出【文件】标记行，返回 (清理后的文本, 文件路径列表)。"""
    files = [m.strip().strip('"').strip("'") for m in re.findall(r"【文件】\s*([^\r\n]+)", reply or "")]
    cleaned = re.sub(r"【文件】[^\r\n]*\r?\n?", "", reply or "").strip()
    return cleaned, files


def _cut_paragraph(text, max_len):
    """把一段文字按句子切成 ≤max_len 的语义块（中英文句号/问号/叹号/分号断句）。"""
    if len(text) <= max_len:
        return [text]
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    chunks = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_len:
            if buf:
                chunks.append(buf)
                buf = ""
            start, n = 0, len(p)
            while start < n:
                remain = n - start
                # 尾巴太短（≤15字）并入当前块，避免「没关系」这种孤字单独成条
                if remain > max_len and remain - max_len <= 15:
                    chunks.append(p[start:])
                    start = n
                    continue
                if remain > max_len:
                    # 在最合适的地方切：优先句末标点，其次句中标点，再其次空格
                    end = start + max_len
                    cut = None
                    for i in range(end, start + max_len // 2, -1):
                        if p[i - 1] in "。！？；!?;":
                            cut = i
                            break
                    if cut is None:
                        for i in range(end, start + max_len // 2, -1):
                            if p[i - 1] in "，、：,: ":
                                cut = i
                                break
                    if cut is None:
                        # 无任何自然断点：超得不多就整段保留，避免乱切
                        if remain <= int(max_len * 1.3):
                            cut = n
                        else:
                            cut = end
                    chunks.append(p[start:cut])
                    start = cut
                else:
                    chunks.append(p[start:])
                    start = n
        elif len(buf) + len(p) + 1 > max_len:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c and c.strip()]


def _cap_parts(chunks, max_parts=MAX_REPLY_PARTS):
    """回复条数上限保护：超出 max_parts 条的尾部合并进最后一条，避免 iLink 吞消息。"""
    if len(chunks) <= max_parts:
        return chunks
    head = chunks[:max_parts - 1]
    tail = "\n".join(chunks[max_parts - 1:])
    return head + [tail]


def _looks_like_document(text):
    """表格/文档类检测：命中代表结构化内容，应整块发送避免被切成碎片。"""
    lines = [l for l in str(text or "").splitlines() if l.strip()]
    if len(lines) < 3:
        return False
    pipe_rows = sum(1 for l in lines if l.count("|") >= 2)
    if pipe_rows >= 2:
        return True
    aligned_rows = sum(1 for l in lines if re.search(r" {2,}", l))
    if aligned_rows >= 3:
        return True
    kv_rows = sum(1 for l in lines if re.search(r"[=|｜]", l))
    return kv_rows >= 2 and kv_rows * 3 >= len(lines)


def _split_by_lines(text, max_len):
    """超长文档按行边界打包，保证每一行完整、不跨条乱切。"""
    lines = [l for l in str(text).splitlines() if l.strip()]
    chunks = []
    buf = ""
    for l in lines:
        if len(l) > max_len:
            if buf:
                chunks.append(buf)
                buf = ""
            for j in range(0, len(l), max_len):
                chunks.append(l[j:j + max_len])
            continue
        if buf and len(buf) + 1 + len(l) > max_len:
            chunks.append(buf)
            buf = l
        else:
            buf = (buf + "\n" + l) if buf else l
    if buf:
        chunks.append(buf)
    return chunks


def _split_weixin_text(text, max_len=MAX_MSG_LEN):
    """微信友好分片：段落/句子感知，代码块整体保留；长回复自动切成多条短消息。"""
    text = str(text or "")
    if not text:
        return []
    if "\n" not in text:
        # 无换行的单段文本：超过 SPLIT_TARGET 字按句子切成多条，短文本一条发
        if len(text) <= SPLIT_TARGET:
            return [text]
        return _cap_parts(_cut_paragraph(text, SPLIT_TARGET))
    # 表格/文档类内容：整块一条发送（≤max_len），超长才按行边界打包，绝不按句子乱切
    if _looks_like_document(text):
        if len(text) <= max_len:
            return [text]
        return _cap_parts(_split_by_lines(text, max_len))
    if len(text) <= SPLIT_TARGET:
        return [text]
    # 1. 先把代码块和普通行区分开
    lines = text.split("\n")
    units = []  # (text, is_code)
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip().startswith("```"):
            block = [lines[i]]
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            if i < n:
                block.append(lines[i])
                i += 1
            units.append(("\n".join(block), True))
        else:
            units.append((lines[i], False))
            i += 1
    # 2. 普通文本按空行分段（一段一个语义块），段落超长按句子切；代码块整体保留
    chunks = []
    para = []

    def flush_para():
        nonlocal para
        if para:
            for seg in _cut_paragraph("\n".join(para), SPLIT_TARGET):
                chunks.append(seg)
            para = []

    for u, is_code in units:
        if is_code:
            flush_para()
            if len(u) > max_len:
                open_line, rest = u.split("\n", 1)
                close = ""
                if rest.rstrip().endswith("```"):
                    rest = rest.rstrip()[:-3].rstrip("\n")
                    close = "```"
                chunks.append(open_line)
                for j in range(0, len(rest), max_len - 8):
                    chunks.append(rest[j:j + max_len - 8])
                if close:
                    chunks.append(close)
            else:
                chunks.append(u)
        elif u.strip() == "":
            flush_para()
        else:
            para.append(u)
    flush_para()
    return _cap_parts([c for c in chunks if c and c.strip()])


def _load_memory():
    """读取微信桥记忆文件（每轮注入，不依赖线程）。"""
    try:
        if MEMORY_FILE.exists():
            return MEMORY_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log(f"记忆读取失败: {str(e)[:80]}")
    return ""


def _load_knowledge():
    """读取持久化知识，作为问题消息的最高优先级事实来源。"""
    try:
        if KNOWLEDGE_FILE.exists():
            return KNOWLEDGE_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log(f"知识读取失败: {str(e)[:80]}")
    return ""


def _knowledge_prompt(text):
    """Build a clearly delimited, exhaustive knowledge context for a user turn."""
    knowledge = _load_knowledge()
    if not knowledge:
        return ""

    # Blank-line separated notes are the current Markdown store's record format.
    # Numbering them makes it harder for the model to treat the file as one
    # continuous passage and accidentally stop after the first useful paragraph.
    records = [part.strip() for part in re.split(r"\n\s*\n+", knowledge) if part.strip()]
    formatted = "\n\n".join(
        f"【知识记录 {index}】\n{record}" for index, record in enumerate(records, 1)
    )
    exhaustive = any(hint in str(text or "") for hint in KNOWLEDGE_EXHAUSTIVE_HINTS)
    mode = (
        "这是一个穷举问题。必须从知识记录 1 扫描到最后一条，返回所有直接相关记录；"
        "不能只挑几条代表项，也不能因为某条较早或较短就省略。"
        if exhaustive else
        "请逐条检查全部知识记录，回答中包含所有与问题直接相关的记录。"
    )
    return (
        "【knowledge.md（最高优先级事实来源）】\n"
        "以下是本地 knowledge.md 的完整内容，空行分隔的内容是独立知识记录。\n"
        f"{mode}\n"
        "如果记录没有相关内容，不要编造；如果有多条相关记录，请合并但逐条覆盖。\n"
        + formatted + "\n\n"
    )


def _should_attach_knowledge(text):
    """Attach the store for questions, list requests, or explicit store requests."""
    raw = str(text or "")
    return (
        bool(QUESTION_HINTS.search(raw))
        or any(hint in raw for hint in KNOWLEDGE_EXHAUSTIVE_HINTS)
        or bool(re.search(r"knowledge|知识库|MDS", raw, re.IGNORECASE))
    )


def _source_precedence_block():
    """Return per-turn instructions that override stale persistent chat context."""
    return """【回答来源优先级（必须遵守）】
如果本轮附带了 knowledge.md，先逐项判断其中是否有与用户问题直接相关的信息；仅仅“知识库非空”不代表有相关信息。只要有相关信息，knowledge.md 就是唯一的事实来源；它与微信记忆、此前聊天内容、你的常识或网页信息冲突时，一律以 knowledge.md 为准。此时直接回答其中的结论，不要补充相冲突的经验性建议，也不要联网搜索。
如果 knowledge.md 未附带，或其中没有相关信息，才可参考微信桥记忆；微信桥记忆是次级上下文，可能含有先前不准确的回答。
只有这两处本地来源都没有相关信息时，才搜索网页后回答；不要把模型自身的泛化经验伪装成查证过的事实。回答时不必解释这套优先级，除非用户询问来源。

"""


def _append_memory(entry):
    """对话后追加一行摘要到记忆文件（原子写 + 锁，超量滚动裁剪）。"""
    try:
        with _state_lock:
            header_lt = "## 长期事实"
            header_chat = "## 最近对话"
            text = ""
            if MEMORY_FILE.exists():
                text = MEMORY_FILE.read_text(encoding="utf-8", errors="replace")
            # 拆分：长期事实段保留原样，最近对话段滚动追加
            if header_chat in text:
                before, _, after = text.partition(header_chat)
                lines = [l for l in after.splitlines() if l.strip()][-MEMORY_MAX_ENTRIES:]
                new_after = "\n" + "\n".join(lines) + ("\n" if lines else "") + entry + "\n"
                text = before + header_chat + new_after
            else:
                text = (text.rstrip() + "\n\n" if text.strip() else "") + header_chat + "\n" + entry + "\n"
            tmp = MEMORY_FILE.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, MEMORY_FILE)
    except Exception as e:
        log(f"记忆写入失败: {str(e)[:80]}")


def _research_checkpoint(checkpoint, stage, error="", session_id=None, partial=None):
    """Send a small durable backend checkpoint without letting it break a turn."""
    if not checkpoint:
        return
    try:
        checkpoint({"stage": stage, "error": str(error or "")[:500],
                    "session_id": session_id, "partial": str(partial or "")[:1000],
                    "ts": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass


def _backend_cancelled(cancel_check=None):
    """Check a durable job cancellation without relying on the global chat flag."""
    if not cancel_check:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        return False


def codex_reply(prompt, session_id=None, on_progress=None, model=None, effort=None, base=None, timeout=None,
                preempt_evt=None, preempt_ctx=None, deadline=None, checkpoint=None, cancel_check=None):
    """通过常驻 app-server 连接生成回复（不每条消息重启 Codex）。"""
    global _helper_proc
    hang_timeout = timeout or TURN_HANG_TIMEOUT
    if _cancel_event.is_set() or _backend_cancelled(cancel_check):
        return None, session_id
    if deadline is not None and time.monotonic() >= deadline:
        _research_checkpoint(checkpoint, "deadline", "deadline expired before provider probe", session_id)
        return None, session_id
    if not _probe_codex_provider():
        _research_checkpoint(checkpoint, "backend", "provider unavailable", session_id)
        log("⚠️ Codex 模型服务不可达（本地代理未启动），跳过本轮")
        return None, session_id
    for attempt in range(3):
        if _cancel_event.is_set() or _backend_cancelled(cancel_check):
            return None, session_id
        if deadline is not None and time.monotonic() >= deadline:
            _research_checkpoint(checkpoint, "deadline", "deadline expired before retry", session_id)
            log("调研执行超时：已达到总时间限制")
            return None, session_id
        if preempt_evt is not None and preempt_evt.is_set():
            return None, session_id
        if _cancel_event.is_set() or _backend_cancelled(cancel_check):
            return None, session_id
        _cancel_event.clear()
        with _helper_lock:
            ok = _helper_ensure()
        if not ok:
            _research_checkpoint(checkpoint, "helper_startup", "Codex helper unavailable", session_id)
            log("⚠️ Codex 常驻服务不可用")
            return None, session_id
        try:
            req_id = uuid.uuid4().hex[:8]
            if preempt_ctx is not None:
                preempt_ctx["req_id"] = req_id
            my_q = queue.Queue()
            with _turn_queues_lock:
                _turn_queues[req_id] = my_q
            partial_accum = ""
            last_partial_checkpoint = time.monotonic()
            try:
                payload = {"type": "turn", "id": req_id, "threadId": session_id,
                           "prompt": prompt, "cwd": CODEX_WORK_DIR}
                if model:
                    payload["model"] = model
                if effort:
                    payload["effort"] = effort
                if base:
                    payload["base"] = base
                if _backend_cancelled(cancel_check):
                    return None, session_id
                if deadline is not None and time.monotonic() >= deadline:
                    _research_checkpoint(checkpoint, "deadline", "deadline expired before turn", session_id)
                    return None, session_id
                if not _helper_send(payload):
                    _research_checkpoint(checkpoint, "backend", "helper send failed", session_id)
                    raise RuntimeError("助手未连接")
                last_event = time.monotonic()
                last_alive = time.monotonic()
                while True:
                    if _cancel_event.is_set():
                        return None, session_id
                    if _backend_cancelled(cancel_check):
                        return None, session_id
                    if preempt_evt is not None and preempt_evt.is_set():
                        return None, session_id
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        _helper_send({"type": "interrupt", "reqId": req_id})
                        _research_checkpoint(checkpoint, "deadline", "overall research deadline", session_id)
                        log("调研执行超时：已达到总时间限制")
                        return None, session_id
                    if now - last_event > hang_timeout:
                        try:
                            _helper_send({"type": "interrupt", "reqId": req_id})
                        except Exception:
                            pass
                        _research_checkpoint(checkpoint, "backend", "single-turn inactivity timeout", session_id)
                        raise RuntimeError("HANG: 单轮无事件超时")
                    if now - last_alive >= ALIVE_NUDGE_INTERVAL and now - last_event >= ALIVE_NUDGE_S:
                        try:
                            if on_progress:
                                on_progress("⏳ 还在处理中…")
                        except Exception:
                            pass
                        last_alive = now
                    try:
                        obj = my_q.get(timeout=5)
                    except queue.Empty:
                        continue
                    last_event = time.monotonic()
                    sid_hint = obj.get("threadId") or obj.get("sessionId")
                    if sid_hint:
                        session_id = sid_hint
                        _research_checkpoint(checkpoint, "session", "", sid_hint)
                    t = obj.get("type")
                    if t == "exit":
                        _research_checkpoint(checkpoint, "helper_startup", "helper exited", session_id)
                        raise RuntimeError("助手退出")
                    if t == "delta":
                        partial_accum += str(obj.get("text") or "")
                        if (len(partial_accum) >= 1000
                                or time.monotonic() - last_partial_checkpoint >= 10):
                            _research_checkpoint(checkpoint, "partial", "", session_id,
                                                 partial_accum[-3000:])
                            partial_accum = ""
                            last_partial_checkpoint = time.monotonic()
                    elif t == "session":
                        _research_checkpoint(checkpoint, "session", "", sid_hint or session_id)
                    elif t == "progress" and on_progress:
                        try:
                            on_progress(obj.get("text") or "")
                        except Exception:
                            pass
                    elif t == "result":
                        text = (obj.get("text") or "").strip()
                        reply = _clean_final(text, set())
                        new_sid = obj.get("threadId") or session_id
                        if partial_accum:
                            _research_checkpoint(checkpoint, "partial", "", new_sid, partial_accum[-3000:])
                        _research_checkpoint(checkpoint, "completed", "", new_sid, reply)
                        return reply or None, new_sid
                    elif t == "error":
                        _research_checkpoint(checkpoint, "backend", obj.get("message") or "turn error", session_id)
                        raise RuntimeError(obj.get("message") or "turn error")
            finally:
                if partial_accum:
                    _research_checkpoint(checkpoint, "partial", "", session_id, partial_accum[-3000:])
                with _turn_queues_lock:
                    _turn_queues.pop(req_id, None)
        except Exception as e:
            msg = str(e)
            _research_checkpoint(checkpoint, "backend", msg, session_id)
            log(f"⚠️ Codex 常驻连接异常(第{attempt+1}次): {msg[:120]}")
            if _backend_cancelled(cancel_check):
                return None, session_id
            if "HANG" in msg:
                if attempt >= 2:
                    log("🧹 多次卡死：放弃旧线程并重启常驻连接（自愈）")
                    session_id = None  # 卡死的旧线程不再续接，改用新线程
                    try:
                        _helper_restart()
                    except Exception as ex:
                        log(f"重启常驻连接异常: {str(ex)[:80]}")
                else:
                    log("🔧 单轮无事件超时：中断重试（保留线程上下文）")
                time.sleep(3)
            elif "thread not found" in msg:
                log("🧹 旧线程失效，自动开新线程重试")
                session_id = None
            else:
                with _helper_lock:
                    if _helper_proc:
                        try:
                            _helper_proc.kill()
                        except Exception:
                            pass
                        _helper_proc = None
                time.sleep(3)
            if _backend_cancelled(cancel_check):
                return None, session_id
    return None, session_id


# ───────────────────────── 多后端适配（Codex / Claude Code / 通用 CLI） ─────────────────────────

def _load_backend():
    """读取 backend.json 选择回复后端。返回 (backend, cfg)。"""
    try:
        if BACKEND_FILE.exists():
            cfg = json.loads(BACKEND_FILE.read_text(encoding="utf-8-sig"))
            b = str(cfg.get("backend") or DEFAULT_BACKEND).strip().lower()
            if b in ("codex", "claude", "generic"):
                return b, cfg
    except Exception as e:
        log(f"backend.json 读取失败（用默认 {DEFAULT_BACKEND}）: {e}")
    return DEFAULT_BACKEND, {}


# ───────────────────────── Notion long-task capture ─────────────────────────

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _notion_token():
    return os.environ.get("NOTION_ACCESS_TOKEN", "").strip()


def _notion_request(method, path, payload=None):
    """Make one Notion API request; never include credentials in errors or logs."""
    token = _notion_token()
    if not token:
        raise RuntimeError("未配置 NOTION_ACCESS_TOKEN")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        NOTION_API_BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=NOTION_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _notion_children(block_id):
    results, cursor = [], None
    while True:
        suffix = f"?page_size=100{('&start_cursor=' + urllib.parse.quote(cursor)) if cursor else ''}"
        page = _notion_request("GET", f"/blocks/{block_id}/children{suffix}")
        results.extend(page.get("results") or [])
        cursor = page.get("next_cursor") if page.get("has_more") else None
        if not cursor:
            return results


def _notion_block_text(block):
    kind = block.get("type", "")
    if kind == "child_page":
        return str((block.get("child_page") or {}).get("title") or "")
    value = block.get(kind) or {}
    rich_text = value.get("rich_text") or []
    return "".join(str(item.get("plain_text") or "") for item in rich_text).strip()


def _notion_normalize_task(value):
    return re.sub(r"[\s，,。；;！!？?、]+", "", str(value or "")).casefold()


def _notion_week_page(today):
    """Resolve today's weekly page, including weeks stored under either month."""
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    week_title = f"{week_start:%m%d}-{week_end:%m%d}"
    month_titles = []
    for month_title in (week_start.strftime("%Y-%m"), week_end.strftime("%Y-%m")):
        if month_title not in month_titles:
            month_titles.append(month_title)
    tracker_children = _notion_children(NOTION_HABIT_TRACKER_ID)
    months = { _notion_block_text(block): block for block in tracker_children
                if block.get("type") == "child_page" }
    for month_title in month_titles:
        month = months.get(month_title)
        if not month:
            continue
        for block in _notion_children(month["id"]):
            if block.get("type") == "child_page" and _notion_block_text(block) == week_title:
                return block
    raise RuntimeError("找不到包含今天的 Notion 周计划页面")


def _notion_day_marker(week_page_id, weekday_label):
    """Return the marker's parent column, its children, and marker index."""
    def walk(parent_id):
        children = _notion_children(parent_id)
        for index, child in enumerate(children):
            if _notion_block_text(child) == weekday_label:
                return parent_id, children, index
        for child in children:
            if child.get("has_children"):
                found = walk(child["id"])
                if found:
                    return found
        return None

    result = walk(week_page_id)
    if not result:
        raise RuntimeError(f"找不到今天的 Notion 区块（{weekday_label}）")
    return result


def _notion_add_long_task(task):
    """Append an unchecked task directly after today's weekday marker."""
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(REMINDER_TIMEZONE)).date()
    week_page = _notion_week_page(today)
    weekday = _WEEKDAY_LABELS[today.weekday()]
    parent_id, siblings, marker_index = _notion_day_marker(week_page["id"], weekday)
    wanted = _notion_normalize_task(task)

    # Stop at the next day marker, so identical tasks on another day are allowed.
    for sibling in siblings[marker_index + 1:]:
        if _notion_block_text(sibling) in _WEEKDAY_LABELS:
            break
        if _notion_normalize_task(_notion_block_text(sibling)) == wanted:
            return "duplicate"

    _notion_request("PATCH", f"/blocks/{parent_id}/children", {
        "after": siblings[marker_index]["id"],
        "children": [{
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": task}}],
                "checked": False,
            },
        }],
    })
    return "created"


def _extract_long_task(text):
    """Return a task only when the explicit 长任务 keyword is present."""
    if "长任务" not in str(text):
        return None
    task = str(text).replace("长任务", "").strip(" \t\r\n，,。；;:：-—()（）[]【】")
    return task or ""


_KNOWLEDGE_TRIM = " \t\r\n，,。；;:：-—()（）[]【】"
_KNOWLEDGE_SEPARATOR = r"[\s，,。；;:：\-—()（）\[\]【】]"
_KNOWLEDGE_START_RE = re.compile(
    rf"^(?:knowledge|知识)(?=$|{_KNOWLEDGE_SEPARATOR})", re.IGNORECASE)
_KNOWLEDGE_END_RE = re.compile(
    rf"(?:(?<={_KNOWLEDGE_SEPARATOR})(?:knowledge|知识)|^(?:knowledge|知识))$",
    re.IGNORECASE)


def _extract_knowledge(text):
    """Capture notes marked by knowledge at an edge, preserving internal words."""
    note = str(text or "").strip(_KNOWLEDGE_TRIM)
    if not (_KNOWLEDGE_START_RE.search(note) or _KNOWLEDGE_END_RE.search(note)):
        return None
    note = _KNOWLEDGE_START_RE.sub("", note, count=1).strip(_KNOWLEDGE_TRIM)
    note = _KNOWLEDGE_END_RE.sub("", note, count=1).strip(_KNOWLEDGE_TRIM)
    return note or ""


def _append_knowledge(note):
    """Append one captured note to the durable Markdown knowledge file."""
    KNOWLEDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        needs_newline = KNOWLEDGE_FILE.exists() and KNOWLEDGE_FILE.stat().st_size > 0
        with KNOWLEDGE_FILE.open("a", encoding="utf-8") as handle:
            if needs_newline:
                handle.write("\n")
            handle.write(str(note).strip() + "\n")


_DIARY_RE = re.compile(r"(?<![A-Za-z])(?:diary|ddd)(?![A-Za-z])|日记", re.IGNORECASE)


def _extract_diary(text):
    """Remove an explicit diary keyword and return the thought to capture."""
    raw = str(text or "")
    if not _DIARY_RE.search(raw):
        return None
    note = _DIARY_RE.sub("", raw).strip(" \t\r\n，,。；;:：-—()（）[]【】")
    return note or ""


def _append_diary_local(note, timestamp=None):
    """Insert one timestamped thought at the newest position in diary.md."""
    from zoneinfo import ZoneInfo

    local_time = (timestamp or datetime.now(timezone.utc)).astimezone(ZoneInfo(REMINDER_TIMEZONE))
    date_heading = f"## {local_time:%Y-%m-%d}"
    thought = str(note).strip().replace("\r\n", "\n").replace("\r", "\n")
    thought = thought.replace("\n", "\n  ")
    entry = f"- {local_time:%H:%M}: {thought}"

    with _state_lock:
        content = DIARY_FILE.read_text(encoding="utf-8") if DIARY_FILE.exists() else ""
        lines = content.splitlines()
        heading_index = next((i for i, line in enumerate(lines) if line.strip() == date_heading), None)
        if heading_index is None:
            updated_lines = [date_heading, "", entry]
            if lines:
                updated_lines.extend(["", *lines])
        else:
            updated_lines = lines[:heading_index + 1] + ["", entry] + lines[heading_index + 1:]
        DIARY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = DIARY_FILE.with_suffix(".md.tmp")
        temp_file.write_text("\n".join(updated_lines).rstrip("\n") + "\n", encoding="utf-8")
        os.replace(temp_file, DIARY_FILE)


def _notion_diary_manager(page_id):
    """Load the tested scheduler NotionDiaryManager without importing its package."""
    global _NOTION_DIARY_MANAGER_CLASS
    if _NOTION_DIARY_MANAGER_CLASS is None:
        if not NOTION_DIARY_MANAGER_FILE.exists():
            raise RuntimeError(f"找不到 Notion 日记管理器: {NOTION_DIARY_MANAGER_FILE}")
        spec = importlib.util.spec_from_file_location("wechat_reused_notion_diary", NOTION_DIARY_MANAGER_FILE)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载 Notion 日记管理器")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _NOTION_DIARY_MANAGER_CLASS = module.NotionDiaryManager
    from zoneinfo import ZoneInfo

    return _NOTION_DIARY_MANAGER_CLASS(
        token=_notion_token(),
        page_id=page_id,
        tzinfo=ZoneInfo(REMINDER_TIMEZONE),
    )


def _append_diary_notion(note, timestamp, page_id):
    """Append the thought through the scheduler's shared Notion diary manager."""
    if not page_id:
        raise RuntimeError("未配置 Notion 日记页面 ID")
    _notion_diary_manager(page_id).append_message(str(note).strip(), timestamp)


def _reminder_db():
    """Open reminder storage; every thread gets its own SQLite connection."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(REMINDER_DB_FILE), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        due_at_utc TEXT NOT NULL,
        timezone TEXT NOT NULL,
        calendar_event TEXT NOT NULL,
        calendar_status TEXT NOT NULL DEFAULT 'pending',
        status TEXT NOT NULL DEFAULT 'scheduled',
        created_at_utc TEXT NOT NULL,
        delivered_at_utc TEXT,
        last_error TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders(status, due_at_utc)")
    # Existing installations created before calendar_status get a safe migration.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
    if "calendar_status" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN calendar_status TEXT NOT NULL DEFAULT 'pending'")
    for name, definition in {
        "google_calendar_id": "TEXT NOT NULL DEFAULT ''",
        "google_event_id": "TEXT NOT NULL DEFAULT ''",
        "google_status": "TEXT NOT NULL DEFAULT 'not_requested'",
        "google_error": "TEXT",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE reminders ADD COLUMN {name} {definition}")
    conn.commit()
    return conn


def _load_reminder_config():
    """Read the non-sensitive default calendar configuration."""
    try:
        cfg = json.loads(REMINDER_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _parse_reminder_json(reply):
    """Accept only the constrained JSON contract returned by Codex."""
    if not reply:
        return None
    match = re.search(r"\{.*\}", str(reply), re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("action") not in {"create", "cancel", "list", "none", "clarify"}:
        return None
    return {"action": obj["action"], "datetime": str(obj.get("datetime") or "").strip(),
            "title": str(obj.get("title") or "").strip()[:200]}


def _reminder_clock_time(text, now):
    """Resolve a simple Chinese clock clause independently of the model.

    Return None for other time formats. A clock clause we cannot safely resolve
    raises ValueError so it cannot reach storage or either calendar unchecked.
    Only inspect the clause before 提醒, never clock words in the task title.
    """
    clause, marker, _ = str(text).partition("提醒")
    if not marker:
        return None
    number = r"[0-9零〇一二两三四五六七八九十]+"
    clock = rf"(?P<hour>{number})(?:点|时|[:：])(?P<minute>半|一刻|三刻|{number}分?)?(?:(?P<second>{number})秒)?"
    if not re.search(rf"{number}(?:点|时|[:：])", clause):
        return None
    match = re.fullmatch(
        rf"\s*(?:请|麻烦|帮我|请帮我|在|到|于|给我|请在|请到|麻烦在|帮我在)*\s*"
        rf"(?P<day>今天|明天|后天|今晚|明早|明晚)?\s*"
        rf"(?P<period>凌晨|早上|早晨|上午|中午|下午|晚上|傍晚)?\s*"
        rf"{clock}\s*(?:整|钟|的时候|时)?\s*[，,]?\s*", clause)
    if not match:
        raise ValueError("unsupported or conflicting clock clause")

    def integer(value):
        if value.isascii() and value.isdigit():
            return int(value)
        digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
        digits.update({"〇": 0, "两": 2})
        if "十" in value:
            tens, units = value.split("十")
            return (digits[tens] if tens else 1) * 10 + (digits[units] if units else 0)
        return digits[value]

    try:
        hour = integer(match["hour"])
        minute_text = match["minute"] or "0"
        if (match["second"] and match["minute"] and minute_text not in {"半", "一刻", "三刻"}
                and not minute_text.endswith("分")):
            raise ValueError("seconds require a minute separator")
        minute = {"半": 30, "一刻": 15, "三刻": 45}.get(minute_text)
        if minute is None:
            minute = integer(minute_text.removesuffix("分"))
        second = integer(match["second"] or "0")
    except (KeyError, ValueError):
        raise ValueError("invalid clock number") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError("invalid clock time")
    day, period = match["day"], match["period"]
    implied_period = {"今晚": "晚上", "明早": "早上", "明晚": "晚上"}.get(day)
    if implied_period and period and period != implied_period:
        raise ValueError("conflicting time periods")
    period = period or implied_period
    if period:
        if period in {"凌晨", "早上", "早晨", "上午"}:
            if hour > 12 or (hour == 12 and period != "凌晨"):
                raise ValueError("conflicting morning hour")
            hours = [hour % 12]
        elif period in {"下午", "晚上", "傍晚"}:
            if (hour == 0 or (period in {"晚上", "傍晚"} and hour not in {6, 7, 8, 9, 10, 11, 18, 19, 20, 21, 22, 23})
                    or (period == "下午" and hour not in {1, 2, 3, 4, 5, 6, 12, 13, 14, 15, 16, 17, 18})):
                raise ValueError("ambiguous afternoon hour")
            hours = [hour % 12 + 12 if hour <= 12 else hour]
        else:
            # 中午一点/十二点 are conventional; other noon hours need confirmation.
            if hour not in {1, 11, 12, 13}:
                raise ValueError("ambiguous noon hour")
            hours = [13 if hour == 1 else hour]
    else:
        hours = [hour % 12, hour % 12 + 12] if 1 <= hour <= 12 else [hour]
    offsets = {"今天": 0, "今晚": 0, "明天": 1, "明早": 1, "明晚": 1, "后天": 2}
    days = [offsets[day]] if day else [0, 1]
    candidates = [now.replace(hour=h, minute=minute, second=second, microsecond=0) + timedelta(days=d)
                  for d in days for h in hours]
    future = [candidate for candidate in candidates if candidate > now]
    if not future:
        raise ValueError("specified time has already passed")
    return min(future).strftime("%Y-%m-%dT%H:%M:%S")


def _reminder_due_at(value):
    """Validate the Codex-normalised China-local time and return UTC ISO text."""
    try:
        from zoneinfo import ZoneInfo
        local = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo(REMINDER_TIMEZONE))
    except Exception:
        return None
    if local <= datetime.now(local.tzinfo):
        return None
    return local.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _create_icloud_reminder(title, local_start):
    """Create the calendar event with its display alarm exactly 10 minutes beforehand."""
    cfg = _load_reminder_config()
    calendar_id = str(cfg.get("calendar_id") or "").strip()
    if not CALDAV_CREDENTIALS.exists():
        return None, "iCloud 日历尚未配置凭据"
    if not calendar_id:
        return None, "尚未设置默认 iCloud 日历"
    if calendar_id != ICLOUD_REMINDER_CALENDAR_ID:
        log(f"拒绝非 AI助手 日历配置: {calendar_id!r}")
        return None, "iCloud 日历配置无效"
    if not CALDAV_HELPER.exists():
        return None, "找不到 iCloud Calendar helper"
    try:
        caldav_start = datetime.strptime(local_start, "%Y-%m-%dT%H:%M:%S").strftime("%Y%m%dT%H%M%S")
    except ValueError:
        return None, "提醒时间格式无效"
    try:
        result = subprocess.run([BASH, str(CALDAV_HELPER), "add", calendar_id, title, caldav_start, "30", "10"],
                                cwd=str(CALDAV_SKILL_DIR), capture_output=True, text=True, timeout=45)
    except Exception as e:
        log(f"日历提醒创建异常: {str(e)[:120]}")
        return None, "写入 iCloud 日历失败"
    output = (result.stdout or "").strip()
    if result.returncode != 0 or not output.startswith("HTTP 2"):
        log(f"日历提醒创建失败: rc={result.returncode} {output[:160]} {(result.stderr or '')[:120]}")
        return None, "写入 iCloud 日历失败"
    return output, None


def _google_calendar_call(action, arguments):
    command = [CODEX_JS] if CODEX_NATIVE else [CODEX_NODE, CODEX_JS]
    request = {"command": command, "cwd": CODEX_WORK_DIR,
               "action": action, "arguments": arguments}
    result = subprocess.run(
        [sys.executable, str(HERE / "google_calendar_bridge.py")],
        input=json.dumps(request), capture_output=True, text=True, timeout=90)
    reply = json.loads(result.stdout)
    if result.returncode or reply.get("error"):
        raise RuntimeError("Google Calendar operation failed")
    return reply.get("data")


def _create_google_reminder(title, local_start, calendar_id):
    try:
        from zoneinfo import ZoneInfo
        start = datetime.strptime(local_start, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo(REMINDER_TIMEZONE))
        data = _google_calendar_call("create_event", {
            "calendar_id": calendar_id, "title": title, "attendees": [],
            "start_time": start.isoformat(), "end_time": (start + timedelta(minutes=30)).isoformat(),
            "timezone_str": REMINDER_TIMEZONE,
            "reminders": {"use_default": False, "overrides": [{"method": "popup", "minutes": 10}]}})
        event = data.get("event", data) if isinstance(data, dict) else {}
        event_id = event.get("id") or event.get("event_id")
        if not event_id:
            raise RuntimeError("Missing Google event ID")
        return event_id, None
    except Exception:
        # Do not retry an ambiguous write: the event may already exist remotely.
        return None, "Google 日历同步未确认，请检查日历后再重试"


def _delete_google_reminder(event_id, calendar_id):
    if not event_id:
        return True
    try:
        _google_calendar_call("delete_event", {"calendar_id": calendar_id, "event_id": event_id})
        return True
    except Exception:
        return False


def _delete_icloud_reminder(event_ref):
    """Best-effort calendar cleanup; local cancellation never depends on iCloud availability."""
    match = re.search(r"HTTP\s+\d+\s+([^\s]+)", str(event_ref or ""))
    if not match or not CALDAV_CREDENTIALS.exists() or not CALDAV_HELPER.exists():
        return False
    try:
        result = subprocess.run([BASH, str(CALDAV_HELPER), "delete", match.group(1)], cwd=str(CALDAV_SKILL_DIR),
                                capture_output=True, text=True, timeout=45)
        return result.returncode == 0 and (result.stdout or "").lstrip().startswith("HTTP 2")
    except Exception as e:
        log(f"日历提醒删除异常: {str(e)[:120]}")
        return False


def _claude_reply_persistent(prompt, session_id=None, on_progress=None, base=None, timeout=None,
                             cancel_check=None):
    """通过常驻 claude 子进程生成回复（不每条消息冷启动）。"""
    global _claude_proc
    hang_timeout = timeout or TURN_HANG_TIMEOUT
    if _backend_cancelled(cancel_check):
        return None, session_id
    if _cancel_event.is_set() or _backend_cancelled(cancel_check):
        return None, session_id
    _cancel_event.clear()
    for attempt in range(3):
        if _cancel_event.is_set() or _backend_cancelled(cancel_check):
            return None, session_id
        with _claude_lock:
            ok = _claude_helper_ensure()
        if not ok:
            log("⚠️ Claude 常驻服务不可用")
            return None, session_id
        try:
            req_id = uuid.uuid4().hex[:8]
            my_q = queue.Queue()
            with _claude_turn_queues_lock:
                _claude_turn_queues[req_id] = my_q
            try:
                payload = {"type": "turn", "id": req_id, "session": session_id,
                           "prompt": prompt}
                if base:
                    payload["base"] = base
                if _backend_cancelled(cancel_check):
                    return None, session_id
                if not _claude_helper_send(payload):
                    raise RuntimeError("助手未连接")
                last_event = time.time()
                last_alive = time.time()
                while True:
                    if _cancel_event.is_set() or _backend_cancelled(cancel_check):
                        return None, session_id
                    now = time.time()
                    if now - last_event > hang_timeout:
                        try:
                            _claude_helper_send({"type": "interrupt", "reqId": req_id})
                        except Exception:
                            pass
                        raise RuntimeError("HANG: 单轮无事件超时")
                    if now - last_alive >= ALIVE_NUDGE_INTERVAL and now - last_event >= ALIVE_NUDGE_S:
                        try:
                            if on_progress:
                                on_progress("⏳ 还在处理中…")
                        except Exception:
                            pass
                        last_alive = now
                    try:
                        obj = my_q.get(timeout=5)
                    except queue.Empty:
                        continue
                    last_event = time.time()
                    t = obj.get("type")
                    if t == "exit":
                        raise RuntimeError("助手退出")
                    if t == "progress" and on_progress:
                        try:
                            on_progress(obj.get("text") or "")
                        except Exception:
                            pass
                    elif t == "result":
                        text = (obj.get("text") or "").strip()
                        reply = _clean_final(text, set())
                        return reply or None, obj.get("session") or session_id
                    elif t == "error":
                        raise RuntimeError(obj.get("message") or "turn error")
            finally:
                with _claude_turn_queues_lock:
                    _claude_turn_queues.pop(req_id, None)
        except Exception as e:
            msg = str(e)
            log(f"⚠️ Claude 常驻连接异常(第{attempt+1}次): {msg[:120]}")
            if _backend_cancelled(cancel_check):
                return None, session_id
            if "HANG" in msg:
                if attempt >= 2:
                    log("🧹 多次卡死：重启 Claude 常驻连接（自愈）")
                    session_id = None
                    try:
                        _claude_helper_restart()
                    except Exception as ex:
                        log(f"重启 Claude 常驻连接异常: {str(ex)[:80]}")
                else:
                    log("🔧 单轮无事件超时：中断重试（保留会话上下文）")
                time.sleep(3)
            else:
                with _claude_lock:
                    if _claude_proc:
                        try:
                            _claude_proc.kill()
                        except Exception:
                            pass
                        _claude_proc = None
                time.sleep(3)
            if _backend_cancelled(cancel_check):
                return None, session_id
    return None, session_id


def claude_reply(prompt, session_id=None, on_progress=None, base=None, timeout=None, cancel_check=None):
    """Claude Code 回复：优先常驻子进程（不冷启动），失败自动回退 spawn 模式。"""
    reply, sid = _claude_reply_persistent(prompt, session_id, on_progress,
                                          base=base, timeout=timeout, cancel_check=cancel_check)
    if reply is not None:
        return reply, sid
    if _backend_cancelled(cancel_check):
        return None, sid
    log("↩️ Claude 常驻通道失败，回退 spawn 模式")
    return _claude_reply_spawn(prompt, session_id, on_progress, base=base,
                               cancel_check=cancel_check)


def _claude_reply_spawn(prompt, session_id=None, on_progress=None, base=None, cancel_check=None):
    """（回退路径）调本机 Claude Code CLI（stream-json）生成回复。支持续接、进度转发与 /stop。"""
    global _active_codex_proc
    exe = CLAUDE_EXE
    if not exe or not os.path.exists(exe):
        log("⚠️ claude 可执行文件未找到：请安装 Claude Code（npm i -g @anthropic-ai/claude-code）或配置 backend.json 的 claude_exe")
        return None, session_id
    cmd_head = [exe]
    if exe.lower().endswith(".js"):
        cmd_head = [CODEX_NODE, exe]  # npm 包 bin 是 js 入口，需要 node 启动
    full_prompt = prompt
    if base and not session_id:
        full_prompt = base.strip() + "\n\n" + prompt
    cmd = cmd_head + ["-p", full_prompt, "--output-format", "stream-json",
            "--include-partial-messages", "--verbose", "--dangerously-skip-permissions"]
    if session_id:
        cmd += ["--resume", session_id]
    for attempt in range(3):
        if _backend_cancelled(cancel_check):
            return None, session_id
        if _cancel_event.is_set() or _backend_cancelled(cancel_check):
            return None, session_id
        _cancel_event.clear()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                    errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    cwd=CODEX_WORK_DIR)
            with _proc_lock:
                _active_codex_proc = proc
            result = {"sid": session_id, "reply": None}
            sent_markers = set()
            buf = ""
            last_output = [time.time()]
            last_alive = [time.time()]

            def _handle_text(text):
                nonlocal buf
                if not text:
                    return
                buf += text
                lines = buf.split("\n")
                buf = lines.pop()
                for l in lines:
                    l = l.strip()
                    mm = re.search(r"【(计划|进度)】\s*(.+)", l)
                    if mm and on_progress and l not in sent_markers \
                            and "步骤描述" not in l and "自动转发" not in l:
                        sent_markers.add(l)
                        try:
                            on_progress(("📋 " if mm.group(1) == "计划" else "✅ ") + mm.group(2).strip())
                        except Exception:
                            pass

            def _pump():
                for line in iter(proc.stdout.readline, ""):
                    line = line.strip()
                    if not line:
                        continue
                    last_output[0] = time.time()
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    t = obj.get("type")
                    if t == "stream_event":
                        ev = obj.get("event") or {}
                        if ev.get("type") == "content_block_delta":
                            d = ev.get("delta") or {}
                            if d.get("type") == "text_delta":
                                _handle_text(d.get("text") or "")
                    elif t == "assistant":
                        for blk in (obj.get("message") or {}).get("content") or []:
                            if blk.get("type") == "text":
                                _handle_text(blk.get("text") or "")
                    elif t == "result":
                        result["reply"] = str(obj.get("result") or "").strip()
                        result["sid"] = obj.get("session_id") or result["sid"]

            pump_thread = threading.Thread(target=_pump, daemon=True)
            pump_thread.start()
            while proc.poll() is None:
                if _cancel_event.is_set() or _backend_cancelled(cancel_check):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc.wait()
                    break
                now = time.time()
                if now - last_output[0] >= ALIVE_NUDGE_S and now - last_alive[0] >= ALIVE_NUDGE_INTERVAL:
                    try:
                        if on_progress:
                            on_progress("⏳ 还在处理中…")
                    except Exception:
                        pass
                    last_alive[0] = now
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    continue
            _handle_text("\n")  # 冲刷缓冲，补扫最后一行
            pump_thread.join(timeout=5)
            with _proc_lock:
                if _active_codex_proc is proc:
                    _active_codex_proc = None
            if _cancel_event.is_set() or _backend_cancelled(cancel_check):
                return None, session_id
            if proc.returncode == 0 and result["reply"]:
                return result["reply"], result["sid"] or session_id
            log(f"⚠️ claude CLI失败(第{attempt+1}次): rc={proc.returncode}")
        except Exception as e:
            log(f"⚠️ claude CLI异常(第{attempt+1}次): {e}")
        if _backend_cancelled(cancel_check):
            return None, session_id
        time.sleep(20 * (attempt + 1))
    return None, session_id


def generic_reply(prompt, session_id=None, on_progress=None, cfg=None, cancel_check=None):
    """通用 CLI 后端：按 backend.json 里的 new_cmd/resume_cmd 模板调用任意 agent。"""
    global _active_codex_proc
    cfg = cfg or {}
    new_cmd = cfg.get("new_cmd") or []
    resume_cmd = cfg.get("resume_cmd") or []
    sess_re = cfg.get("session_regex") or r"session[=_ ]([0-9a-fA-F-]{8,})"
    if session_id and resume_cmd:
        cmd = [c.replace("{prompt}", prompt).replace("{session}", session_id) for c in resume_cmd]
    elif new_cmd:
        cmd = [c.replace("{prompt}", prompt) for c in new_cmd]
    else:
        log("generic 后端未配置 new_cmd/resume_cmd，无法调用")
        return None, session_id
    if cmd:
        cmd[0] = shutil.which(cmd[0]) or cmd[0]   # Windows: 裸命令自动解析（.cmd/.exe）
        if cmd[0].lower().endswith(".js"):
            cmd.insert(0, CODEX_NODE)
    for attempt in range(3):
        if _backend_cancelled(cancel_check):
            return None, session_id
        if _cancel_event.is_set() or _backend_cancelled(cancel_check):
            return None, session_id
        _cancel_event.clear()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                    errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    cwd=CODEX_WORK_DIR)
            with _proc_lock:
                _active_codex_proc = proc
            out_lines = []
            found_sid = [session_id]
            sent_markers = set()
            last_output = [time.time()]
            last_alive = [time.time()]

            def _scan(line, is_err):
                last_output[0] = time.time()
                m = re.search(sess_re, line)
                if m:
                    found_sid[0] = m.group(1)
                if on_progress and not _cancel_event.is_set():
                    mm = re.search(r"【(计划|进度)】\s*(.+)", line.strip())
                    if mm and line.strip() not in sent_markers \
                            and "步骤描述" not in line and "自动转发" not in line:
                        sent_markers.add(line.strip())
                        try:
                            on_progress(("📋 " if mm.group(1) == "计划" else "✅ ") + mm.group(2).strip())
                        except Exception:
                            pass

            def _pump_stdout():
                for line in iter(proc.stdout.readline, ""):
                    _scan(line, False)
                    out_lines.append(line)

            def _pump_stderr():
                for line in iter(proc.stderr.readline, ""):
                    _scan(line, True)

            t1 = threading.Thread(target=_pump_stdout, daemon=True)
            t2 = threading.Thread(target=_pump_stderr, daemon=True)
            t1.start()
            t2.start()
            while proc.poll() is None:
                if _cancel_event.is_set() or _backend_cancelled(cancel_check):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    proc.wait()
                    break
                now = time.time()
                if now - last_output[0] >= ALIVE_NUDGE_S and now - last_alive[0] >= ALIVE_NUDGE_INTERVAL:
                    try:
                        if on_progress:
                            on_progress("⏳ 还在处理中…")
                    except Exception:
                        pass
                    last_alive[0] = now
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    continue
            t1.join(timeout=5)
            t2.join(timeout=5)
            with _proc_lock:
                if _active_codex_proc is proc:
                    _active_codex_proc = None
            if _cancel_event.is_set() or _backend_cancelled(cancel_check):
                return None, session_id
            reply = "".join(out_lines).strip()
            if proc.returncode == 0 and reply:
                return reply, found_sid[0] or session_id
            log(f"⚠️ generic CLI失败(第{attempt+1}次): rc={proc.returncode}")
        except Exception as e:
            log(f"⚠️ generic CLI异常(第{attempt+1}次): {e}")
        if _backend_cancelled(cancel_check):
            return None, session_id
        time.sleep(20 * (attempt + 1))
    return None, session_id


def agent_reply(backend, prompt, session_id=None, on_progress=None, cfg=None,
                model=None, effort=None, base=None, timeout=None,
                preempt_evt=None, preempt_ctx=None, deadline=None, checkpoint=None,
                cancel_check=None):
    """按后端分派回复生成。"""
    # Each dispatch is a new invocation; an earlier /stop must not consume
    # the next chat message. Research cancellation belongs to the durable job
    # and must survive this reset (and another chat's dispatch).
    if _backend_cancelled(cancel_check):
        return None, session_id
    _cancel_event.clear()
    if backend == "claude":
        return claude_reply(prompt, session_id, on_progress, base=base, timeout=timeout,
                            cancel_check=cancel_check)
    if backend == "generic":
        return generic_reply(prompt, session_id, on_progress, (cfg or {}).get("generic") or {},
                             cancel_check=cancel_check)
    return codex_reply(prompt, session_id, on_progress, model=model, effort=effort,
                       base=base, timeout=timeout, preempt_evt=preempt_evt, preempt_ctx=preempt_ctx,
                       deadline=deadline, checkpoint=checkpoint, cancel_check=cancel_check)


# ───────────────────────── 注册（QR 扫码） ─────────────────────────

def register():
    base = ILINK_DEFAULT_BASE
    qr = _req(f"{base}/{EP_GET_BOT_QR}?bot_type=3", timeout=40)
    qrcode_value = str(qr.get("qrcode") or "")
    qrcode_url = str(qr.get("qrcode_img_content") or "")
    scan_data = qrcode_url or qrcode_value
    if not scan_data:
        log(f"QR 获取失败: {json.dumps(qr, ensure_ascii=False)[:300]}")
        return 1

    def _save_qr_png(data):
        try:
            import qrcode
            qrcode.make(data).save(QR_PNG)
            log(f"二维码图片已保存: {QR_PNG}")
            try:
                import shutil
                home = os.environ.get("USERPROFILE", "")
                desktop = Path(home) / "Desktop"
                if not desktop.exists():
                    desktop = Path(home) / "OneDrive" / "Desktop"
                if desktop.exists():
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    dest = desktop / f"微信重绑二维码-{ts}.png"
                    shutil.copy2(QR_PNG, dest)
                    log(f"二维码已复制到桌面: {dest}")
            except Exception as e:
                log(f"桌面副本失败: {str(e)[:80]}")
        except Exception:
            pass

    _save_qr_png(scan_data)
    _safe_print("\n====== Codex 微信 bot 注册 ======")
    _safe_print("请用微信扫描以下链接对应的二维码（图片见 " + str(QR_PNG) + "）：")
    _safe_print(scan_data)
    try:
        import qrcode as _q
        _q.QRCode().add_data(scan_data).make(fit=True).print_ascii(invert=True)
    except Exception:
        pass

    refresh = 0
    while True:
        try:
            st = _req(f"{base}/{EP_GET_QR_STATUS}?qrcode={urllib.parse.quote(qrcode_value)}", timeout=40)
        except Exception as e:
            log(f"QR轮询异常: {e}")
            time.sleep(2)
            continue
        status = str(st.get("status") or "wait")
        if status == "wait":
            _safe_print(".")
        elif status == "scaned":
            log("已扫码，请在微信里确认…")
        elif status == "scaned_but_redirect":
            host = str(st.get("redirect_host") or "")
            if host:
                base = f"https://{host}"
                log(f"重定向到: {base}")
        elif status == "expired":
            refresh += 1
            if refresh > 3:
                log("二维码多次过期，注册失败，请重试")
                return 1
            log("二维码过期，刷新中…")
            qr = _req(f"{base}/{EP_GET_BOT_QR}?bot_type=3", timeout=40)
            qrcode_value = str(qr.get("qrcode") or "")
            qrcode_url = str(qr.get("qrcode_img_content") or "")
            _save_qr_png(qrcode_url or qrcode_value)
        elif status == "confirmed":
            account = {
                "account_id": str(st.get("ilink_bot_id") or ""),
                "token": str(st.get("bot_token") or ""),
                "base_url": str(st.get("baseurl") or base),
                "user_id": str(st.get("ilink_user_id") or ""),
                "saved_at": datetime.now().isoformat(),
            }
            if not account["account_id"] or not account["token"]:
                log("确认成功但凭据不完整，注册失败")
                return 1
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            ACCOUNT_FILE.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
            ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
            (ACCOUNTS_DIR / f"{account['account_id']}.json").write_text(
                json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"✅ 注册成功: {account['account_id']}")
            return 0
        time.sleep(1.5)


# ───────────────────────── 桥主循环（稳定性加固版） ─────────────────────────

class Bridge:
    def __init__(self):
        self.accounts_dir = ACCOUNTS_DIR
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.account = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        if not (self.account.get("user_id") or ""):
            log("⚠️ 账号缺少 user_id：已进入拒收模式（fail-closed），请重新扫码注册")
        self.state = {"sync_buf": "", "context_token": "", "boss_chat": "",
                      "codex_session": None, "session_account": "", "seen": [],
                      "seen_fps": {}, "sessions": []}
        if STATE_FILE.exists():
            try:
                self.state.update(json.loads(STATE_FILE.read_text(encoding="utf-8-sig")))
            except Exception:
                pass
        # v2.1 stores fingerprint timestamps.  Earlier versions stored a bare
        # list, which had no expiry and could incorrectly suppress a message
        # repeated days later; legacy entries are therefore intentionally dropped.
        self._prune_seen_fps()
        self.reply_q = deque()
        self._q_cond = threading.Condition()
        self.task_q = deque()          # 后台任务队列（FIFO 顺序执行）
        self._task_cond = threading.Condition()
        self._research_lock = threading.RLock()
        self._task_preempt = threading.Event()   # 聊天到达时置位：任务让位
        self._chat_active = 0                    # 当前正在处理的聊天数
        self._running_task = {"active": False, "sid": None, "desc": "", "req_id": None}
        self._resume_task = None                 # 被打断待恢复的任务（队首优先）
        self._send_ts = []
        self._send_lock = threading.Lock()
        replied_fps = set(self.state.get("replied_fps") or [])
        pending_replies = self.state.pop("pending_replies", []) or []
        interrupted = self.state.pop("processing", None) or []
        persisted_tasks = self.state.pop("task_queue", []) or []
        self._restore_persisted_queues(pending_replies, interrupted, persisted_tasks, replied_fps)
        self._restore_research_jobs()
        self.cb = _CircuitBreaker(self.account.get("account_id", "bridge"))
        self._last_poll_ts = time.time()
        self._retry_queue_flush_ts = 0.0
        self._expiry_pause_until = 0.0
        self._rebind_attempted_at = 0.0
        self._expiry_streak = 0
        self._poll_timeout = POLL_TIMEOUT
        self._account_mtime = ACCOUNT_FILE.stat().st_mtime if ACCOUNT_FILE.exists() else 0.0
        self.backend, self.backend_cfg = _load_backend()
        if self.backend == "codex" and self.state.get("permission_policy_version") != PERMISSION_POLICY_VERSION:
            # Older threads retain the former blanket refusal in their base instructions.
            # Preserve their IDs for diagnostics, but start fresh under the new policy.
            self.state["legacy_sessions"] = self.state.get("sessions") or []
            self.state["sessions"] = []
            self.state["codex_session"] = None
            self.state["permission_policy_version"] = PERMISSION_POLICY_VERSION
            log("🔄 权限规则已更新，下一条微信消息将开启新 Codex 会话")
            self.save_state()
        # 当前主账号进池，作为后续备用
        self._persist_account_state(self.account, self.state)
        self._cleanup_stale_session()

    def save_state(self):
        with _state_lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.state["seen"] = self.state["seen"][-200:]
            try:
                self.state["pending_replies"] = list(self.reply_q)
            except Exception:
                pass
            try:
                self.state["task_queue"] = list(self.task_q)
            except Exception:
                pass
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
            self._persist_account_state(self.account, self.state)

    def _restore_persisted_queues(self, pending_replies, interrupted, persisted_tasks, replied_fps):
        """Normalize legacy queue strings and deduplicate durable research work."""
        research_ids = set()

        def enqueue_research_text(text):
            route = _research_request(text)
            if not route:
                return False
            previous_account = self.state.get("session_account") or ""
            if previous_account and previous_account != self.account.get("account_id", ""):
                log(f"♻️ 跳过旧账号调研恢复: {text[:40]}")
                return True
            # Legacy queue entries contain only the request text.  A terminal
            # durable job therefore proves that this is an old admission, not
            # a new request: new admissions carry their task-id envelope.  Do
            # not resurrect a delivered/failed/cancelled task from the stale
            # ``processing``/``pending_replies`` strings left by older builds.
            job = self._find_research_job(text, include_terminal=True)
            if job and job.get("status") in {"delivered", "failed", "cancelled"}:
                log(f"♻️ 跳过已结束调研的旧文本恢复: {text[:40]}")
                research_ids.add(job.get("task_id"))
                return True
            task_id = job["task_id"] if job else self._create_research_job(text, route)
            if task_id in research_ids:
                return True
            research_ids.add(task_id)
            self._research_queue_item(task_id)
            return True

        for item in pending_replies:
            text = item.get("text", "") if isinstance(item, dict) else item
            if not text:
                continue
            if enqueue_research_text(text):
                continue
            self.reply_q.appendleft(item)

        for item in interrupted:
            text = item.get("text", "") if isinstance(item, dict) else item
            if not text:
                continue
            if enqueue_research_text(text):
                continue
            fp = hashlib.md5((self.account.get("user_id", "") + "|" + text).encode("utf-8")).hexdigest()
            if fp in replied_fps:
                log(f"♻️ 跳过补回（已回复过）: {text[:30]}")
                continue
            log(f"♻️ 上次中断时正在处理的消息补回队列: {text[:40]}")
            self.reply_q.appendleft(item)

        for item in persisted_tasks:
            if isinstance(item, dict) and item.get("research_task_id"):
                task_id = str(item.get("research_task_id"))
                job = self._research_jobs().get(task_id)
                if (job and job.get("account_id") == self.account.get("account_id")
                        and job.get("status") in {"queued", "archived"}
                        and (job.get("status") != "archived"
                             or self._research_ts(job.get("next_delivery_ts")) <= time.time())
                        and task_id not in research_ids):
                    job["queue_enqueued"] = True
                    self.task_q.append({"research_task_id": task_id, "text": job.get("request", item.get("text", ""))})
                    research_ids.add(task_id)
                continue
            text = item
            if isinstance(item, str) and enqueue_research_text(text):
                continue
            if item:
                self.task_q.append(item)

    # ── durable research jobs ───────────────────────────────────────
    def _research_jobs(self):
        if not hasattr(self, "_research_lock"):
            self._research_lock = threading.RLock()
        jobs = self.state.setdefault("research_jobs", {})
        if isinstance(jobs, list):
            jobs = {str(j.get("task_id")): j for j in jobs if isinstance(j, dict) and j.get("task_id")}
            self.state["research_jobs"] = jobs
        return jobs

    @staticmethod
    def _research_now():
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _research_ts(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    def _find_research_job(self, request_text, include_terminal=False):
        with self._research_lock:
            account = getattr(self, "account", {}) or {}
            account_id = account.get("account_id") or ""
            jobs = self._research_jobs()
            candidates = [j for j in jobs.values()
                          if j.get("request") == request_text
                          and j.get("account_id") == account_id]
            if not include_terminal:
                candidates = [j for j in candidates if j.get("status") not in {"delivered", "failed", "cancelled"}]
            candidates.sort(key=lambda j: j.get("created_ts", ""), reverse=True)
            return candidates[0] if candidates else None

    def _research_job_status(self, task_id=None, request_text=None):
        """Return an account-scoped durable job snapshot for status/follow-up routing."""
        with self._research_lock:
            job = None
            if task_id:
                candidate = self._research_jobs().get(str(task_id))
                account = getattr(self, "account", {}) or {}
                if candidate and candidate.get("account_id") == (account.get("account_id") or ""):
                    job = candidate
            elif request_text:
                job = self._find_research_job(request_text, include_terminal=True)
            return dict(job) if job else None

    def _research_is_cancelled(self, task_id):
        if not task_id:
            return False
        with self._research_lock:
            job = self._research_jobs().get(str(task_id))
            return bool(job and (job.get("cancel_requested") or job.get("status") == "cancelled"))

    def _retry_research_job(self, task_id):
        """Explicitly restart a failed job or flush an archived delivery retry."""
        with self._research_lock:
            job = self._research_jobs().get(str(task_id))
            account = getattr(self, "account", {}) or {}
            if not job or job.get("account_id") != (account.get("account_id") or ""):
                return None
            if job.get("status") in {"archived", "delivery_ambiguous"}:
                job["next_delivery_ts"] = self._research_now()
                job["queue_enqueued"] = False
                if job.get("status") == "delivery_ambiguous":
                    delivery = job.setdefault("delivery", {})
                    for kind in ("summary", "file", "completion"):
                        if delivery.get(kind) in {"sending", "ambiguous"}:
                            delivery[kind] = "pending"
                    job["delivery_attempts"] = 0
                    self._research_transition_locked(job, "archived", reason="explicit retry after ambiguous delivery",
                                                      delivery_claim={"state": "pending"})
                self.save_state()
            elif job.get("status") in {"failed", "cancelled"}:
                has_archive = bool(job.get("archive_paths"))
                old_note = job.setdefault("failure_notification", {})
                old_note["superseded"] = True
                old_note["superseded_ts"] = self._research_now()
                job["notification_generation"] = int(job.get("notification_generation") or 0) + 1
                job["attempts"] = 0
                job["delivery_attempts"] = 0
                job["cancel_requested"] = False
                job["failure_stage"] = ""
                job["failure_reason"] = ""
                job["failure_notification"] = {"state": "none",
                                                "generation": job["notification_generation"],
                                                "parts": []}
                if has_archive:
                    self._research_transition_locked(job, "archived", reason="explicit delivery retry",
                                                      override_terminal=True,
                                                      next_delivery_ts=self._research_now(), queue_enqueued=False)
                else:
                    self._research_transition_locked(job, "queued", reason="explicit retry", queue_enqueued=False,
                                                      override_terminal=True,
                                                      next_retry_ts=None)
                self.save_state()
            else:
                return job
        if job.get("status") == "archived":
            self._research_queue_item(str(task_id), front=True)
        elif job.get("status") == "queued":
            self._research_queue_item(str(task_id), front=True)
        return job

    def _create_research_job(self, request_text, route=None):
        """Register a research request before it enters either worker queue."""
        with self._research_lock:
            existing = self._find_research_job(request_text)
            if existing:
                return existing["task_id"]
            task_id = "research-" + uuid.uuid4().hex[:16]
            now = self._research_now()
            account = getattr(self, "account", {}) or {}
            job = {
                "task_id": task_id,
                "request": request_text,
                "created_ts": now,
                "account_id": account.get("account_id", ""),
                "route": route or _research_request(request_text) or "unknown",
                "status": "queued",
                "attempts": 0,
                "max_attempts": RESEARCH_MAX_ATTEMPTS,
                "next_retry_ts": None,
                "session_id": None,
                "partial_findings": [],
                "archive_paths": [],
                "archive_target": None,
                "summary": "",
                "failure_stage": "",
                "failure_reason": "",
                "queue_enqueued": False,
                "delivery": {"summary": "pending", "file": "pending", "completion": "pending"},
                "notification_generation": 0,
                "failure_notification": {"state": "none", "generation": 0, "parts": []},
                "history": [],
            }
            self._research_jobs()[task_id] = job
            self._research_transition_locked(job, "queued", reason="admitted")
            self.save_state()
            return task_id

    def _research_transition_locked(self, job, status, reason="", override_terminal=False, **updates):
        old = job.get("status")
        if old in {"delivered", "failed", "cancelled"} and status != old and not override_terminal:
            return False
        if old == status and not updates:
            return True
        job.update(updates)
        job["status"] = status
        event = {"from": old, "to": status, "ts": self._research_now()}
        if reason:
            event["reason"] = str(reason)[:500]
        history = job.setdefault("history", [])
        history.append(event)
        job["history"] = history[-40:]
        return True

    def _research_transition(self, task_id, status, reason="", override_terminal=False, **updates):
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            if not job or job.get("account_id") != (account.get("account_id") or ""):
                return None
            self._research_transition_locked(job, status, reason, override_terminal=override_terminal, **updates)
            self.save_state()
            return job

    def _research_checkpoint(self, task_id, *, stage=None, error=None, session_id=None, partial=None):
        if not task_id:
            return
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            if not job or job.get("account_id") != (account.get("account_id") or ""):
                return
            if session_id:
                job["session_id"] = session_id
            if stage:
                if stage != "completed":
                    if stage not in {"progress", "partial", "session"}:
                        try:
                            from research_diagnostics import failure_stage
                            stage = failure_stage(error, stage) if error else stage
                        except Exception:
                            pass
                        job["failure_stage"] = stage
                else:
                    job["failure_stage"] = ""
            if error:
                try:
                    from research_diagnostics import redact_diagnostic
                    safe_error = redact_diagnostic(error)
                except Exception:
                    safe_error = re.sub(r"(?i)(token|sig(?:nature)?|cookie)[=:][^\s,;]+", r"\1=[REDACTED]", str(error))
                job["failure_reason"] = str(safe_error)[:500]
            if partial:
                findings = job.setdefault("partial_findings", [])
                value = str(partial)[:1000]
                if not findings or findings[-1].get("text") != value:
                    findings.append({"ts": self._research_now(), "stage": stage or "progress", "text": value})
                job["partial_findings"] = findings[-12:]
            self.save_state()

    def _research_mark_running(self, task_id, session_id=None):
        notify_failure = False
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            if not job or job.get("account_id") != (account.get("account_id") or ""):
                return None
            # A worker may claim only a queued research execution.  In
            # particular, retry_wait is not an implicit bypass of backoff and
            # running is never claimed a second time after a duplicate queue item.
            if job.get("status") != "queued":
                return None
            if int(job.get("attempts") or 0) >= int(job.get("max_attempts") or RESEARCH_MAX_ATTEMPTS):
                self._research_transition_locked(job, "failed", reason="retry exhaustion before claim",
                                                  next_retry_ts=None, queue_enqueued=False,
                                                  failure_stage=job.get("failure_stage") or "retry_limit",
                                                  failure_reason=job.get("failure_reason") or "maximum attempts reached")
                self.save_state()
                notify_failure = True
                result = None
            else:
                job["attempts"] = int(job.get("attempts") or 0) + 1
                job["started_ts"] = self._research_now()
                job["next_retry_ts"] = None
                job["queue_enqueued"] = False
                job["failure_stage"] = ""
                job["failure_reason"] = ""
                job["claim_token"] = uuid.uuid4().hex
                self._research_transition_locked(job, "running", reason=f"attempt {job['attempts']}",
                                                  session_id=session_id or job.get("session_id"))
                self.save_state()
                result = job
        if notify_failure:
            self._notify_research_failure(task_id)
        return result

    def _research_queue_item(self, task_id, front=False):
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            # Queue entries are the only handoff point between durable state
            # and a worker.  Only an admission (queued) or an archived report
            # waiting for delivery may enter it.  retry_wait is first moved to
            # queued by the scheduler, preserving its persisted due time.
            if (not job or job.get("account_id") != (account.get("account_id") or "")
                    or job.get("status") not in {"queued", "archived"}):
                return False
            if job.get("status") == "archived" and self._research_ts(job.get("next_delivery_ts")) > time.time():
                return False
            if job.get("queue_enqueued"):
                return False
            job["queue_enqueued"] = True
            item = {"research_task_id": task_id, "text": job.get("request", "")}
            with self._task_cond:
                if front:
                    self.task_q.appendleft(item)
                else:
                    self.task_q.append(item)
                self._task_cond.notify()
            self.save_state()
            return True

    def _research_fail(self, task_id, stage, reason):
        """Checkpoint a failed attempt and schedule bounded retry or terminal failure."""
        try:
            from research_diagnostics import redact_diagnostic
            safe_reason = redact_diagnostic(reason)
            try:
                from research_diagnostics import failure_stage
                stage = failure_stage(safe_reason, stage or "model_execution")
            except Exception:
                pass
        except Exception:
            safe_reason = re.sub(r"(?i)(token|sig(?:nature)?|cookie)[=:][^\s,;]+", r"\1=[REDACTED]", str(reason or ""))
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            if (not job or job.get("account_id") != (account.get("account_id") or "")
                    or job.get("status") in {"delivered", "failed", "cancelled"}):
                return job
            job["failure_stage"] = str(stage or "unknown")[:120]
            job["failure_reason"] = str(safe_reason or "unknown failure")[:500]
            if job.get("cancel_requested"):
                self._research_transition_locked(job, "cancelled", reason="user cancellation")
                self.save_state()
                return job
            attempts = int(job.get("attempts") or 0)
            if attempts < int(job.get("max_attempts") or RESEARCH_MAX_ATTEMPTS):
                delay = min(RESEARCH_RETRY_MAX, RESEARCH_RETRY_BASE * (2 ** max(0, attempts - 1)))
                next_ts = datetime.now(timezone.utc).timestamp() + delay
                next_iso = datetime.fromtimestamp(next_ts, timezone.utc).replace(microsecond=0).isoformat()
                self._research_transition_locked(job, "retry_wait", reason=f"attempt {attempts} failed",
                                                  next_retry_ts=next_iso, queue_enqueued=False)
                self.save_state()
                log(f"调研任务 {task_id} 将在 {next_iso} 重试（{stage}: {safe_reason}）")
                return job
            self._research_transition_locked(job, "failed", reason="retry exhaustion", next_retry_ts=None)
            self.save_state()
        self._notify_research_failure(task_id)
        return self._research_jobs().get(task_id)

    def _research_mark_delivery(self, task_id):
        """Atomically claim one delivery attempt before any external send."""
        terminal = False
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            if not job or job.get("account_id") != (account.get("account_id") or ""):
                return None
            if job.get("status") == "delivering":
                return None
            if job.get("status") not in {"running", "archived"}:
                return None
            attempts = int(job.get("delivery_attempts") or 0) + 1
            if attempts > RESEARCH_MAX_DELIVERY_ATTEMPTS:
                self._research_transition_locked(job, "failed", reason="delivery retry exhaustion",
                                                  failure_stage="wechat_delivery",
                                                  failure_reason="maximum delivery attempts reached",
                                                  next_delivery_ts=None, queue_enqueued=False)
                self.save_state()
                terminal = True
                result = None
            else:
                job["delivery_attempts"] = attempts
                job["delivery_claim"] = {"state": "sending", "attempt": attempts,
                                          "ts": self._research_now()}
                job["next_delivery_ts"] = None
                self._research_transition_locked(job, "delivering", reason=f"delivery attempt {attempts}",
                                                  queue_enqueued=False)
                self.save_state()
                result = job
        if terminal:
            self._notify_research_failure(task_id)
        return result

    def _research_delivery_failure(self, task_id, outcome, session_id=None):
        """Bound delivery-only retries while retaining the archived report."""
        if not task_id:
            return
        # A lost delivery claim belongs to another worker.  In particular, do
        # not turn an active claimant's in-flight send into a retry merely
        # because a duplicate queue item reached this method.
        if getattr(outcome, "stage", None) == "delivery_claim":
            return
        try:
            from research_diagnostics import redact_diagnostic
            outcome_reason = str(redact_diagnostic(getattr(outcome, "reason", "") or ""))[:500]
        except Exception:
            outcome_reason = re.sub(r"(?i)(token|sig(?:nature)?|cookie)[^:=]*[=:][^\s,;]+",
                                    r"\1=[REDACTED]", str(getattr(outcome, "reason", "") or ""))[:500]
        terminal = False
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            if (not job or job.get("account_id") != (account.get("account_id") or "")
                    or job.get("status") in {"delivered", "failed", "cancelled"}):
                return
            attempts = int(job.get("delivery_attempts") or 0)
            if job.get("status") != "delivering":
                attempts += 1
            paths = outcome.get("archive_paths") or job.get("archive_paths") or []
            if attempts >= RESEARCH_MAX_DELIVERY_ATTEMPTS:
                self._research_transition_locked(job, "failed", reason="delivery retry exhaustion",
                                                  session_id=session_id or job.get("session_id"),
                                                  archive_paths=[str(p) for p in paths if p],
                                                  delivery_attempts=attempts, next_delivery_ts=None,
                                                  delivery_claim={"state": "failed", "attempt": attempts},
                                                  failure_stage=("archive_processing"
                                                                 if getattr(outcome, "stage", "") == "archive_processing"
                                                                 else "wechat_delivery"),
                                                  failure_reason=outcome_reason or "delivery failed")
                terminal = True
            else:
                delay = min(RESEARCH_RETRY_MAX, RESEARCH_RETRY_BASE * (2 ** (attempts - 1)))
                next_iso = datetime.fromtimestamp(time.time() + delay, timezone.utc).replace(microsecond=0).isoformat()
                self._research_transition_locked(job, "archived", reason=outcome_reason or "delivery failed",
                                                  session_id=session_id or job.get("session_id"),
                                                  archive_paths=[str(p) for p in paths if p],
                                                  delivery_attempts=attempts, next_delivery_ts=next_iso,
                                                  delivery_claim={"state": "retry_wait", "attempt": attempts},
                                                  failure_stage=("archive_processing"
                                                                 if getattr(outcome, "stage", "") == "archive_processing"
                                                                 else "wechat_delivery"),
                                                  failure_reason=outcome_reason or "delivery failed")
            self.save_state()
        if terminal:
            self._notify_research_failure(task_id)

    def _research_recover_archive_paths(self, job):
        """Recover a complete reserved report after a write-before-state crash.

        ``archive_target`` is reserved before writing.  If the process dies
        after the file is complete but before ``archive_paths``/``archived``
        are persisted, the target is authoritative and the model must not be
        run again.  This helper is deliberately read-only apart from returning
        normalized paths; callers persist the result under the research lock.
        """
        paths = [str(p) for p in (job.get("archive_paths") or []) if p]
        target = str(job.get("archive_target") or "")
        if target:
            try:
                target_path = Path(target)
                if target_path.exists() and target_path.is_file():
                    body = target_path.read_text(encoding="utf-8")
                    if _is_complete_research_report(body):
                        if str(target_path) not in paths:
                            paths.insert(0, str(target_path))
                        txt_path = target_path.with_suffix(".txt")
                        if txt_path.exists() and txt_path.is_file() and str(txt_path) not in paths:
                            paths.append(str(txt_path))
            except Exception:
                pass
        return list(dict.fromkeys(paths))

    def _research_archived_report(self, task_id):
        """Return durable report paths, including an uncommitted target file."""
        with self._research_lock:
            job = self._research_jobs().get(str(task_id))
            account = getattr(self, "account", {}) or {}
            if not job or job.get("account_id") != (account.get("account_id") or ""):
                return []
            paths = self._research_recover_archive_paths(job)
            if paths != [str(p) for p in (job.get("archive_paths") or []) if p]:
                job["archive_paths"] = paths
                self.save_state()
            # A path string alone is not evidence of a retained report.  Only
            # a present, structurally complete Markdown file may divert an
            # exception into delivery-only retry.
            for candidate in paths:
                try:
                    candidate_path = Path(candidate)
                    if (candidate_path.suffix.lower() == ".md" and candidate_path.exists()
                            and _is_complete_research_report(
                                candidate_path.read_text(encoding="utf-8"))):
                        return paths
                except Exception:
                    continue
            return []

    def _research_post_archive_failure(self, task_id, reason, session_id=None):
        """Schedule delivery/archive processing retry without rerunning research."""
        paths = self._research_archived_report(task_id)
        if not paths:
            return False
        try:
            from research_diagnostics import redact_diagnostic
            safe_reason = redact_diagnostic(reason)
        except Exception:
            safe_reason = re.sub(r"(?i)(token|sig(?:nature)?|cookie)[=:][^\s,;]+",
                                 r"\1=[REDACTED]", str(reason or ""))
        outcome = ResearchDeliveryOutcome(stage="archive_processing", reason=str(safe_reason)[:500],
                                          archive_paths=paths)
        self._research_delivery_failure(task_id, outcome, session_id=session_id)
        return True

    def _research_failure_text(self, job):
        paths = [str(p) for p in (job.get("archive_paths") or []) if p]
        saved = "已保存报告：" + ", ".join(paths) if paths else "没有保存完整报告"
        partial_count = len(job.get("partial_findings") or [])
        partial_location = f"{STATE_FILE}/{job.get('task_id')}"
        if partial_count:
            saved += f"；已保存 {partial_count} 条部分进展到 {partial_location}"
        else:
            saved += f"；没有保存部分结果（部分进展位置：{partial_location}）"
        raw_created = str(job.get("created_ts") or "")
        try:
            created_dt = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            created = (created_dt.astimezone(timezone.utc).replace(microsecond=0)
                       .isoformat().replace("+00:00", "Z"))
        except Exception:
            created = raw_created or "未知时间"
        stage = _RESEARCH_STAGE_LABELS.get(job.get("failure_stage"), job.get("failure_stage") or "未知阶段")
        model_attempts = int(job.get("attempts") or 0)
        delivery_attempts = int(job.get("delivery_attempts") or 0)
        return (f"“{_research_subject(job.get('request', '调研'))}”调研失败："
                f"{stage}，{job.get('failure_reason') or '未返回结果'}。"
                f"调研执行 {model_attempts} 轮，发送尝试 {delivery_attempts} 轮，暂不再自动重试。{saved}。"
                f"任务 {job.get('task_id')}（{created}）；回复“重试”可重新开始。")

    def _notify_research_failure(self, task_id):
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            if (not job or job.get("account_id") != (account.get("account_id") or "")
                    or job.get("status") != "failed"):
                return False
            note = job.setdefault("failure_notification", {"state": "none"})
            if note.get("superseded"):
                return False
            if note.get("state") == "sent":
                return note.get("state") == "sent"
            # A second notifier must not join or overwrite an active send.
            # Restart recovery marks an interrupted send ambiguous; only an
            # explicit retry can supersede that generation.
            if note.get("state") in {"sending", "ambiguous"}:
                return False
            text = self._research_failure_text(job)
            attempts = int(note.get("attempts") or 0) + 1
            if attempts > RESEARCH_MAX_DELIVERY_ATTEMPTS:
                note.update({"state": "outbox_exhausted", "text": text, "attempts": attempts})
                self.save_state()
                return False
            generation = int(job.get("notification_generation") or note.get("generation") or 0)
            claim_id = uuid.uuid4().hex
            parts = _split_weixin_text(text)
            note.update({"state": "sending", "text": text, "attempts": attempts,
                         "created_ts": self._research_now(), "task_id": task_id,
                         "account_id": job.get("account_id"), "generation": generation,
                         "claim_id": claim_id,
                         "parts": [{"index": index, "state": "pending", "generation": generation,
                                    "claim_id": claim_id}
                                   for index, _ in enumerate(parts)]})
            self.save_state()
        try:
            ok = (self._send_research_message(text, task_id, "最终失败通知",
                                               generation=generation, claim_id=claim_id)
                  if isinstance(self, Bridge) else self.send_weixin(text))
            sent = (ok is not False)
        except Exception as exc:
            log(f"调研最终失败通知发送异常: {str(exc)[:120]}")
            sent = False
        ack_applied = False
        with self._research_lock:
            job = self._research_jobs().get(task_id)
            account = getattr(self, "account", {}) or {}
            note = (job or {}).get("failure_notification") or {}
            # Explicit retry can replace the note while the external send is
            # still in flight.  Never acknowledge the old generation on the
            # new cycle's note.
            if (job and job.get("account_id") == (account.get("account_id") or "")
                    and job.get("status") == "failed"
                    and note.get("generation") == generation
                    and note.get("claim_id") == claim_id
                    and note.get("state") == "sending"):
                note = job.setdefault("failure_notification", {})
                note.update({"state": "sent" if sent else "pending",
                             "sent_ts": self._research_now() if sent else None})
                if not sent:
                    note["next_retry_ts"] = datetime.fromtimestamp(
                        time.time() + RESEARCH_RETRY_BASE * max(1, int(note.get("attempts") or 1)),
                        timezone.utc).replace(microsecond=0).isoformat()
                self.save_state()
                ack_applied = True
        return bool(sent and ack_applied)

    def _restore_research_jobs(self):
        """Turn persisted work into queue entries; no helper restart counts as a resume."""
        with self._research_lock:
            jobs = self._research_jobs()
            now = time.time()
            exhausted = []
            for task_id, job in list(jobs.items()):
                if job.get("account_id") != (self.account.get("account_id") or ""):
                    continue
                # A report target is reserved before its bytes are written.
                # Recovering a complete target closes the crash window before
                # the archive transition and prevents a second model run.
                recovered_paths = self._research_recover_archive_paths(job)
                stored_paths = [str(p) for p in (job.get("archive_paths") or []) if p]
                if recovered_paths != stored_paths:
                    job["archive_paths"] = recovered_paths
                reserved_complete = False
                try:
                    target_path = Path(str(job.get("archive_target") or ""))
                    reserved_complete = (bool(job.get("archive_target"))
                                         and target_path.exists() and target_path.is_file()
                                         and _is_complete_research_report(
                                             target_path.read_text(encoding="utf-8")))
                except Exception:
                    reserved_complete = False
                if (reserved_complete
                        and recovered_paths
                        and job.get("status") in {"running", "retry_wait", "queued"}):
                    self._research_transition_locked(
                        job, "archived", reason="recovered reserved archive target",
                        archive_paths=recovered_paths, queue_enqueued=False,
                        next_delivery_ts=self._research_now())
                if job.get("status") == "delivering":
                    # A process crash leaves the remote send ambiguous.  Do
                    # not silently resend; require an explicit retry so the
                    # user can choose between a possible duplicate and no
                    # further delivery.
                    delivery = job.setdefault("delivery", {})
                    for kind in ("summary", "file", "completion"):
                        if delivery.get(kind) == "sending":
                            delivery[kind] = "ambiguous"
                    self._research_transition_locked(job, "delivery_ambiguous",
                                                      reason="bridge restarted during delivery",
                                                      next_delivery_ts=None, queue_enqueued=False,
                                                      delivery_claim={"state": "ambiguous",
                                                                       "attempt": job.get("delivery_attempts", 0),
                                                                       "ts": self._research_now()})
                if job.get("status") == "failed":
                    note = job.get("failure_notification") or {}
                    if note.get("state") == "sending":
                        note["state"] = "ambiguous"
                        note["ambiguous_ts"] = self._research_now()
                    elif note.get("state") in {None, "none"} and not note.get("superseded"):
                        note["state"] = "pending"
                        note["next_retry_ts"] = self._research_now()
                if job.get("status") == "running":
                    if int(job.get("attempts") or 0) >= int(job.get("max_attempts") or RESEARCH_MAX_ATTEMPTS):
                        self._research_transition_locked(job, "failed", reason="retry exhaustion after restart",
                                                          next_retry_ts=None, queue_enqueued=False,
                                                          failure_stage="restart_recovery",
                                                          failure_reason="bridge restarted after max attempts")
                        exhausted.append(task_id)
                    else:
                        self._research_transition_locked(job, "retry_wait", reason="bridge restart recovery",
                                                          next_retry_ts=self._research_now(), queue_enqueued=False,
                                                          failure_stage="restart_recovery",
                                                          failure_reason="bridge restarted during execution")
                if job.get("status") == "retry_wait" and int(job.get("attempts") or 0) >= int(job.get("max_attempts") or RESEARCH_MAX_ATTEMPTS):
                    self._research_transition_locked(job, "failed", reason="retry exhaustion",
                                                      next_retry_ts=None, queue_enqueued=False)
                    exhausted.append(task_id)
                if job.get("status") == "retry_wait" and self._research_ts(job.get("next_retry_ts")) <= now:
                    self._research_transition_locked(job, "queued", reason="retry due", queue_enqueued=False)
                if job.get("status") == "queued":
                    self._research_queue_item(task_id)
                elif job.get("status") == "archived" and self._research_delivery_pending(job):
                    self._research_queue_item(task_id)
            # Legacy plain task entries remain ordinary tasks.  New research
            # jobs are represented by task-id envelopes and are deduplicated.
            self.save_state()
        for task_id in exhausted:
            self._notify_research_failure(task_id)

    def _research_delivery_pending(self, job):
        delivery = job.get("delivery") or {}
        if all(delivery.get(k) == "sent" for k in ("summary", "file", "completion")):
            return False
        next_ts = job.get("next_delivery_ts")
        return not next_ts or self._research_ts(next_ts) <= time.time()

    def _research_retry_loop(self):
        while True:
            try:
                due = []
                now = time.time()
                account = getattr(self, "account", {}) or {}
                with self._research_lock:
                    for task_id, job in self._research_jobs().items():
                        if job.get("account_id") != (account.get("account_id") or ""):
                            continue
                        if job.get("status") == "retry_wait" and self._research_ts(job.get("next_retry_ts")) <= now:
                            self._research_transition_locked(job, "queued", reason="retry due", queue_enqueued=False)
                            due.append(task_id)
                        elif job.get("status") == "archived" and self._research_delivery_pending(job):
                            due.append(task_id)
                        elif job.get("status") == "failed":
                            note = job.get("failure_notification") or {}
                            if (note.get("state") in {"pending", "none"}
                                    and self._research_ts(note.get("next_retry_ts")) <= now):
                                due.append((task_id, "failure_notification"))
                    if due:
                        self.save_state()
                for item in due:
                    if isinstance(item, tuple):
                        self._notify_research_failure(item[0])
                    else:
                        self._research_queue_item(item)
            except Exception as exc:
                log(f"调研持久化调度异常: {str(exc)[:120]}")
            time.sleep(RESEARCH_RETRY_POLL)

    def _prune_seen_fps(self, now=None):
        """Keep only recent content fingerprints for updates without message IDs."""
        now = time.time() if now is None else now
        seen_fps = self.state.get("seen_fps", {})
        if not isinstance(seen_fps, dict):
            self.state["seen_fps"] = {}
            return
        cutoff = now - FINGERPRINT_TTL_S
        self.state["seen_fps"] = {
            fp: ts for fp, ts in seen_fps.items()
            if isinstance(ts, (int, float)) and cutoff <= ts <= now
        }

    def _is_duplicate_content(self, fp, now=None):
        """Record a fingerprint and return whether it was recently seen."""
        now = time.time() if now is None else now
        self._prune_seen_fps(now)
        seen_fps = self.state["seen_fps"]
        if fp in seen_fps:
            return True
        seen_fps[fp] = now
        return False

    def _mark_replied(self, text):
        """记录已成功回复的入站消息指纹，重启补回队列时跳过，防止重复回复。"""
        try:
            fp = hashlib.md5((self.account.get("user_id", "") + "|" + text).encode("utf-8")).hexdigest()
            fps = self.state.setdefault("replied_fps", [])
            fps.append(fp)
            self.state["replied_fps"] = fps[-100:]
        except Exception:
            pass

    def _persist_account_state(self, account, state):
        try:
            acct = dict(account)
            acct["sync_buf"] = state.get("sync_buf", "")
            acct["context_token"] = state.get("context_token", "")
            acct["typing_ticket"] = state.get("typing_ticket", "")
            (self.accounts_dir / f"{account.get('account_id')}.json").write_text(
                json.dumps(acct, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log(f"账号池状态保存失败: {e}")

    def _cleanup_stale_session(self):
        """重绑后旧账号的 codex_session/context_token 必须清空，否则 Codex 回复全灭。"""
        sa = self.state.get("session_account") or ""
        if sa and sa != self.account.get("account_id"):
            log(f"🧹 检测到 codex_session 属于旧账号 {sa}，清空残留会话/令牌")
            self.state["codex_session"] = None
            self.state["context_token"] = ""
            self.state["typing_ticket"] = ""
            self.state["session_account"] = self.account.get("account_id")
            self.save_state()

    # ── 微信 → 回复队列 ──
    def ilink_loop(self):
        while True:
            try:
                self._last_poll_ts = time.time()

                # 定期排空发送重试队列
                if time.time() - self._retry_queue_flush_ts > FLUSH_RETRY_INTERVAL:
                    self._retry_queue_flush_ts = time.time()
                    self._flush_retry_queue()

                # 会话过期暂停期：期间检查热加载，新账号就绪立即恢复
                if time.time() < self._expiry_pause_until:
                    if self._check_account_reload():
                        self._expiry_pause_until = 0.0
                        continue
                    time.sleep(min(10, self._expiry_pause_until - time.time()))
                    continue

                # 熔断检查
                if not self.cb.before_request():
                    wait = self.cb.remaining_cooldown() or 60.0
                    log(f"🔒 熔断 OPEN，{int(wait)}s 后重试")
                    time.sleep(min(wait, 60.0))
                    continue

                resp = _req(f"{self.account['base_url']}/{EP_GET_UPDATES}",
                            {"get_updates_buf": self.state["sync_buf"]},
                            token=self.account["token"], timeout=self._poll_timeout)
            except Exception as e:
                wait = self.cb.on_failure()
                log(f"⚠️ getupdates 异常(cb={self.cb.state} failures={self.cb.consecutive_failures} "
                    f"backoff={wait:.1f}s): {str(e)[:100]}")
                time.sleep(wait)
                continue

            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            suggested = resp.get("longpolling_timeout_ms")
            if isinstance(suggested, int) and 10 <= suggested <= 120:
                self._poll_timeout = suggested
            if ret not in (0, None) or errcode not in (0, None):
                if errcode == ERRCODE_SESSION_EXPIRED or _is_stale_session_ret(ret, errcode, resp.get("errmsg")):
                    self._handle_session_expired()
                    continue
                if errcode == ERRCODE_RATE_LIMIT:
                    wait = self.cb.on_failure(is_rate_limit=True)
                    log(f"⚠️ 限流，退避 {wait:.1f}s")
                    time.sleep(min(wait, 30.0))
                    continue
                wait = self.cb.on_failure()
                log(f"⚠️ getupdates ret={ret} errcode={errcode} {str(resp.get('errmsg'))[:80]} "
                    f"(cb={self.cb.state} backoff={wait:.1f}s)")
                time.sleep(wait)
                continue

            self.cb.on_success()
            self._expiry_streak = 0
            new_buf = resp.get("get_updates_buf")
            if new_buf:
                self.state["sync_buf"] = new_buf
            updates = resp.get("msgs") or resp.get("update_list") or resp.get("updates") or []
            if updates:
                self.save_state()
            for upd in updates:
                self.handle_update(upd)

    def _handle_session_expired(self):
        log("❌ 微信会话过期(-14/失效)，重置熔断；保持单一账号，暂停后自动重试")
        self.cb.on_success()  # 会话过期视为瞬时事件，不累计熔断失败
        self._expiry_pause_until = time.time() + SESSION_EXPIRED_PAUSE
        self._expiry_streak += 1
        if self._expiry_streak < EXPIRY_REBIND_THRESHOLD:
            log(f"会话过期第 {self._expiry_streak}/{EXPIRY_REBIND_THRESHOLD} 轮，静默重试中（先不出码）")
            return
        if time.time() - self._rebind_attempted_at > REBIND_ATTEMPT_COOLDOWN:
            self._rebind_attempted_at = time.time()
            bus_notice("@用户 微信专线持续过期，已自动生成新二维码，请尽快扫码（见桌面）")
            _toast("微信通道需要重新扫码",
                   "会话持续过期，二维码已生成到桌面（微信重绑二维码-时间.png），扫码即可恢复。")
            threading.Thread(target=self._auto_rebind, daemon=True).start()
        else:
            log(f"重绑提醒冷却期内（上次 {int(time.time() - self._rebind_attempted_at)}s 前），继续静默重试")

    def _activate_account(self, account):
        """重绑热加载：保存旧状态，加载新账号状态，清空旧 AI 会话残留。"""
        self._persist_account_state(self.account, self.state)
        self.account = account
        self.cb = _CircuitBreaker(account.get("account_id", "bridge"))
        self.state["sync_buf"] = account.get("sync_buf", "")
        self.state["context_token"] = account.get("context_token", "")
        self.state["typing_ticket"] = account.get("typing_ticket", "")
        self.state["boss_chat"] = ""
        self.state["codex_session"] = None
        self.state["session_account"] = account.get("account_id")
        clean = {k: v for k, v in account.items() if k in ("account_id", "token", "base_url", "user_id", "saved_at")}
        try:
            ACCOUNT_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._account_mtime = ACCOUNT_FILE.stat().st_mtime if ACCOUNT_FILE.exists() else 0.0
        self.save_state()
        log(f"🔄 已加载新绑定账号 {account['account_id']}")
        try:
            self.send_weixin("微信通道已重新绑定，恢复使用。")
        except Exception:
            pass

    def _check_account_reload(self):
        """检测 account.json 被重新注册覆盖 → 热加载新账号（返回是否切换）。"""
        try:
            if not ACCOUNT_FILE.exists():
                return False
            mtime = ACCOUNT_FILE.stat().st_mtime
            if mtime == self._account_mtime:
                return False
            self._account_mtime = mtime
            new = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
            if new.get("account_id") and new["account_id"] != self.account.get("account_id"):
                log(f"🔄 检测到新绑定 {new['account_id']}，热切换账号")
                self._activate_account(new)
                return True
        except Exception:
            pass
        return False

    def _auto_rebind(self):
        """后台自动重绑：生成二维码 PNG，用户扫码后 account.json 更新，主循环热加载。"""
        log("🔄 自动生成重新绑定二维码…")
        try:
            code = register()
            if code == 0:
                log("✅ 后台重绑完成，等待主循环热加载新账号")
                bus_notice("@用户 微信专线已重新扫码绑定成功，通道恢复中")
            else:
                log("⚠️ 自动重绑未完成（二维码过期或失败），需要人工 register")
                bus_notice("@用户 微信专线会话过期，自动重绑失败，请运行 register 扫码")
        except Exception as e:
            log(f"自动重绑线程异常: {e}")

    def _research_followup_candidates(self, text, include_terminal=True):
        account = getattr(self, "account", {}) or {}
        raw = str(text or "")
        task_match = re.search(r"research-[0-9a-f]{8,40}", raw, re.I)
        with self._research_lock:
            jobs = [j for j in self._research_jobs().values()
                    if j.get("account_id") == (account.get("account_id") or "")]
        if task_match:
            return [j for j in jobs if j.get("task_id", "").lower() == task_match.group(0).lower()]
        if not include_terminal:
            jobs = [j for j in jobs if j.get("status") not in {"delivered", "failed", "cancelled"}]
        if not jobs:
            return []
        jobs.sort(key=lambda j: j.get("created_ts", ""), reverse=True)
        # A topic-bearing follow-up may select one job; a generic follow-up
        # stays ambiguous when more than one account-scoped job is active.
        compact = re.sub(r"[\s，。！？!?、,:：;；（）()【】\[\]]", "", raw)
        scored = []
        for job in jobs:
            subject = _research_subject(job.get("request", ""))
            pieces = [p for p in re.split(r"-+", subject) if len(p) >= 2]
            score = max((len(piece) for piece in pieces if piece in compact), default=0)
            if score:
                scored.append((score, job))
        if scored:
            best = max(score for score, _ in scored)
            return [job for score, job in scored if score == best]
        active = [j for j in jobs if j.get("status") not in {"delivered", "failed", "cancelled"}]
        return active or jobs

    def _research_status_text(self, job):
        labels = {
            "queued": "已排队",
            "running": "研究中",
            "retry_wait": "等待下一次研究重试",
            "archived": "报告已保存，等待微信发送",
            "delivering": "正在发送报告",
            "delivery_ambiguous": "发送结果不确定，等待你确认后重试",
            "delivered": "已完成并发送",
            "failed": "已失败，不再自动重试",
            "cancelled": "已取消",
        }
        status = labels.get(job.get("status"), job.get("status", "未知"))
        paths = [Path(p).name for p in (job.get("archive_paths") or []) if p]
        saved = "；已保存 " + ", ".join(paths) if paths else "；没有保存完整报告"
        partial = len(job.get("partial_findings") or [])
        if partial:
            saved += f"；部分进展 {partial} 条在任务记录中"
        next_retry = job.get("next_retry_ts") or job.get("next_delivery_ts")
        when = f"；下一次时间 {next_retry}" if next_retry else ""
        return (f"任务 {job.get('task_id')}：{status}；研究尝试 {int(job.get('attempts') or 0)} 次，"
                f"发送尝试 {int(job.get('delivery_attempts') or 0)} 次{when}{saved}。")

    def _handle_research_followup(self, text):
        """Handle status/retry/cancel follow-ups without invoking reminder/model routing."""
        raw = str(text or "").strip()
        norm = _norm_msg(raw)
        task_id_present = bool(re.search(r"research-[0-9a-f]{8,40}", raw, re.I))
        cancel = bool(re.search(r"(?:取消|停止|别再|不要再).*(?:调研|研究|任务)", raw)) or (
            task_id_present and bool(re.search(r"(?:取消|停止)", raw)))
        retry = (norm in {"重试", "再试一次", "继续调研"}
                 or task_id_present and bool(re.search(r"(?:重试|再试一次)", raw))
                 or bool(re.search(r"(?:重试|再试一次).*(?:调研|研究)", raw)))
        status = (norm in {"有结果了吗", "调研还会继续吗", "调研进展", "调研状态", "任务状态", "报告呢"}
                  or bool(re.search(r"(?:有结果了吗|调研还会继续吗|调研进展|调研状态|任务状态)", raw)))
        if (_research_request(raw) and not task_id_present and not cancel and not status and not retry
                and norm not in {"重试", "再试一次"}):
            return False
        if not (task_id_present or cancel or retry or status):
            return False
        include_terminal = status or retry or cancel
        candidates = self._research_followup_candidates(raw, include_terminal=include_terminal)
        if len(candidates) != 1:
            if not candidates:
                self.send_weixin("没有找到对应的调研任务。请提供任务编号（例如 research-xxxxxxxx）或先发起调研。")
            else:
                lines = ["找到多个调研任务，请回复任务编号："]
                for job in candidates[:8]:
                    lines.append(f"{job.get('task_id')} · {_research_subject(job.get('request', ''))} · {job.get('status')}")
                self.send_weixin("\n".join(lines))
            return True
        job = candidates[0]
        task_id = job.get("task_id")
        if cancel:
            updated = self._cancel_research_job(task_id)
            self.send_weixin(f"已取消调研任务 {task_id}。" if updated else "这个调研任务已经结束，未再安排重试。")
            return True
        if retry:
            updated = self._retry_research_job(task_id)
            if not updated:
                self.send_weixin("没有找到可重试的调研任务。")
            elif updated.get("status") in {"queued", "archived"}:
                self.send_weixin(f"已安排重试任务 {task_id}；当前状态：{self._research_status_text(updated)}")
            else:
                self.send_weixin(self._research_status_text(updated))
            return True
        self.send_weixin(self._research_status_text(job))
        return True

    def handle_update(self, upd):
        if not isinstance(upd, dict):
            return
        msg = upd.get("message") if isinstance(upd.get("message"), dict) else upd
        if not isinstance(msg, dict):
            return
        from_user = msg.get("from_user_id", "")
        allowed = self.account.get("user_id", "") or ""
        if not from_user or from_user != allowed:
            log(f"⛔ 拦截未授权发信人: {from_user!r}（仅接受绑定用户 {allowed}）")
            return
        ctx = msg.get("context_token", "")
        msg_id = msg.get("message_id") or msg.get("client_id") or ""
        if msg_id and msg_id in self.state["seen"]:
            return
        if msg_id:
            self.state["seen"].append(msg_id)
        if ctx:
            self.state["context_token"] = ctx
        if msg.get("message_type") == MSG_TYPE_BOT:
            return
        texts = []
        attachments = []
        image_attachments = []
        other_attachments = []
        for item in (msg.get("item_list") or []):
            t = item.get("type")
            if t == ITEM_TEXT:
                txt = (item.get("text_item") or {}).get("text", "")
                if txt:
                    texts.append(txt)
            elif t in (ITEM_IMAGE, ITEM_FILE, ITEM_VOICE, ITEM_VIDEO):
                p = _download_media(item)
                if p:
                    attachments.append(p)
                    (image_attachments if t == ITEM_IMAGE else other_attachments).append(p)
        text = "\n".join(texts).strip()
        if not from_user:
            return
        # 纯图片作为待处理附件：只保存，不回复、不调用 Agent、不进入任何队列。
        # inbox 内文件不设自动过期，等用户后续手动清除或明确指令处理。
        if image_attachments and not text and not other_attachments:
            self.state["boss_chat"] = from_user
            log(f"📎 纯图片已保存到 inbox，等待后续指令: {', '.join(Path(a).name for a in image_attachments)}")
            self.save_state()
            return
        if not text and attachments:
            text = f"[用户发来 {len(attachments)} 个文件，请处理]"
        if not text:
            return
        if attachments:
            text += "\n\n[用户这次发来的文件]\n" + "\n".join(f"- {Path(a).name} -> {a}" for a in attachments)
        self.state["boss_chat"] = from_user
        log(f"📩 微信[{from_user[:14]}…]: {text[:60]}")
        if not msg_id:
            fp = hashlib.md5((from_user + "|" + text).encode("utf-8")).hexdigest()
            if self._is_duplicate_content(fp):
                log("♻️ 无消息 ID，短期内容指纹去重，跳过重复消息")
                return
        norm = _norm_msg(text)
        raw = text.strip()
        if norm in STOP_PHRASES:
            stop_current_task()
            self._cancel_research_jobs()
            self.send_weixin("好，已停下。")
            return
        if norm in STATUS_PHRASES:
            identity = BACKEND_IDENTITY.get(self.backend, "Codex")
            self.send_typing()
            self.send_weixin(f"微信通道正常，{identity} 在线，有事直接说。")
            self.save_state()
            return
        if norm in NEW_PHRASES:
            self.state["codex_session"] = None
            self.save_state()
            self.send_weixin("好，已开启全新会话，你直接说事就行。")
            return
        m_resume = re.search(r"(?:切回|回到|继续|换到|第)\s*(\d+)\s*(?:个)?(?:会话)?\s*$", raw)
        if not m_resume:
            m_resume = re.fullmatch(r"/resume\s*(\d+)", raw) or re.fullmatch(r"resume\s*(\d+)", raw)
        if m_resume:
            idx = int(m_resume.group(1)) - 1
            sessions = self.state.get("sessions") or []
            if 0 <= idx < len(sessions):
                self.state["codex_session"] = sessions[idx]["sid"]
                self.save_state()
                self.send_weixin(f"好的，切回第{idx+1}个会话：{sessions[idx].get('desc', '')}")
            else:
                self.send_weixin(f"没有第{idx+1}个会话，发「历史会话」看看有哪些。")
            return
        if norm in ("继续", "接着聊", "续上", "继续聊"):
            sessions = self.state.get("sessions") or []
            if sessions:
                self.state["codex_session"] = sessions[-1]["sid"]
                self.save_state()
                self.send_weixin(f"好的，继续上次的会话：{sessions[-1].get('desc', '')}")
            else:
                self.send_weixin("没有历史会话，直接说新任务就行。")
            return
        if norm in SESSIONS_PHRASES:
            sessions = self.state.get("sessions") or []
            if not sessions:
                self.send_weixin("当前还没有历史会话，直接说事就行。")
            else:
                lines = ["历史会话："]
                for i, s in enumerate(sessions, 1):
                    lines.append(f"第{i}个：{s.get('desc', '')}（{str(s.get('ts', ''))[:16]}）")
                lines.append("回复「切回第N个」切换")
                self.send_weixin("\n".join(lines))
            return
        knowledge = _extract_knowledge(text)
        if knowledge is not None:
            if not knowledge:
                self.send_weixin("请在 knowledge 或“知识”后写清楚要保存的内容。")
                return
            try:
                _append_knowledge(knowledge)
                self.send_weixin(f"已保存到 knowledge.md：{knowledge}")
            except Exception as e:
                log(f"知识记录写入失败: {type(e).__name__}: {str(e)[:120]}")
                self.send_weixin("写入 knowledge.md 失败，内容没有保存，请稍后重试。")
            self.save_state()
            return
        diary = _extract_diary(text)
        if diary is not None:
            if not diary:
                self.send_weixin("请在 diary、ddd 或“日记”后写清楚要记录的想法。")
                return
            timestamp = datetime.now(timezone.utc)
            try:
                _append_diary_local(diary, timestamp)
            except Exception as e:
                log(f"日记本地写入失败: {type(e).__name__}: {str(e)[:120]}")
                self.send_weixin("写入 diary.md 失败，内容没有保存，请稍后重试。")
                self.save_state()
                return
            notion_page_id = str((self.backend_cfg or {}).get("notion_diary_page_id") or os.environ.get("NOTION_DIARY_PAGE_ID", "")).strip()
            try:
                _append_diary_notion(diary, timestamp, notion_page_id)
            except Exception as e:
                log(f"Notion 日记写入失败: {type(e).__name__}: {str(e)[:120]}")
                self.send_weixin("已保存到 diary.md，但写入 Notion 日记失败，请稍后重试。")
            else:
                self.send_weixin("已保存到 diary.md 和 Notion 日记。")
            self.save_state()
            return
        long_task = _extract_long_task(text)
        if long_task is not None:
            if not long_task:
                self.send_weixin("请在“长任务”前写清楚任务内容。")
                return
            self.send_typing()
            try:
                outcome = _notion_add_long_task(long_task)
                if outcome == "duplicate":
                    self.send_weixin(f"今天已经有相同的长任务“{long_task}”，没有重复添加。")
                else:
                    self.send_weixin(f"已写入今天的长任务：{long_task}。")
            except Exception as e:
                log(f"Notion 长任务写入失败: {type(e).__name__}: {str(e)[:120]}")
                self.send_weixin("写入 Notion 失败，任务没有添加，请稍后重试。")
            self.save_state()
            return
        if self._handle_research_followup(text):
            return
        # 由 Codex 识别自然语言提醒；不依赖“几点提醒我”这类硬编码命令。
        self.send_typing()
        try:
            intent = self._understand_reminder(text)
            if self._handle_reminder_action(intent):
                return
        except Exception as e:
            log(f"提醒意图识别异常，继续普通对话: {str(e)[:120]}")
        level = classify_message(text)
        log(f"💬 收到消息（{level}）：{text[:30]}")
        research_route = _research_request(text)
        if research_route:
            # Acknowledge immediately, before the request can wait in either worker queue.
            self.send_weixin("Okay, I'm starting to do the investigation.")
            # Research is always admitted to the durable task queue, even when
            # the generic classifier calls it a medium-sized chat turn.
            task_id = self._create_research_job(text, research_route)
            queued = self._research_queue_item(task_id)
            if queued:
                self.send_typing()
                self.send_weixin(f"收到，调研任务已登记（任务 {task_id}），完成后我会发送结果。")
            else:
                job = self._research_jobs().get(task_id) or {}
                if job.get("status") in {"running", "retry_wait", "queued"}:
                    self.send_weixin(f"这个调研任务已在处理中（任务 {task_id}）。")
            self.save_state()
            return
        if level == "complex":
            # 复杂任务：进后台任务队列，最新任务优先（appendleft），不占聊天通道
            with self._task_cond:
                self.task_q.appendleft(text)
                self._task_cond.notify()
            self.send_typing()
            preview = text[:50] + ("…" if len(text) > 50 else "")
            queue_pos = len(self.task_q)  # 已 appendleft，长度含刚入队的自己
            self.send_weixin(f"收到，任务已入队（最新优先，排第{queue_pos}）：{preview}，会按顺序执行并实时报进度。")
            self.save_state()
            return
        # 对话消息：新消息优先；若后台任务在跑，先打断它，任务处理完聊天后自动恢复
        if self._running_task.get("active"):
            self._preempt_task()
        with self._q_cond:
            self.reply_q.appendleft(text)  # 新消息优先
            self._q_cond.notify()
        self.save_state()

    def _get_typing_ticket(self):
        entry = self.state.get("typing_ticket")
        if entry:
            if isinstance(entry, dict):
                if time.time() - entry.get("ts", 0) < 600:
                    return entry.get("ticket", "")
            else:
                return entry  # 旧格式字符串兼容
        to = self.state.get("boss_chat", "") or self.account.get("user_id", "")
        try:
            resp = _req(f"{self.account['base_url']}/{EP_GET_CONFIG}",
                        {"ilink_user_id": to, "context_token": self.state.get("context_token", "")},
                        token=self.account["token"], timeout=10)
            t = str(resp.get("typing_ticket") or "")
            if t:
                self.state["typing_ticket"] = {"ticket": t, "ts": time.time()}
            return t
        except Exception:
            return ""

    def send_typing(self):
        to = self.state.get("boss_chat", "") or self.account.get("user_id", "")
        ticket = self._get_typing_ticket()
        if not to or not ticket:
            return
        try:
            _req(f"{self.account['base_url']}/{EP_SEND_TYPING}",
                 {"ilink_user_id": to, "typing_ticket": ticket, "status": 1},
                 token=self.account["token"], timeout=10)
        except Exception:
            pass

    def _understand_reminder(self, text):
        """Ask Codex to interpret reminder language without changing the user's chat session."""
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(REMINDER_TIMEZONE))
        prompt = REMINDER_INTERPRETER_PROMPT.format(
            timezone=REMINDER_TIMEZONE, now=now.strftime("%Y-%m-%d %H:%M:%S")) + str(text)
        cfg = self.backend_cfg or {}
        reply, _ = codex_reply(prompt, model=cfg.get("chat_model") or CHAT_MODEL,
                               effort=cfg.get("chat_effort") or CHAT_EFFORT, timeout=45)
        intent = _parse_reminder_json(reply)
        if intent and intent["action"] == "create":
            try:
                clock_time = _reminder_clock_time(text, now)
            except ValueError:
                intent["action"] = "clarify"
            else:
                if clock_time:
                    intent["datetime"] = clock_time
        return intent

    def _handle_reminder_action(self, intent):
        """Handle a Codex-recognised reminder action. Return True only when it consumed this message."""
        if not intent or intent["action"] == "none":
            return False
        action = intent["action"]
        if action == "clarify":
            self.send_weixin("请确认提醒的具体日期和时间，例如“今天晚上九点半提醒我茶壶和保温壶”。")
            return True
        if action == "create":
            due_utc = _reminder_due_at(intent["datetime"])
            if not due_utc or not intent["title"]:
                self.send_weixin("提醒尚未创建，请补充提醒内容和未来的具体日期、时间。")
                return True
            google_calendar_id = str(_load_reminder_config().get("google_calendar_id") or "").strip()
            try:
                conn = _reminder_db()
                with conn:
                    conn.execute("""INSERT INTO reminders
                        (user_id, title, due_at_utc, timezone, calendar_event, calendar_status, created_at_utc,
                         google_calendar_id, google_status)
                        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                        (self.state.get("boss_chat") or self.account.get("user_id", ""), intent["title"], due_utc,
                         REMINDER_TIMEZONE, "", datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                         google_calendar_id, "pending" if google_calendar_id else "not_requested"))
                    reminder_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.close()
            except Exception as e:
                log(f"提醒存储失败: {str(e)[:120]}")
                self.send_weixin("本地提醒保存失败，请再试一次。")
                return True
            # Confirm the durable WeChat timer before attempting the optional calendar sync.
            # A rejected or slow CalDAV request must never make the user think that the
            # WeChat notification itself was not created.
            self.send_weixin(
                f"已设置微信提醒：{intent['datetime'].replace('T', ' ')} 提醒你“{intent['title']}”。"
                + ("正在同步 iCloud 和 Google 日历。" if google_calendar_id else "正在同步 iCloud 日历。")
            )

            # The local durable timer is deliberately created first. Calendar trouble must not suppress WeChat delivery.
            event_ref, error = _create_icloud_reminder(intent["title"], intent["datetime"])
            try:
                conn = _reminder_db()
                with conn:
                    conn.execute("UPDATE reminders SET calendar_event=?, calendar_status=?, last_error=? WHERE id=?",
                                 (event_ref or "", "created" if not error else "failed", error, reminder_id))
                conn.close()
            except Exception as e:
                log(f"提醒日历状态保存失败: {str(e)[:120]}")
            if error:
                self.send_weixin("iCloud 同步失败，但微信定时提醒已保留。")
            else:
                self.send_weixin("已同步到 iCloud 的 AI助手日历（提前 10 分钟提醒）。")
            if google_calendar_id:
                google_event, google_error = _create_google_reminder(
                    intent["title"], intent["datetime"], google_calendar_id)
                try:
                    conn = _reminder_db()
                    with conn:
                        conn.execute("UPDATE reminders SET google_event_id=?, google_status=?, google_error=? WHERE id=?",
                                     (google_event or "", "unconfirmed" if google_error else "created",
                                      google_error, reminder_id))
                    conn.close()
                except Exception:
                    self.send_weixin("Google 日历同步状态保存失败，请检查日历。")
                if google_error:
                    self.send_weixin(google_error + "；微信定时提醒已保留。")
                else:
                    self.send_weixin("已同步到 Google 日历（提前 10 分钟提醒）。")
            return True
        if action == "cancel":
            user_id = self.state.get("boss_chat") or self.account.get("user_id", "")
            try:
                conn = _reminder_db()
                query = ("SELECT id, title, calendar_event, google_event_id, google_calendar_id, google_status "
                         "FROM reminders WHERE status='scheduled' AND user_id=?")
                args = [user_id]
                if intent["title"]:
                    query += " AND title LIKE ?"
                    args.append(f"%{intent['title']}%")
                row = conn.execute(query + " ORDER BY id DESC LIMIT 1", args).fetchone()
                if row:
                    with conn:
                        conn.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (row["id"],))
                conn.close()
            except Exception as e:
                log(f"取消提醒失败: {str(e)[:120]}")
                row = None
            if row:
                deleted = _delete_icloud_reminder(row["calendar_event"])
                google_deleted = _delete_google_reminder(row["google_event_id"], row["google_calendar_id"])
                try:
                    conn = _reminder_db()
                    with conn:
                        conn.execute("UPDATE reminders SET calendar_status=? WHERE id=?",
                                     ("cancelled" if deleted else "cancelled_local", row["id"]))
                        conn.execute("UPDATE reminders SET google_status=? WHERE id=?",
                                     ("cancelled" if google_deleted else "cancel_failed", row["id"]))
                    conn.close()
                except Exception:
                    pass
                self.send_weixin(f"已取消提醒“{row['title']}”。")
                if row["calendar_event"] and not deleted:
                    self.send_weixin("iCloud 日历事件未能删除，请手动检查。")
                if not google_deleted or row["google_status"] in {"pending", "unconfirmed"}:
                    self.send_weixin("Google 日历事件删除未确认，请手动检查。")
            else:
                self.send_weixin("没有找到可取消的提醒。")
            return True
        if action == "list":
            try:
                conn = _reminder_db()
                rows = conn.execute("SELECT title, due_at_utc FROM reminders WHERE status='scheduled' "
                                    "AND user_id=? ORDER BY due_at_utc LIMIT 10",
                                    (self.state.get("boss_chat") or self.account.get("user_id", ""),)).fetchall()
                conn.close()
            except Exception:
                rows = []
            if not rows:
                self.send_weixin("目前没有待发送的微信提醒。")
            else:
                from zoneinfo import ZoneInfo
                lines = []
                for row in rows:
                    due = datetime.fromisoformat(row["due_at_utc"]).astimezone(ZoneInfo(REMINDER_TIMEZONE))
                    lines.append(f"{due:%Y-%m-%d %H:%M} · {row['title']}")
                self.send_weixin("待发送提醒：\n" + "\n".join(lines))
            return True
        return False

    def _reminder_loop(self):
        """Durable scheduler: events due while the bridge is offline are sent after it returns."""
        while True:
            try:
                now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                conn = _reminder_db()
                rows = conn.execute("SELECT id, user_id, title FROM reminders "
                                    "WHERE status='scheduled' AND due_at_utc<=? ORDER BY due_at_utc", (now,)).fetchall()
                for row in rows:
                    self.send_weixin(f"⏰ 提醒：{row['title']}")
                    with conn:
                        conn.execute("UPDATE reminders SET status='handed_off', delivered_at_utc=? WHERE id=?",
                                     (datetime.now(timezone.utc).replace(microsecond=0).isoformat(), row["id"]))
                conn.close()
            except Exception as e:
                log(f"提醒调度异常: {str(e)[:120]}")
            time.sleep(REMINDER_POLL_INTERVAL)

    # ── 回复工作线程池：Agent CLI → 微信（新消息优先，多消息并发） ──
    def _reply_worker_loop(self):
        while True:
            with self._q_cond:
                while not self.reply_q:
                    self._q_cond.wait()
                text = self.reply_q.popleft()
            with _active_turns_lock:
                self._chat_active += 1
            try:
                self._process_one(text)
            except Exception as e:
                log(f"⚠️ 回复线程异常: {e}")
            finally:
                with _active_turns_lock:
                    self._chat_active -= 1

    def _deliver_research_report(self, report, request_text, file_paths=(), task_id=None):
        """Archive Markdown and deliver it with explicit archive/delivery outcomes.

        Once a durable job reaches ``archived`` this method reuses its saved
        paths and summary.  A transient WeChat failure therefore retries only
        delivery; the model is never run again and no second Markdown report is
        created.  The notification intent is persisted by the job before each
        send.  WeChat has no idempotency key, so a process crash after a
        successful remote send can still leave an ambiguous pending intent; the
        state deliberately records that uncertainty instead of claiming
        exactly-once delivery.
        """
        job = self._research_jobs().get(task_id) if task_id else None
        if job and self._research_is_cancelled(task_id):
            return ResearchDeliveryOutcome(stage="cancelled", reason="user cancellation")
        if job and job.get("account_id") != (getattr(self, "account", {}) or {}).get("account_id"):
            return ResearchDeliveryOutcome(stage="account", reason="job belongs to another account")
        # A durable delivery invocation must own a fresh claim.  In-flight,
        # failed, cancelled, or ambiguous jobs are not eligible and must not
        # be allowed to send under another worker's claim.
        if job and job.get("status") not in {"running", "archived"}:
            return ResearchDeliveryOutcome(stage="delivery_claim", handled=False,
                                           reason="delivery claim unavailable")
        send_research = (lambda text, kind: self._send_research_message(text, task_id, kind)
                         if isinstance(self, Bridge) else self.send_weixin(text))
        delivery = (job.setdefault("delivery", {}) if job else {})
        requested_summary, _ = _split_research_reply(report or "")
        report_body, source = _report_body_from_reply(report or "", file_paths)
        path = None
        txt_path = None
        if job:
            # Include a complete reserved target even if the previous process
            # died between the write and the archive transition/save.
            recovered_paths = self._research_recover_archive_paths(job)
            if recovered_paths != [str(p) for p in (job.get("archive_paths") or []) if p]:
                with self._research_lock:
                    current = self._research_jobs().get(task_id)
                    if current:
                        current["archive_paths"] = recovered_paths
                        self.save_state()
                job = self._research_jobs().get(task_id) or job
            if job.get("archive_paths"):
                paths = [Path(p) for p in job.get("archive_paths") if p]
            else:
                paths = []
            path = next((p for p in paths if p.suffix.lower() == ".md" and p.exists()), None)
            txt_path = next((p for p in paths if p.suffix.lower() == ".txt" and p.exists()), None)
            if path:
                report_body = path.read_text(encoding="utf-8")
        elif file_paths:
            paths = [Path(p) for p in file_paths if p]
        else:
            paths = []
        if not path:
            if not _is_complete_research_report(report_body):
                log("调研归档失败: 未收到完整报告正文，未保存或发送摘要文件")
                send_research("调研报告归档失败：未收到完整报告正文（结论、调研范围和参考来源/笔记）。未生成 MD/TXT，请补充完整报告后重试。", "归档失败")
                return ResearchDeliveryOutcome(stage="archive", reason="incomplete report body")
            if source:
                log(f"调研归档采用 Agent 生成的完整报告: {source}")
            try:
                reuse_source = (source and _is_relative_to(source, RESEARCH_ARCHIVE_DIR.resolve())
                                and (not job or str(source) == str(job.get("archive_target") or ""))
                                and source.read_text(encoding="utf-8").strip() == report_body)
            except Exception:
                reuse_source = False
            target = None
            if job:
                with self._research_lock:
                    current = self._research_jobs().get(task_id)
                    if current and not current.get("archive_target"):
                        current["archive_target"] = str(_research_archive_target(request_text, task_id))
                        self.save_state()
                    target = (current or {}).get("archive_target")
            if job and self._research_is_cancelled(task_id):
                return ResearchDeliveryOutcome(stage="cancelled", reason="user cancellation")
            path = source if reuse_source else _save_research_report(report_body, request_text, target_path=target)
            if not path:
                send_research("调研报告归档失败：完整正文未能保存，未发送 TXT 文件。请重试。", "归档失败")
                return ResearchDeliveryOutcome(stage="archive", reason="report archive failed")
            if job:
                self._research_transition(task_id, "archived", reason="report archived",
                                          archive_paths=[str(path)], archive_target=str(path),
                                          summary=requested_summary or _fallback_research_summary(report_body))
        if job:
            if self._research_is_cancelled(task_id):
                return ResearchDeliveryOutcome(stage="cancelled", reason="user cancellation",
                                               archive_paths=[path] if path else ())
            current = self._research_jobs().get(task_id) or {}
            if current.get("status") not in {"running", "archived"}:
                return ResearchDeliveryOutcome(stage="delivery_claim", handled=False,
                                               reason="delivery claim unavailable",
                                               archive_paths=[path] if path else ())
            if not self._research_mark_delivery(task_id):
                return ResearchDeliveryOutcome(stage="delivery_claim", handled=False,
                                               reason="delivery claim unavailable",
                                               archive_paths=[path] if path else ())
            job = self._research_jobs().get(task_id) or job
            delivery = job.setdefault("delivery", {})
        if not txt_path:
            if job and self._research_is_cancelled(task_id):
                return ResearchDeliveryOutcome(stage="cancelled", reason="user cancellation", archive_paths=[path])
            txt_path = _save_research_text_copy(path)
            if txt_path and job:
                paths = list(job.get("archive_paths") or [])
                if str(txt_path) not in paths:
                    paths.append(str(txt_path))
                self._research_checkpoint(task_id, partial=f"archived: {path}")
                with self._research_lock:
                    current = self._research_jobs().get(task_id)
                    if current:
                        current["archive_paths"] = paths
                        self.save_state()
        summary = (job or {}).get("summary") or requested_summary or _fallback_research_summary(report_body)
        if job and not job.get("knowledge_recorded"):
            try:
                title = _research_subject(request_text)
                _append_knowledge(f"{title} completed, reported {path.name}")
                self._research_transition(task_id, job.get("status", "archived"), reason="knowledge recorded",
                                          knowledge_recorded=True)
            except Exception as e:
                log(f"调研完成记录写入知识库失败: {str(e)[:120]}")
        elif not job:
            try:
                title = _research_subject(request_text)
                _append_knowledge(f"{title} completed, reported {path.name}")
            except Exception as e:
                log(f"调研完成记录写入知识库失败: {str(e)[:120]}")

        summary_sent = delivery.get("summary") == "sent"
        file_sent = delivery.get("file") == "sent"
        completion_sent = delivery.get("completion") == "sent"
        if not summary_sent:
            if job and self._research_is_cancelled(task_id):
                return ResearchDeliveryOutcome(stage="cancelled", reason="user cancellation",
                                               archive_paths=[path, txt_path])
            if job:
                with self._research_lock:
                    delivery["summary"] = "sending"
                    delivery["summary_intent"] = summary
                    self.save_state()
            try:
                result = send_research(summary, "摘要")
                summary_sent = result is not False
            except Exception as exc:
                log(f"调研摘要发送异常: {str(exc)[:120]}")
            if job:
                delivery["summary"] = "sent" if summary_sent else "pending"
        if not file_sent:
            if job and self._research_is_cancelled(task_id):
                return ResearchDeliveryOutcome(stage="cancelled", reason="user cancellation",
                                               archive_paths=[path, txt_path], summary_sent=summary_sent)
            if not txt_path:
                delivery["file"] = "failed"
            else:
                if job:
                    with self._research_lock:
                        delivery["file"] = "sending"
                        delivery["file_intent"] = str(txt_path)
                        self.save_state()
                try:
                    file_sent = bool(self.send_file(str(txt_path), task_id=task_id)
                                     if isinstance(self, Bridge)
                                     else self.send_file(str(txt_path)))
                except Exception as exc:
                    log(f"调研 TXT 发送异常: {str(exc)[:120]}")
                    file_sent = False
                if job:
                    delivery["file"] = "sent" if file_sent else "pending"
        if summary_sent and file_sent and not completion_sent:
            if job and self._research_is_cancelled(task_id):
                return ResearchDeliveryOutcome(stage="cancelled", reason="user cancellation",
                                               archive_paths=[path, txt_path], summary_sent=summary_sent,
                                               file_sent=file_sent)
            completion = f"调研完成：已发送可在微信打开的 {txt_path.name}；Markdown 原件保存在本机：{path.name}"
            if job:
                with self._research_lock:
                    delivery["completion"] = "sending"
                    delivery["completion_intent"] = completion
                    self.save_state()
            try:
                result = send_research(completion, "完成通知")
                completion_sent = result is not False
            except Exception as exc:
                log(f"调研完成通知发送异常: {str(exc)[:120]}")
            if job:
                delivery["completion"] = "sent" if completion_sent else "pending"
        elif summary_sent and not file_sent:
            note = (f"Markdown 原件已保存为 {path.name}，但 TXT 文件发送失败。"
                    if txt_path else f"Markdown 原件已保存为 {path.name}，但 TXT 副本创建失败。")
            if job:
                with self._research_lock:
                    delivery["completion"] = "sending"
                    delivery["completion_intent"] = note
                    self.save_state()
            try:
                note_sent = send_research(note, "失败说明") is not False
            except Exception as exc:
                log(f"调研失败说明发送异常: {str(exc)[:120]}")
                note_sent = False
            if job:
                delivery["completion"] = "sent" if note_sent else "pending"
        if job:
            with self._research_lock:
                current = self._research_jobs().get(task_id)
                if current:
                    current["delivery"] = delivery
                    current["summary"] = summary
                    current["archive_paths"] = list(dict.fromkeys(current.get("archive_paths") or [str(path)]))
                    self.save_state()
        complete = summary_sent and file_sent and completion_sent
        if complete:
            return ResearchDeliveryOutcome(success=True, stage="delivered", reason="",
                                           archive_paths=[path, txt_path], summary_sent=True,
                                           file_sent=True, notification_sent=True)
        return ResearchDeliveryOutcome(success=False, stage="delivery", reason="one or more delivery steps failed",
                                       archive_paths=[path, txt_path], summary_sent=summary_sent,
                                       file_sent=file_sent, notification_sent=completion_sent)

    def _process_one(self, text):
        if not hasattr(self, "_research_lock"):
            self._research_lock = threading.RLock()
        with _active_turns_lock:
            parallel = self._chat_active > 1
            token = uuid.uuid4().hex
            _active_turns.add(token)
        self.state.setdefault("processing", []).append(text)
        self.save_state()
        research_task_id = None
        try:
            level = classify_message(text)
            research_route = _research_request(text)
            if research_route:
                research_task_id = self._create_research_job(text, research_route)
                if level == "complex":
                    self._research_queue_item(research_task_id, front=True)
                    return
                existing = self._research_jobs().get(research_task_id) or {}
                if existing.get("status") == "archived":
                    if self._research_ts(existing.get("next_delivery_ts")) > time.time():
                        return
                    outcome = self._deliver_research_report("", text, (), task_id=research_task_id)
                    if outcome.success:
                        self._research_transition(research_task_id, "delivered", reason="archived report delivered")
                    elif outcome.stage == "delivery_claim":
                        return
                    elif outcome.stage.startswith("delivery") or outcome.stage == "archive_processing":
                        self._research_delivery_failure(research_task_id, outcome)
                    return
                if not self._research_mark_running(research_task_id, self.state.get("codex_session")):
                    return
            if level == "complex":
                # 兜底：复杂任务一律转后台任务队列（最新优先），不占聊天通道
                with self._task_cond:
                    self.task_q.appendleft(text)
                    self._task_cond.notify()
                self.save_state()
                return
            sid = self.state.get("codex_session")
            if parallel:
                sid = None  # 并行模式：新开线程独立处理，不占用主会话
            identity = BACKEND_IDENTITY.get(self.backend, "Codex")
            mem = _load_memory()
            memory_block = ("【微信桥记忆】\n" + mem + "\n\n") if mem else ""
            # Knowledge is attached to questions, including Chinese questions without
            # a terminal question mark, with an explicit exhaustive-scan protocol.
            knowledge_block = _knowledge_prompt(text) if _should_attach_knowledge(text) else ""
            research_hint = _research_instruction(research_route) if research_route else ""
            prompt = (knowledge_block + memory_block + PERMISSION_POLICY + _source_precedence_block()
                      + SYSTEM_HINT.format(identity=identity) + research_hint + text)
            base_prompt = PRIMER.format(identity=identity) if not sid else ""
            on_progress = self.send_weixin
            if research_task_id:
                def on_progress(progress_text):
                    self._research_checkpoint(research_task_id, stage="progress", partial=progress_text)
                    self.send_weixin(progress_text)
            cfg = self.backend_cfg or {}
            chat_model = cfg.get("chat_model") or CHAT_MODEL
            chat_effort = cfg.get("chat_effort") or CHAT_EFFORT
            casual_effort = cfg.get("casual_effort") or CASUAL_EFFORT
            # 简单消息降推理档省 token；普通对话用 chat 档；复杂任务由 _process_task 用 task 档处理
            turn_model, turn_effort = (chat_model, casual_effort if level == "casual" else chat_effort)
            turn_timeout = RESEARCH_HANG_TIMEOUT if research_route else TURN_HANG_TIMEOUT
            deadline = time.monotonic() + RESEARCH_TOTAL_TIMEOUT if research_route else None
            checkpoint = (lambda event: self._research_checkpoint(
                research_task_id, stage=event.get("stage"), error=event.get("error"),
                session_id=event.get("session_id"), partial=event.get("partial"))) if research_task_id else None
            cancel_check = (lambda: self._research_is_cancelled(research_task_id)) if research_task_id else None
            reply, new_sid = agent_reply(self.backend, prompt, sid, on_progress=on_progress,
                                         cfg=self.backend_cfg, model=turn_model, effort=turn_effort,
                                         base=base_prompt or None, timeout=turn_timeout, deadline=deadline,
                                         checkpoint=checkpoint, cancel_check=cancel_check)
            if not parallel and new_sid:
                self.state["codex_session"] = new_sid
                self.state["session_account"] = self.account.get("account_id")
            if research_task_id and new_sid:
                self._research_checkpoint(research_task_id, session_id=new_sid)
            if reply:
                reply, files = _extract_files(reply)
                outcome = (self._deliver_research_report(reply, text, files, task_id=research_task_id)
                           if research_route and (reply.strip() or files) else None)
                report_handled = outcome is not None
                report_sent = bool(outcome) if outcome is not None else False
                if research_task_id and outcome is not None:
                    if outcome.stage == "cancelled":
                        self._research_transition(research_task_id, "cancelled", reason=outcome.reason,
                                                  cancel_requested=True, next_delivery_ts=None)
                    elif outcome.success:
                        self._research_transition(research_task_id, "delivered", reason="report delivered",
                                                  session_id=new_sid or sid)
                    elif outcome.stage == "delivery_claim":
                        # Another worker owns the unique send claim.  Do not
                        # mutate its retry state or fall back to a duplicate
                        # raw-report message.
                        pass
                    elif outcome.stage in {"delivery", "archive_processing"}:
                        self._research_delivery_failure(research_task_id, outcome, new_sid or sid)
                    else:
                        self._research_fail(research_task_id, outcome.stage or "archive", outcome.reason)
                if not research_route:
                    for fp in files:
                        p = Path(fp).expanduser()
                        if not p.is_absolute():
                            p = Path(CODEX_WORK_DIR) / p
                        archived = _archive_temp_report_file(p, text)
                        if archived:
                            p = archived
                        try:
                            if not self.send_file(str(p)):
                                self.send_weixin(f"文件发送失败：{fp}")
                        except Exception as e:
                            log(f"发送文件异常: {str(e)[:120]}")
                            self.send_weixin(f"文件发送失败：{str(e)[:80]}")
                if reply.strip() and not report_handled:
                    self.send_weixin(reply)
                    self._mark_replied(text)
                elif report_sent and (not outcome or outcome.success):
                    self._mark_replied(text)
                if not parallel:
                    self._record_session(new_sid, text)
                    _append_memory(f"- {datetime.now():%Y-%m-%d %H:%M} | 用户：{text[:60]} → 桥：{reply[:80]}")
                self.save_state()
            elif _cancel_event.is_set():
                log("⏹ 任务被 /stop 停止")
                if research_task_id:
                    self._research_transition(research_task_id, "cancelled", reason="user cancellation",
                                              cancel_requested=True)
                _cancel_event.clear()
            else:
                if research_task_id:
                    current = self._research_jobs().get(research_task_id) or {}
                    self._research_fail(research_task_id, current.get("failure_stage") or "model_execution",
                                        current.get("failure_reason") or "backend returned no report")
                else:
                    self.send_weixin("（暂时连不上模型服务，稍后再问我一次）")
        except Exception as e:
            log(f"⚠️ 回复线程异常: {e}")
            if research_task_id:
                if not self._research_post_archive_failure(research_task_id, str(e)):
                    self._research_fail(research_task_id, "bridge", str(e))
        finally:
            if getattr(self, "_running_task", {}).get("desc") == text:
                self._running_task.update({"active": False, "req_id": None})
            with _active_turns_lock:
                _active_turns.discard(token)
            try:
                self.state["processing"].remove(text)
            except Exception:
                pass
            self.save_state()

    def _task_worker_loop(self):
        """后台任务线程：FIFO 顺序执行；被聊天打断后自动恢复。"""
        while True:
            with self._task_cond:
                while not self.task_q:
                    self._task_cond.wait()
                item = self.task_q.popleft()
            if isinstance(item, dict):
                task_id = item.get("research_task_id")
                text = item.get("text", "")
                if task_id:
                    with self._research_lock:
                        job = self._research_jobs().get(task_id)
                        account = getattr(self, "account", {}) or {}
                        if (not job or job.get("account_id") != (account.get("account_id") or "")
                                or job.get("status") not in {"queued", "archived"}
                                or (job.get("status") == "archived"
                                    and self._research_ts(job.get("next_delivery_ts")) > time.time())):
                            continue
                        job["queue_enqueued"] = False
                        self.save_state()
            else:
                task_id = None
                text = item
            try:
                self._process_task(text, task_id=task_id)
            except Exception as e:
                log(f"⚠️ 任务线程异常: {e}")

    def _process_task(self, text, task_id=None):
        """后台执行复杂任务；聊天打断时保存恢复标记，聊天结束后自动续做。"""
        if not hasattr(self, "_research_lock"):
            self._research_lock = threading.RLock()
        preempt_evt = self._task_preempt
        preempt_ctx = self._running_task   # codex_reply 会把当前 turn 的 req_id 实时写进来
        research_route = _research_request(text)
        if research_route and not task_id:
            task_id = self._create_research_job(text, research_route)
        if research_route and task_id:
            existing = self._research_jobs().get(task_id) or {}
            if existing.get("status") == "archived":
                if self._research_ts(existing.get("next_delivery_ts")) > time.time():
                    return
                outcome = self._deliver_research_report("", text, (), task_id=task_id)
                if outcome.success:
                    self._research_transition(task_id, "delivered", reason="archived report delivered")
                elif outcome.stage == "delivery_claim":
                    return
                elif outcome.stage.startswith("delivery") or outcome.stage == "archive_processing":
                    self._research_delivery_failure(task_id, outcome)
                return
        if research_route and task_id:
            if not self._research_mark_running(task_id):
                return
        resume_sid = None
        if self._resume_task and self._resume_task.get("desc") == text:
            resume_sid = self._resume_task.pop("sid", None)
            self._resume_task = None
        if research_route and task_id:
            job = self._research_jobs().get(task_id) or {}
            resume_sid = resume_sid or job.get("session_id")
        with _active_turns_lock:
            token = uuid.uuid4().hex
            _active_turns.add(token)
        self.state.setdefault("processing", []).append(text)
        self.save_state()
        try:
            identity = BACKEND_IDENTITY.get(self.backend, "Codex")
            mem = _load_memory()
            memory_block = ("【微信桥记忆】\n" + mem + "\n\n") if mem else ""
            knowledge_block = _knowledge_prompt(text) if _should_attach_knowledge(text) else ""
            if resume_sid:
                prompt = ("继续执行刚才被打断的任务，从断点接着做，完成后按协议汇报。任务内容：\n" + text)
                base_prompt = ""
                sid = resume_sid
            else:
                prompt = text
                base_prompt = PRIMER.format(identity=identity)
                sid = None
            if self.backend == "codex":
                prompt = (knowledge_block + memory_block + PERMISSION_POLICY + _source_precedence_block()
                          + SYSTEM_HINT.format(identity=identity) + prompt)
            else:
                prompt = knowledge_block + memory_block + _source_precedence_block() + prompt
            if research_route:
                prompt += _research_instruction(research_route)
            prompt += TASK_PROTOCOL
            # 任务主动汇报：开始/恢复执行时先告诉老板一声
            if resume_sid:
                self.send_weixin(f"🔄 继续处理刚才被打断的任务：{text[:40]}…")
            else:
                self.send_weixin(f"✅ 开始执行任务：{text[:40]}…")
            self._running_task.update({"sid": sid, "desc": text, "active": True, "req_id": None})
            cfg = self.backend_cfg or {}
            task_model = cfg.get("task_model") or TASK_MODEL
            task_effort = cfg.get("task_effort") or TASK_EFFORT
            chat_model = cfg.get("chat_model") or CHAT_MODEL
            chat_effort = cfg.get("chat_effort") or CHAT_EFFORT
            turn_timeout = RESEARCH_HANG_TIMEOUT if research_route else TURN_HANG_TIMEOUT_TASK
            deadline = time.monotonic() + RESEARCH_TOTAL_TIMEOUT if research_route else None
            checkpoint = (lambda event: self._research_checkpoint(
                task_id, stage=event.get("stage"), error=event.get("error"),
                session_id=event.get("session_id"), partial=event.get("partial"))) if task_id else None
            cancel_check = (lambda: self._research_is_cancelled(task_id)) if task_id else None
            on_progress = self.send_weixin
            if task_id:
                def on_progress(progress_text):
                    self._research_checkpoint(task_id, stage="progress", partial=progress_text)
                    self.send_weixin(progress_text)
            reply, new_sid = agent_reply(self.backend, prompt, sid, on_progress=on_progress,
                                         cfg=self.backend_cfg, model=task_model, effort=task_effort,
                                         base=base_prompt or None, timeout=turn_timeout, deadline=deadline,
                                         preempt_evt=preempt_evt, preempt_ctx=preempt_ctx,
                                         checkpoint=checkpoint, cancel_check=cancel_check)
            if (not reply and not preempt_evt.is_set() and not _cancel_event.is_set()
                    and not _backend_cancelled(cancel_check)
                    and (deadline is None or time.monotonic() < deadline)):
                # task 档失败：用 chat 档重试一次（配置不同即降档；相同也是多一次瞬时错误兜底）
                log("↩️ task 档失败，用 chat 档重试一次")
                reply, new_sid = agent_reply(self.backend, prompt, sid, on_progress=on_progress,
                                             cfg=self.backend_cfg, model=chat_model, effort=chat_effort,
                                             base=base_prompt or None, timeout=turn_timeout, deadline=deadline,
                                             preempt_evt=preempt_evt, preempt_ctx=preempt_ctx,
                                             checkpoint=checkpoint, cancel_check=cancel_check)
            if task_id and new_sid:
                self._research_checkpoint(task_id, session_id=new_sid)
            self._running_task.update({"active": False, "sid": new_sid or sid, "req_id": None})
            if preempt_evt.is_set():
                # 被打断：保存恢复标记，等聊天清空后放回队首自动续做
                preempt_evt.clear()
                self._resume_task = {"sid": new_sid or sid, "desc": text}
                log(f"⏸️ 任务被新消息打断，聊天结束后自动恢复: {text[:30]}")
                deadline = time.time() + 180
                while (self.reply_q or self._chat_active > 0) and time.time() < deadline:
                    time.sleep(0.5)
                if task_id:
                    self._research_transition(task_id, "retry_wait", reason="preempted by chat",
                                              next_retry_ts=self._research_now(), queue_enqueued=False)
                    self._research_transition(task_id, "queued", reason="resume after chat", queue_enqueued=False)
                    self._research_queue_item(task_id, front=True)
                else:
                    with self._task_cond:
                        self.task_q.appendleft(text)
                        self._task_cond.notify()
                return
            if reply:
                reply, files = _extract_files(reply)
                outcome = (self._deliver_research_report(reply, text, files, task_id=task_id)
                           if research_route and (reply.strip() or files) else None)
                report_handled = outcome is not None
                report_sent = bool(outcome) if outcome is not None else False
                if task_id and outcome is not None:
                    if outcome.stage == "cancelled":
                        self._research_transition(task_id, "cancelled", reason=outcome.reason,
                                                  cancel_requested=True, next_delivery_ts=None)
                    elif outcome.success:
                        self._research_transition(task_id, "delivered", reason="report delivered",
                                                  session_id=new_sid or sid)
                    elif outcome.stage == "delivery_claim":
                        pass
                    elif outcome.stage.startswith("delivery") or outcome.stage == "archive_processing":
                        # Archive succeeded before a delivery fault: the retry
                        # scheduler only retries delivery and never reruns model work.
                        self._research_delivery_failure(task_id, outcome, new_sid or sid)
                    else:
                        self._research_fail(task_id, outcome.stage or "archive", outcome.reason)
                if not research_route:
                    for fp in files:
                        p = Path(fp).expanduser()
                        if not p.is_absolute():
                            p = Path(CODEX_WORK_DIR) / p
                        archived = _archive_temp_report_file(p, text)
                        if archived:
                            p = archived
                        try:
                            if not self.send_file(str(p)):
                                self.send_weixin(f"文件发送失败：{fp}")
                        except Exception as e:
                            log(f"发送文件异常: {str(e)[:120]}")
                            self.send_weixin(f"文件发送失败：{str(e)[:80]}")
                if reply.strip() and not report_handled:
                    self.send_weixin(reply)
                    self._mark_replied(text)
                elif report_sent and (not outcome or outcome.success):
                    self._mark_replied(text)
                self._record_session(new_sid or sid, text)
                _append_memory(f"- {datetime.now():%Y-%m-%d %H:%M} | 任务：{text[:60]} → 结果：{reply[:80]}")
            else:
                if task_id:
                    current = self._research_jobs().get(task_id) or {}
                    job = self._research_fail(task_id, current.get("failure_stage") or "model_execution",
                                              current.get("failure_reason") or "backend returned no report")
                    if job and job.get("status") == "retry_wait":
                        self.send_weixin(f"调研任务 {task_id} 暂未完成，将按计划重试。")
                else:
                    self.send_weixin("（任务执行失败，稍后我重新试一次）")
        except Exception as e:
            log(f"⚠️ 任务处理异常: {e}")
            if task_id:
                if not self._research_post_archive_failure(task_id, str(e),
                                                          session_id=(self._running_task or {}).get("sid")):
                    self._research_fail(task_id, "bridge", str(e))
        finally:
            if getattr(self, "_running_task", {}).get("desc") == text:
                self._running_task.update({"active": False, "req_id": None})
            with _active_turns_lock:
                _active_turns.discard(token)
            try:
                self.state["processing"].remove(text)
            except Exception:
                pass
            self.save_state()

    def _preempt_task(self):
        """新消息优先：打断当前后台任务（按 reqId 定向中断，任务自动恢复）。仅 codex 后端支持。"""
        if self.backend != "codex":
            return  # claude/generic 无法定向打断：任务继续后台跑，聊天并行处理
        with self._task_cond:
            if not self._running_task.get("active"):
                return
            self._task_preempt.set()
        req = self._running_task.get("req_id")
        try:
            if req:
                _helper_send({"type": "interrupt", "reqId": req})
            else:
                _helper_send({"type": "interrupt"})
        except Exception:
            pass

    def _cancel_research_jobs(self):
        """Make explicit user cancellation durable and suppress future retries."""
        with self._research_lock:
            changed = False
            for job in self._research_jobs().values():
                if job.get("account_id") != (self.account.get("account_id") or ""):
                    continue
                if job.get("status") in {"queued", "running", "retry_wait", "delivering"} or (
                        job.get("status") == "archived" and self._research_delivery_pending(job)):
                    job["cancel_requested"] = True
                    self._research_transition_locked(job, "cancelled", reason="user cancellation",
                                                      next_retry_ts=None, queue_enqueued=False)
                    changed = True
            if changed:
                self.save_state()

    def _cancel_research_job(self, task_id):
        with self._research_lock:
            job = self._research_jobs().get(str(task_id))
            account = getattr(self, "account", {}) or {}
            if not job or job.get("account_id") != (account.get("account_id") or ""):
                return None
            job["cancel_requested"] = True
            if job.get("status") not in {"delivered", "failed", "cancelled"}:
                self._research_transition_locked(job, "cancelled", reason="user cancellation",
                                                  next_retry_ts=None, next_delivery_ts=None,
                                                  queue_enqueued=False)
            self.save_state()
            return job

    def _record_session(self, sid, text):
        if not sid:
            return
        sessions = self.state.setdefault("sessions", [])
        sessions = [s for s in sessions if s.get("sid") != sid]
        sessions.append({"sid": sid, "ts": datetime.now().isoformat(), "desc": text[:40]})
        self.state["sessions"] = sessions[-10:]

    def _post_message(self, item_list, to, ctx, timeout=20):
        self._throttle_send()
        msg = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": f"k3b-{uuid.uuid4().hex[:16]}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": item_list,
        }
        payload = dict(msg)
        if ctx:
            payload["context_token"] = ctx
        try:
            resp = _req(f"{self.account['base_url']}/{EP_SEND_MESSAGE}", {"msg": payload},
                        token=self.account["token"], timeout=timeout)
            errcode = resp.get("errcode", 0)
            if errcode == ERRCODE_SESSION_EXPIRED:
                log("发送时会话已过期(-14)")
                return "expired"
            if errcode not in (0, None):
                log(f"发送返回 errcode={errcode} {str(resp.get('errmsg'))[:80]}")
                return "error"
            return "ok"
        except Exception as e:
            log(f"微信发送失败: {str(e)[:100]}")
            return "error"

    def _throttle_send(self):
        """微信发送限流：窗口内最多 SEND_RATE_LIMIT 条，超出则等待（防服务端限流丢消息）。"""
        wait = 0.0
        with self._send_lock:
            now = time.time()
            cutoff = now - SEND_RATE_WINDOW
            self._send_ts = [t for t in self._send_ts if t > cutoff]
            if len(self._send_ts) >= SEND_RATE_LIMIT:
                wait = self._send_ts[0] + SEND_RATE_WINDOW - now
            if wait <= 0:
                self._send_ts.append(now)
        if wait > 0:
            time.sleep(wait)  # 锁外等待：不阻塞其他发送线程
            with self._send_lock:
                now = time.time()
                self._send_ts = [t for t in self._send_ts if t > now - SEND_RATE_WINDOW]
                self._send_ts.append(now)

    def _send_message(self, part, to, ctx, timeout=20):
        return self._post_message([{"type": ITEM_TEXT, "text_item": {"text": part}}], to, ctx, timeout)

    def _research_send_allowed(self, task_id, *, kind="delivery", generation=None, claim_id=None):
        """Check ownership and the current durable send claim immediately before I/O."""
        if not task_id:
            return True
        with self._research_lock:
            job = self._research_jobs().get(str(task_id))
            account = getattr(self, "account", {}) or {}
            if not job or job.get("account_id") != (account.get("account_id") or ""):
                return False
            if job.get("cancel_requested") or job.get("status") == "cancelled":
                return False
            note = job.get("failure_notification") or {}
            if kind in {"最终失败通知", "failure", "failure_notification"}:
                return (job.get("status") == "failed"
                        and note.get("state") == "sending"
                        and note.get("generation") == generation
                        and note.get("claim_id") == claim_id
                        and not note.get("superseded"))
            # Normal report sends are owned by the delivery claimant.  A hot
            # account switch or explicit cancellation therefore stops before
            # either upload or text-message I/O.
            return job.get("status") == "delivering"

    def _research_notification_part(self, task_id, generation, claim_id, index, state):
        """Persist a failure-notification part state only for its live claim."""
        with self._research_lock:
            job = self._research_jobs().get(str(task_id))
            account = getattr(self, "account", {}) or {}
            note = (job or {}).get("failure_notification") or {}
            if (not job or job.get("account_id") != (account.get("account_id") or "")
                    or job.get("status") != "failed"
                    or note.get("generation") != generation
                    or note.get("claim_id") != claim_id
                    or note.get("state") != "sending"):
                return False
            parts = note.setdefault("parts", [])
            while len(parts) <= index:
                parts.append({"index": len(parts), "state": "pending",
                              "generation": generation, "claim_id": claim_id})
            part = parts[index]
            if (part.get("generation") != generation or part.get("claim_id") != claim_id):
                return False
            part.update({"state": state, "updated_ts": self._research_now()})
            self.save_state()
            return True

    def send_file(self, path, task_id=None):
        """上传并发送文件/图片/视频/语音到微信。成功返回 True。"""
        p = Path(path)
        if not p.exists() or not p.is_file():
            log(f"发送文件不存在: {path}")
            return False
        if task_id and not self._research_send_allowed(task_id):
            log(f"调研文件发送 ownership/状态检查失败: {task_id}")
            return False
        to = self.state["boss_chat"] or self.account.get("user_id", "")
        if not to:
            log("⚠️ 还不知道用户chat_id，无法发文件")
            return False
        ext = p.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            media_type, kind = 1, "image"
        else:
            media_type, kind = 3, "file"  # 视频/语音暂按文件通道发送（已验证的稳妥路径）
        try:
            if task_id and not self._research_send_allowed(task_id):
                return False
            ref = _upload_media(self.account["base_url"], self.account["token"], str(p), media_type, to)
        except Exception as e:
            log(f"文件上传失败: {str(e)[:120]}")
            return False
        ctx = self.state["context_token"]
        aes_b64 = base64.b64encode(ref["aes_key"].encode("utf-8")).decode("utf-8")
        if kind == "image":
            item = {"type": ITEM_IMAGE, "image_item": {"media": {
                "encrypt_query_param": ref["encrypt_query_param"],
                "aes_key": aes_b64, "encrypt_type": 1},
                "mid_size": ref["file_size_encrypted"]}}
        else:
            item = {"type": ITEM_FILE, "file_item": {"file_name": ref["file_name"],
                "file_size": ref["file_size"], "media": {
                "encrypt_query_param": ref["encrypt_query_param"],
                "aes_key": aes_b64, "encrypt_type": 1}}}
        if task_id and not self._research_send_allowed(task_id):
            return False
        r = self._post_message([item], to, ctx, timeout=60)
        if r == "expired":
            log("发文件遇会话过期，去掉 context_token 重试")
            if task_id and not self._research_send_allowed(task_id):
                return False
            r = self._post_message([item], to, None, timeout=60)
        if r != "ok":
            log(f"发文件失败: {p.name} (rc={r})")
            return False
        log(f"📎 已发文件: {p.name} ({kind})")
        return True

    def _send_research_message(self, text, task_id=None, kind="delivery", generation=None, claim_id=None):
        """Send a durable research outbox item without the generic retry queue."""
        if not isinstance(self, Bridge):
            # Unit-test doubles and narrow integrations may provide only the
            # legacy send_weixin method.
            return self.send_weixin(text)
        if task_id and not self._research_send_allowed(task_id, kind=kind,
                                                       generation=generation, claim_id=claim_id):
            return False
        to = self.state["boss_chat"] or self.account.get("user_id", "")
        if not to:
            return False
        ctx = self.state.get("context_token", "")
        ok = True
        for index, part in enumerate(_split_weixin_text(text)):
            if task_id and not self._research_send_allowed(task_id, kind=kind,
                                                           generation=generation, claim_id=claim_id):
                return False
            failure_part = kind in {"最终失败通知", "failure", "failure_notification"}
            if failure_part and not self._research_notification_part(
                    task_id, generation, claim_id, index, "sending"):
                return False
            r = self._send_message(part, to, ctx)
            if r == "expired":
                if task_id and not self._research_send_allowed(task_id, kind=kind,
                                                               generation=generation, claim_id=claim_id):
                    return False
                r = self._send_message(part, to, None)
            if r != "ok":
                ok = False
                if failure_part:
                    self._research_notification_part(task_id, generation, claim_id, index, "pending")
                log(f"调研{kind}发送失败，留在任务 outbox: {task_id or 'untracked'}")
            elif failure_part and not self._research_notification_part(
                    task_id, generation, claim_id, index, "sent"):
                return False
            time.sleep(1)
        return ok

    def send_weixin(self, text, research_task_id=None):
        to = self.state.get("boss_chat", "") or self.account.get("user_id", "")
        ctx = self.state.get("context_token", "")
        if not to:
            log("⚠️ 还不知道用户chat_id，回复暂缓")
            return False
        all_sent = True
        for part in _split_weixin_text(text):
            r = self._send_message(part, to, ctx)
            if r == "expired":
                log("去掉 context_token 重试一次")
                r = self._send_message(part, to, None)
            if r != "ok":
                all_sent = False
                _save_to_retry_queue(to, part, None if r == "expired" else ctx,
                                     research_task_id=research_task_id)
            else:
                log(f"📤 已回微信: {part[:40]}")
            time.sleep(1)
        return all_sent

    def _flush_retry_queue(self):
        if not RETRY_QUEUE_FILE.exists():
            return
        try:
            lines = [l for l in RETRY_QUEUE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:
            return
        if not lines:
            return
        log(f"🔄 排空发送重试队列（{len(lines)} 条）…")
        remaining = []
        for line in lines:
            try:
                entry = json.loads(line)
            except Exception:
                remaining.append(line)
                continue
            part = entry.get("text") or ""
            task_id = entry.get("research_task_id")
            if task_id:
                with self._research_lock:
                    job = self._research_jobs().get(task_id)
                    if job and (job.get("status") in {"delivered", "cancelled"}
                                or job.get("status") in {"queued", "running", "retry_wait", "archived", "delivering"}
                                or (job.get("failure_notification") or {}).get("superseded")):
                        log(f"♻️ 丢弃已被结果覆盖的调研发送重试: {task_id}")
                        continue
            ctx = entry.get("context_token")
            to = entry.get("to") or self.state["boss_chat"] or self.account.get("user_id", "")
            if not part or not to:
                continue
            r = self._send_message(part, to, ctx, timeout=15)
            if r == "expired" and ctx:
                log("补发遇会话过期，去掉 context_token 重试")
                r = self._send_message(part, to, None, timeout=15)
            if r != "ok":
                remaining.append(line)
                continue
            log(f"📤 补发成功: {part[:40]}")
        try:
            if remaining:
                tmp = RETRY_QUEUE_FILE.with_suffix(".tmp")
                tmp.write_text("\n".join(remaining) + "\n", encoding="utf-8")
                os.replace(tmp, RETRY_QUEUE_FILE)
            else:
                RETRY_QUEUE_FILE.unlink(missing_ok=True)
        except Exception as e:
            log(f"重试队列写回失败: {e}")

    # ── 轮询悬挂看门狗：防同步轮询整段卡死 ──
    def watchdog(self):
        while True:
            time.sleep(30)
            if time.time() < self._expiry_pause_until:
                continue
            if time.time() - self._last_poll_ts > HANG_TIMEOUT_S:
                log(f"❌ 轮询看门狗：超过 {HANG_TIMEOUT_S}s 无轮询心跳（疑似悬挂），自杀重启")
                os._exit(1)
            # 常驻连接健康：Codex 后端确保 app-server/助手在跑（自动拉起，不重启整桥）
            if self.backend == "codex":
                try:
                    with _helper_lock:
                        if _helper_proc is None or _helper_proc.poll() is not None:
                            log("🔄 看门狗：常驻助手不在，重新拉起")
                            _helper_ensure()
                        elif not _port_listening(_APP_SERVER_PORT):
                            log("🔄 看门狗：app-server 不在，重新拉起")
                            _helper_ensure()
                except Exception as e:
                    log(f"看门狗助手检查异常: {str(e)[:80]}")

    def _guarded(self, name, fn):
        """线程崩溃写日志（pythonw 无 stderr，崩溃必须落盘）"""
        try:
            fn()
        except Exception as e:
            log(f"❌ 线程 {name} 崩溃: {type(e).__name__}: {e}")
            os._exit(1)

    def run(self):
        if not _acquire_lock():
            return
        log(f"🌉 Codex 微信桥（专线版 v2）启动 (bot={self.account['account_id']})")
        _helper_ensure()  # 预热 Codex 常驻连接（常驻进程）
        t1 = threading.Thread(target=lambda: self._guarded("ilink", self.ilink_loop), daemon=True)
        t2a = threading.Thread(target=lambda: self._guarded("reply", self._reply_worker_loop), daemon=True)
        t2b = threading.Thread(target=lambda: self._guarded("reply", self._reply_worker_loop), daemon=True)
        t2c = threading.Thread(target=lambda: self._guarded("task", self._task_worker_loop), daemon=True)
        t2d = threading.Thread(target=lambda: self._guarded("research-retry", self._research_retry_loop), daemon=True)
        t3 = threading.Thread(target=lambda: self._guarded("watchdog", self.watchdog), daemon=True)
        t4 = threading.Thread(target=lambda: self._guarded("reminder", self._reminder_loop), daemon=True)
        t1.start()
        t2a.start()
        t2b.start()
        t2c.start()
        t2d.start()
        t3.start()
        t4.start()
        while True:
            time.sleep(60)
            if not t1.is_alive() or not t2a.is_alive() or not t2b.is_alive() or not t2c.is_alive() or not t2d.is_alive() or not t4.is_alive():
                log("⚠️ 子线程退出，重启桥进程")
                os._exit(1)


def status():
    if not ACCOUNT_FILE.exists():
        print("未注册。先执行: python wechat_bridge.py register")
        return 1
    a = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    print(f"account_id: {a.get('account_id')}")
    print(f"user_id: {a.get('user_id')}")
    print(f"saved_at: {a.get('saved_at')}")
    print(f"base_url: {a.get('base_url')}")
    print(f"token: {'已配置' if a.get('token') else '缺失'}")
    backend, _ = _load_backend()
    print(f"backend: {backend}")
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        print(f"pending_replies: {len(st.get('pending_replies') or [])}")
        print(f"codex_session: {st.get('codex_session')}")
    q = 0
    if RETRY_QUEUE_FILE.exists():
        q = len([l for l in RETRY_QUEUE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()])
    print(f"retry_queue: {q}")
    if LOCK_FILE.exists():
        print(f"lock_pid: {LOCK_FILE.read_text(encoding='utf-8').strip()}")
    return 0




def doctor():
    """自检环境：python/node/codex/依赖/数据文件/后端配置（不修改状态）。"""
    print("== wechat-channel doctor ==")
    print(f"python: {sys.version.split()[0]}")
    print(f"data_dir: {DATA_DIR}")
    print(f"script: {Path(__file__).resolve()}")
    try:
        import Crypto
        print(f"pycryptodome: OK ({Crypto.__version__})")
    except Exception as e:
        print(f"pycryptodome: MISSING ({e})")
    print(f"node: {CODEX_NODE if os.path.exists(CODEX_NODE) else 'NOT FOUND'}")
    print(f"codex: {CODEX_JS if os.path.exists(CODEX_JS) else 'NOT FOUND'}")
    print(f"claude: {CLAUDE_EXE if os.path.exists(CLAUDE_EXE) else 'not set / not found'}")
    print(f"work_dir: {CODEX_WORK_DIR}")
    print(f"backend_file: {BACKEND_FILE if BACKEND_FILE.exists() else 'MISSING'}")
    backend, _ = _load_backend()
    print(f"backend: {backend}")
    if ACCOUNT_FILE.exists():
        acc = json.loads(ACCOUNT_FILE.read_text(encoding='utf-8'))
        print(f"account: {acc.get('account_id')} saved_at={acc.get('saved_at')}")
    else:
        print("account: NOT REGISTERED (run: python wechat_bridge.py register)")
    if LOCK_FILE.exists():
        print(f"lock_pid: {LOCK_FILE.read_text(encoding='utf-8').strip()}")
    return 0


def env_cmd():
    """打印环境相关关键信息（给排障/日志处理用）。"""
    print("== wechat-channel env ==")
    print(f"PATH: {os.environ.get('PATH', '')}")
    print(f"APPDATA: {os.environ.get('APPDATA', '')}")
    print(f"HOME: {os.path.expanduser('~')}")
    print(f"node: {CODEX_NODE}")
    print(f"codex: {CODEX_JS}")
    print(f"claude: {CLAUDE_EXE}")
    return 0


def main():

    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "register":
        return register()
    if cmd == "status":
        return status()
    if cmd == "doctor":
        return doctor()
    if cmd == "env":
        return env_cmd()
    if not ACCOUNT_FILE.exists():
        print("未注册。先执行: python wechat_bridge.py register")
        return 1
    Bridge().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
