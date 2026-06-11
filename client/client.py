import time
import threading
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
from rich.columns import Columns
from rich.rule import Rule
from rich import box
from stun_client import discover_endpoint_with_fallback

BASE_URL = "https://server-try.fastapicloud.dev"

console = Console()

# ── Global state ─────────────────────────────────────────────────────────────

@dataclass
class Client:
    username: str
    ip: str
    port: int

client: Client | None = None
USERNAME: str = ""

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", 0))
sock.settimeout(0.5)

incoming_messages: list[tuple[tuple, str]] = []
_msg_lock = threading.Lock()
_in_chat = threading.Event()


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_peer(data: dict) -> dict:
    return {
        "username": data["username"],
        "ip":       str(data["ip"]),
        "port":     int(data["port"]),
    }

def ts() -> str:
    """Current time as HH:MM."""
    return datetime.now().strftime("%H:%M")


# ── STUN / Registration ───────────────────────────────────────────────────────

def discover_endpoint() -> Client:
    external_ip, external_port = discover_endpoint_with_fallback(sock)
    return Client(username=USERNAME, ip=external_ip, port=external_port)


def register():
    try:
        r = requests.post(
            f"{BASE_URL}/register",
            json={"username": client.username, "ip": client.ip, "port": client.port},
            timeout=5,
        )
        if r.status_code != 200:
            console.print(f"[dim red]  register failed {r.status_code}[/dim red]")
    except Exception as e:
        console.print(f"[dim red]  register error: {e}[/dim red]")


def refresh_endpoint():
    global client
    try:
        new = discover_endpoint()
        if client is None or new.ip != client.ip or new.port != client.port:
            client = new
            register()
    except Exception:
        pass


def heartbeat():
    try:
        requests.post(
            f"{BASE_URL}/register",
            json={"username": client.username, "ip": client.ip, "port": client.port},
            timeout=5,
        )
    except Exception:
        pass


# ── UDP Receiver ──────────────────────────────────────────────────────────────

def udp_receiver():
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            decoded = data.decode(errors="replace")
            if decoded in ("HOLEPUNCH", "HELLO"):
                continue
            with _msg_lock:
                incoming_messages.append((addr, decoded))
        except socket.timeout:
            continue
        except Exception:
            pass


# ── Chat printer ──────────────────────────────────────────────────────────────

def chat_printer(peer_name: str):
    while _in_chat.is_set():
        with _msg_lock:
            while incoming_messages:
                _, msg = incoming_messages.pop(0)
                # Right-aligned peer bubble
                bubble = Text()
                bubble.append(f" {peer_name} ", style="bold white on dark_green")
                bubble.append(f"  {msg}  ", style="white on grey23")
                bubble.append(f" {ts()} ", style="dim")
                console.print(bubble)
        time.sleep(0.1)


# ── Hole Punching ─────────────────────────────────────────────────────────────

def hole_punch(peer_ip: str, peer_port: int, rounds: int = 20):
    console.print(f"[dim]  punching {peer_ip}:{peer_port}…[/dim]", end="\r")
    for _ in range(rounds):
        try:
            sock.sendto(b"HOLEPUNCH", (peer_ip, peer_port))
        except Exception:
            pass
        time.sleep(0.1)
    console.print(" " * 40, end="\r")   # clear the line


# ── Chat Window ───────────────────────────────────────────────────────────────

