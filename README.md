# mini-ci:用 Kubernetes 手搓的 CI/CD

[![CI](https://github.com/XinyunZhang5/mini-ci/actions/workflows/ci.yml/badge.svg)](https://github.com/XinyunZhang5/mini-ci/actions/workflows/ci.yml)

![mini-ci 网页:运行列表、步骤依赖图和日志](docs/ui.png)

一次 `git push` → webhook 创建一个 `PipelineRun` 资源 → operator 读取仓库里的 `.ci.yaml`,按依赖关系为每个步骤起一个 Pod(互不依赖的并行),并把进度写回 `PipelineRun.status` → 新版本上线。

```
push ──▶ git-server ─┐
                     │ (scripts/trigger.py 模拟 GitHub 发 webhook)
                     ▼
               ci-webhook ──创建──▶ PipelineRun ◀──写 status──┐
                                        │                      │
                                        └──watch──▶ ci-operator(kopf,读 .ci.yaml)
                                                        │ 每个步骤一个 Pod,共用一个工作区 PVC
                                                        ▼
                                              ┌▶ lint ─┐
                                       clone ─┤        ├▶ smoke ▶ build(Kaniko) ▶ deploy
                                              └▶ test ─┘              │              │
                                                                      ▼              ▼
                                                               kind-registry   apps/demo-app
```

## 用法

前置:Docker 在运行,已装 `kind`、`kubectl`。

```bash
make up                  # 起集群、镜像仓库、git 服务、webhook 和 operator
make demo                # 提交并 push demo-app/,触发流水线并等它跑完
open http://localhost:18080   # 网页:运行列表、依赖图、日志
curl localhost:18090     # 访问部署出来的应用
```

改 `demo-app/app.py` 里的 `MESSAGE` 再跑一次 `push-demo.sh` 就能看到更新发布。
把测试改挂再 push,流水线会停在 test,线上版本不动。

| 地址 | 是什么 |
|---|---|
| `localhost:18080` | 网页 |
| `localhost:18080/runs` | 所有运行记录及状态(`/runs/<run>` 看单个) |
| `localhost:18080/runs/<run>/logs` | 某次运行各步骤的日志(`/logs/<step>` 看单个步骤) |
| `localhost:18090` | 被部署的 demo-app |
| `git://localhost:19418/demo-app.git` | 集群内的 git 服务 |

`make down` 全部拆掉。

## 接到真实的 GitHub 仓库

```bash
gh extension install cli/gh-webhook                # 只需一次
./scripts/github-connect.sh <owner/repo>           # 前台运行,Ctrl-C 停止
```

之后往这个仓库 push,本机集群里就会跑流水线,结果写回 GitHub 的 commit status(显示在 commit 和 PR 上的勾或叉)。

这个脚本做两件事:

- 把 `gh` 当前账号的 token 存进集群(`secret/github-token`)。operator 用它通过 API 读 `.ci.yaml`、回写状态;clone 步骤用它拉私有仓库,用完不会留在工作区里。
- 用 `gh webhook forward` 让 GitHub 把 push 事件转发到本机,不需要公网地址。脚本停掉后转发也就停了。

仓库里要有 `.ci.yaml`、`Dockerfile` 和部署清单;仓库名和 Deployment 名不同时,在 `deploy.rollout` 里写明。

## 测试

```bash
make test    # 单元测试:配置解析和调度逻辑,不需要集群
make e2e     # 端到端测试:对着 make up 拉起的集群跑,约 2 分钟
```

端到端测试覆盖:并行与依赖顺序、失败传播、非法配置、无配置、取消、operator 中途重启、构建并部署、签名校验。
GitHub Actions 里每次 push 都会起一个 kind 集群把两套测试跑一遍。

## 用 kubectl 操作运行

运行记录就是集群里的 `PipelineRun` 资源(简称 `run`):

```bash
alias k='kubectl --context kind-cicd -n ci'
k get runs                    # 列表:仓库、阶段、当前步骤
k get runs -w                 # 实时看进度
k get run <name> -o yaml      # 每个步骤的状态和退出码
k delete run <name>           # 取消:它名下的 Pod 和工作区会被自动回收
```

阶段(`status.phase`):`Pending` → `Running` → `Succeeded` / `Failed` / `Skipped`(commit 里没有 `.ci.yaml`)。
失败原因在 `status.message` 里,`k get runs -o wide` 能直接看到。

## 目录

| 路径 | 内容 |
|---|---|
| `controller/app.py` | webhook:验 HMAC 签名、校验参数、创建 `PipelineRun`、查状态和日志、提供网页 |
| `controller/static/index.html` | 网页,单文件,无依赖 |
| `controller/github.py` | 读 GitHub 上的 `.ci.yaml`、回写 commit status |
| `controller/reconciler.py` | operator(kopf):reconcile 循环,按依赖图建步骤 Pod,把进度写回 status,清理旧运行 |
| `controller/runstatus.py` | 调度核心:由步骤图和各 Pod 状态算出"谁该开始、整体到哪了"的纯函数(`test_runstatus.py` 是它的单元测试) |
| `controller/ciconfig.py` | 取出并校验某个 commit 里的 `.ci.yaml`(`test_ciconfig.py` 是它的单元测试) |
| `controller/pipeline.py` | 把 `.ci.yaml` 翻译成步骤图,以及每个步骤的 Pod 和工作区 PVC |
| `manifests/crd.yaml` | `PipelineRun` 的定义:字段校验、status 子资源、`kubectl get` 的显示列 |
| `manifests/base.yaml` | 命名空间和 RBAC(webhook 只能建 `PipelineRun`,operator 才能建 Pod,只有 deploy 步骤能往 `apps` 发布) |
| `manifests/git-server.yaml` | 集群内 git daemon,代替 GitHub 当代码源 |
| `manifests/controller.yaml` | webhook 和 operator 的 Deployment(共用一个镜像) |
| `cluster/kind-config.yaml` | kind 集群配置和端口映射 |
| `demo-app/` | 被构建和部署的示例应用 |
| `scripts/` | `up.sh` / `push-demo.sh` / `trigger.py` / `github-connect.sh` / `down.sh` |
| `tests/e2e.py` | 端到端测试 |
| `.github/workflows/ci.yml` | CI:单元测试 + kind 上的端到端测试 |

## .ci.yaml

放在仓库根目录。没有这个文件的运行会标成 `Skipped`,写错了标成 `Failed` 并在 `status.message` 里给出具体原因。

```yaml
steps:                      # 必填
  - name: lint              # 没写 needs:clone 完就开始,和其它没写 needs 的步骤并行
    image: python:3.12-alpine
    run: python -m compileall -q .
  - name: test
    image: python:3.12-alpine
    run: python -m unittest discover -v
    env: {KEY: value}       # 可选
  - name: smoke
    needs: [lint, test]     # 等这些步骤都成功了才开始
    image: python:3.12-alpine
    run: python -c "import app"
build:                      # 可选:所有 steps 成功后,用 Kaniko 构建并推送镜像
  dockerfile: Dockerfile    # 默认 Dockerfile
  context: .                # 默认 .
deploy:                     # 可选:build 之后 kubectl apply 并等 rollout 完成
  manifest: deploy.yaml     # 默认 deploy.yaml
  rollout: deployment/xxx   # 默认 deployment/<仓库名>
```

每个步骤在 `/workspace/src`(仓库根目录)下用 `sh -ec` 执行,自动带上 `CI`、`CI_REPO`、`CI_COMMIT_SHA`。
所有步骤共享 `/workspace`,前面步骤产出的文件后面能看到;并行的步骤也在同一个目录里,注意别写同一个文件。

一个步骤失败后,依赖它的步骤标为 `Skipped`,已经在跑的并行步骤会跑完,然后整次运行记为 `Failed`。
`needs` 指向不存在的步骤或形成循环,都会在开始前被拒绝。

## 几个设计点

- **声明式**:webhook 只写"要跑什么"(`spec`),operator 负责让它发生并回报"跑成什么样了"(`status`)。所有状态都在集群里,operator 自己不记东西,所以它停机期间收到的 push 不会丢,运行中途重启也能接上。
- **每个步骤一个 Pod**:这样互不依赖的步骤才能并行。operator 每次 reconcile 都重新看一遍"哪些 Pod 已成功",把依赖刚好满足的步骤建出来;解析后的 `.ci.yaml` 存在 `status.pipeline` 里,不用反复读仓库。
- **Pod 和工作区归 PipelineRun 所有**:带 `ownerReferences`,删掉 `PipelineRun` 就等于取消运行并回收一切。Pod 名是 `<run>-<step>`,重复创建会 409,同一步骤不会跑两遍。
- **历史保留**:每个仓库保留最近 10 次已结束的运行(`RUN_HISTORY_LIMIT`),日志和工作区跟着运行一起保留、一起删除。
- **凭证隔离**:`.ci.yaml` 里的步骤跑的是仓库里写的任意命令,它们的 Pod 不挂任何 ServiceAccount token,只有 deploy 步骤用能发布的身份。
- **镜像仓库有两个名字**:构建 Pod 往 `kind-registry:5000` 推,节点从 `localhost:5001` 拉,指向同一个仓库容器。
- **部署约定**:清单里的 `__IMAGE__` 会被替换成本次构建的镜像。
- **webhook 格式**:和 GitHub 的 push 事件一致(`X-Hub-Signature-256` + `repository.clone_url` + `after`),接真实 GitHub 时只需要把 webhook 暴露到公网。

## 还没做的

- 工作区是 `ReadWriteOnce` 的 PVC,只在单节点集群上成立;多节点要换成 RWX 存储,或改成步骤间用对象存储传产物。
- 没有并发上限,一次能并行多少步骤只受集群资源限制;一个步骤失败不会主动取消正在跑的并行步骤。
- 没有按分支过滤,任何分支的 push 都会跑。
- 回写 commit status 是尽力而为的:写失败只记一条警告,不会重试。
- GitHub 接入是开发用途:靠 `gh webhook forward` 转发,用的是 `gh` 登录账号的 token(权限比实际需要的大)。正式使用应换成 GitHub App 和公网入口。
- 没有构建缓存,没有给步骤注入密钥的办法。
- 网页没有登录,只适合本机访问;日志随运行记录一起清理,没有长期存储。
- 只有 `PipelineRun` 一种资源,没有登记仓库的 `Pipeline` 资源:webhook 信任签名过的请求里带的 `clone_url`(GitHub token 只会发给 github.com 上对应的仓库)。
- operator 是单副本,没有 leader 选举。
- 超时是每个步骤 15 分钟,按墙上时钟算:电脑中途休眠会让正在跑的步骤超时失败。
- git 服务的数据在 `emptyDir` 里,Pod 重启后仓库会变空,重跑一次 `push-demo.sh` 即可。
- git 服务没有鉴权,只适合本机练手。
