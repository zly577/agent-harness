"""Pure message text and tool-log presentation, shared by history readers."""
from app.agent.tool_outcome import classify_tool_result


def extract_text(content) -> str:
    """把 BaseMessage.content（str 或 list）归一为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text", "") or ""))
            else:
                parts.append(str(p))
        return "".join(parts)
    return str(content or "")


def merge_tool_log(history: list[dict], log_rows: list[dict]) -> list[dict]:
    """把持久化工具流水合并进回放历史（原地修改并返回）。

    - 补齐：回放出的工具调用若结果为空（如服务器在工具执行中途被关闭），
      用日志中的结果回填；
    - 兜底：回放完全没有工具痕迹（如 checkpoint 老消息被上下文压缩裁剪），
      用日志重建一条带完整工具调用面板的助手消息。

    同时补齐/带回 `intentLabel` / `intentSource`。**回放必须能确定性还原**，
    所以这里只搬运**已经落库的**标签，绝不重新推导 —— 重新推导会随着
    目录/规则变化而漂移，那就不是"重放同一条事件得到同一个标签"了。

    还要带上 `status`（`interrupted` / `error`）与 `ok`/`exitCode`/`reason`。
    以前回放一律按 `done` 渲染 —— 那一轮被强杀的工具，刷新页面后就"变成成功了"，
    这正是"明明没跑完却显示已完成/进行中"的来源。
    """
    if not log_rows:
        return history
    by_id = {r["id"]: r for r in log_rows if r["id"]}

    def _apply(tc: dict, lr: dict) -> None:
        """把一条流水搬回一个工具条目（toolCalls 与 delegations 共用）。"""
        if not tc.get("result"):
            tc["result"] = lr["result"]
        if not tc.get("intentLabel") and lr.get("intent_label"):
            tc["intentLabel"] = lr["intent_label"]
            tc["intentSource"] = lr.get("intent_source")
        # 流水里的状态比 checkpoint 更权威：checkpoint 只有"调用过"这件事，
        # 而结果是被强杀/中断的，checkpoint 里根本没有痕迹。
        row_status = str(lr.get("status") or "")
        if row_status == "interrupted":
            # ⚠️ 中断**不能**走下面的成败判定：中断时我们会写一句占位说明
            # （"（上一轮运行被中断，未返回结果）"），那是非空文本，
            # 交给分类器会得出"成功" —— 那就等于把"没跑完"伪装成"跑完了"。
            tc["status"] = "interrupted"
            tc["ok"] = None
            tc["exitCode"] = None
            tc["reason"] = "本轮被中断，未收到结果"
            return
        if row_status == "error":
            tc["status"] = "error"
        # 成败判定走同一个纯函数（实时/回放必须一致）
        outcome = classify_tool_result(str(tc.get("name") or ""), tc.get("result"))
        # 老数据里 `status` 一律是 done（不分成败），所以**判定结果说了算**：
        # 只要判定为失败就标 error。否则会出现"结果写着 Error、徽标却说已完成"
        # 这种自相矛盾的卡片 —— 那比不显示更让人困惑。
        if outcome["ok"] is False:
            tc["status"] = "error"
        elif outcome["ok"] is True:
            # 旧分类器会把文件正文中的命令记录误判为失败；回放时纠正徽标。
            tc["status"] = "done"
        tc["ok"] = outcome["ok"]
        tc["exitCode"] = outcome["exit_code"]
        tc["reason"] = outcome["reason"]

    # 1) 按 call_id 补齐缺失的结果与意图标签
    for row in history:
        if row.get("role") != "assistant":
            continue
        for tc in row.get("toolCalls") or []:
            lr = by_id.get(str(tc.get("id", "")))
            if lr:
                _apply(tc, lr)
        for d in row.get("delegations") or []:
            lr = by_id.get(str(d.get("id", "")))
            if lr:
                _apply(d, lr)

    # 2) 兜底重建
    has_tools = any(
        (row.get("toolCalls") or row.get("delegations"))
        for row in history if row.get("role") == "assistant"
    )
    if not has_tools:
        tool_calls = [
            {"id": r["id"], "name": r["name"], "args": r["args"],
             "result": r["result"],
             "status": (r.get("status") if r.get("status") in ("interrupted", "error") else "done"),
             "intentLabel": r.get("intent_label"), "intentSource": r.get("intent_source")}
            for r in log_rows if r["kind"] == "tool"
        ]
        delegations = [
            {"id": r["id"],
             "subagent_type": (r["args"] or {}).get("subagent_type", ""),
             "description": (r["args"] or {}).get("description", ""),
             "result": r["result"],
             "status": (r.get("status") if r.get("status") in ("interrupted", "error") else "done"),
             "intentLabel": r.get("intent_label"), "intentSource": r.get("intent_source")}
            for r in log_rows if r["kind"] == "task"
        ]
        if tool_calls or delegations:
            history.append({
                "role": "assistant", "content": "",
                "toolCalls": tool_calls, "delegations": delegations,
            })
    # 兜底/补齐出来的条目也要有成败判定（否则前端又是清一色"已完成"）
    for row in history:
        if row.get("role") != "assistant":
            continue
        for tc in (row.get("toolCalls") or []) + (row.get("delegations") or []):
            if "ok" not in tc:
                lr = by_id.get(str(tc.get("id", "")))
                if lr:
                    _apply(tc, lr)
                    continue
                outcome = classify_tool_result(str(tc.get("name") or ""), tc.get("result"))
                tc["ok"] = outcome["ok"]
                tc["exitCode"] = outcome["exit_code"]
                tc["reason"] = outcome["reason"]
    return history
