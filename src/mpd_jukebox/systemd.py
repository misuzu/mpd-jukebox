"""systemd service notifications (`Type=notify`), without libsystemd."""

import os
import socket
from collections.abc import Mapping


def notify(message: str, env: Mapping[str, str] = os.environ) -> bool:
    """Send a message like `READY=1` to `$NOTIFY_SOCKET`. Returns False when
    not running under systemd, or when the message cannot be sent."""
    address = env.get('NOTIFY_SOCKET')
    if not address:
        return False
    if address.startswith('@'):
        address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError:
        return False
    return True
