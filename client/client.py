import time
import threading
import base64
from dataclasses import dataclass
from datetime import datetime
import requests
import schedule
import socket
from rich.table import Table
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from rich.rule import Rule
from rich.align import Align
from rich.live import Live
from rich.layout import Layout
from rich import box
from nacl.public import PublicKey
from stun_client import discover_endpoint_with_fallback
from encrypt import generate_key, encrypt, decrypt
from dotenv import load_dotenv  
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# Load .env
load_dotenv(ROOT_DIR / ".env")


BASE_URL = os.getenv("BASE_URL")

console = Console()

# ── Global state ──────────────────────────────────────────────────────────────

@dataclass
class Client:
    username: str
    ip: str
    port: int
    public_key: str
    private_key: object

client: Client | None = None
USERNAME: str = ""

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", 0))
sock.settimeout(0.5)

incoming_messages: list[tuple[tuple, str]] = []
_msg_lock = threading.Lock()
_current_peer_public_key: PublicKey | None = None
_in_chat = threading.Event()


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_peer(data: dict) -> dict:
    return {
        "username":   data["username"],
        "ip":         str(data["ip"]),
        "port":       int(data["port"]),
        "public_key": str(data["public_key"]),
    }

def peer_public_key_obj(peer: dict) -> PublicKey:
    return PublicKey(base64.b64decode(peer["public_key"]))

def ts() -> str:
    return datetime.now().strftime("%H:%M")

def now_full() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ── STUN / Registration ───────────────────────────────────────────────────────

def discover_endpoint() -> tuple[str, int]:
    external_ip, external_port = discover_endpoint_with_fallback(sock)
    return external_ip, external_port

def register():
    try:
        requests.post(
            f"{BASE_URL}/register",
            json={
                "username":   client.username,
                "ip":         client.ip,
                "port":       client.port,
                "public_key": client.public_key,
            },
            timeout=5,
        )
    except Exception:
        pass

def refresh_endpoint():
    global client
    try:
        new_ip, new_port = discover_endpoint()
        if client is None or new_ip != client.ip or new_port != client.port:
            client.ip   = new_ip
            client.port = new_port
            register()
    except Exception:
        pass

def heartbeat():
    try:
        requests.post(
            f"{BASE_URL}/register",
            json={
                "username":   client.username,
                "ip":         client.ip,
                "port":       client.port,
                "public_key": client.public_key,
            },
            timeout=5,
        )
    except Exception:
        pass


# ── UDP Receiver ──────────────────────────────────────────────────────────────

def udp_receiver():
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            peer_key = _current_peer_public_key
            if peer_key is None:
                continue
            try:
                data_decrypt = decrypt(client.private_key, peer_key, data)
            except Exception:
                continue
            decoded = data_decrypt.decode(errors="replace")
            if decoded in ("HOLEPUNCH", "HELLO"):
                continue
            with _msg_lock:
                incoming_messages.append((addr, decoded))
        except socket.timeout:
            continue
        except Exception:
            pass


# ── Hole Punching ─────────────────────────────────────────────────────────────

def hole_punch(peer_ip: str, peer_port: int, rounds: int = 20):
    for _ in range(rounds):
        try:
            sock.sendto(b"HOLEPUNCH", (peer_ip, peer_port))
        except Exception:
            pass
        time.sleep(0.1)


# ── UI Primitives ─────────────────────────────────────────────────────────────

BRAND   = "cyan"
ACCENT  = "green"
DIM     = "dim"
WARN    = "yellow"
ERR     = "red"
MY_BG   = "steel_blue"
PEER_BG = "dark_green"

def header_bar(title: str, subtitle: str = "") -> Text:
    """Top bar — cyan background, white text, right-aligned subtitle."""
    t = Text(overflow="crop")
    t.append(f"  ▲ MiniTail  ", style=f"bold white on {BRAND}")
    t.append(f"  {title}  ",    style=f"bold white on grey23")
    if subtitle:
        t.append(f"  {subtitle}  ", style=f"dim white on grey19")
    return t

def status_bar(left: str, right: str = "") -> Text:
    """Bottom status bar."""
    width = console.width
    gap   = width - len(left) - len(right) - 4
    t = Text(overflow="crop")
    t.append(f"  {left}", style=f"white on grey19")
    t.append(" " * max(gap, 1), style="on grey19")
    t.append(f"{right}  ", style=f"dim white on grey19")
    return t

def msg_bubble_mine(msg: str) -> Text:
    """Right-aligned bubble for outgoing messages."""
    timestamp = ts()
    text = Text(justify="right")
    text.append(f"  {msg}  ", style=f"white on {MY_BG}")
    text.append(f" {timestamp} ", style=DIM)
    return text

def msg_bubble_peer(peer_name: str, msg: str) -> Text:
    """Left-aligned bubble for incoming messages."""
    timestamp = ts()
    text = Text(justify="left")
    text.append(f" {timestamp} ", style=DIM)
    text.append(f"  {peer_name}  ", style=f"bold white on {PEER_BG}")
    text.append(f"  {msg}  ", style="white on grey23")
    return text

