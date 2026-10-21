#!/bin/bash
# 量化系统启动脚本
# 用 setsid + nohup 脱离当前终端/会话进程组，避免关闭终端或工具会话时被一起杀掉。

cd "$(dirname "$0")"
BASE="$(pwd)"
cd backend || exit 1

export PYTHONPATH=""
export PYTHONHOME=""

# 已有实例先停掉（按端口找，不依赖 pid 文件，避免残留 pid 误导）
OLD=$(lsof -nP -iTCP:8000 -sTCP:LISTEN -t 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "停止旧实例 PID=$OLD"
  kill $OLD 2>/dev/null
  sleep 2
fi

# 检查依赖
if ! /usr/bin/python3 -c "import fastapi" 2>/dev/null; then
  echo "正在安装依赖..."
  /usr/bin/python3 -m pip install --user fastapi uvicorn sqlalchemy pydantic akshare pandas numpy python-multipart aiosqlite
fi

mkdir -p "$BASE/logs"
LOG="$BASE/logs/server.log"

echo "启动量化系统..."
# macOS 没有 setsid，用 nohup + disown 让进程脱离当前 shell 的作业表，避免终端关闭被带走
nohup /usr/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 >> "$LOG" 2>&1 &
APP_PID=$!
disown $APP_PID 2>/dev/null || true
echo $APP_PID > "$BASE/logs/server.pid"

# 等端口就绪（最多 30s）
for i in $(seq 1 30); do
  sleep 1
  if lsof -nP -iTCP:8000 -sTCP:LISTEN -t >/dev/null 2>&1; then
    PID=$(lsof -nP -iTCP:8000 -sTCP:LISTEN -t)
    echo "$PID" > "$BASE/logs/server.pid"
    echo "已启动 PID=$PID"
    echo "访问地址: http://localhost:8000"
    echo "日志: $LOG"
    exit 0
  fi
done

echo "启动超时，请查看日志: $LOG"
exit 1
