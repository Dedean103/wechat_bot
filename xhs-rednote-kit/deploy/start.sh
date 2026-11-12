#!/usr/bin/env bash
set -euo pipefail

base_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_dir="$(cd "$base_dir/.." && pwd)"
cd "$workspace_dir"

export XHS_BACKEND="${XHS_BACKEND:-rednote}"

if curl -sS -o /dev/null --max-time 2 http://127.0.0.1:18060/mcp; then
  echo "xiaohongshu-mcp is already running at http://127.0.0.1:18060/mcp"
  exit 0
fi

nohup "$base_dir/bin/xiaohongshu-mcp" >> "$base_dir/server.log" 2>&1 &
echo $! > "$base_dir/server.pid"
echo "Started xiaohongshu-mcp (PID $!)"
echo "Log: $base_dir/server.log"