def msg_system(msg: str) -> Text:
    """Centered system/event message."""
    t = Text(justify="center")
    t.append(f"── {msg} ──", style="dim italic")
    return t


# ── Chat Window ───────────────────────────────────────────────────────────────

def chat_printer(peer_name: str):
    while _in_chat.is_set():
        with _msg_lock:
            while incoming_messages:
                _, msg = incoming_messages.pop(0)
                console.print(msg_bubble_peer(peer_name, msg))
        time.sleep(0.1)

def chat_window(peer: dict):
    global _current_peer_public_key

    peer_ip   = peer["ip"]
    peer_port = peer["port"]
    peer_name = peer["username"]

    _current_peer_public_key = peer_public_key_obj(peer)

    console.clear()
    console.print(header_bar(f"chatting with {peer_name}", f"{peer_ip}:{peer_port}"))
    console.print()
    console.print(msg_system(f"session started · {now_full()} · end-to-end encrypted · /exit to leave"))
    console.print()

    try:
        sock.sendto(b"HELLO", (peer_ip, peer_port))
    except Exception:
        pass

    _in_chat.set()
    threading.Thread(target=chat_printer, args=(peer_name,), daemon=True).start()

    while True:
        # status bar drawn before each prompt
        console.print(
            status_bar(
                f"🔒 encrypted  ·  {USERNAME} → {peer_name}",
                now_full()
            )
        )
        try:
            msg = Prompt.ask(f"[bold {BRAND}] ›[/bold {BRAND}]")
        except (EOFError, KeyboardInterrupt):
            break

        if msg.strip() == "/exit":
            break

        if not msg.strip():
            continue

        try:
            encrypted = encrypt(client.private_key, _current_peer_public_key, msg.encode())
            sock.sendto(encrypted, (peer_ip, peer_port))
            console.print(msg_bubble_mine(msg))
        except Exception as e:
            console.print(f"[{ERR}]  ✗ send failed: {e}[/{ERR}]")

    _in_chat.clear()
    _current_peer_public_key = None
    console.print()
    console.print(msg_system(f"session ended · {now_full()}"))
    console.print()


# ── Peer List ─────────────────────────────────────────────────────────────────

def show_peers():
    try:
        r = requests.get(f"{BASE_URL}/peers", timeout=5)
        peers = r.json()
    except Exception as e:
        console.print(f"[{ERR}]  ✗ {e}[/{ERR}]")
        return

    console.print()
    table = Table(
        box=box.SIMPLE_HEAD,
        border_style=BRAND,
        header_style=f"bold {BRAND}",
        show_edge=False,
        padding=(0, 2),
    )
    table.add_column("#",        style=DIM,    width=4,  justify="right")
    table.add_column("Username", style="bold white")
    table.add_column("Status",   style=DIM,    width=12)
    table.add_column("",         style=DIM,    width=6)

    for i, p in enumerate(peers, 1):
        if p == USERNAME:
            table.add_row(str(i), p, f"[{ACCENT}]● online[/{ACCENT}]", "(you)")
        else:
            table.add_row(str(i), p, f"[{ACCENT}]● online[/{ACCENT}]", "")

    console.print(Align.left(table, pad=True))
    console.print(
        f"  [dim]{len(peers)} peer{'s' if len(peers) != 1 else ''} online[/dim]\n"
    )


# ── Outgoing Request ──────────────────────────────────────────────────────────

def connect_peer():
    peer_name = Prompt.ask(f"  [{BRAND}]peer username[/{BRAND}]")

    if peer_name == USERNAME:
        console.print(f"  [{WARN}]  that's you.[/{WARN}]")
        return

    with console.status(f"  [dim]sending request to {peer_name}…[/dim]", spinner="dots"):
        try:
            r = requests.post(
                f"{BASE_URL}/connect",
                json={"from_user": USERNAME, "to_user": peer_name},
                timeout=5,
            )
            if r.status_code != 200:
                console.print(f"  [{ERR}]✗ {r.json().get('detail', r.text)}[/{ERR}]")
                return
        except Exception as e:
            console.print(f"  [{ERR}]✗ {e}[/{ERR}]")
            return

        try:
            pr = requests.get(f"{BASE_URL}/peer/{peer_name}", timeout=5)
            if pr.status_code != 200:
                console.print(f"  [{ERR}]✗ peer not found[/{ERR}]")
                return
            peer = parse_peer(pr.json())
        except Exception as e:
            console.print(f"  [{ERR}]✗ {e}[/{ERR}]")
            return

    console.print(f"\n  [{WARN}]  waiting for {peer_name} to accept…[/{WARN}]")

    with console.status(f"  [dim]hole-punching {peer['ip']}:{peer['port']}…[/dim]", spinner="line"):
        hole_punch(peer["ip"], peer["port"])
        time.sleep(1)

    chat_window(peer)


