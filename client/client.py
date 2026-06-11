import time
import threading
import base64
from dataclasses import dataclass
import requests
import schedule
import socket
from rich.table import Table
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from nacl.public import PublicKey
from stun_client import discover_endpoint_with_fallback
from encrypt import generate_key, encrypt, decrypt

BASE_URL = "http://129.121.114.238:8000"

console = Console()

# ── Global state ─────────────────────────────────────────────────────────────

@dataclass
class Client:
    username: str
    ip: str
    port: int
    public_key: str   # base64-encoded string for transport
    private_key: object  # nacl PrivateKey object

client: Client | None = None
USERNAME: str = ""

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", 0))
sock.settimeout(0.5)

incoming_messages: list[tuple[tuple, str]] = []
_msg_lock = threading.Lock()

# The public key of whoever we're currently chatting with (nacl PublicKey object)
_current_peer_public_key: PublicKey | None = None

# Signals the chat printer thread to stop when we leave chat
_in_chat = threading.Event()


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_peer(data: dict) -> dict:
    return {
        "username": data["username"],
        "ip":       str(data["ip"]),
        "port":     int(data["port"]),
        "public_key": str(data["public_key"])   # base64 string
    }


def peer_public_key_obj(peer: dict) -> PublicKey:
    """Decode a peer's base64 public key string into a nacl PublicKey."""
    return PublicKey(base64.b64decode(peer["public_key"]))


# ── STUN / Registration ───────────────────────────────────────────────────────

def discover_endpoint() -> tuple[str, int]:
    external_ip, external_port = discover_endpoint_with_fallback(sock)
    console.print(f"[dim][STUN] {external_ip}:{external_port}[/dim]")
    return external_ip, external_port


def register():
    try:
        r = requests.post(
            f"{BASE_URL}/register",
            json={
                "username":   client.username,
                "ip":         client.ip,
                "port":       client.port,
                "public_key": client.public_key,
            },
            timeout=5,
        )
        console.print(f"[dim][REGISTER] {r.status_code}[/dim]")
    except Exception as e:
        console.print(f"[red][REGISTER ERROR] {e}[/red]")


def refresh_endpoint():
    global client
    try:
        new_ip, new_port = discover_endpoint()
        if client is None or new_ip != client.ip or new_port != client.port:
            console.print(f"[yellow][UPDATE] {new_ip}:{new_port}[/yellow]")
            client.ip   = new_ip
            client.port = new_port
            register()
        else:
            console.print("[dim][UPDATE] Endpoint unchanged[/dim]")
    except Exception as e:
        console.print(f"[red][REFRESH ERROR] {e}[/red]")


def heartbeat():
    try:
        r = requests.post(
            f"{BASE_URL}/register",
            json={
                "username":   client.username,
                "ip":         client.ip,
                "port":       client.port,
                "public_key": client.public_key,
            },
            timeout=5,
        )
        console.print(f"[dim][HEARTBEAT] {r.status_code}[/dim]")
    except Exception as e:
        console.print(f"[red][HEARTBEAT ERROR] {e}[/red]")


# ── UDP Receiver (background thread) ─────────────────────────────────────────

def udp_receiver():
    """Receive all UDP packets and push to incoming_messages queue."""
    while True:
        try:
            data, addr = sock.recvfrom(4096)

            # Only decrypt if we have a peer key (i.e. we're in a chat)
            peer_key = _current_peer_public_key
            if peer_key is None:
                continue

            try:
                data_decrypt = decrypt(client.private_key, peer_key, data)
            except Exception:
                continue  # Undecryptable packet — ignore

            decoded = data_decrypt.decode(errors="replace")
            if decoded in ("HOLEPUNCH", "HELLO"):
                continue
            with _msg_lock:
                incoming_messages.append((addr, decoded))
        except socket.timeout:
            continue
        except Exception as e:
            console.print(f"[red][UDP ERROR] {e}[/red]")


# ── Chat printer (background thread) ─────────────────────────────────────────

def chat_printer(peer_name: str):
    """
    While in chat, continuously drain incoming_messages and print them.
    """
    while _in_chat.is_set():
        with _msg_lock:
            while incoming_messages:
                addr, msg = incoming_messages.pop(0)
                console.print(
                    f"\n[green]{peer_name}[/green] "
                    f"[dim]({addr[0]}:{addr[1]})[/dim]: {msg}"
                )
        time.sleep(0.1)


# ── Hole Punching ─────────────────────────────────────────────────────────────

def hole_punch(peer_ip: str, peer_port: int, rounds: int = 20):
    console.print(f"[yellow]Punching hole to {peer_ip}:{peer_port}…[/yellow]")
    for _ in range(rounds):
        try:
            sock.sendto(b"HOLEPUNCH", (peer_ip, peer_port))
        except Exception:
            pass
        time.sleep(0.1)


# ── Chat Window ───────────────────────────────────────────────────────────────

