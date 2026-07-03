"""
Agent Chatroom Server — 本地 Agent + Human 聊天室
FastAPI + SQLite + SSE
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import aiosqlite
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional

# ─── Config ──────────────────────────────────────────────────
EVENT_ID = "chatroom-001"
PORT = int(os.environ.get("PORT", 8765))
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "chatroom.db")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# ─── App ─────────────────────────────────────────────────────
app = FastAPI(title="Agent Chatroom", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── SSE Manager ─────────────────────────────────────────────
class SSEManager:
    """Simple pub/sub for SSE connections."""
    def __init__(self):
        self._queues: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, event_id: str) -> asyncio.Queue:
        q = asyncio.Queue()
        self._queues.setdefault(event_id, []).append(q)
        return q

    def unsubscribe(self, event_id: str, q: asyncio.Queue):
        if event_id in self._queues:
            self._queues[event_id] = [x for x in self._queues[event_id] if x is not q]

    async def publish(self, event_id: str, data: dict):
        for q in self._queues.get(event_id, []):
            await q.put(data)

sse = SSEManager()

# ─── DB Helpers ──────────────────────────────────────────────
async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS participants (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            name TEXT NOT NULL,
            bio TEXT DEFAULT '',
            avatar TEXT DEFAULT '🤖',
            agent_name TEXT NOT NULL,
            interests TEXT DEFAULT '[]',
            looking_for TEXT DEFAULT '',
            socials TEXT DEFAULT '{}',
            sender_type TEXT DEFAULT 'agent',
            api_token TEXT UNIQUE NOT NULL,
            last_heartbeat TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            avatar TEXT DEFAULT '🤖',
            text TEXT NOT NULL,
            type TEXT DEFAULT 'chat',
            sender_type TEXT DEFAULT 'agent',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (participant_id) REFERENCES participants(id)
        );
        CREATE INDEX IF NOT EXISTS idx_msg_event_time ON messages(event_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_participant_token ON participants(api_token);
    """)
    # 兼容旧数据库：如果 last_heartbeat 列不存在则添加
    try:
        await db.execute("ALTER TABLE participants ADD COLUMN last_heartbeat TEXT")
    except:
        pass
    await db.commit()
    await db.close()

# ─── Models ──────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    name: str
    bio: str = ""
    avatar: str = "🤖"
    agent_name: str
    interests: list[str] = []
    looking_for: str = ""
    socials: dict = {}
    sender_type: str = "agent"  # "agent" | "human"

class ChatMessage(BaseModel):
    text: str
    type: str = "chat"  # intro|chat|roast|question|hype
    mentions: Optional[list[str]] = None

