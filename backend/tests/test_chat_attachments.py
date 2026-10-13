import asyncio
import base64
import io
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from PIL import Image

from app.agent.attachments import prepare_content, supports_vision, save_refusal, REFUSAL
from app.agent.model import ProviderConfig, effective_parameters
from app.schemas.chat import ChatRequest


def request(name, data, message="请分析"):
    return ChatRequest(message=message, attachments=[{"name": name, "data": base64.b64encode(data).decode()}])


class AttachmentTests(unittest.TestCase):
    def test_empty_and_attachment_only(self):
        with self.assertRaises(ValidationError):
            ChatRequest(message=" ")
        req = request("notes.txt", b"hello", "")
        blocks, images = prepare_content(req)
        self.assertFalse(images)
        self.assertIn("hello", blocks[-1]["text"])

    def test_image_and_text_share_message(self):
        data = io.BytesIO()
        Image.new("RGB", (2, 2)).save(data, format="PNG")
        blocks, images = prepare_content(request("photo.png", data.getvalue()))
        self.assertTrue(images)
        self.assertEqual(blocks[0]["text"], "请分析")
        self.assertTrue(blocks[-1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_corrupt_unsupported_and_oversized(self):
        for name, data in [("fake.png", b"not an image"), ("x.exe", b"binary"), ("x.txt", b"x" * (5 * 1024 * 1024 + 1))]:
            with self.subTest(name=name), self.assertRaises(HTTPException):
                prepare_content(request(name, data))
        with self.assertRaises(HTTPException):
            prepare_content(ChatRequest(attachments=[{"name": "x.txt", "data": "!!!"}]))

    def test_capability_and_local_parameter(self):
        self.assertFalse(supports_vision(ProviderConfig(model="deepseek-chat")))
        self.assertTrue(supports_vision(ProviderConfig(model="gpt-4o")))
        self.assertTrue(supports_vision(ProviderConfig(model="custom", parameters={"supports_vision": True})))
        cfg = ProviderConfig(model="gpt-4o", parameters={"supports_vision": False})
        self.assertFalse(supports_vision(cfg))
        self.assertNotIn("supports_vision", effective_parameters(cfg)[0])

    def test_refusal_persisted_without_invoking_model(self):
        agent = AsyncMock()
        with patch("app.agent.runtime.get_agent", AsyncMock(return_value=agent)), patch("app.agent.runtime.invalidate_agents") as invalidate:
            reply = asyncio.run(save_refusal("sid", "[附件：photo.png]", "uid"))
        self.assertEqual(reply, REFUSAL)
        agent.ainvoke.assert_not_called()
        agent.aupdate_state.assert_awaited_once()
        messages = agent.aupdate_state.call_args.args[1]["messages"]
        self.assertEqual(messages[-1].content, REFUSAL)
        invalidate.assert_called_once_with("sid")

    def test_routes_refuse_before_model_call(self):
        from app.api.routes import chat, chat_stream
        data = io.BytesIO()
        Image.new("RGB", (2, 2)).save(data, format="PNG")
        req = request("photo.png", data.getvalue())
        req.session_id = "sid"
        for route in [chat, chat_stream]:
            with self.subTest(route=route.__name__), ExitStack() as stack:
                stack.enter_context(patch.object(route, "owned_session_or_404", return_value=SimpleNamespace(title="existing")))
                stack.enter_context(patch.object(route.runtime, "touch_session"))
                stack.enter_context(patch.object(route.runtime, "session_context", return_value=("uid", "pid")))
                stack.enter_context(patch("app.agent.providers.resolve_provider", return_value=(ProviderConfig(model="deepseek-chat"), "session")))
                stack.enter_context(patch.object(route, "save_refusal", AsyncMock(return_value=REFUSAL)))
                invoke = stack.enter_context(patch.object(route.runtime, "run_chat", AsyncMock()))
                if route is chat_stream:
                    stack.enter_context(patch.object(route.run_manager, "is_running", return_value=False))
                    start = stack.enter_context(patch.object(route.run_manager, "start"))
                    async def collect():
                        response = await route.chat_stream(req, SimpleNamespace(id="uid"), None)
                        return "".join([part async for part in response.body_iterator])
                    output = asyncio.run(collect())
                    self.assertIn(REFUSAL, output)
                    self.assertIn("event: done", output)
                    start.assert_not_called()
                else:
                    response = asyncio.run(route.chat(req, SimpleNamespace(id="uid"), None))
                    self.assertEqual(response.reply, REFUSAL)
                invoke.assert_not_called()

    def test_word_text_extraction(self):
        from docx import Document
        document = Document()
        document.add_paragraph("附件测试内容")
        data = io.BytesIO()
        document.save(data)
        blocks, images = prepare_content(request("report.docx", data.getvalue()))
        self.assertFalse(images)
        self.assertIn("附件测试内容", blocks[-1]["text"])


if __name__ == "__main__":
    unittest.main()