def chat_window(peer: dict):
    peer_ip   = peer["ip"]
    peer_port = peer["port"]
    peer_name = peer["username"]

    console.clear()

    # Header
    header = Text(justify="center")
    header.append("  MiniTail  ", style="bold black on cyan")
    header.append(f"  {peer_name}  ", style="bold white on dark_green")
    header.append(f"  {peer_ip}:{peer_port}  ", style="dim")
    console.print(header)
    console.print(Rule(style="dim cyan"))
    console.print(
        f"  [dim]Connected at {datetime.now().strftime('%H:%M:%S')}  ·  /exit to leave[/dim]\n"
    )

    try:
        sock.sendto(b"HELLO", (peer_ip, peer_port))
    except Exception:
        pass

    _in_chat.set()
    threading.Thread(target=chat_printer, args=(peer_name,), daemon=True).start()

    while True:
        try:
            msg = Prompt.ask(f"[bold cyan] {USERNAME}[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            break

        if msg.strip() == "/exit":
            break

        if not msg.strip():
            continue

        try:
            sock.sendto(msg.encode(), (peer_ip, peer_port))
            # Show own message right-aligned style
            own = Text(justify="right")
            own.append(f"  {msg}  ", style="white on steel_blue")
            own.append(f" {ts()} ", style="dim")
            console.print(own)
        except Exception as e:
            console.print(f"[red]  send error: {e}[/red]")

    _in_chat.clear()
    console.print()
    console.print(Rule(style="dim"))
    console.print("  [dim]Left chat.[/dim]\n")


# ── Peer list ─────────────────────────────────────────────────────────────────

def show_peers():
    try:
        r = requests.get(f"{BASE_URL}/peers", timeout=5)
        peers = r.json()

        table = Table(
            box=box.ROUNDED,
            border_style="dim cyan",
            header_style="bold cyan",
            show_lines=False,
        )
        table.add_column("  #", style="dim", width=4)
        table.add_column("Username", style="bold white")
        table.add_column("Status", style="dim")

        for i, p in enumerate(peers, 1):
            status = "[green]● you[/green]" if p == USERNAME else "[green]● online[/green]"
            table.add_row(f"  {i}", p, status)

        console.print()
        console.print(table)
        console.print()

    except Exception as e:
        console.print(f"[red]  peer list error: {e}[/red]")


# ── Outgoing connection request ───────────────────────────────────────────────

def connect_peer():
    peer_name = Prompt.ask("  [cyan]Peer username[/cyan]")

    if peer_name == USERNAME:
        console.print("  [red]That's you.[/red]")
        return

    try:
        r = requests.post(
            f"{BASE_URL}/connect",
            json={"from_user": USERNAME, "to_user": peer_name},
            timeout=5,
        )
        if r.status_code != 200:
            console.print(f"  [red]Server error: {r.text}[/red]")
            return
    except Exception as e:
        console.print(f"  [red]Request error: {e}[/red]")
        return

    try:
        pr = requests.get(f"{BASE_URL}/peer/{peer_name}", timeout=5)
        if pr.status_code != 200:
            console.print("  [red]Peer not found.[/red]")
            return
        peer = parse_peer(pr.json())
    except Exception as e:
        console.print(f"  [red]Peer lookup error: {e}[/red]")
        return

    console.print(f"\n  [yellow]Requesting {peer_name} · hole-punching…[/yellow]")
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

        console.print()
        console.print(
            Panel.fit(
                f"[bold white]📞  {from_name}[/bold white] wants to chat",
                border_style="magenta",
                padding=(0, 2),
            )
        )

        answer = Prompt.ask("  Accept?", choices=["y", "n"], default="y")

        if answer == "n":
            try:
                requests.post(
                    f"{BASE_URL}/decline",
                    json={"from_user": from_name, "to_user": USERNAME},
                    timeout=5,
                )
            except Exception:
                pass
            console.print(f"  [dim]Declined {from_name}.[/dim]")
            continue

        try:
            ar = requests.post(
                f"{BASE_URL}/accept",
                json={"from_user": from_name, "to_user": USERNAME},
                timeout=5,
            )
            data = ar.json()
        except Exception as e:
            console.print(f"  [red]Accept error: {e}[/red]")
            continue

        if "ip" not in data:
            console.print(f"  [red]Accept failed: {data}[/red]")
            continue

        peer = parse_peer(data)
        console.print(f"  [green]Connecting to {from_name}…[/green]")
        hole_punch(peer["ip"], peer["port"])
        time.sleep(0.5)
        chat_window(peer)


# ── Main menu ─────────────────────────────────────────────────────────────────

def draw_menu():
    console.print(
        Panel(
            "  [bold cyan]1[/bold cyan]  Show peers\n"
            "  [bold cyan]2[/bold cyan]  Connect to peer\n"
            "  [bold cyan]3[/bold cyan]  Exit",
            title="[bold white]MiniTail[/bold white]",
            border_style="cyan",
            padding=(0, 2),
            box=box.ROUNDED,
        )
    )


def main():
    global client, USERNAME

    console.clear()
    console.print(
        Panel.fit(
            "[bold cyan]MiniTail[/bold cyan]  [dim]p2p encrypted chat[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )
    console.print()

    USERNAME = Prompt.ask("  [bold cyan]Username[/bold cyan]")

    console.print("\n  [dim]Discovering your endpoint…[/dim]")
    client = discover_endpoint()

    console.print(
        Panel(
            f"  [dim]username[/dim]   [bold white]{client.username}[/bold white]\n"
            f"  [dim]public ip[/dim]  [bold white]{client.ip}[/bold white]\n"
            f"  [dim]port[/dim]       [bold white]{client.port}[/bold white]",
            border_style="dim cyan",
            padding=(0, 1),
        )
    )

    register()

    threading.Thread(target=udp_receiver, daemon=True).start()

    schedule.every(30).seconds.do(heartbeat)
    schedule.every(5).minutes.do(refresh_endpoint)

    while True:
        schedule.run_pending()
        check_incoming_requests()
        draw_menu()

        choice = Prompt.ask("  [bold]›[/bold]", choices=["1", "2", "3"])

        if choice == "1":
            show_peers()
        elif choice == "2":
            connect_peer()
        elif choice == "3":
            console.print("\n  [dim]Goodbye.[/dim]\n")
            break


if __name__ == "__main__":
    main()