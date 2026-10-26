"""Virtual workspace paths and shell command path translation."""
import re
from typing import Final


def anchored_glob_args(pattern: str, path: str | None) -> tuple[str, str | None]:
    """把「绝对模式 + search path」归一成 `FilesystemBackend.glob` 真正认的组合。

    背景（实测，见 `_probe/p12_glob_probe.py`）：`FilesystemBackend.glob` 的匹配是拿
    **相对 search path** 的路径去比对模式的。所以

        glob('/proj/*.py', path='/proj')   →  []            # 绝对模式 vs 相对比对
        glob('*.py',      path='/proj')   →  ['/proj/existing.py']

    而模型给的是绝对路径。这里在调用前把绝对模式按 search path 折成相对模式
    （模式在 search path 之外时，退化成"以根为锚"解释，等价于 path=None 的行为）。

    Args:
        pattern: 已折掉工具前缀的模式（如 `/proj/*.py`；挂载点已折掉 `/mnt/<名>`）。
        path: 已折掉工具前缀的搜索目录（如 `/proj`），或 None。

    Returns:
        `(pattern, path)`：可直接传给 `FilesystemBackend.glob`。
    """
    if not pattern or not pattern.startswith("/"):
        return pattern, path  # 相对模式：本来就是相对 search path 的

    search = (path or "/").rstrip("/") or "/"
    if search == "/":
        # 以根为锚：去掉开头的 `/` 就是"相对根目录"的模式
        return (pattern.lstrip("/") or "**"), "/"
    if pattern == search or pattern.startswith(search + "/"):
        rel = pattern[len(search):].lstrip("/")
        return (rel or "**"), search
    # 模式指向 search path 之外：按根锚解释（忽略 path，比返回空更符合直觉）
    return (pattern.lstrip("/") or "**"), "/"

# ============================================================================
# 路径映射：虚拟绝对路径  <->  沙箱真实路径
# ============================================================================

# 工具/系统目录不参与映射（否则 `ls /bin` 会被改写成 `/workspace/bin`）
_SYSTEM_ROOTS: Final[frozenset[str]] = frozenset(
    {"/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev", "/var", "/opt"}
)
_PATH_TOKEN: Final = re.compile(r"(?<![\w./-])/[A-Za-z0-9_./\-]*")


def translate_command(command: str, root: str) -> str:
    """把命令里的虚拟绝对路径改写成沙箱真实路径。

    例（root=/workspace）：`python /scripts/a.py` → `python /workspace/scripts/a.py`

    跳过：系统目录（`_SYSTEM_ROOTS`）、`/`、`//`、以及已带 root 前缀的路径（避免二次改写）。
    注：装了 `prefix=/workspace` 之后，模型的路径本来就是容器路径，这个函数通常什么都不改；
    它保留是为了兼容 `prefix=None` 的部署形态。
    """
    root = root.rstrip("/")

    def _sub(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok in ("/", "//") or tok.startswith(root + "/"):
            return tok
        head = "/" + tok.lstrip("/").split("/", 1)[0]
        if head in _SYSTEM_ROOTS:
            return tok
        return root + tok

    return _PATH_TOKEN.sub(_sub, command)


class PathMapper:
    """虚拟路径（模型看到的 `/workspace/a/b.txt`）与沙箱路径（`/workspace/a/b.txt`）互转。

    两种情况：
    - **无工具前缀**（`prefix=None`）：沙箱根 = `settings.sandbox_root`（如 `/workspace`），
      模型的 `/a.txt` 在容器里是 `/workspace/a.txt`。
    - **有工具前缀**（`prefix="/workspace"`）：模型路径本身就带 `/workspace`，容器根仍是
      `/workspace`，所以前缀外的部分是**同一个字符串** —— 模型不需要做路径翻译。
    """

    def __init__(self, root: str, prefix: str | None = None) -> None:
        self.root = "/" + root.strip("/")
        self.prefix = PathMapper._norm(prefix)

    @staticmethod
    def _norm(prefix: str | None) -> str | None:
        if not prefix:
            return None
        norm = "/" + prefix.strip("/")
        return None if norm == "/" else norm

    def to_sandbox(self, virtual_path: str) -> str:
        """虚拟路径 → 沙箱内真实路径（幂等）。"""
        if virtual_path in ("", "/"):
            return self.root
        if virtual_path == self.root or virtual_path.startswith(self.root + "/"):
            return virtual_path  # 已经是容器路径（含带 prefix 的情形）
        if self.prefix and (virtual_path == self.prefix or virtual_path.startswith(self.prefix + "/")):
            # `/workspace/a.txt` → 去掉 prefix 后落到容器根：`/workspace/a.txt`
            return self.root + virtual_path[len(self.prefix):]
        return self.root + "/" + virtual_path.lstrip("/")

    def to_virtual(self, sandbox_path: str) -> str:
        """沙箱内真实路径 → 虚拟路径（带 prefix 的模型视角）。"""
        inner = sandbox_path
        if sandbox_path == self.root:
            inner = "/"
        elif sandbox_path.startswith(self.root + "/"):
            inner = sandbox_path[len(self.root):]
        if self.prefix and inner != "/":
            return self.prefix + inner
        return inner or "/"