# ─── Auth Helper ─────────────────────────────────────────────
async def get_participant(request: Request):
    """Extract and verify Bearer token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要 Authorization: Bearer <token>")
    token = auth.removeprefix("Bearer ").strip()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM participants WHERE api_token = ?", (token,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="无效 token")
        return dict(row)
    finally:
        await db.close()

# ─── Routes ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    """Web UI for human users."""
    html_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return HTMLResponse("<h1>Agent Chatroom</h1><p>index.html not found</p>")

@app.get("/skill.md")
async def skill_md(request: Request):
    """Agent 接入指南（动态生成，适配 localhost 和公网隧道）."""
    return _serve_skill(request)

@app.get("/api/skill")
async def skill_api(request: Request):
    """Agent 接入指南 — JSON API 版（绕过 ngrok 拦截页，供云端 Agent 使用）."""
    return _serve_skill(request)


def _serve_skill(request: Request):
    host = request.headers.get("host", f"localhost:{PORT}")
    scheme = request.url.scheme
    base = f"{scheme}://{host}"
    api_base = f"{base}/api"
    skill_path = os.path.join(os.path.dirname(__file__), "skill.md")
    with open(skill_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("http://localhost:8765/api", api_base)
    content = content.replace("http://localhost:8765", base)
    return HTMLResponse(content, media_type="text/markdown; charset=utf-8")

@app.post("/api/events/{event_id}/register")
async def register(event_id: str, body: RegisterRequest, request: Request):
    eid = event_id or EVENT_ID
    host = request.headers.get("host", f"localhost:{PORT}")
    scheme = request.url.scheme
    base = f"{scheme}://{host}"

    db = await get_db()
    try:
        # 防重复：agent 和人类都按名字去重，复用已有 token
        cursor = await db.execute(
            "SELECT id, api_token FROM participants WHERE event_id=? AND agent_name=? AND sender_type=? LIMIT 1",
            (eid, body.agent_name, body.sender_type),
        )
        existing = await cursor.fetchone()
        if existing:
            return {
                    "participant_id": existing["id"],
                    "api_token": existing["api_token"],
                    "event": {"id": eid, "title": "Agent 咖啡馆", "chat_ui": f"{base}/"},
                    "message": f"✅ {body.name} 已在聊天室（复用已有身份）",
                }
        await db.close()
    except:
        await db.close()

    pid = str(uuid.uuid4())
    prefix = "human__" if body.sender_type == "human" else "agent__"
    token = prefix + str(uuid.uuid4()).replace("-", "")[:24]

    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO participants (id, event_id, name, bio, avatar, agent_name,
               interests, looking_for, socials, sender_type, api_token)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, eid, body.name, body.bio, body.avatar, body.agent_name,
             json.dumps(body.interests), body.looking_for, json.dumps(body.socials),
             body.sender_type, token),
        )
        await db.commit()
    finally:
        await db.close()

    # 返回 token，agent 自己按 skill.md 发入场消息
    await sse.publish(eid, {"type": "participant_joined", "participant": {
        "agent_name": body.agent_name, "avatar": body.avatar or "🤖",
        "name": body.name, "sender_type": body.sender_type
    }})

    return {
        "participant_id": pid,
        "api_token": token,
        "event": {"id": eid, "title": "Agent 咖啡馆", "chat_ui": f"{base}/"},
        "message": f"✅ {body.name} 已加入聊天室",
    }


@app.get("/api/events/{event_id}/live-chat")
async def read_chat(event_id: str, limit: int = Query(30, ge=1, le=100)):
    eid = event_id or EVENT_ID
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM messages WHERE event_id = ? ORDER BY created_at DESC LIMIT ?",
            (eid, limit),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    messages = []
    for r in reversed(rows):
        d = dict(r)
        d["message_id"] = d.pop("id")
        messages.append(d)
    return {"event_id": eid, "messages": messages, "count": len(messages)}


@app.post("/api/events/{event_id}/live-chat")
async def send_chat(event_id: str, body: ChatMessage, request: Request):
    participant = await get_participant(request)
    eid = event_id or EVENT_ID

    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO messages (id, event_id, participant_id, agent_name,
               avatar, text, type, sender_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, eid, participant["id"], participant["agent_name"],
             participant["avatar"], body.text, body.type, participant["sender_type"], now),
        )
        await db.commit()
    finally:
        await db.close()

    msg_obj = {
        "message_id": msg_id,
        "agent_name": participant["agent_name"],
        "avatar": participant["avatar"],
        "text": body.text,
        "type": body.type,
        "sender_type": participant["sender_type"],
        "created_at": now,
    }
    await sse.publish(eid, {"type": "new_message", "message": msg_obj})

    return msg_obj


@app.get("/api/events/{event_id}/lobby")
async def lobby(event_id: str):
    eid = event_id or EVENT_ID
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, agent_name, avatar, bio, interests, looking_for, sender_type, last_heartbeat "
            "FROM participants WHERE event_id = ? ORDER BY created_at",
            (eid,),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    participants = []
    for r in rows:
        d = dict(r)
        d["interests"] = json.loads(d.get("interests", "[]"))
        participants.append(d)
    return {"event_id": eid, "participants": participants}


@app.get("/api/events/{event_id}/heartbeat")
async def heartbeat(event_id: str, request: Request):
    participant = await get_participant(request)
    eid = event_id or EVENT_ID
    agent_name = participant["agent_name"]

    # 记录心跳时间
    db = await get_db()
    try:
        await db.execute(
            "UPDATE participants SET last_heartbeat = datetime('now') WHERE id = ?",
            (participant["id"],),
        )
        await db.commit()
    finally:
        await db.close()

    # 检查是否有 @自己的未回复消息
    db = await get_db()
    try:
        # 找最近消息中提到该 agent 的
        cursor = await db.execute(
            """SELECT agent_name, text, created_at FROM messages
               WHERE event_id=? AND text LIKE ?
               ORDER BY created_at DESC LIMIT 1""",
            (eid, f"%@{agent_name}%"),
        )
        mentioned = await cursor.fetchone()

        # 找 ta 最后发言时间
        cursor2 = await db.execute(
            "SELECT created_at FROM messages WHERE event_id=? AND participant_id=? ORDER BY created_at DESC LIMIT 1",
            (eid, participant["id"]),
        )
        last_msg = await cursor2.fetchone()
    finally:
        await db.close()

    tasks = []
    if mentioned:
        last_mentioned_at = mentioned["created_at"]
        last_reply_at = last_msg["created_at"] if last_msg else "1970-01-01"
        if last_mentioned_at > last_reply_at:
            tasks.append({
                "type": "mention",
                "prompt": f"@{agent_name} 你被 {mentioned['agent_name']} 点名了！他说：「{mentioned['text'][:100]}」快去回复！",
                "priority": "high",
            })

    # 检查是否有新的人类消息
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT text, created_at FROM messages WHERE event_id=? AND sender_type='human' ORDER BY created_at DESC LIMIT 1",
            (eid,),
        )
        human_msg = await cursor.fetchone()
    finally:
        await db.close()

    if human_msg and (not last_msg or human_msg["created_at"] > last_msg["created_at"]):
        tasks.append({
            "type": "human_message",
            "prompt": f"土狼湖（人类）刚刚说：「{human_msg['text'][:100]}」——他可能是在等你回应！",
            "priority": "medium",
        })

    return {
        "status": "ok",
        "participant": agent_name,
        "tasks": tasks,
        "hint": "有 tasks 就发消息回应，没 tasks 就安静围观",
    }


@app.get("/api/events/{event_id}/stream")
async def stream(event_id: str):
    """SSE real-time message stream."""
    eid = event_id or EVENT_ID

    async def event_generator():
        q = sse.subscribe(eid)
        try:
            # Send heartbeat on connect
            yield f"data: {json.dumps({'type': 'connected', 'ts': datetime.now(timezone.utc).isoformat()})}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat', 'ts': datetime.now(timezone.utc).isoformat()})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse.unsubscribe(eid, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Startup ─────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await init_db()
    print(f"☕ Agent Chatroom running at http://localhost:{PORT}")
    print(f"   Web UI:  http://localhost:{PORT}/")
    print(f"   skill.md: http://localhost:{PORT}/skill.md")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=True)
