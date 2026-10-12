#!/usr/bin/env python3
"""基准测试:同样的 N 个任务,串行跑和并行跑各要多久;以及调度延迟有多大。

每个任务是 `sleep T`,模拟一个耗时固定的构建/测试任务。两种流水线唯一的区别是
.ci.yaml 里有没有 needs:串行版每一步依赖上一步,并行版互不依赖。

  - 总耗时    : 从 webhook 返回到 PipelineRun 结束的墙上时间
  - 调度延迟  : 一个步骤的依赖全部结束,到这个步骤的容器开始运行之间的时间
                (包含 operator 发现、建 Pod、调度、挂卷、起容器)

用法: python3 scripts/bench.py [--jobs 4] [--seconds 10] [--trials 3]
需要先 make up。
"""

import argparse
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import e2e  # noqa: E402  复用推代码、发 webhook、查状态的那几个函数

e2e.REPO = "bench-app"


def config(jobs: int, seconds: int, *, parallel: bool) -> str:
    lines = ["steps:"]
    for i in range(jobs):
        needs = None if parallel or i == 0 else [f"job{i - 1}"]
        lines.append(e2e.step(f"job{i}", f"sleep {seconds}", needs))
    return "\n".join(lines) + "\n"


def ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def run_once(jobs: int, seconds: int, *, parallel: bool) -> tuple[float, list[float]]:
    sha = e2e.push({".ci.yaml": config(jobs, seconds, parallel=parallel)})
    started = time.monotonic()
    name = e2e.post_webhook(sha)["run"]
    deadline = started + 600
    while (run := e2e.get_run(name))["phase"] in ("Pending", "Running"):
        if time.monotonic() > deadline:
            raise SystemExit(f"{name} 超过 10 分钟没结束")
        time.sleep(0.2)
    elapsed = time.monotonic() - started
    if run["phase"] != "Succeeded":
        raise SystemExit(f"{name} 没有成功: {run}")

    # Pod 上的时间戳精度是秒,所以单个延迟有 ±1s 的误差,看中位数
    times = e2e.step_times(name)
    latencies = []
    for step in run["steps"]:
        if step["needs"]:
            ready = max(ts(times[n][1]) for n in step["needs"])
            latencies.append(ts(times[step["name"]][0]) - ready)
    return elapsed, latencies


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()

    results = {}
    latencies: list[float] = []
    try:
        for label, parallel in (("串行", False), ("并行", True)):
            totals = []
            for trial in range(args.trials):
                elapsed, lat = run_once(args.jobs, args.seconds, parallel=parallel)
                totals.append(elapsed)
                latencies += lat
                print(f"  {label} 第 {trial + 1} 次: {elapsed:.1f}s", flush=True)
            results[label] = totals
    finally:
        e2e.kubectl("delete", "pipelineruns", "-l", f"ci/repo={e2e.REPO}", "--wait=false", check=False)

    serial, parallel = (statistics.median(results[k]) for k in ("串行", "并行"))
    print(f"\n{args.jobs} 个任务,每个 sleep {args.seconds}s,各跑 {args.trials} 次(取中位数)")
    print(f"  串行总耗时: {serial:.1f}s")
    print(f"  并行总耗时: {parallel:.1f}s")
    print(f"  加速比:     {serial / parallel:.2f}x")
    print(f"  调度延迟:   中位数 {statistics.median(latencies):.1f}s,最大 {max(latencies):.1f}s({len(latencies)} 个样本)")


if __name__ == "__main__":
    main()
