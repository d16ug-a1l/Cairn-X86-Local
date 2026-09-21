#!/usr/bin/env bash
# Cairn 一键管理脚本：启动 / 关停 / 重启 / 状态 / 日志
# 用法: ./cairnctl.sh {start|stop|restart|status|logs}
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/datas/run"
PORT="${CAIRN_PORT:-8000}"
BIND_HOST="${CAIRN_HOST:-0.0.0.0}"
CONFIG="$ROOT/dispatch.yaml"

UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  echo "[x] 未找到 uv，请先安装（pip install uv 或见 https://docs.astral.sh/uv/）" >&2
  exit 1
fi

mkdir -p "$RUN_DIR"

# ---- 工具函数 ----

_tree_pids() {  # $1=pid：输出自身及全部后代 pid
  local pid="$1" child
  echo "$pid"
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    _tree_pids "$child"
  done
}

_is_alive() {  # $1=pidfile：记录的进程是否存活
  local pidfile="$1" pid
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile")"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

_tracked_pids() {  # 所有受本脚本管理的 pid（含进程树后代）
  local f pid
  for f in "$RUN_DIR"/server.pid "$RUN_DIR"/dispatcher.pid; do
    [ -f "$f" ] || continue
    pid="$(cat "$f")"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && _tree_pids "$pid"
  done
}

_foreign_pids() {  # 不受本脚本管理的 cairn 进程
  local tracked p
  tracked=" $( _tracked_pids | tr '\n' ' ' )"
  for p in $(pgrep -f "cairn (serve|dispatch)" 2>/dev/null); do
    case "$tracked" in
      *" $p "*) ;;
      *) echo "$p" ;;
    esac
  done
}

_lan_urls() {
  echo "  - 本机:   http://127.0.0.1:$PORT"
  ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | while read -r ip; do
    echo "  - 局域网: http://$ip:$PORT"
  done
}

_start_one() {  # $1=name $2=pidfile $3=logfile $4...=cmd
  local name="$1" pidfile="$2" logfile="$3"; shift 3
  if _is_alive "$pidfile"; then
    echo "[=] $name 已在运行 (pid $(cat "$pidfile"))，跳过"
    return 0
  fi
  # exec 让子 shell 直接替换为服务进程，stdin/stdout/stderr 全部重定向走，
  # 避免遗留持有管道 fd 的 bash 进程导致调用方无法结束
  ( cd "$ROOT" && exec nohup "$@" >> "$logfile" 2>&1 < /dev/null ) &
  echo $! > "$pidfile"
  echo "[+] $name 已启动 (pid $(cat "$pidfile"))，日志: $logfile"
}

_kill_tree() {  # $1=pid $2=signal：先杀子孙再杀自身
  local pid="$1" sig="$2" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    _kill_tree "$child" "$sig"
  done
  kill "-$sig" "$pid" 2>/dev/null
}

_stop_one() {  # $1=name $2=pidfile
  local name="$1" pidfile="$2" pid tree p alive
  if ! _is_alive "$pidfile"; then
    echo "[=] $name 未在运行"
    rm -f "$pidfile"
    return 0
  fi
  pid="$(cat "$pidfile")"
  # 先给整棵树拍快照（父进程死后子孙会被 reparent，之后再遍历就找不到了）
  tree="$(_tree_pids "$pid")"
  _kill_tree "$pid" TERM
  alive=""
  for _ in $(seq 1 10); do
    alive=""
    for p in $tree; do
      kill -0 "$p" 2>/dev/null && alive="$alive $p"
    done
    [ -z "$alive" ] && break
    sleep 1
  done
  if [ -n "$alive" ]; then
    for p in $alive; do
      kill -9 "$p" 2>/dev/null
    done
    echo "[!] $name 已强制停止 (pid $pid)"
  else
    echo "[-] $name 已停止 (pid $pid)"
  fi
  rm -f "$pidfile"
}

_wait_server() {
  for _ in $(seq 1 30); do
    curl -sf -o /dev/null -m 2 "http://127.0.0.1:$PORT/settings" && return 0
    sleep 1
  done
  return 1
}

# ---- 子命令 ----

cmd_start() {
  echo "== 启动 Cairn =="
  _start_one "Server" "$RUN_DIR/server.pid" "$RUN_DIR/server.log" \
    "$UV" run --project cairn cairn serve --host "$BIND_HOST" --port "$PORT"
  if _wait_server; then
    echo "[✓] Server 就绪，访问地址："
    _lan_urls
  else
    echo "[x] Server 未能在 30 秒内就绪，请查看日志: $RUN_DIR/server.log" >&2
    exit 1
  fi
  if [ ! -f "$CONFIG" ]; then
    echo "[!] 未找到 $CONFIG，跳过 Dispatcher（仅启动了 Server）"
    return 0
  fi
  _start_one "Dispatcher" "$RUN_DIR/dispatcher.pid" "$RUN_DIR/dispatcher.log" \
    "$UV" run --project cairn cairn dispatch --config "$CONFIG"
  echo "[✓] 全部启动完成"
}

cmd_stop() {
  echo "== 关停 Cairn =="
  _stop_one "Dispatcher" "$RUN_DIR/dispatcher.pid"
  _stop_one "Server" "$RUN_DIR/server.pid"
  local foreign
  foreign="$(_foreign_pids || true)"
  if [ -n "$foreign" ]; then
    echo "[!] 发现非本脚本启动的 cairn 进程: $foreign（未处理，可手动 kill）"
  fi
  echo "[✓] 已关停"
}

_status_one() {  # $1=name $2=pidfile
  local name="$1" pidfile="$2" pid
  if _is_alive "$pidfile"; then
    pid="$(cat "$pidfile")"
    printf "  %-11s 运行中 (pid %s, 已运行 %s)\n" "$name" "$pid" "$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
  else
    printf "  %-11s 已停止\n" "$name"
  fi
}

cmd_status() {
  echo "== Cairn 状态 =="
  _status_one "Server" "$RUN_DIR/server.pid"
  _status_one "Dispatcher" "$RUN_DIR/dispatcher.pid"
  if curl -sf -m 2 -o /dev/null "http://127.0.0.1:$PORT/settings"; then
    echo "  HTTP 检查  : 正常 (http://127.0.0.1:$PORT)"
  else
    echo "  HTTP 检查  : 无响应 (http://127.0.0.1:$PORT)"
  fi
  local foreign
  foreign="$(_foreign_pids || true)"
  [ -n "$foreign" ] && echo "  [注意] 存在非本脚本管理的 cairn 进程: $foreign"
  if _is_alive "$RUN_DIR/server.pid"; then
    echo "  访问地址："
    _lan_urls
  fi
  echo "  日志: $RUN_DIR/server.log / $RUN_DIR/dispatcher.log"
}

cmd_logs() {
  echo "== 最近日志 =="
  for f in server dispatcher; do
    if [ -f "$RUN_DIR/$f.log" ]; then
      echo "--- $f.log (最后 15 行) ---"
      tail -15 "$RUN_DIR/$f.log"
    fi
  done
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; echo; cmd_start ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs}"
    echo "  start   启动 Server + Dispatcher（幂等，已在运行则跳过）"
    echo "  stop    关停全部服务"
    echo "  restart 先停后启"
    echo "  status  查看运行状态与访问地址"
    echo "  logs    查看最近日志"
    exit 1
    ;;
esac
