#!/usr/bin/env bash
# 非交互构建成品 agent。自动选执行方式：
#   1) Docker 的 agent-api 容器在跑     → 进容器执行
#   2) 本地原生服务已起(Milvus 可达)+venv → 本地直跑
#   3) 都没起                            → 拉起 Docker 再进容器执行
# 用法:
#   ./build.sh "构建一个能查天气并总结成简报的 agent"
#   ./build.sh "<任务描述>" --thread-id my_run
# 成品统一落宿主机 BuiltAgents/<slug>/。
set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -eq 0 ]; then
  echo "用法: ./build.sh \"任务描述\" [--thread-id <id>]" >&2
  exit 2
fi

MILVUS_PORT="${MILVUS_PORT:-19530}"
LOCAL_PY=".venv/bin/python"
COMPOSE=(docker compose -f Docker/docker-compose.yaml)
SVC=agent-api

_port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
_container_running() { [ -n "$("${COMPOSE[@]}" ps --status running -q "$SVC" 2>/dev/null || true)" ]; }

if _container_running; then
  echo "[build.sh] 走 Docker：进 $SVC 容器执行。" >&2
  exec "${COMPOSE[@]}" exec "$SVC" python build_agent.py "$@"
fi

if [ -x "$LOCAL_PY" ] && _port_open "$MILVUS_PORT"; then
  echo "[build.sh] 走本地：检测到本地服务 (127.0.0.1:$MILVUS_PORT)，本地直跑。" >&2
  exec "$LOCAL_PY" build_agent.py "$@"
fi

echo "[build.sh] 本地服务未起，拉起 Docker 后进容器执行。" >&2
"${COMPOSE[@]}" up -d --build
exec "${COMPOSE[@]}" exec "$SVC" python build_agent.py "$@"
