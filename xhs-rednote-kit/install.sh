#!/usr/bin/env bash
# 把 skills/ 下的技能装到客户端目录
#   ./install.sh                    -> ~/.codex/skills
#   ./install.sh ~/.claude/skills   -> Claude Code
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:-$HOME/.codex/skills}"

mkdir -p "$target"
cp -r "$here"/skills/* "$target"/

echo "skills -> $target"
echo "下一步：重启客户端会话，然后让助手调用 check_login_status 验证连接。"