# ── Incoming Request Polling ──────────────────────────────────────────────────

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

        console.print()
        console.print(
            Panel(
                f"  [bold white]{from_name}[/bold white] wants to connect\n"
                f"  [dim]{requester['ip']}:{requester['port']}[/dim]",
                title="[bold magenta]  📞  incoming[/bold magenta]",
                border_style="magenta",
                box=box.ROUNDED,
                padding=(0, 2),
                expand=False,
            )
        )

        answer = Prompt.ask(f"  [{BRAND}]accept?[/{BRAND}]", choices=["y", "n"], default="y")

        if answer == "n":
            try:
                requests.post(
                    f"{BASE_URL}/decline",
                    json={"from_user": from_name, "to_user": USERNAME},
                    timeout=5,
                )
            except Exception:
                pass
            console.print(f"  [dim]declined {from_name}.[/dim]\n")
            continue

        try:
            ar   = requests.post(
                f"{BASE_URL}/accept",
                json={"from_user": from_name, "to_user": USERNAME},
                timeout=5,
            )
            data = ar.json()
        except Exception as e:
            console.print(f"  [{ERR}]✗ accept error: {e}[/{ERR}]")
            continue

        if "ip" not in data:
            console.print(f"  [{ERR}]✗ {data}[/{ERR}]")
            continue

        peer = parse_peer(data)

        console.print(f"  [{ACCENT}]  connecting to {from_name}…[/{ACCENT}]")
        with console.status("  [dim]punching hole…[/dim]", spinner="line"):
            hole_punch(peer["ip"], peer["port"])
            time.sleep(0.5)

        chat_window(peer)


# ── Main Menu ─────────────────────────────────────────────────────────────────

def draw_menu():
    console.print(header_bar("main menu", f"{client.ip}:{client.port}"))
    console.print()
    console.print(
        Panel(
            f"  [bold {BRAND}]1[/bold {BRAND}]  [white]peers[/white]         [dim]show who's online[/dim]\n"
            f"  [bold {BRAND}]2[/bold {BRAND}]  [white]connect[/white]       [dim]open a chat[/dim]\n"
            f"  [bold {BRAND}]3[/bold {BRAND}]  [white]exit[/white]          [dim]quit minitail[/dim]",
            box=box.ROUNDED,
            border_style=BRAND,
            padding=(0, 3),
            expand=False,
        )
    )
    console.print(
        status_bar(
            f"  {USERNAME}  ·  🔑 E2E encrypted",
            now_full()
        )
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global client, USERNAME

    console.clear()

    # Splash
    console.print()
    console.print(Align.center(
        Panel(
            Align.center(
                "[bold cyan]▲  MiniTail[/bold cyan]\n"
                "[dim]encrypted p2p chat · no servers in the loop[/dim]"
            ),
            box=box.DOUBLE_EDGE,
            border_style=BRAND,
            padding=(1, 6),
            expand=False,
        )
    ))
    console.print()

    USERNAME = Prompt.ask(f"  [{BRAND}]username[/{BRAND}]")

    with console.status("  [dim]discovering your endpoint via STUN…[/dim]", spinner="dots"):
        external_ip, external_port = discover_endpoint()

    private_key, public_key = generate_key()
    public_key_b64 = base64.b64encode(bytes(public_key)).decode()

    client = Client(
        username=USERNAME,
        ip=external_ip,
        port=external_port,
        public_key=public_key_b64,
        private_key=private_key,
    )

    console.print()
    console.print(
        Panel(
            f"  [dim]user[/dim]       [bold white]{client.username}[/bold white]\n"
            f"  [dim]public ip[/dim]  [bold white]{client.ip}[/bold white]\n"
            f"  [dim]port[/dim]       [bold white]{client.port}[/bold white]\n"
            f"  [dim]pubkey[/dim]     [bold white]{client.public_key[:24]}…[/bold white]",
            box=box.ROUNDED,
            border_style=f"dim {BRAND}",
            padding=(0, 2),
            expand=False,
        )
    )

    with console.status("  [dim]registering…[/dim]", spinner="dots"):
        register()

    console.print(f"  [{ACCENT}]  registered ✓[/{ACCENT}]\n")

    threading.Thread(target=udp_receiver, daemon=True).start()

    schedule.every(30).seconds.do(heartbeat)
    schedule.every(5).minutes.do(refresh_endpoint)

    while True:
        schedule.run_pending()
        check_incoming_requests()

        draw_menu()

        choice = Prompt.ask(f"  [{BRAND}]›[/{BRAND}]", choices=["1", "2", "3"])
        console.print()

        if choice == "1":
            show_peers()
        elif choice == "2":
            connect_peer()
        elif choice == "3":
            console.print()
            console.print(Align.center("[dim]goodbye.[/dim]"))
            console.print()
            break


if __name__ == "__main__":
    main()