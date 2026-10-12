#!/usr/bin/env bash
# 一键拉起:本地镜像仓库 + kind 集群 + git 服务 + CI 控制面(webhook + operator)。可重复执行。
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=cicd
REG_NAME=kind-registry
REG_PORT=5001
KCTL="kubectl --context kind-${CLUSTER}"

# 1. 本地镜像仓库
if [ "$(docker inspect -f '{{.State.Running}}' "$REG_NAME" 2>/dev/null || true)" != "true" ]; then
  docker rm -f "$REG_NAME" >/dev/null 2>&1 || true
  docker run -d --restart=always -p "127.0.0.1:${REG_PORT}:5000" --name "$REG_NAME" registry:2 >/dev/null
fi

# 2. kind 集群
if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config cluster/kind-config.yaml
fi

# 3. 让节点把 localhost:5001 解析到仓库容器,并把仓库接进 kind 网络
REG_DIR="/etc/containerd/certs.d/localhost:${REG_PORT}"
for node in $(kind get nodes --name "$CLUSTER"); do
  docker exec "$node" mkdir -p "$REG_DIR"
  printf '[host."http://%s:5000"]\n' "$REG_NAME" | docker exec -i "$node" cp /dev/stdin "$REG_DIR/hosts.toml"
done
if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REG_NAME")" = "null" ]; then
  docker network connect kind "$REG_NAME"
fi

# 4. 构建并推送控制面镜像(webhook 和 operator 共用)
docker build -q -t "localhost:${REG_PORT}/ci-controller:latest" controller
docker push -q "localhost:${REG_PORT}/ci-controller:latest"

# 5. 部署
$KCTL apply -f manifests/crd.yaml
$KCTL wait --for=condition=Established crd/pipelineruns.mini-ci.dev --timeout=60s >/dev/null
$KCTL apply -f manifests/base.yaml
if ! $KCTL -n ci get secret webhook-secret >/dev/null 2>&1; then
  $KCTL -n ci create secret generic webhook-secret --from-literal=secret="$(openssl rand -hex 20)"
fi
$KCTL apply -f manifests/git-server.yaml -f manifests/controller.yaml
$KCTL -n ci rollout restart deployment/ci-webhook deployment/ci-operator >/dev/null
$KCTL -n ci rollout status deployment/git-server --timeout=180s
$KCTL -n ci rollout status deployment/ci-webhook --timeout=180s
$KCTL -n ci rollout status deployment/ci-operator --timeout=180s
# Pod Ready 之后,NodePort 的转发规则还要一小会儿才生效,等本机真能连上再算就绪
for _ in $(seq 1 30); do
  curl -sf -o /dev/null -m 2 http://localhost:18080/healthz && break
  sleep 1
done

cat <<EOF

就绪:
  webhook     http://localhost:18080   (GET /runs 看运行记录)
  运行记录    kubectl --context kind-cicd -n ci get pipelineruns
  git 服务    git://localhost:19418/demo-app.git
  demo-app    http://localhost:18090   (第一次流水线跑完后才有)

下一步: ./scripts/push-demo.sh
EOF
