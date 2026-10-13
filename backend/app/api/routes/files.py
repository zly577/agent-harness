"""虚拟文件系统路由：浏览 / 下载 / 删除智能体生成的文件。

文件落在**当前用户**的工作区目录（`<repo>/workspace/<user_id>/`，
模型看到的是 `/workspace/...`），智能体通过 write_file / edit_file 等工具写入。
本路由提供浏览、下载、删除能力。

## 为什么这里没有 session_id

工作区的隔离粒度是**用户**（见 `app/core/workspace.py` 的说明），
所以文件 API 只认"当前登录用户"，不接受任何能指向他人目录的参数 ——
这比"先查 session 归属再放行"更强：越权的输入根本不存在。
同一用户的不同会话共享一份工作区（"我的工作台"），这也是 `Agent.md`
能"跨会话生效"的前提。

## 删除的安全边界（重要）

`backend.delete()` 底层是**递归删除**（目录连内容一起没），而且它对
`delete('/workspace')` 会「返回成功但什么都没做」——所以边界不能指望它兜：

1. **只允许删工作区内的路径**：`norm` 必须严格以 `<WORKSPACE_PREFIX>/` 开头。
   `/mnt/<盘符>/...` 是本机真实磁盘（挂载）——那是用户电脑上的文件，
   面板里根本不该有这种路径，更不能经由这个接口删掉。
2. **不允许删工作区根本身**（以及它的任何写法，如 `/workspace/`）。
3. 路径里的 `..` 由 backend 自己的虚拟路径解析处理；这里再用前缀校验兜一层。

确认动作放在**前端**（用户点删除 → 弹出确认框写明完整路径）——这是用户自己的操作，
不走智能体的 HITL 审批流。
"""
from urllib.parse import quote
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.agent.opensandbox_backend import MACHINE_MOUNT_ROOT
from app.agent.runtime import get_main_backend
from app.agent.sandbox import WORKSPACE_PREFIX
from app.api.deps import get_current_user, owned_session_or_404
from app.db.session import get_db
from sqlalchemy.orm import Session as OrmSession
from app.db.models import User

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _workspace_root(user: User, workspace_id: str | None, db):
    from pathlib import Path
    from app.core.workspace import ensure_workspace
    from app.db.models import Workspace
    if not workspace_id or workspace_id == "legacy":
        return ensure_workspace(user.id)
    workspace = db.get(Workspace, workspace_id)
    if not workspace or workspace.user_id != user.id:
        raise HTTPException(status_code=404, detail="工作区不存在")
    return Path(workspace.directory)


@router.post("/open-workspace")
def open_workspace(user: User = Depends(get_current_user), session_id: str | None = None, workspace_id: str | None = None, db: OrmSession = Depends(get_db)):
    """打开当前用户的根工作区；不接受任意路径或其他用户 id。"""
    from app.core.config import settings
    from app.core.workspace import ensure_workspace

    if not re.fullmatch(r"[0-9a-f]{32}", user.id):
        raise HTTPException(status_code=400, detail="用户工作区标识无效")
    root = _workspace_root(user, workspace_id, db)
    if session_id:
        owned_session_or_404(db, session_id, user)
        from app.agent.runtime import session_workspace
        root = session_workspace(session_id, user.id)
        root.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            os.startfile(str(root))
        except OSError as exc:
            raise HTTPException(status_code=503, detail="当前 Windows 会话无法打开资源管理器") from exc
        return {"opened": True, "open_url": None}
    if settings.workspace_open_protocol:
        suffix = f"/{root.name}" if session_id and root != ensure_workspace(user.id) else ""
        return {"opened": False, "open_url": f"agent-harness-workspace://open/{user.id}{suffix}"}
    raise HTTPException(
        status_code=409,
        detail="此部署没有本机文件夹打开入口。Windows Docker 请通过 start-windows.ps1 启动；远程服务器请使用下载功能。",
    )


def _user_backend(user: User, session_id: str | None = None, db=None, workspace_id: str | None = None):
    """当前用户工作区对应的 backend。

    为什么不用 `get_main_backend()` 的默认参数：那个默认会落到"匿名目录"，
    于是所有人共用一个目录 —— 正是本期要消除的行为。这里显式传 user.id，
    让"文件面板看的是谁的工作区"在代码上一眼可见。
    """
    if session_id:
        owned_session_or_404(db, session_id, user)
    if not session_id and workspace_id and workspace_id != "legacy":
        from app.agent.sandbox import build_main_backend
        return build_main_backend(workspace=_workspace_root(user, workspace_id, db))
    return get_main_backend(session_key=session_id, user_id=user.id)



def _normalize_path(path: str) -> str:
    """规范化虚拟路径：去除尾部斜杠；空/纯斜杠视为根目录 '/'。"""
    path = (path or "").strip()
    if not path or path == "/":
        return "/"
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path 必须是绝对路径（以 / 开头）")
    norm = path.rstrip("/")
    return norm or "/"


