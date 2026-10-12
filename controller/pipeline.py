"""把一个 PipelineRun 和它的 .ci.yaml 翻译成一张步骤图,以及每个步骤对应的 Pod。

每个步骤是一个独立的 Pod,所以互不依赖的步骤可以同时跑。
所有 Pod 挂同一个 PVC 当工作区(/workspace),clone 出来的代码和各步骤的产出都在里面。
"""

import os
from dataclasses import dataclass

from kubernetes import client

from ciconfig import Pipeline

GROUP, VERSION, PLURAL = "mini-ci.dev", "v1alpha1", "pipelineruns"

CI_NAMESPACE = os.environ.get("CI_NAMESPACE", "ci")
DEPLOY_NAMESPACE = os.environ.get("DEPLOY_NAMESPACE", "apps")
PUSH_REGISTRY = os.environ.get("PUSH_REGISTRY", "kind-registry:5000")
PULL_REGISTRY = os.environ.get("PULL_REGISTRY", "localhost:5001")
WORKSPACE_SIZE = os.environ.get("WORKSPACE_SIZE", "1Gi")
STEP_TIMEOUT_SECONDS = 900

SRC = "/workspace/src"
WORKSPACE = client.V1VolumeMount(name="workspace", mount_path="/workspace")


# token 只通过命令行的 -c 传给这一次 clone,不会写进工作区的 .git/config,
# 后面跑用户命令的步骤在工作区里找不到它
CLONE_SCRIPT = f"""\
if [ -n "${{GIT_TOKEN:-}}" ]; then
  auth=$(printf 'x-access-token:%s' "$GIT_TOKEN" | base64 | tr -d '\\n')
  git -c "http.extraHeader=Authorization: Basic $auth" clone "$CLONE_URL" {SRC}
else
  git clone "$CLONE_URL" {SRC}
fi
git -C {SRC} checkout --detach "$SHA"
"""

# 只有 GitHub 来的运行、只有 clone 这一步会拿到 token(私有仓库要用)
GITHUB_TOKEN_ENV = [
    client.V1EnvVar(
        name="GIT_TOKEN",
        value_from=client.V1EnvVarSource(
            secret_key_ref=client.V1SecretKeySelector(name="github-token", key="token", optional=True)
        ),
    )
]


@dataclass
class Node:
    """步骤图里的一个节点。"""

    name: str
    needs: list[str]
    container: client.V1Container
    can_deploy: bool = False  # 只有 deploy 步骤拿得到能发布的身份


def _env(**values: str) -> list[client.V1EnvVar]:
    return [client.V1EnvVar(name=k, value=v) for k, v in values.items()]


def build_graph(
    repo: str, clone_url: str, sha: str, pipeline: Pipeline, *, from_github: bool = False
) -> list[Node]:
    """返回按依赖排好序的节点:clone -> 用户步骤 -> build(可选) -> deploy(可选)。"""
    tag = sha[:12]
    push_image = f"{PUSH_REGISTRY}/{repo}:{tag}"
    pull_image = f"{PULL_REGISTRY}/{repo}:{tag}"
    user_steps = [s.name for s in pipeline.steps]

    # 外部传进来的值一律走环境变量,不拼进 shell 字符串
    nodes = [
        Node(
            name="clone",
            needs=[],
            container=client.V1Container(
                name="step",
                image="alpine/git:2.45.2",
                command=["sh", "-ec"],
                args=[CLONE_SCRIPT],
                env=_env(CLONE_URL=clone_url, SHA=sha) + (GITHUB_TOKEN_ENV if from_github else []),
            ),
        )
    ]

    for step in pipeline.steps:
        nodes.append(
            Node(
                name=step.name,
                needs=step.needs or ["clone"],
                container=client.V1Container(
                    name="step",
                    image=step.image,
                    working_dir=SRC,
                    command=["sh", "-ec", step.run],
                    env=_env(CI="true", CI_REPO=repo, CI_COMMIT_SHA=sha, **step.env),
                ),
            )
        )

    if pipeline.build:
        nodes.append(
            Node(
                name="build",
                needs=user_steps,
                container=client.V1Container(
                    name="step",
                    image="gcr.io/kaniko-project/executor:v1.23.2",
                    args=[
                        f"--context=dir://{SRC}/{pipeline.build.context}",
                        f"--dockerfile={SRC}/{pipeline.build.dockerfile}",
                        f"--destination={push_image}",
                        "--insecure",  # 本地仓库走 http
                    ],
                ),
            )
        )

    if pipeline.deploy:
        nodes.append(
            Node(
                name="deploy",
                needs=["build"] if pipeline.build else user_steps,
                can_deploy=True,
                container=client.V1Container(
                    name="step",
                    image="alpine/k8s:1.31.4",
                    working_dir=SRC,
                    command=["sh", "-ec"],
                    args=[
                        # 把清单里的镜像占位符换成这次构建出的镜像
                        'sed "s|__IMAGE__|$IMAGE|g" "$MANIFEST" > /workspace/rendered.yaml\n'
                        'kubectl apply -n "$NS" -f /workspace/rendered.yaml\n'
                        'kubectl rollout status -n "$NS" "$ROLLOUT" --timeout=120s\n'
                    ],
                    env=_env(
                        NS=DEPLOY_NAMESPACE,
                        MANIFEST=pipeline.deploy.manifest,
                        ROLLOUT=pipeline.deploy.rollout or f"deployment/{repo}",
                        IMAGE=pull_image if pipeline.build else "",
                    ),
                ),
            )
        )
    return nodes


def _owner(run_name: str, run_uid: str) -> list[client.V1OwnerReference]:
    # 归 PipelineRun 所有:删掉 PipelineRun,它名下的 Pod 和 PVC 会被自动回收(=取消运行)
    return [
        client.V1OwnerReference(
            api_version=f"{GROUP}/{VERSION}",
            kind="PipelineRun",
            name=run_name,
            uid=run_uid,
            controller=True,
        )
    ]


def pod_name(run_name: str, step: str) -> str:
    return f"{run_name}-{step}"


def build_pod(node: Node, *, run_name: str, run_uid: str) -> client.V1Pod:
    node.container.volume_mounts = [WORKSPACE]
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            # 名字是确定的:重复创建会 409,同一个步骤不会跑出两个 Pod
            name=pod_name(run_name, node.name),
            namespace=CI_NAMESPACE,
            labels={"app": "ci-run", "ci/run": run_name, "ci/step": node.name},
            owner_references=_owner(run_name, run_uid),
        ),
        spec=client.V1PodSpec(
            restart_policy="Never",  # 步骤失败就是失败,不自动重试
            active_deadline_seconds=STEP_TIMEOUT_SECONDS,
            # 用户步骤跑的是仓库里写的任意命令,不给它们任何集群凭证
            service_account_name="ci-runner" if node.can_deploy else "default",
            automount_service_account_token=node.can_deploy,
            containers=[node.container],
            volumes=[
                client.V1Volume(
                    name="workspace",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=run_name),
                )
            ],
        ),
    )


def build_workspace(*, run_name: str, run_uid: str) -> client.V1PersistentVolumeClaim:
    return client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=run_name,
            namespace=CI_NAMESPACE,
            labels={"app": "ci-run", "ci/run": run_name},
            owner_references=_owner(run_name, run_uid),
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1VolumeResourceRequirements(requests={"storage": WORKSPACE_SIZE}),
        ),
    )
