import asyncio
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.tools import tool
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from app.agent.clarification import ask_user, AskUserInput, ClarificationMiddleware
from app.agent.approvals import read_pending_approval, build_resume_map, known_pending_sessions
from app.agent.runner import SessionRun, RunManager, to_sse_event

class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs): return self

@tool
def change_file() -> str:
    """Change a file requiring approval."""
    return "changed"

def question(parallel=False):
    calls = [{"name":"ask_user", "args":{"question":"哪个文件？", "options":["a","b"]}, "id":"q"}]
    if parallel: calls.append({"name":"change_file", "args":{}, "id":"premature"})
    return AIMessage(content="", tool_calls=calls)

def agent(responses, saver=None, approval=True):
    return create_agent(Model(responses=responses), tools=[ask_user,change_file],
        middleware=[ClarificationMiddleware()]+([HumanInTheLoopMiddleware(interrupt_on={"change_file":True})] if approval else []),
        checkpointer=saver or InMemorySaver())

def test_answer_then_approval():
    async def run():
        g=agent([question(True), AIMessage(content="",tool_calls=[{"name":"change_file","args":{},"id":"change"}]),AIMessage(content="完成")])
        cfg={"configurable":{"thread_id":"ask-approve"}}
        await g.ainvoke({"messages":[{"role":"user","content":"修改"}]},cfg)
        p=await read_pending_approval(g,cfg)
        assert p["actions"] == [] and len(p["clarifications"])==1
        assert "ask-approve" not in known_pending_sessions()
        await g.ainvoke(Command(resume={p["clarifications"][0]["id"]:{"answer":"a"}}),cfg)
        p=await read_pending_approval(g,cfg)
        assert not p["clarifications"] and p["actions"][0]["tool"]=="change_file"
        r=await g.ainvoke(Command(resume=build_resume_map(p["interrupts"],True)),cfg)
        assert r["messages"][-1].content=="完成"
        assert any(isinstance(m,ToolMessage) and m.name=="ask_user" and m.content=="a" for m in r["messages"])
        assert not (await read_pending_approval(g,cfg))["pending"]
    asyncio.run(run())

def test_cancel_stops():
    async def run():
        g=agent([question(),AIMessage(content="",tool_calls=[{"name":"change_file","args":{},"id":"bad"}])],approval=False)
        cfg={"configurable":{"thread_id":"cancel"}}
        await g.ainvoke({"messages":[{"role":"user","content":"修改"}]},cfg)
        p=await read_pending_approval(g,cfg)
        r=await g.ainvoke(Command(resume={p["clarifications"][0]["id"]:{"cancelled":True}}),cfg)
        assert r["messages"][-1].content=="已取消当前任务。"
        assert not any(isinstance(m,ToolMessage) and m.name=="change_file" for m in r["messages"])
    asyncio.run(run())

