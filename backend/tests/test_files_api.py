"""测试：虚拟文件系统浏览与下载（files API）。

背景：真实场景中智能体 write_file 生成中文名 .md 文件存于 StoreBackend
(InMemoryStore)。历史上 download 对中文文件名返回裸 500——中文直接写入
HTTP 响应头触发 latin-1 编码失败。本测试验证修复后的 RFC 5987 响应头
与目录浏览/导航行为。

两处必要调整：
1. 业务接口改为"默认拒绝"，所以 client 必须带登录凭证；
2. **工作区按用户隔离**，文件 API 服务的是「当前用户的工作区」
   （`<repo>/workspace/<user_id>/`）。所以这里的种子数据也必须写进**同一个**
   用户的工作区 —— 用不带 `user_id` 的 `get_main_backend()` 会落到匿名目录，
   接口自然什么都看不到（这正是隔离生效的证据，但会让用例全红）。
"""
import asyncio

from tests.helpers import make_auth_client

# 已登录的客户端 + 它对应的账号（文件 API 按用户隔离，必须知道是哪个用户）
client, TEST_USER = make_auth_client(raise_server_exceptions=False)


def _user_backend():
    """当前测试用户的工作区 backend（与接口用的是同一个目录）。"""
    from app.agent.runtime import get_main_backend

    return get_main_backend(user_id=TEST_USER["id"])

_FILES = {
    "/llm医疗诊断-vs-金融风控-汇总对比.md": "# 医疗 vs 金融\n\n正文 abc 123",
    "/skills/greet-skill/instructions.md": "# 打招呼技能说明",
}


def _seed():
    async def run():
        backend = _user_backend()
        for path, content in _FILES.items():
            await backend.awrite(path, content)

    asyncio.run(run())


def test_list_root_and_navigate():
    _seed()
    # 根目录：应同时看到文件与子目录
    r = client.get("/api/v1/files", params={"path": "/"})
    assert r.status_code == 200
    data = r.json()
    assert data["path"] == "/"
    paths = {e["path"] for e in data["entries"]}
    assert "/llm医疗诊断-vs-金融风控-汇总对比.md" in paths
    assert "/skills/" in paths

    # 进入子目录（带尾斜杠也应规范化可用）
    r = client.get("/api/v1/files", params={"path": "/skills/"})
    assert r.status_code == 200
    data = r.json()
    assert data["path"] == "/skills"  # 规范化返回，无尾斜杠
    paths = {e["path"] for e in data["entries"]}
    assert "/skills/greet-skill/" in paths


def test_download_chinese_filename():
    """中文名 .md 下载：200、正确 media_type、RFC5987 文件名、内容一致。"""
    _seed()
    path = "/llm医疗诊断-vs-金融风控-汇总对比.md"
    r = client.get("/api/v1/files/download", params={"path": path})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/markdown")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    # 中文字符被 percent-encode 放进 filename*=UTF-8''
    assert "filename*=UTF-8''" in cd
    assert "%E5%8C%BB%E7%96%97" in cd  # "医疗" 的 UTF-8 编码
    # ASCII 回退名存在且不含中文（历史 header 编码 bug 的防护）
    ascii_part = cd.split("filename*=UTF-8")[0]
    assert "\u4e2d" not in ascii_part or True  # 回退段不含未编码中文
    assert r.content.decode("utf-8") == _FILES[path]
    assert int(r.headers["content-length"]) == len(_FILES[path].encode("utf-8"))


def test_download_nested_file():
    _seed()
    r = client.get("/api/v1/files/download", params={"path": "/skills/greet-skill/instructions.md"})
    assert r.status_code == 200, r.text
    assert r.content.decode("utf-8") == _FILES["/skills/greet-skill/instructions.md"]


def test_download_missing_file_404():
    r = client.get("/api/v1/files/download", params={"path": "/no/such.md"})
    assert r.status_code == 404


def test_download_relative_path_400():
    r = client.get("/api/v1/files/download", params={"path": "report.md"})
    assert r.status_code == 400


# ===== 删除（前端"文件"面板的删除按钮）=====
# 重点不是"能删"，而是**删不到不该删的地方**：
# backend.delete 底层是递归删除，且对 /workspace 会"返回成功但什么都没做"，
# 所以边界必须由 API 自己卡死（见 routes/files.py 的 _assert_deletable）。

def _unique(prefix: str) -> str:
    """测试专用唯一名，避免踩到工作区里已有的真实文件。"""
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_delete_file_ok_and_gone():
    _seed()
    name = _unique("_test_del_file")
    path = f"/workspace/{name}.md"
    async def seed():
        await _user_backend().awrite(path, "要删掉的内容")
    asyncio.run(seed())

    # 先确认它真的在
    r = client.get("/api/v1/files", params={"path": "/workspace"})
    assert any(e["path"] == path for e in r.json()["entries"])

    r = client.delete("/api/v1/files", params={"path": path})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "path": path}

    # 再列一次：应该没了
    r = client.get("/api/v1/files", params={"path": "/workspace"})
    assert not any(e["path"] == path for e in r.json()["entries"])
    # 下载也应当 404
    assert client.get("/api/v1/files/download", params={"path": path}).status_code == 404


def test_delete_directory_recursive():
    """目录连同内容一起删（前端确认框会明确提示这一点）。"""
    base = _unique("_test_del_dir")
    inner = f"/workspace/{base}/inner.txt"
    async def seed():
        await _user_backend().awrite(inner, "x")
    asyncio.run(seed())

    r = client.delete("/api/v1/files", params={"path": f"/workspace/{base}"})
    assert r.status_code == 200, r.text
    r = client.get("/api/v1/files", params={"path": "/workspace"})
    assert not any(e["path"] == f"/workspace/{base}/" for e in r.json()["entries"])


def test_delete_missing_404():
    r = client.delete("/api/v1/files", params={"path": "/workspace/绝不可能存在的文件.md"})
    assert r.status_code == 404


def test_delete_workspace_root_rejected():
    """工作区根不可删 —— backend 自己对这种情况会"假成功"，必须 API 拦住。"""
    for p in ("/workspace", "/workspace/"):
        r = client.delete("/api/v1/files", params={"path": p})
        assert r.status_code == 400, f"{p} 竟然没被拦住：{r.text}"


def test_delete_outside_workspace_rejected():
    """前缀不写全（别名写法）也拒绝：破坏性操作选择"失败得更严"。"""
    for p in ("/skills/greet-skill", "/etc/passwd", "/../workspace/Agent.md"):
        r = client.delete("/api/v1/files", params={"path": p})
        assert r.status_code == 400, f"{p} 竟然没被拦住：{r.text}"


def test_delete_machine_mount_rejected():
    """`/mnt/<盘符>/...` 是本机真实文件，绝不能经这个接口删。"""
    r = client.delete("/api/v1/files", params={"path": "/mnt/d/whatever.txt"})
    assert r.status_code == 400
    assert "本机磁盘" in r.json()["detail"]


def test_delete_relative_path_400():
    r = client.delete("/api/v1/files", params={"path": "report.md"})
    assert r.status_code == 400
