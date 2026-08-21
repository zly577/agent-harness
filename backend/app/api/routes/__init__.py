"""API 路由聚合。"""
from fastapi import APIRouter

from app.api.routes import approvals, chat, chat_stream, skills

api_router = APIRouter()
api_router.include_router(chat.router)
api_router.include_router(chat_stream.router)
api_router.include_router(skills.router)
api_router.include_router(approvals.router)