def test_restart(tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    async def run():
        cfg={"configurable":{"thread_id":"restart"}}
        path=str(tmp_path/'checkpoint.sqlite')
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            g=agent([question()],saver,False)
            await g.ainvoke({"messages":[{"role":"user","content":"修改"}]},cfg)
            before=(await read_pending_approval(g,cfg))["clarifications"]
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            g=agent([AIMessage(content="收到")],saver,False)
            after=(await read_pending_approval(g,cfg))["clarifications"]
            assert before==after
            r=await g.ainvoke(Command(resume={after[0]["id"]:{"answer":"配置a"}}),cfg)
            assert r["messages"][-1].content=="收到"
    asyncio.run(run())

def test_waiting_event():
    manager=RunManager()
    r=SessionRun(session_id="waiting",status="awaiting_input",finished_at=1)
    manager._runs["waiting"]=r
    assert manager.current("waiting") is r and r.suspended
    e=to_sse_event("clarification",{"pending":True,"clarifications":[{"id":"a","question":"哪个？"}]})
    assert e["type"]=="clarification" and e["clarifications"][0]["id"]=="a"

def test_validation():
    from pydantic import ValidationError
    with pytest.raises(ValidationError): AskUserInput(question="  ")
    with pytest.raises(ValidationError): AskUserInput(question="选择",options=["x"*301])
    assert AskUserInput(question="选择",options=["a","a"]).options==["a"]

def test_toggle_preserves_question(monkeypatch):
    from app.api.routes import approvals as route
    async def run():
        g=agent([question()],approval=False)
        cfg={"configurable":{"thread_id":"toggle"}}
        await g.ainvoke({"messages":[{"role":"user","content":"修改"}]},cfg)
        async def get_agent(*args,**kwargs): return g
        monkeypatch.setattr(route.runtime,"get_agent",get_agent)
        monkeypatch.setattr(route.runtime,"session_owner",lambda sid:"user")
        assert await route._resume_pending("toggle") is None
        assert (await read_pending_approval(g,cfg))["clarifications"]
    asyncio.run(run())


def test_mixed_interrupts_resume_separately():
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt
    from typing import TypedDict
    class State(TypedDict, total=False):
        answer: str
        approved: bool
    async def run():
        def clarify(state):
            response=interrupt({"kind":"clarification","question":"哪个文件？","options":[]})
            return {"answer":response["answer"]}
        def approve(state):
            response=interrupt({"action_requests":[{"name":"write_file","args":{}}]})
            return {"approved":response["decisions"][0]["type"]=="approve"}
        graph=StateGraph(State)
        graph.add_node("clarify",clarify);graph.add_node("approve",approve)
        graph.add_edge(START,"clarify");graph.add_edge(START,"approve")
        graph.add_edge("clarify",END);graph.add_edge("approve",END)
        g=graph.compile(checkpointer=InMemorySaver())
        cfg={"configurable":{"thread_id":"mixed"}}
        await g.ainvoke({},cfg)
        p=await read_pending_approval(g,cfg)
        assert len(p["clarifications"])==1 and len(p["interrupts"])==1
        await g.ainvoke(Command(resume=build_resume_map(p["interrupts"],False)),cfg)
        p=await read_pending_approval(g,cfg)
        assert p["clarifications"] and not p["actions"]
        r=await g.ainvoke(Command(resume={p["clarifications"][0]["id"]:{"answer":"a"}}),cfg)
        assert r=={"answer":"a","approved":False}
    asyncio.run(run())


def test_deepagents_integration():
    from deepagents import create_deep_agent
    from app.agent.middleware import build_failure_limit_middlewares
    async def run():
        failure,recovery=build_failure_limit_middlewares()
        g=create_deep_agent(model=Model(responses=[question(),AIMessage(content="收到")]),
            tools=[ask_user],middleware=[ClarificationMiddleware(),*failure],checkpointer=InMemorySaver(),
            interrupt_on={"__hitl_disabled__":False})
        cfg={"configurable":{"thread_id":"deepagents"}}
        await g.ainvoke({"messages":[{"role":"user","content":"修改"}]},cfg)
        p=await read_pending_approval(g,cfg)
        assert p["clarifications"] and not p["actions"]
        r=await g.ainvoke(Command(resume={p["clarifications"][0]["id"]:{"answer":"a"}}),cfg)
        assert r["messages"][-1].content=="收到"
    asyncio.run(run())


@pytest.fixture
def clarification_client(monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from app.api.routes import approvals as route
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from types import SimpleNamespace
    g=SimpleNamespace()
    async def get_agent(*args,**kwargs): return g
    async def pending(*args,**kwargs):
        return {"pending":True,"clarifications":[{"id":"question-id","question":"哪个文件？","options":[]}],
                "actions":[{"tool":"write_file"}],"interrupts":[{"id":"approval-id","actions":[{"tool":"write_file"}]}]}
    manager=SimpleNamespace(running=False,commands=[])
    manager.is_running=lambda sid:manager.running
    def start(sid,command,**kwargs):
        if manager.running: raise RuntimeError("正在处理中")
        manager.running=True;manager.commands.append(command)
        return SimpleNamespace(events=[])
    async def subscribe(*args,**kwargs):
        yield {"type":"result","content":"已继续"}
    manager.start=start;manager.subscribe=subscribe
    def owned(db,sid,user):
        if sid!="owned": raise HTTPException(status_code=404,detail="not found")
    monkeypatch.setattr(route.runtime,"get_agent",get_agent)
    monkeypatch.setattr(route.approvals,"read_pending_approval",pending)
    monkeypatch.setattr(route,"run_manager",manager)
    monkeypatch.setattr(route,"owned_session_or_404",owned)
    app=FastAPI();app.include_router(route.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id="user")
    app.dependency_overrides[get_db]=lambda:None
    with TestClient(app) as client:
        yield client,manager


def test_answer_api_validates_ids_and_ownership(clarification_client):
    client,manager=clarification_client
    for sid,iid,answer,status in [("other","question-id","a",404),("owned","approval-id","a",409),("owned","missing","a",409),("owned","question-id","  ",422)]:
        r=client.post(f"/api/v1/approvals/{sid}/answer",json={"interrupt_id":iid,"answer":answer})
        assert r.status_code==status
    assert not manager.commands


def test_answer_api_and_duplicate(clarification_client):
    client,manager=clarification_client
    r=client.post('/api/v1/approvals/owned/answer',json={"interrupt_id":"question-id","answer":"配置 a"})
    assert r.status_code==200 and '已继续' in r.text
    assert manager.commands[0].resume=={"question-id":{"answer":"配置 a","cancelled":False}}
    r=client.post('/api/v1/approvals/owned/answer',json={"interrupt_id":"question-id","answer":"a"})
    assert r.status_code==409 and len(manager.commands)==1


def test_approval_api_does_not_answer_questions(clarification_client):
    client,manager=clarification_client
    r=client.post('/api/v1/approvals/owned',json={"decision":"approve"})
    assert r.status_code==200
    assert manager.commands[0].resume=={"approval-id":{"decisions":[{"type":"approve"}]}}


def test_manager_rejects_new_message_while_waiting():
    manager=RunManager()
    manager._runs["waiting"]=SessionRun(session_id="waiting",status="awaiting_input")
    with pytest.raises(RuntimeError,match="请先回答"):
        manager.start("waiting",{"messages":[{"role":"user","content":"another task"}]})


def test_cancel_api_preserves_approval(clarification_client):
    client,manager=clarification_client
    r=client.post('/api/v1/approvals/owned/answer',json={"interrupt_id":"question-id","cancelled":True})
    assert r.status_code==200
    assert manager.commands[0].resume=={"question-id":{"answer":"","cancelled":True}}


def test_nested_subagent_cancellation_stops_parent():
    from deepagents import create_deep_agent
    from deepagents import SubAgent
    async def run():
        model=Model(responses=[AIMessage(content="",tool_calls=[{"name":"task","args":{"subagent_type":"worker","description":"询问用户后修改"},"id":"delegate"}]),
                              question(),AIMessage(content="不应该继续")])
        g=create_deep_agent(model=model,tools=[ask_user],middleware=[ClarificationMiddleware()],
            subagents=[SubAgent(name="worker",description="test",system_prompt="ask user",middleware=[ClarificationMiddleware()])],
            checkpointer=InMemorySaver(),interrupt_on={"__hitl_disabled__":False})
        cfg={"configurable":{"thread_id":"nested-cancel"}}
        await g.ainvoke({"messages":[{"role":"user","content":"修改"}]},cfg)
        p=await read_pending_approval(g,cfg)
        assert p["clarifications"]
        r=await g.ainvoke(Command(resume={p["clarifications"][0]["id"]:{"cancelled":True}}),cfg)
        assert r["messages"][-1].content=="已取消当前任务。"
    asyncio.run(run())
