"""
Shared protocol definitions for the SDN Content Delivery application.

Application protocol (over TCP): a one-line text header, optionally followed by binary data.

  Client -> Server
      GET <filename>\\n                 request a file
      PING\\n                           health check

  Server -> Client
      OK <size> <server_name>\\n        followed by exactly <size> bytes of file data
      PONG <server_name> <active>\\n    reply to PING (active = current connections)
      ERR <code> <message>\\n           error

  Error codes: 400 = bad request, 404 = file not found, 500 = server error
"""

DEFAULT_PORT = 9000          # TCP port every content server listens on
CHUNK_SIZE = 64 * 1024       # bytes read/sent per socket call for file data
MAX_HEADER = 1024            # longest header line we accept (protects the server)
ENCODING = "utf-8"


class ProtocolError(Exception):
    """Raised when the other side sends something that breaks the protocol."""


def send_line(sock, text):
    """Send one header line (adds the newline)."""
    sock.sendall((text + "\n").encode(ENCODING))


def recv_line(sock, max_len=MAX_HEADER):
    """
    Read one header line, byte by byte, up to the newline.

    We read one byte at a time on purpose: reading a bigger block could
    accidentally swallow the start of the file data that follows the header.
    """
    buf = bytearray()
    while True:
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("connection closed before a full header line was received")
        if byte == b"\n":
            break
        buf += byte
        if len(buf) > max_len:
            raise ProtocolError("header line too long")
    try:
        return buf.decode(ENCODING).strip()
    except UnicodeDecodeError:
        raise ProtocolError("header is not valid text")


def recv_exact(sock, n):
    """
    Receive exactly n bytes of data and return how many were received.

    TCP is a byte stream: one recv() can return fewer bytes than asked for,
    so we keep calling recv() until everything has arrived. The data itself is
    discarded because the client only needs to measure the transfer.
    """
    received = 0
    while received < n:
        data = sock.recv(min(CHUNK_SIZE, n - received))
        if not data:
            raise ConnectionError(f"connection closed after {received}/{n} bytes")
        received += len(data)
    return received
