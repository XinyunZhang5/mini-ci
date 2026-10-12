"""读取并校验仓库里的 .ci.yaml。

格式:

    steps:                      # 必填。没写 needs 的步骤在 clone 之后并行开始
      - name: test
        image: python:3.12-alpine
        run: python -m unittest discover -v
        env: {KEY: value}       # 可选
        needs: [lint]           # 可选:等这些步骤都成功了才开始
    build:                      # 可选:所有 steps 成功后,用 Kaniko 构建并推送镜像
      dockerfile: Dockerfile    # 默认 Dockerfile
      context: .                # 默认 .
    deploy:                     # 可选:build 之后 kubectl apply 并等 rollout 完成
      manifest: deploy.yaml     # 默认 deploy.yaml,其中 __IMAGE__ 换成本次构建的镜像
      rollout: deployment/xxx   # 默认 deployment/<仓库名>
"""

import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field

import yaml

CONFIG_FILE = ".ci.yaml"
MAX_STEPS = 20
RESERVED_NAMES = {"clone", "build", "deploy"}

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
ROLLOUT_RE = re.compile(r"^(deployment|statefulset|daemonset)/[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class ConfigError(ValueError):
    """.ci.yaml 写得不对,错误信息会原样返回给调用方。"""


class ConfigNotFound(Exception):
    """这个 commit 里没有 .ci.yaml。"""


class FetchError(Exception):
    """取不到代码(仓库连不上、超时、commit 不存在)。可能是暂时的,调用方可以重试。"""


@dataclass
class Step:
    name: str
    image: str
    run: str
    env: dict[str, str] = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)


@dataclass
class Build:
    dockerfile: str = "Dockerfile"
    context: str = "."


@dataclass
class Deploy:
    manifest: str = "deploy.yaml"
    rollout: str | None = None


@dataclass
class Pipeline:
    steps: list[Step]
    build: Build | None = None
    deploy: Deploy | None = None


def _mapping(value, where: str, allowed: set[str]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} 必须是一个映射")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"{where} 里有不认识的字段: {', '.join(sorted(map(str, unknown)))}")
    return value


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} 必须是非空字符串")
    return value


def _needs(value, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise ConfigError(f"{where} 必须是步骤名的列表")
    if len(set(value)) != len(value):
        raise ConfigError(f"{where} 里有重复的步骤名")
    return list(value)


def _repo_path(value, where: str) -> str:
    path = _text(value, where)
    if not PATH_RE.match(path) or path.startswith("/") or ".." in path.split("/"):
        raise ConfigError(f"{where} 必须是仓库内的相对路径: {path!r}")
    return path


def _topo_sort(steps: list[Step]) -> list[Step]:
    """按依赖排好序(被依赖的在前,其余保持书写顺序);有环就报错。"""
    by_name = {s.name: s for s in steps}
    for step in steps:
        for need in step.needs:
            if need == step.name:
                raise ConfigError(f"步骤 {step.name!r} 不能依赖自己")
            if need not in by_name:
                raise ConfigError(f"步骤 {step.name!r} 的 needs 里有不存在的步骤: {need!r}")
    ordered: list[Step] = []
    placed: set[str] = set()
    remaining = list(steps)
    while remaining:
        ready = [s for s in remaining if set(s.needs) <= placed]
        if not ready:
            raise ConfigError("needs 形成了循环依赖: " + ", ".join(s.name for s in remaining))
        ordered += ready
        placed |= {s.name for s in ready}
        remaining = [s for s in remaining if s.name not in placed]
    return ordered


def parse(text: str) -> Pipeline:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败: {exc}") from exc
    return from_dict(raw)


def to_dict(pipeline: Pipeline) -> dict:
    """可以存进 PipelineRun.status、再用 from_dict 原样读回来的形式。"""
    return {k: v for k, v in asdict(pipeline).items() if v is not None}


def from_dict(raw) -> Pipeline:
    raw = _mapping(raw, CONFIG_FILE, {"steps", "build", "deploy"})

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ConfigError("steps 必须是非空列表")
    if len(raw_steps) > MAX_STEPS:
        raise ConfigError(f"steps 最多 {MAX_STEPS} 个")

    steps: list[Step] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_steps):
        where = f"steps[{i}]"
        item = _mapping(item, where, {"name", "image", "run", "env", "needs"})
        name = _text(item.get("name"), f"{where}.name")
        if not NAME_RE.match(name):
            raise ConfigError(f"{where}.name 只能用小写字母、数字和连字符: {name!r}")
        if name in RESERVED_NAMES:
            raise ConfigError(f"{where}.name 不能用保留名 {name!r}")
        if name in seen:
            raise ConfigError(f"步骤名重复: {name!r}")
        seen.add(name)

        env = _mapping(item.get("env"), f"{where}.env", set(item.get("env") or {}))
        for key in env:
            if not isinstance(key, str) or not ENV_RE.match(key):
                raise ConfigError(f"{where}.env 的变量名不合法: {key!r}")
        steps.append(
            Step(
                name=name,
                image=_text(item.get("image"), f"{where}.image"),
                run=_text(item.get("run"), f"{where}.run"),
                env={k: str(v) for k, v in env.items()},
                needs=_needs(item.get("needs"), f"{where}.needs"),
            )
        )
    steps = _topo_sort(steps)

    build = None
    if "build" in raw:
        b = _mapping(raw["build"], "build", {"dockerfile", "context"})
        build = Build(
            dockerfile=_repo_path(b.get("dockerfile", "Dockerfile"), "build.dockerfile"),
            context=_repo_path(b.get("context", "."), "build.context"),
        )

    deploy = None
    if "deploy" in raw:
        d = _mapping(raw["deploy"], "deploy", {"manifest", "rollout"})
        rollout = d.get("rollout")
        if rollout is not None and not ROLLOUT_RE.match(_text(rollout, "deploy.rollout")):
            raise ConfigError(f"deploy.rollout 应形如 deployment/<name>: {rollout!r}")
        deploy = Deploy(
            manifest=_repo_path(d.get("manifest", "deploy.yaml"), "deploy.manifest"),
            rollout=rollout,
        )

    return Pipeline(steps=steps, build=build, deploy=deploy)


def fetch(clone_url: str, sha: str) -> str:
    """取出某个 commit 里的 .ci.yaml 原文。

    这里直接 clone 一份裸仓库再 git show,够本地用。
    接真实 GitHub 时应换成 contents API,不用拉整个仓库。
    """
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--bare", "--", clone_url, tmp],
                check=True, capture_output=True, text=True, timeout=120,
            )
        except subprocess.CalledProcessError as exc:
            raise FetchError(f"clone 失败: {exc.stderr.strip()}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FetchError("clone 超时") from exc

        show = subprocess.run(
            ["git", "--git-dir", tmp, "show", f"{sha}:{CONFIG_FILE}"],
            capture_output=True, text=True, timeout=30,
        )
        if show.returncode != 0:
            exists = subprocess.run(
                ["git", "--git-dir", tmp, "cat-file", "-e", f"{sha}^{{commit}}"],
                capture_output=True, timeout=30,
            )
            if exists.returncode != 0:
                raise FetchError(f"仓库里找不到 commit {sha}")
            raise ConfigNotFound()
        return show.stdout


def load(clone_url: str, sha: str) -> Pipeline:
    return parse(fetch(clone_url, sha))
