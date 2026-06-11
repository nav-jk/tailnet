# stun_client.py

import os
import socket
import struct

MAGIC_COOKIE = 0x2112A442

DEFAULT_STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 3478),
    ("stun2.l.google.com", 19302),
    ("stun3.l.google.com", 3478),
    ("stun4.l.google.com", 19302),
]


def discover_endpoint(
    sock: socket.socket,
    stun_server: tuple[str, int] | None = None,
) -> tuple[str, int]:
    """
    Discover the public IP and port of an existing UDP socket.

    Args:
        sock: Existing UDP socket.
        stun_server: Optional STUN server (host, port).

    Returns:
        (public_ip, public_port)
    """

    if stun_server is None:
        stun_server = DEFAULT_STUN_SERVERS[0]

    transaction_id = os.urandom(12)

    request = (
        struct.pack(
            "!HHI",
            0x0001,
            0,
            MAGIC_COOKIE,
        )
        + transaction_id
    )

    sock.sendto(
        request,
        stun_server,
    )

    data, _ = sock.recvfrom(2048)

    msg_type, _, _ = struct.unpack(
        "!HHI",
        data[:8]
    )

    if msg_type != 0x0101:
        raise RuntimeError(
            f"Unexpected STUN response: {hex(msg_type)}"
        )

    offset = 20

    while offset < len(data):

        attr_type, attr_len = struct.unpack(
            "!HH",
            data[offset:offset + 4]
        )

        value = data[
            offset + 4:
            offset + 4 + attr_len
        ]

        if attr_type == 0x0020:  # XOR-MAPPED-ADDRESS

            family = value[1]

            if family != 0x01:
                raise RuntimeError(
                    "IPv6 STUN responses are not supported yet"
                )

            xor_port = struct.unpack(
                "!H",
                value[2:4]
            )[0]

            port = xor_port ^ (MAGIC_COOKIE >> 16)

            cookie_bytes = struct.pack(
                "!I",
                MAGIC_COOKIE
            )

            ip_bytes = bytes(
                value[4 + i] ^ cookie_bytes[i]
                for i in range(4)
            )

            ip = socket.inet_ntoa(ip_bytes)

            return ip, port # return statement here!!!

        offset += 4 + attr_len

        if attr_len % 4:
            offset += 4 - (attr_len % 4)

    raise RuntimeError(
        "XOR-MAPPED-ADDRESS not found"
    )


def discover_endpoint_with_fallback(
    sock: socket.socket,
) -> tuple[str, int]:
    """
    Try multiple STUN servers until one succeeds.
    """

    last_error = None

    for server in DEFAULT_STUN_SERVERS:

        try:
            return discover_endpoint(
                sock,
                stun_server=server
            )

        except Exception as e:
            last_error = e

    raise RuntimeError(
        f"All STUN servers failed: {last_error}"
    )