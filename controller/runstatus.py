"""调度的核心:看着步骤图和各个 Pod 的实际状态,决定每个步骤现在该是什么状态、
哪些步骤可以开始了、整次运行算不算结束。纯函数,不碰集群。"""

TERMINAL_PHASES = {"Succeeded", "Failed", "Skipped"}
TERMINAL_STATES = {"Succeeded", "Failed", "Skipped"}


def observe(pod: dict) -> dict:
    """一个步骤 Pod 的状态 -> 步骤状态。Pod 只要存在,步骤就算已经开始。"""
    status = pod.get("status", {})
    phase = status.get("phase")
    if phase == "Succeeded":
        return {"state": "Succeeded", "exitCode": 0}
    if phase == "Failed":
        result = {"state": "Failed"}
        for cs in status.get("containerStatuses") or []:
            terminated = (cs.get("state") or {}).get("terminated")
            if terminated:
                result["exitCode"] = terminated.get("exitCode", 1)
        if status.get("reason"):  # 比如 DeadlineExceeded
            result["reason"] = status["reason"]
        return result
    return {"state": "Running"}


def plan(graph: list[tuple[str, list[str]]], pods: dict[str, dict], previous: dict[str, dict]) -> dict:
    """
    graph    : [(步骤名, 依赖的步骤名)],已按依赖排好序
    pods     : 步骤名 -> 这个步骤的 Pod(没有 Pod 的步骤不在里面)
    previous : 步骤名 -> 上次记录在 status 里的步骤状态

    返回要写回 status 的字段,外加 "start":这一轮应该创建 Pod 的步骤名。
    """
    states: dict[str, dict] = {}
    start: list[str] = []
    for name, needs in graph:
        if name in pods:
            current = observe(pods[name])
        elif previous.get(name, {}).get("state") in TERMINAL_STATES:
            # Pod 已经不在了,但结果之前记过,以记录为准,不重跑
            current = {k: v for k, v in previous[name].items() if k in ("state", "exitCode", "reason")}
        elif any(states[n]["state"] in ("Failed", "Skipped") for n in needs):
            current = {"state": "Skipped"}
        elif all(states[n]["state"] == "Succeeded" for n in needs):
            current = {"state": "Running"}
            start.append(name)
        else:
            current = {"state": "Pending"}
        states[name] = current

    steps = [{"name": name, "needs": needs, **states[name]} for name, needs in graph]
    names = lambda state: [s["name"] for s in steps if s["state"] == state]  # noqa: E731
    total, done, failed, running = len(steps), len(names("Succeeded")), names("Failed"), names("Running")

    if all(s["state"] in TERMINAL_STATES for s in steps):
        phase = "Failed" if failed else "Succeeded"
    else:
        phase = "Running"  # 有步骤失败时,已经在跑的并行步骤会让它跑完

    if phase == "Succeeded":
        current_step = f"done ({total}/{total})"
    else:
        current_step = f"{', '.join(running or failed) or '-'} ({done}/{total})"

    result = {"phase": phase, "steps": steps, "currentStep": current_step, "start": start}
    if failed:
        first = states[failed[0]]
        if first.get("reason") == "DeadlineExceeded":
            result["message"] = f"步骤 {failed[0]} 超时"
        else:
            result["message"] = f"步骤 {failed[0]} 失败 (exit {first.get('exitCode', '?')})"
    return result