def chat_window(peer: dict):
    global _current_peer_public_key

    peer_ip   = peer["ip"]
    peer_port = peer["port"]
    peer_name = peer["username"]

    # Decode peer's public key once for this session
    _current_peer_public_key = peer_public_key_obj(peer)

    console.clear()
    console.print(
        Panel.fit(
            f"Connected to [green]{peer_name}[/green]  ({peer_ip}:{peer_port})",
            title="MiniTail Chat",
        )
    )
    console.print("[yellow]Type messages below. /exit to leave.[/yellow]\n")

    # Send HELLO so peer's receiver confirms the hole is open
    try:
        sock.sendto(b"HELLO", (peer_ip, peer_port))
    except Exception:
        pass

    # Start background printer
    _in_chat.set()
    threading.Thread(target=chat_printer, args=(peer_name,), daemon=True).start()

    while True:
        try:
            msg = Prompt.ask("[bold cyan]You[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            break

        if msg.strip() == "/exit":
            break

        try:
            encrypted = encrypt(client.private_key, _current_peer_public_key, msg.encode())
            sock.sendto(encrypted, (peer_ip, peer_port))
        except Exception as e:
            console.print(f"[red]Send error: {e}[/red]")

    _in_chat.clear()
    _current_peer_public_key = None
    console.print("[yellow]Left chat.[/yellow]")


# ── Peer list ─────────────────────────────────────────────────────────────────

def show_peers():
    try:
        r = requests.get(f"{BASE_URL}/peers", timeout=5)
        peers = r.json()

        table = Table(title="Online Peers")
        table.add_column("Username", style="cyan")
        table.add_column("", style="dim")

        for p in peers:
            table.add_row(p, "(you)" if p == USERNAME else "")

        console.print(table)
    except Exception as e:
        console.print(f"[red]Peer list error: {e}[/red]")


# ── Outgoing connection request ───────────────────────────────────────────────

def connect_peer():
    peer_name = Prompt.ask("Enter peer username")

    if peer_name == USERNAME:
        console.print("[red]That's you.[/red]")
        return

    try:
        r = requests.post(
            f"{BASE_URL}/connect",
            json={"from_user": USERNAME, "to_user": peer_name},
            timeout=5,
        )
        if r.status_code != 200:
            console.print(f"[red]Server error: {r.text}[/red]")
            return
    except Exception as e:
        console.print(f"[red]Request error: {e}[/red]")
        return

    try:
        pr = requests.get(f"{BASE_URL}/peer/{peer_name}", timeout=5)
        if pr.status_code != 200:
            console.print("[red]Peer not found.[/red]")
            return
        peer = parse_peer(pr.json())
    except Exception as e:
        console.print(f"[red]Peer lookup error: {e}[/red]")
        return

    console.print(
        f"[yellow]Request sent to [bold]{peer_name}[/bold]. "
        "Hole-punching now — they need to accept on their end.[/yellow]"
    )

    hole_punch(peer["ip"], peer["port"])
    time.sleep(1)
    chat_window(peer)


# ── Incoming request polling ──────────────────────────────────────────────────

def check_incoming_requests():
    try:
        r = requests.get(f"{BASE_URL}/requests/{USERNAME}", timeout=5)
        if r.status_code != 200:
            return
        requesters = r.json()
    except Exception:
        return

    for requester in requesters:
        from_name = requester["username"]

        console.print(
            f"\n[bold magenta]📞 Incoming request from "
            f"[cyan]{from_name}[/cyan][/bold magenta]"
        )

        answer = Prompt.ask("Accept?", choices=["y", "n"], default="y")

        if answer == "n":
            try:
                requests.post(
                    f"{BASE_URL}/decline",
                    json={"from_user": from_name, "to_user": USERNAME},
                    timeout=5,
                )
            except Exception:
                pass
            console.print(f"[yellow]Declined {from_name}.[/yellow]")
            continue

        try:
            ar = requests.post(
                f"{BASE_URL}/accept",
                json={"from_user": from_name, "to_user": USERNAME},
                timeout=5,
            )
            data = ar.json()
        except Exception as e:
            console.print(f"[red]Accept error (network): {e}[/red]")
            continue

        if "ip" not in data:
            console.print(f"[red]Accept error: server said → {data}[/red]")
            continue

        peer = parse_peer(data)

        console.print(f"[green]Accepted! Connecting to {from_name}…[/green]")
        hole_punch(peer["ip"], peer["port"])
        time.sleep(0.5)
        chat_window(peer)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global client, USERNAME

    USERNAME = Prompt.ask("[bold cyan]Enter your username[/bold cyan]")

    # Generate keypair ONCE at startup
    private_key, public_key = generate_key()
    public_key_b64 = base64.b64encode(bytes(public_key)).decode()

    external_ip, external_port = discover_endpoint()

    client = Client(
        username=USERNAME,
        ip=external_ip,
        port=external_port,
        public_key=public_key_b64,
        private_key=private_key,
    )

    console.print(
        Panel.fit(
            f"[bold cyan]MiniTail Client[/bold cyan]\n\n"
            f"Username : {client.username}\n"
            f"Public IP: {client.ip}\n"
            f"Port     : {client.port}",
            title="Client Information",
        )
    )

    register()

    threading.Thread(target=udp_receiver, daemon=True).start()

    schedule.every(30).seconds.do(heartbeat)
    schedule.every(5).minutes.do(refresh_endpoint)

    while True:
        schedule.run_pending()
        check_incoming_requests()

        console.print(
            Panel.fit(
                "[1] Show Peers\n"
                "[2] Connect to Peer\n"
                "[3] Exit",
                title="MiniTail",
            )
        )

        choice = Prompt.ask("Select option", choices=["1", "2", "3"])

        if choice == "1":
            show_peers()
        elif choice == "2":
            connect_peer()
        elif choice == "3":
            console.print("[yellow]Shutting down MiniTail…[/yellow]")
            break


if __name__ == "__main__":
    main()