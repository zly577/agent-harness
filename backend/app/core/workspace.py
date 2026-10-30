"""工作区根目录：按用户隔离。

## 为什么需要单独一个模块

工作区路径原来只有一个入口 `opensandbox_backend.local_workspace_root()`，
它返回的是**一个全局目录** `<repo>/workspace`。 要求按用户隔离之后，
"工作区在哪"这件事被三处独立引用（backend 构建、长期记忆 Agent.md、文件 API），
如果再各自拼一遍路径，很容易出现"某个角落还在用旧路径"的不一致 ——
那种 bug 的表现是"文件写进去了但看不着"，排查成本很高。
默认用户目录统一收在这里，由 `workspace_for(user_id)` 提供；自选工作区则通过
`runtime.session_workspace()` 解析会话绑定的目录。backend、记忆和文件 API 必须
使用同一个已解析的工作区根目录。

## 默认目录按用户隔离，记忆按工作区共享

`<base>/<user_id>/`

未选择项目目录的会话共享上述默认目录。自选工作区的文件和 `Agent.md` 均放在
选定目录内；同一工作区的不同会话共享记忆，切换工作区使用该工作区自己的记忆。
跨会话生效不等于跨工作区共享，也不应每个会话创建一份独立记忆。

## 旧数据搬迁

所有文件都躺在 `<base>/` 根下（含 `Agent.md`）。升级后它们必须
搬进 `<base>/<引导管理员 id>/`，否则用户会看到"工作区空了"。
搬迁见 `migrate_legacy_workspace()`：**先整目录备份、再移动**，可无损回滚。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 未指定用户时使用的子目录名。
#: 只在"没有用户上下文"的场景用到（探针、权限自检、测试辅助），
#: 真实请求一律带 user_id —— 这个目录不是"公共工作区"，别往里放东西。
ANONYMOUS_DIR = "__anonymous__"


def workspace_base() -> Path:
    """所有用户工作区的父目录。

    默认 `<repo>/workspace`（`backend/app/core/workspace.py` → 上溯三层到仓库根），
    可用环境变量 `HARNESS_WORKSPACE` 覆盖（与的语义一致，现在它指的是**父目录**）。
    """
    override = (os.environ.get("HARNESS_WORKSPACE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # app/core/workspace.py → app/core → app → backend → repo
    return Path(__file__).resolve().parents[3] / "workspace"


def workspace_for(user_id: str | None) -> Path:
    """某个用户的工作区根目录（不保证存在）。"""
    uid = (user_id or "").strip() or ANONYMOUS_DIR
    return workspace_base() / uid


def ensure_workspace(user_id: str | None) -> Path:
    """取用户工作区目录并确保存在。"""
    root = workspace_for(user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _backup_dir_name() -> str:
    return f"workspace_backup_{time.strftime('%Y%m%d_%H%M%S')}"


def backup_root() -> Path:
    """搬迁备份的存放目录：**仓库根**下的 `workspace_backups/`。

    ## ⚠️ 为什么绝不能放在 `backend/` 里面

    踩过一次真实的坑：最初放在 `backend/data/workspace_backups/`，而项目是用
    `uvicorn --reload` 起的（watch 目录 = `backend/`）。于是形成死循环：

        启动 → 备份（在 backend/ 里写文件）→ 触发 reload → 再启动 → 再备份 → …

    实测 4 分钟生成了 **41 份备份、8.1MB**，而且后端一直在重启 ——
    表现是"端口 LISTENING 但请求全部失败/500"，看起来像服务挂了。
    放在仓库根（`backend/` 之外）就没有这个问题。

    同理，也**不能**放在 `<base>` 里面：`copytree` 会把目标复制进自己的源目录，直接报错。
    放仓库根需要注意 git：已在 `.gitignore` 里加 `workspace_backups/`
    —— 里面是用户的真实文件，绝不能有被提交的机会。
    """
    return Path(__file__).resolve().parents[3] / "workspace_backups"


def _marker_path() -> Path:
    """迁移完成标记（`<base>/.workspace_layout_v2`）。

    为什么需要它：搬迁的正确判据不是"根目录下还有没有散落条目"，而是
    "这件事做过没有"。只按条目判断的话，只要有一个条目因为"目标已存在"而搬不走
    （例如根下那个不请自来的 `skills/`），每！次！启！动！都会重新整目录备份一遍
    —— 而备份又会触发 --reload，直接变成上面那个死循环。
    """
    return workspace_base() / ".workspace_layout_v2"


def legacy_entries() -> list[Path]:
    """`<base>/` 下那些"属于旧布局"的条目。

    判据：不是目录名形态的用户工作区（32 位 hex）也不是匿名目录，且不是备份目录。
    这样重复执行搬迁不会把已经分好的用户目录再搬一遍。
    """
    base = workspace_base()
    if not base.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(base.iterdir()):
        name = entry.name
        if name in (ANONYMOUS_DIR, ".docker-import-complete") or name.startswith("workspace_backup_"):
            continue
        # 32 位十六进制 = uuid4().hex，正是我们的 user_id / 已搬迁目录
        if entry.is_dir() and len(name) == 32 and all(c in "0123456789abcdef" for c in name):
            continue
        out.append(entry)
    return out


def migrate_legacy_workspace(owner_user_id: str | None) -> dict:
    """把旧布局（`<base>/*`）搬进 `<base>/<owner_user_id>/`。

    ## 为什么先备份再移动

    这是**用户的工作成果**（报告、代码、还有智能体自己攒了半年的 `Agent.md`），
    一旦搬错就是不可逆的损失。所以顺序是：

        1. 整目录复制一份 `<repo>/workspace_backup_<时间戳>/`
        2. 再把条目 `mv` 进 `<base>/<uid>/`
        3. 留下日志说明备份在哪

    回滚方式（无损）：删掉 `<base>/<uid>/`，把备份目录改名回 `workspace` 即可。

    ## 幂等

    第二次执行时 `legacy_entries()` 已经返回空（根下只剩用户目录与备份目录），
    所以是个空操作。这也让它可以安全地挂在每次启动上 —— 升级过渡期里
    如果有老进程又往根下写了文件，下次重启会被顺手搬走。

    ## 幂等（靠标记文件，不靠"还剩没剩条目"）

    见 `_marker_path()` 的说明：完成一次后写标记，之后再启动就是纯空操作。
    这一点是**必须**的 —— 只要有一次不空转，就会形成
    "备份 → 触发 --reload → 再迁移 → 再备份"的死循环。

    Args:
        owner_user_id: 接收旧数据的用户（引导管理员）。为空则只备份不搬。

    Returns:
        `{"moved": [...], "skipped": [...], "backup": "<路径>", "note": "..."}`
    """
    base = workspace_base()
    result: dict = {"moved": [], "skipped": [], "backup": "", "note": ""}

    if not base.exists():
        result["note"] = "工作区目录不存在，无需搬迁"
        return result

    # ---- 0) 已迁移过？直接返回（绝不再复制）----
    marker = _marker_path()
    if marker.exists():
        leftovers = [e.name for e in legacy_entries()]
        result["note"] = "已迁移过（存在标记文件），跳过"
        result["skipped"] = leftovers
        if leftovers:
            logger.info(
                "[workspace] 工作区布局已升级过；根目录仍有历史残留（不影响使用）: %s", leftovers
            )
        return result

    entries = legacy_entries()
    if not entries:
        _write_marker(marker)
        result["note"] = "没有旧布局条目，标记已迁移"
        return result

    if not owner_user_id:
        result["note"] = "没有可用的归属用户，跳过搬迁"
        result["skipped"] = [e.name for e in entries]
        return result

    # ---- 1) 整目录备份（失败就放弃搬迁：宁可让用户看到旧布局，也不能冒丢失风险）----
    backup = backup_root() / _backup_dir_name()
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base, backup, symlinks=True)
        result["backup"] = str(backup)
    except Exception as e:  # noqa: BLE001
        logger.warning("[workspace] 备份失败，取消搬迁（旧文件保持原样）: %s", e)
        result["note"] = f"备份失败，未搬迁：{type(e).__name__}: {e}"
        result["skipped"] = [e_.name for e_ in entries]
        return result

    # ---- 2) 搬进用户目录 ----
    dest = ensure_workspace(owner_user_id)
    for entry in entries:
        target = dest / entry.name
        try:
            if target.exists():
                # 目标已存在（例如上一次搬到一半）：不覆盖，标记出来让人知道
                result["skipped"].append(entry.name)
                logger.warning(
                    "[workspace] %s 在目标目录已存在，保留原位置不覆盖: %s", entry.name, target
                )
                continue
            shutil.move(str(entry), str(target))
            result["moved"].append(entry.name)
        except Exception as e:  # noqa: BLE001 单个条目失败不该中断整体
            result["skipped"].append(entry.name)
            logger.warning("[workspace] 搬迁 %s 失败: %s", entry.name, e)

    if result["moved"]:
        logger.warning(
            "[workspace] 旧工作区已按用户隔离搬迁：%d 个条目 → %s（备份：%s）",
            len(result["moved"]), dest, backup,
        )
    if result["skipped"]:
        logger.warning("[workspace] 以下条目未搬迁（仍留在 %s）: %s", base, result["skipped"])
    # 无论搬成功几个，都写标记：没搬走的那些下次也不会更好（通常是"目标已存在"），
    # 而反复重试的代价是死循环（见 _marker_path 的说明）。
    _write_marker(marker)
    return result


def _write_marker(marker: Path) -> None:
    """写迁移标记；失败只告警（最坏是下次多备份一次，不会丢数据）。"""
    try:
        marker.write_text(
            "工作区已按用户隔离（<base>/<user_id>/）。\n"
            "删除本文件会让下次启动**重新尝试**搬迁旧布局条目。\n",
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("[workspace] 写迁移标记失败（下次启动会重试搬迁）: %s", e)


__all__ = [
    "ANONYMOUS_DIR",
    "backup_root",
    "ensure_workspace",
    "legacy_entries",
    "migrate_legacy_workspace",
    "workspace_base",
    "workspace_for",
]
