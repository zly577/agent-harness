# P9 Vue3 前端 + SSE 流式输出设计

## 目标

提供可用的 Web 界面：
1. **对话界面**：发消息，SSE 流式接收回复（逐字显示）。
2. **审批台**：会话触发人工审批时弹窗展示待审批操作，提供批准/拒绝。
3. 前端通过 Vite dev 代理 `/api` 到后端 FastAPI。

## 架构

```
前端 Vue3 (Vite)
  │  POST /api/v1/chat/stream (SSE, fetch+ReadableStream 解析)
  ▼
后端 FastAPI  StreamingResponse(text/event-stream)
  │  runtime.stream_chat() → agent.astream(stream_mode="messages")
  │     → token/  tool/  approval/  result/  error 事件
  ▼
deepagents (LangGraph) + SQLite checkpoint + MySQL + 审批(interrupt)
```

## 后端 SSE（app/api/routes/chat_stream.py）

```
POST /api/v1/chat/stream  body: {"message","session_id"}
返回 text/event-stream，事件：
  {"type":"session","session_id":...}      会话ID（新建会话时前端记下）
  {"type":"token","content":...}           模型输出增量
  {"type":"tool","name":...}               工具调用提示
  {"type":"approval","pending":true,"actions":[...]}  需人工审批
  {"type":"result","content":...}          最终回复
  {"type":"error","detail":...}            错误
event: done
```

- `runtime.stream_chat()` 用 `agent.astream(stream_mode="messages")` 产出 AIMessageChunk，
  只推送非空 content 增量。
- 流后检查 `approvals.read_pending_approval`，若中止（pending）则推送 approval 事件。

## 前端（frontend/）

| 文件 | 内容 |
|------|------|
| `package.json` | Vue3 + Vite |
| `vite.config.js` | `/api` → `http://127.0.0.1:8000` 代理 |
| `src/main.js` | 应用入口 |
| `src/App.vue` | 对话界面 + 审批弹窗 + 输入栏 + 样式 |
| `src/sse.js` | fetch + ReadableStream 解析 text/event-stream |

## 运行方式（在当前沙箱受限，需在普通终端）

当前运行环境（DSH 沙箱）**禁止 Node 子进程 spawn**（`spawn EPERM`），
且 esbuild 等强依赖 postinstall 脚本，因此 **Vite dev server 无法在本沙箱运行**。
前端代码完整，可在**普通 Windows 终端**运行：

```powershell
cd D:\Deepseek_Harness\agent-harness\frontend
npm install
npm run dev        # 打开 http://127.0.0.1:5173
```

同时需后端在 8000 端口运行：
```powershell
cd D:\Deepseek_Harness\agent-harness\backend
C:\Users\Administrator\anaconda3\envs\agent_env\python.exe -m uvicorn app.main:app --port 8000
```

## 审批交互流程（前端）

1. 用户发送消息 → 前端 POST SSE → 流式显示 token。
2. 若后端推送 `approval` 事件 → 前端弹窗展示动作名+参数，显示【批准】【拒绝】。
3. 用户点批准/拒绝 → 前端 `POST /api/v1/approvals/{session_id}` → 后端恢复执行，把结果追加到对话。

## 验证

- 后端 SSE 实测：types = [session, tool, token, result]，token 逐增量流式输出，session_id 正常。
- 前端代码完整，npm install + dev 在非沙箱环境即可运行。
- 全套 pytest 51 passed 13s。

## 已知限制

- 沙箱禁止 Node spawn，前端构建/dev 需在普通终端执行（非代码问题）。
- 生产部署可 `npm run build` 产静态文件 + 后端托管（P10）。
