from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from ipaddress import IPv4Address

app = FastAPI()


class Client(BaseModel):
    username: str
    ip: IPv4Address
    port: int


class ConnectRequest(BaseModel):
    from_user: str   # who is initiating
    to_user: str     # who they want to talk to


class AcceptRequest(BaseModel):
    from_user: str   # the original initiator
    to_user: str     # the acceptor (you)


# ── Storage ──────────────────────────────────────────────────────────────────

clients: dict[str, Client] = {}

# pending_requests["nav2"] = ["nav", "alice", ...]
# means nav2 has incoming requests from nav and alice
pending_requests: dict[str, list[str]] = {}


# ── Basic endpoints ───────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok"}


@app.post("/register")
async def register(client: Client):
    clients[client.username] = client
    return {"status": "registered"}


@app.get("/peer/{username}")
async def get_peer(username: str):
    if username not in clients:
        raise HTTPException(status_code=404, detail="User not found")
    return clients[username]


@app.get("/peers")
async def get_peers():
    return list(clients.keys())


# ── Phase 7: Connection request endpoints ────────────────────────────────────

@app.post("/connect")
async def request_connect(req: ConnectRequest):
    """
    Initiator calls this to tell the server:
    "I want to talk to <to_user>."
    The server queues the request for to_user to poll.
    """
    if req.from_user not in clients:
        raise HTTPException(status_code=404, detail="Caller not registered")

    if req.to_user not in clients:
        raise HTTPException(status_code=404, detail="Target peer not found")

    if req.to_user not in pending_requests:
        pending_requests[req.to_user] = []

    # Avoid duplicate requests
    if req.from_user not in pending_requests[req.to_user]:
        pending_requests[req.to_user].append(req.from_user)

    return {"status": "request_sent"}


@app.get("/requests/{username}")
async def get_requests(username: str):
    """
    A client polls this to see who wants to connect to them.
    Returns a list of usernames with their IPs/ports so the
    callee can start hole-punching immediately on accept.
    """
    requesters = pending_requests.get(username, [])

    result = []
    for r in requesters:
        if r in clients:
            c = clients[r]
            result.append({
                "username": c.username,
                "ip": str(c.ip),
                "port": c.port,
            })

    return result


@app.post("/accept")
async def accept_connect(req: AcceptRequest):
    """
    Callee calls this after agreeing to connect.
    Clears the pending request and returns the initiator's
    current endpoint so both sides can start hole-punching.
    """
    # Remove from queue regardless of outcome
    if req.to_user in pending_requests:
        pending_requests[req.to_user] = [
            r for r in pending_requests[req.to_user]
            if r != req.from_user
        ]

    if req.from_user not in clients:
        raise HTTPException(status_code=404, detail="Initiator no longer online")

    c = clients[req.from_user]
    return {
        "username": c.username,
        "ip": str(c.ip),
        "port": c.port,
    }


@app.post("/decline")
async def decline_connect(req: AcceptRequest):
    """
    Callee explicitly declines. Clears the pending request.
    """
    if req.to_user in pending_requests:
        pending_requests[req.to_user] = [
            r for r in pending_requests[req.to_user]
            if r != req.from_user
        ]

    return {"status": "declined"}