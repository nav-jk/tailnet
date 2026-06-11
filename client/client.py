import time
import threading
from dataclasses import dataclass
import requests
import schedule
import socket
from rich.table import Table
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from stun_client import discover_endpoint_with_fallback

BASE_URL = "https://server-try.fastapicloud.dev"

USERNAME = "nav"

console = Console()

# ── Global state ─────────────────────────────────────────────────────────────

@dataclass
class Client:
    username: str
    ip: str
    port: int

client: Client | None = None

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", 0))

incoming_messages: list[tuple[tuple, str]] = []
_msg_lock = threading.Lock()

active_peer_addr: tuple | None = None


# ── STUN / Registration ───────────────────────────────────────────────────────

def discover_endpoint() -> Client:
    external_ip, external_port = discover_endpoint_with_fallback(sock)
    console.print(f"[dim][STUN] {external_ip}:{external_port}[/dim]")
    return Client(username=USERNAME, ip=external_ip, port=external_port)


def register():
    try:
        r = requests.post(
            f"{BASE_URL}/register",
            json={"username": client.username, "ip": client.ip, "port": client.port},
            timeout=5,
        )
        console.print(f"[dim][REGISTER] {r.status_code}[/dim]")
    except Exception as e:
        console.print(f"[red][REGISTER ERROR] {e}[/red]")


def refresh_endpoint():
    global client
    try:
        new = discover_endpoint()
        if client is None or new.ip != client.ip or new.port != client.port:
            console.print(f"[yellow][UPDATE] {new.ip}:{new.port}[/yellow]")
            client = new
            register()
        else:
            console.print("[dim][UPDATE] Endpoint unchanged[/dim]")
    except Exception as e:
        console.print(f"[red][REFRESH ERROR] {e}[/red]")


def heartbeat():
    try:
        r = requests.post(
            f"{BASE_URL}/register",
            json={"username": client.username, "ip": client.ip, "port": client.port},
            timeout=5,
        )
        console.print(f"[dim][HEARTBEAT] {r.status_code}[/dim]")
    except Exception as e:
        console.print(f"[red][HEARTBEAT ERROR] {e}[/red]")


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
        except Exception as e:
            console.print(f"[red][UDP ERROR] {e}[/red]")


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
    global active_peer_addr

    peer_ip   = str(peer["ip"])
    peer_port = int(peer["port"])
    peer_name = peer["username"]

    active_peer_addr = (peer_ip, peer_port)

    console.clear()
    console.print(
        Panel.fit(
            f"Connected to [green]{peer_name}[/green]  ({peer_ip}:{peer_port})",
            title="MiniTail Chat",
        )
    )
    console.print("[yellow]Type messages below. /exit to leave.[/yellow]\n")

    try:
        sock.sendto(b"HELLO", (peer_ip, peer_port))
    except Exception:
        pass

    while True:
        with _msg_lock:
            while incoming_messages:
                addr, msg = incoming_messages.pop(0)
                console.print(
                    f"[green]{peer_name}[/green] [dim]({addr[0]}:{addr[1]})[/dim]: {msg}"
                )

        try:
            msg = Prompt.ask("[bold cyan]You[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            break

        if msg.strip() == "/exit":
            break

        try:
            sock.sendto(msg.encode(), (peer_ip, peer_port))
        except Exception as e:
            console.print(f"[red]Send error: {e}[/red]")

    active_peer_addr = None
    console.print("[yellow]Left chat.[/yellow]")


# ── Peer UI ───────────────────────────────────────────────────────────────────

def show_peers():
    try:
        r = requests.get(f"{BASE_URL}/peers", timeout=5)
        peers = [p for p in r.json() if p != USERNAME]
        table = Table(title="Online Peers")
        table.add_column("Username", style="cyan")
        for p in peers:
            table.add_row(p)
        console.print(table)
    except Exception as e:
        console.print(f"[red]Peer list error: {e}[/red]")


# ── Phase 7: Outgoing connection request ──────────────────────────────────────

def connect_peer():
    peer_name = Prompt.ask("Enter peer username")

    try:
        # Tell the server we want to connect
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

    console.print(
        f"[yellow]Request sent to [bold]{peer_name}[/bold]. "
        "Waiting for them to accept…  (Ctrl-C to cancel)[/yellow]"
    )

    # Poll until the peer accepts (their /accept returns their endpoint)
    # We detect acceptance by checking if our pending request disappears
    # AND we receive a HELLO from them, OR we can poll a dedicated status.
    #
    # Simpler approach: poll /peer/{peer_name} (already registered) and
    # just start hole-punching once we confirm the peer is online.
    # The peer's client will call /accept which gives them our endpoint;
    # we already know theirs from /peer.
    try:
        pr = requests.get(f"{BASE_URL}/peer/{peer_name}", timeout=5)
        if pr.status_code != 200:
            console.print("[red]Peer not found.[/red]")
            return
        peer = pr.json()
        peer["ip"]   = str(peer["ip"])
        peer["port"] = int(peer["port"])
    except Exception as e:
        console.print(f"[red]Peer lookup error: {e}[/red]")
        return

    console.print(
        "[cyan]Initiating hole punch. "
        "Chat will open once the peer accepts.[/cyan]"
    )

    # Hole punch from our side immediately
    hole_punch(peer["ip"], peer["port"])

    # Wait briefly for the peer to accept and start punching back
    time.sleep(1)

    chat_window(peer)


# ── Phase 7: Incoming request polling ────────────────────────────────────────

def check_incoming_requests():
    """
    Called from the main loop every cycle.
    If someone has requested to connect, prompt the user to accept/decline.
    """
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
            f"\n[bold magenta]📞 Incoming connection request from "
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

        # Accept — server returns initiator's current endpoint
        try:
            ar = requests.post(
                f"{BASE_URL}/accept",
                json={"from_user": from_name, "to_user": USERNAME},
                timeout=5,
            )
            peer = ar.json()
            peer["ip"]   = str(peer["ip"])
            peer["port"] = int(peer["port"])
        except Exception as e:
            console.print(f"[red]Accept error: {e}[/red]")
            continue

        console.print(f"[green]Accepted! Connecting to {from_name}…[/green]")

        # Both sides hole-punch simultaneously
        hole_punch(peer["ip"], peer["port"])
        time.sleep(0.5)

        chat_window(peer)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global client

    client = discover_endpoint()

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

        # Check for incoming requests on every loop iteration
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