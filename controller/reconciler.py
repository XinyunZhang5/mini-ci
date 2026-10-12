"""mini-ci operator:让集群里的实际状态追上每个 PipelineRun 声明的期望。

  - PipelineRun 被创建 -> 读 .ci.yaml,把解析结果记进 status,建好工作区
  - 之后每次有步骤 Pod 状态变化(外加一个定时兜底)-> reconcile:
        看一眼现在有哪些 Pod、各自什么状态,
        把依赖已经满足的步骤的 Pod 建出来,把进度写回 status

状态全部存在集群里(PipelineRun 和它拥有的 Pod),operator 自己不记任何东西,
所以它随时可以重启:重启后照着集群里的现状再 reconcile 一遍,进度自然接上。
"""

import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone

import kopf
from kubernetes import client, config

import ciconfig
import github
import runstatus
from pipeline import CI_NAMESPACE, GROUP, PLURAL, VERSION, build_graph, build_pod, build_workspace

RUN_LABELS = {"app": "ci-run"}
log = logging.getLogger("mini-ci")
FETCH_RETRIES = 3
RUN_HISTORY_LIMIT = int(os.environ.get("RUN_HISTORY_LIMIT", "10"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "http://localhost:18080")

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()
core = client.CoreV1Api()
custom = client.CustomObjectsApi()

# 同一次运行的 reconcile 排队执行,免得两个 Pod 事件同时到达时互相覆盖 status
_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # kopf 的处理进度只记在注解里,status 完全留给我们自己写
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage()
    settings.posting.level = 30  # 只把 WARNING 以上发成 K8s Event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_run(name: str) -> dict | None:
    try:
        return custom.get_namespaced_custom_object(GROUP, VERSION, CI_NAMESPACE, PLURAL, name)
    except client.ApiException as exc:
        if exc.status == 404:
            return None  # 运行已被删除(取消)
        raise


def _patch_status(name: str, **fields) -> None:
    if fields.get("phase") in runstatus.TERMINAL_PHASES:
        fields.setdefault("finishedAt", _now())
    custom.patch_namespaced_custom_object_status(
        GROUP, VERSION, CI_NAMESPACE, PLURAL, name,
        {"status": fields},  # body 是 dict,客户端会按 merge-patch 发
    )


def _create(call, obj) -> None:
    try:
        call(CI_NAMESPACE, obj)
    except client.ApiException as exc:
        if exc.status != 409:  # 409 = 之前已经建过了
            raise


def _github_repo(spec) -> str | None:
    """这次运行来自 GitHub 且配了 token 时,返回 "owner/repo"。"""
    repo = (spec.get("github") or {}).get("repo")
    return repo if repo and GITHUB_TOKEN else None


def _load_pipeline(spec) -> ciconfig.Pipeline:
    repo = _github_repo(spec)
    if repo:
        return ciconfig.parse(github.fetch_config(repo, spec["sha"], GITHUB_TOKEN))
    return ciconfig.load(spec["cloneUrl"], spec["sha"])


def report(spec, name: str, state: str, description: str, logger=log) -> None:
    """把结果写回 GitHub 的 commit status。尽力而为:写不上不影响流水线本身。"""
    repo = _github_repo(spec)
    if not repo:
        return
    try:
        github.set_status(
            repo, spec["sha"], GITHUB_TOKEN,
            state=state, description=description, target_url=f"{WEB_BASE_URL}/#{name}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"写 commit status 失败: {exc}")


@kopf.on.create(GROUP, VERSION, PLURAL)
def start_run(spec, name, uid, retry, logger, **_):
    try:
        pipeline = _load_pipeline(spec)
    except ciconfig.ConfigNotFound:
        _patch_status(name, phase="Skipped", message=f"commit 里没有 {ciconfig.CONFIG_FILE}")
        prune_history(spec["repo"])
        return
    except ciconfig.ConfigError as exc:
        message = f"{ciconfig.CONFIG_FILE}: {exc}"
        _patch_status(name, phase="Failed", message=message)
        report(spec, name, "failure", message, logger)
        prune_history(spec["repo"])
        return
    except ciconfig.FetchError as exc:
        # 取代码失败可能是暂时的(git 服务还没起来),重试几次再放弃
        if retry + 1 >= FETCH_RETRIES:
            _patch_status(name, phase="Failed", message=str(exc))
            return
        raise kopf.TemporaryError(str(exc), delay=10)

    _create(core.create_namespaced_persistent_volume_claim, build_workspace(run_name=name, run_uid=uid))
    # 把解析好的流水线存进 status:之后的每一轮 reconcile 都照它来,不用再去读仓库
    _patch_status(name, phase="Running", startedAt=_now(), pipeline=ciconfig.to_dict(pipeline))
    report(spec, name, "pending", "Pipeline is running", logger)
    reconcile(name)


@kopf.on.event("pods", labels=RUN_LABELS)
def on_pod_change(event, labels, **_):
    if event["type"] != "DELETED":
        reconcile(labels["ci/run"])


@kopf.timer(GROUP, VERSION, PLURAL, interval=30, initial_delay=30)
def resync(name, status, **_):
    # 兜底:万一漏掉了某个 Pod 事件,最多 30 秒后也会被这里纠正
    if status.get("phase") == "Running":
        reconcile(name)


def reconcile(name: str) -> None:
    with _locks[name]:
        run = _get_run(name)
        status = (run or {}).get("status") or {}
        if status.get("phase") != "Running" or "pipeline" not in status:
            return
        spec, uid = run["spec"], run["metadata"]["uid"]

        nodes = build_graph(
            spec["repo"], spec["cloneUrl"], spec["sha"], ciconfig.from_dict(status["pipeline"]),
            from_github=bool(_github_repo(spec)),
        )
        pods = core.list_namespaced_pod(CI_NAMESPACE, label_selector=f"ci/run={name}").items
        result = runstatus.plan(
            graph=[(n.name, n.needs) for n in nodes],
            pods={p.metadata.labels["ci/step"]: core.api_client.sanitize_for_serialization(p) for p in pods},
            previous={s["name"]: s for s in status.get("steps", [])},
        )

        by_name = {n.name: n for n in nodes}
        for step in result.pop("start"):
            _create(core.create_namespaced_pod, build_pod(by_name[step], run_name=name, run_uid=uid))
        _patch_status(name, **result)

        finished = result["phase"] in runstatus.TERMINAL_PHASES
    if finished:
        _locks.pop(name, None)
        total = len(result["steps"])
        if result["phase"] == "Succeeded":
            report(spec, name, "success", f"All {total} steps passed")
        else:
            report(spec, name, "failure", result.get("message", "Pipeline failed"))
        prune_history(spec["repo"])


def prune_history(repo: str) -> None:
    """每个仓库只留最近 RUN_HISTORY_LIMIT 次已结束的运行。

    已结束运行的 Pod(日志)和工作区会一直留到 PipelineRun 被删为止,
    所以删掉旧的 PipelineRun 就是回收磁盘的方式。
    """
    runs = custom.list_namespaced_custom_object(
        GROUP, VERSION, CI_NAMESPACE, PLURAL, label_selector=f"ci/repo={repo}"
    )["items"]
    finished = [r for r in runs if (r.get("status") or {}).get("phase") in runstatus.TERMINAL_PHASES]
    finished.sort(key=lambda r: r["metadata"]["creationTimestamp"], reverse=True)
    for run in finished[RUN_HISTORY_LIMIT:]:
        try:
            custom.delete_namespaced_custom_object(GROUP, VERSION, CI_NAMESPACE, PLURAL, run["metadata"]["name"])
        except client.ApiException as exc:
            if exc.status != 404:
                raise
