"""本机 PDF 读取 MCP server（stdio transport）。

## 为什么要有它

魔塔上那个 `pdf-reader-mcp` 是**远端**服务 —— 它跑在别人的机器上，`path` 参数指的是
**那台机器**的工作目录，所以永远读不到你本机的 PDF（跟提示词怎么写没关系）。

stdio 类型的 MCP server 完全不同：它由 Agent Harness 后端**在本机拉起一个子进程**，
子进程的权限就是你的用户权限 ⇒ 它能直接 `open("D:/.../x.pdf")`。

## 注册到 Agent Harness（前端「工具源」→ 新增）

- 名称：`local-pdf`（工具会以这个名字做前缀）
- 类型：`Stdio（本机子进程）`
- 启动命令：`C:\\Users\\Administrator\\anaconda3\\envs\\agent_env\\python.exe`
- 参数（每行一个）：
    `D:\\Deepseek_Harness\\agent-harness\\backend\\scripts\\local_pdf_mcp_server.py`
- 环境变量 / 工作目录：可留空

## ⚠️ 自建 stdio server 的第一大坑

**stdout 是 JSON-RPC 通道，不能有任何非协议输出。** 一句 `print("hello")` 就会让客户端
从 stdout 读到一行不是 JSON 的内容 → 解析失败 → 整个 server 被认为挂了。
所有日志必须走 **stderr**（`print(..., file=sys.stderr)`，或 logging 挂 `StreamHandler(sys.stderr)`）。
下面 `logging.basicConfig(stream=sys.stderr)` 就是干这个的。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# 日志一律走 stderr（stdout 留给协议）
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[local-pdf] %(levelname)s %(message)s",
)
logger = logging.getLogger("local-pdf")

#: 单个 PDF 最多回多少字符（防止把模型的上下文撑爆）
MAX_CHARS = 20000

mcp = FastMCP("local-pdf")

#: `/mnt/d/foo/bar.pdf` → 盘符 + 剩余路径
_MNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


def to_host_path(raw: str) -> Path:
    """把模型给的路径翻成本机真实路径。

    模型被系统提示词教成用 `/mnt/d/...` 表示 D 盘（见
    `app/agent/prompt.py::machine_mount_prompt_section`），但本 server 跑在**本机**，
    需要的是 `D:\\...`。这里两种写法都收：

        /mnt/d/docs/a.pdf   →   D:/docs/a.pdf
        D:\\docs\\a.pdf      →   D:/docs/a.pdf（原样）
    """
    p = (raw or "").strip().strip('"').strip("'")
    m = _MNT_RE.match(p)
    if m:
        return Path(f"{m.group(1).upper()}:/{m.group(2) or ''}")
    return Path(p)


def _parse_pages(spec: str, total: int) -> list[int]:
    """把 `"1"` / `"1-3"` / `"2,4,7"` 解析成页号列表（1-based，越界丢弃）。"""
    spec = (spec or "").strip()
    if not spec:
        return list(range(1, total + 1))

    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo_raw, _, hi_raw = part.partition("-")
            try:
                lo, hi = int(lo_raw), int(hi_raw)
            except ValueError:
                continue
            out.extend(i for i in range(lo, hi + 1) if 1 <= i <= total)
        elif part.isdigit():
            i = int(part)
            if 1 <= i <= total:
                out.append(i)
    # 都没解析出来就退回读全文，而不是回一个空结果让模型困惑
    return out or list(range(1, total + 1))


@mcp.tool()
def read_pdf(path: str, pages: str = "") -> str:
    """读取本机 PDF 文件里的文字内容。

    Args:
        path: PDF 的路径。`/mnt/d/docs/a.pdf` 和 `D:\\docs\\a.pdf` 两种写法都支持。
        pages: 只读指定页，如 `1`、`1-3`、`2,4,7`。留空读全部。
    """
    target = to_host_path(path)
    logger.info("read_pdf path=%s pages=%r", target, pages)

    if not target.exists():
        return (
            f"Error: 文件不存在：{target}"
            f"（收到的是 {path!r}；如果是相对路径，请给出绝对路径）"
        )
    if target.is_dir():
        return f"Error: {target} 是一个目录，不是 PDF 文件。"
    if target.suffix.lower() != ".pdf":
        return f"Error: {target} 不是 PDF 文件（后缀是 {target.suffix!r}）。"

    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return (
            "Error: 本机缺少 PDF 解析库。请在**本机**执行："
            "pip install pypdf（或 PyPDF2），然后重试。"
        )

    try:
        reader = PdfReader(str(target))
        total = len(reader.pages)
        wanted = _parse_pages(pages, total)

        chunks: list[str] = []
        for page_no in wanted:
            text = (reader.pages[page_no - 1].extract_text() or "").strip()
            chunks.append(f"--- 第 {page_no} 页 ---\n{text}")
        body = "\n\n".join(chunks)
    except Exception as e:  # noqa: BLE001 工具失败要变成可读文本，不能抛出去
        return f"Error: 解析 PDF 失败（{type(e).__name__}）：{e}"

    header = f"{target}（共 {total} 页，本次读取 {len(wanted)} 页）"
    if not body.replace("-", "").strip():
        return (
            f"{header}\n\n没有可提取的文字层 —— 这个 PDF 很可能是**扫描件/图片版**，"
            "需要先 OCR 才能取到文字。"
        )
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + f"\n\n…（已截断，完整内容约 {len(body)} 字符）"
    return f"{header}\n\n{body}"


def main() -> None:
    logger.info("启动本机 PDF MCP server（stdio transport）")
    mcp.run()  # transport 默认就是 stdio


if __name__ == "__main__":
    main()
