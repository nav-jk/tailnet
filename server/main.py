import time
import aiosqlite
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from ipaddress import IPv4Address

DB_PATH = "minitail.db"

# Peers unseen for longer than this are considered offline
OFFLINE_TIMEOUT_SECONDS = 90


# ── DB setup ──────────────────────────────────────────────────────────────────

async def get_db():
    return await aiosqlite.connect(DB_PATH)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                username TEXT PRIMARY KEY,
                ip       TEXT NOT NULL,
                port     INTEGER NOT NULL,
                last_seen REAL NOT NULL,
                public_key TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_requests (
                to_user   TEXT NOT NULL,
                from_user TEXT NOT NULL,
                PRIMARY KEY (to_user, from_user)
            )
        """)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan)


# ── Models ────────────────────────────────────────────────────────────────────

class Client(BaseModel):
    username: str
    ip: IPv4Address
    port: int
    public_key: str

class ConnectRequest(BaseModel):
    from_user: str
    to_user: str

class AcceptRequest(BaseModel):
    from_user: str
    to_user: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def now() -> float:
    return time.time()


async def get_client(db: aiosqlite.Connection, username: str) -> dict | None:
    """Return a live client row or None if not found / timed out."""
    async with db.execute(
        "SELECT username, ip, port, last_seen, public_key FROM clients WHERE username = ?",
        (username,)
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        return None

    username, ip, port, last_seen, public_key = row

    if now() - last_seen > OFFLINE_TIMEOUT_SECONDS:
        # Treat as offline — clean up stale row
        await db.execute("DELETE FROM clients WHERE username = ?", (username,))
        await db.execute(
            "DELETE FROM pending_requests WHERE to_user = ? OR from_user = ?",
            (username, username)
        )
        await db.commit()
        return None

    return {"username": username, "ip": ip, "port": port, "public_key": public_key}


# ── Basic endpoints ───────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok"}


@app.post("/register")
async def register(client: Client):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO clients (username, ip, port, last_seen, public_key)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                ip         = excluded.ip,
                port       = excluded.port,
                last_seen  = excluded.last_seen,
                public_key = excluded.public_key
        """, (client.username, str(client.ip), client.port, now(), client.public_key))
        await db.commit()

    return {"status": "registered"}


@app.get("/peer/{username}")
async def get_peer(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        peer = await get_client(db, username)

    if peer is None:
        raise HTTPException(status_code=404, detail="User not found")

    return peer


@app.get("/peers")
async def get_peers():
    cutoff = now() - OFFLINE_TIMEOUT_SECONDS

    async with aiosqlite.connect(DB_PATH) as db:
        # Purge stale rows first
        await db.execute(
            "DELETE FROM clients WHERE last_seen < ?", (cutoff,)
        )
        await db.commit()

        async with db.execute(
            "SELECT username FROM clients WHERE last_seen >= ?", (cutoff,)
        ) as cur:
            rows = await cur.fetchall()

    return [r[0] for r in rows]


@app.post("/connect")
async def request_connect(req: ConnectRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        if await get_client(db, req.from_user) is None:
            raise HTTPException(status_code=404, detail="Caller not registered")
        if await get_client(db, req.to_user) is None:
            raise HTTPException(status_code=404, detail="Target peer not found")

        await db.execute("""
            INSERT OR IGNORE INTO pending_requests (to_user, from_user)
            VALUES (?, ?)
        """, (req.to_user, req.from_user))
        await db.commit()

    return {"status": "request_sent"}


@app.get("/requests/{username}")
async def get_requests(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT from_user FROM pending_requests WHERE to_user = ?",
            (username,)
        ) as cur:
            rows = await cur.fetchall()

        result = []
        for (from_user,) in rows:
            peer = await get_client(db, from_user)
            if peer:
                result.append(peer)
            else:
                # Requester went offline — remove stale request
                await db.execute(
                    "DELETE FROM pending_requests WHERE to_user = ? AND from_user = ?",
                    (username, from_user)
                )

        await db.commit()

    return result


@app.post("/accept")
async def accept_connect(req: AcceptRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        # Clear the request
        await db.execute(
            "DELETE FROM pending_requests WHERE to_user = ? AND from_user = ?",
            (req.to_user, req.from_user)
        )
        await db.commit()

        peer = await get_client(db, req.from_user)

    if peer is None:
        raise HTTPException(status_code=404, detail="Initiator no longer online")

    return peer


@app.post("/decline")
async def decline_connect(req: AcceptRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM pending_requests WHERE to_user = ? AND from_user = ?",
            (req.to_user, req.from_user)
        )
        await db.commit()

    return {"status": "declined"}