def _assert_deletable(norm: str) -> None:
    """删除前的边界检查：只允许删工作区内的**具体条目**。

    刻意要求写全前缀 `/workspace/...`（不像 list/download 那样容忍别名写法）——
    删除是破坏性操作，这里选择"失败得更严"：前缀对不上就拒绝，不靠别名语义兜。

    Raises:
        HTTPException 400：越界（不在工作区内 / 是挂载的本机磁盘）或想删工作区根。
    """
    root = WORKSPACE_PREFIX.rstrip("/") or "/workspace"
    if norm == root:
        raise HTTPException(status_code=400, detail="不能删除工作区根目录")
    if norm.startswith(MACHINE_MOUNT_ROOT + "/"):
        # /mnt/<盘符>/... 是用户电脑上的真实文件（挂载），不该经由这里删除
        raise HTTPException(
            status_code=400,
            detail="这是挂载的本机磁盘（用户电脑上的真实文件），不支持在这里删除",
        )
    if not norm.startswith(root + "/"):
        raise HTTPException(
            status_code=400,
            detail=f"只能删除工作区（{root}/）内的文件；该路径不在工作区内",
        )
    # 再挡一层 ..（正常情况下 backend 的虚拟路径解析已经处理，这里不依赖它）
    parts = [p for p in norm.split("/") if p]
    if ".." in parts:
        raise HTTPException(status_code=400, detail="路径不合法")



@router.get("")
async def list_files(
    path: str = Query("/", description="目录路径，默认根目录"),
    user: User = Depends(get_current_user),
    session_id: str | None = None,
    workspace_id: str | None = None,
    db: OrmSession = Depends(get_db),
):
    """列出**当前用户**工作区中指定目录下的文件和子目录。"""
    norm = _normalize_path(path)
    backend = _user_backend(user, session_id, db, workspace_id)
    try:
        result = backend.ls(norm)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"列出文件失败: {e}")
    return {"path": norm, "entries": result.entries}


def _content_disposition(filename: str) -> str:
    """构造同时兼容 ASCII 回退与中文的 Content-Disposition。

    HTTP 响应头只能包含 latin-1 字符，中文文件名直接写入会抛 UnicodeEncodeError
    （表现为主机层裸 500）。按 RFC 5987 用 filename*=UTF-8''<percent-encoded> 传原始名，
    另留 ASCII filename 作回退。
    """
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


@router.get("/download")
async def download_file(
    path: str = Query(..., description="要下载的文件绝对路径，如 /workspace/report.md"),
    user: User = Depends(get_current_user),
    session_id: str | None = None,
    workspace_id: str | None = None,
    db: OrmSession = Depends(get_db),
):
    """下载**当前用户**工作区中的指定文件，返回原始字节流。"""
    norm = _normalize_path(path)

    backend = _user_backend(user, session_id, db, workspace_id)
    try:
        results = backend.download_files([norm])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载文件失败: {e}")

    if not results:
        raise HTTPException(status_code=404, detail="文件不存在")

    item = results[0]
    if item.error:
        raise HTTPException(status_code=404, detail=f"文件不存在: {item.error}")

    content = item.content
    if content is None:
        raise HTTPException(status_code=404, detail="文件内容为空")

    filename = norm.rsplit("/", 1)[-1] or "download"
    # 简单根据扩展名推断 content-type
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type_map = {
        "md": "text/markdown; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
        "json": "application/json; charset=utf-8",
        "csv": "text/csv; charset=utf-8",
        "html": "text/html; charset=utf-8",
        "py": "text/x-python; charset=utf-8",
        "js": "application/javascript; charset=utf-8",
        "css": "text/css; charset=utf-8",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "pdf": "application/pdf",
    }
    media_type = content_type_map.get(ext, "application/octet-stream")

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(filename),
            "Content-Length": str(len(content)),
        },
    )


@router.delete("")
async def delete_file(
    path: str = Query(..., description="要删除的条目绝对路径，如 /workspace/notes/草稿.md"),
    user: User = Depends(get_current_user),
    session_id: str | None = None,
    workspace_id: str | None = None,
    db: OrmSession = Depends(get_db),
):
    """删除**当前用户**工作区里的一个文件或目录（**目录连同内容一起删，不可恢复**）。

    前端在调用前必须先让用户确认（确认框里写明完整路径）。边界见模块头注释。
    """
    norm = _normalize_path(path)
    _assert_deletable(norm)

    backend = _user_backend(user, session_id, db, workspace_id)
    try:
        result = backend.delete(norm)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")

    err = getattr(result, "error", None)
    if err:
        # 不存在最常见（可能刚被别的地方删掉），当成 404 让前端提示得清楚一点
        raise HTTPException(status_code=404, detail=f"删除失败: {err}")

    return {"ok": True, "path": norm}
