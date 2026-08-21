"""虚拟文件系统沙箱与执行路由（P3）。

安全目标（判断路由：默认可写文件但不可执行，需要执行代码才进沙箱）：
- 默认：**StoreBackend + InMemoryStore** 虚拟文件系统 → 模型可读/可写/可列目录，
  但 `execute` 不可用（StoreBackend 不实现 SandboxBackendProtocol，create_deep_agent
  令 execute 工具返回错误）。这符合“否则用 store backend”。
- 需要执行代码：由 Harness 授权后才接入受控执行后端（见 build_exec_backend）。

技术要点（P3 验证所得）：
- StoreBackend 内部 `awrite/aread` 用 **async** store API（aput/aget），而
  `ls/glob` 用 **sync** store API（search，经 asyncio.to_thread）——必须给定一个
  同时支持 async+sync 的 store。`InMemoryStore` 官方原生支持两者，实测全兼容。
- `AsyncSqliteStore` / 同步 `SqliteStore` 各自只支持一半，文件工具会报错，故不采用。
- InMemoryStore 为内存态：虚拟文件不跨进程持久（进程重启丢失）；**会话消息历史仍由
  SQLite checkpoint 持久化，不受影响**。未来接入 E2B/Docker 只需替换此 backend。
- StoreBackend 文件路径采用**绝对路径**约定（如 `/notes.txt`）。
"""
from app.core.config import settings


def _namespace_factory():
    """StoreBackend 命名空间：per-assistant 虚拟文件系统目录。"""
    def ns(runtime):
        return ("assistant", "filesystem")
    return ns


def build_main_backend():
    """主智能体默认 backend：StoreBackend（虚拟文件系统，可写、可列、无 execute）。"""
    from deepagents.backends import StoreBackend
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()
    return StoreBackend(namespace=_namespace_factory(), store=store)


def build_exec_backend():
    """受控执行后端（默认关闭）。

    说明：deepagents 的 execute 属高风险能力（LocalShellBackend 不做隔离，官方文档明确
    unrestricted）。本机无 Docker/E2B 且出网受限，因此默认不提供可执行沙箱。当 Harness
    对某会话授权执行时再返回受限执行后端（白名单 + root_dir 虚拟根），否则 None。
    """
    return None


def execute_allowed() -> bool:
    """判断路由总开关：是否允许 execute。P3 默认关闭，P8 人工审批后可启用。"""
    return False
