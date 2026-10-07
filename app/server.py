#!/usr/bin/env python3
"""
Multithreaded TCP content server.

Each incoming connection is handled in its own thread, so many clients
can download at the same time.

Usage (inside Mininet, from the project folder):
    srv1 python3 app/server.py --name srv1 > results/srv1.log 2>&1 &
"""

import argparse
import json
import os
import socket
import threading
import time

from protocol import (DEFAULT_PORT, CHUNK_SIZE, ProtocolError,
                      send_line, recv_line)

REPORT_TO = "10.0.0.254"   # controller address for load reports
REPORT_PORT = 5555         # UDP port for load reports
REPORT_INTERVAL = 1.0      # seconds between reports

CLIENT_TIMEOUT = 10  # seconds to wait for a client's request before giving up


class ContentServer:
    def __init__(self, name, host, port, content_dir, report_to=None,
                 report_port=REPORT_PORT, report_interval=REPORT_INTERVAL):
        self.name = name
        self.report_to = report_to
        self.report_port = report_port
        self.report_interval = report_interval
        self.host = host
        self.port = port
        self.content_dir = os.path.abspath(content_dir)

        # Load statistics (shared between threads, so protected by a lock).
        # These will later be reported to the SDN controller.
        self.lock = threading.Lock()
        self.active = 0          # connections being served right now
        self.total = 0           # connections served since start
        self.bytes_sent = 0

    # ------------------------------------------------------------------
    def log(self, msg):
        print(f"[{time.strftime('%H:%M:%S')}] [{self.name}] {msg}", flush=True)

    # ------------------------------------------------------------------
    def start(self):
        """Create the listening socket and accept clients forever."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Allow restarting the server immediately without "Address already in use"
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(64)
        # Wake up every second so Ctrl+C can stop the server cleanly
        listener.settimeout(1.0)

        self.log(f"listening on {self.host}:{self.port}, serving files from {self.content_dir}")
        if self.report_to:
            threading.Thread(target=self.report_loop, daemon=True).start()
            self.log(f"sending load reports to {self.report_to}:{self.report_port}/udp "
                     f"every {self.report_interval}s")
        try:
            while True:
                try:
                    conn, addr = listener.accept()
                except socket.timeout:
                    continue
                worker = threading.Thread(target=self.handle_client,
                                          args=(conn, addr), daemon=True)
                worker.start()
        except KeyboardInterrupt:
            self.log("shutting down")
        finally:
            listener.close()

    # ------------------------------------------------------------------
    def handle_client(self, conn, addr):
        """Serve one client connection (runs in its own thread)."""
        client = f"{addr[0]}:{addr[1]}"
        with self.lock:
            self.active += 1
            self.total += 1
            active_now = self.active
        self.log(f"connection from {client} (active={active_now})")

        try:
            conn.settimeout(CLIENT_TIMEOUT)
            request = recv_line(conn)
            parts = request.split()

            if len(parts) == 1 and parts[0].upper() == "PING":
                with self.lock:
                    active_now = self.active
                send_line(conn, f"PONG {self.name} {active_now}")

            elif len(parts) == 2 and parts[0].upper() == "GET":
                self.send_file(conn, parts[1], client)

            else:
                send_line(conn, "ERR 400 bad request")
                self.log(f"bad request from {client}: {request!r}")

        except socket.timeout:
            self.log(f"timeout waiting for request from {client}")
        except ProtocolError as e:
            self.log(f"protocol error from {client}: {e}")
            self._try_send(conn, "ERR 400 bad request")
        except ConnectionError as e:
            # includes ConnectionResetError and BrokenPipeError (client left early)
            self.log(f"connection to {client} lost: {e}")
        except Exception as e:
            self.log(f"unexpected error with {client}: {e}")
            self._try_send(conn, "ERR 500 server error")
        finally:
            conn.close()
            with self.lock:
                self.active -= 1

    # ------------------------------------------------------------------
    def send_file(self, conn, filename, client):
        """Send the header, then the file contents in chunks."""
        # Security: only allow plain file names, never paths like ../../etc/passwd
        safe_name = os.path.basename(filename)
        path = os.path.join(self.content_dir, safe_name)
        if safe_name != filename or not os.path.isfile(path):
            send_line(conn, "ERR 404 file not found")
            self.log(f"{client} requested missing file {filename!r}")
            return

        size = os.path.getsize(path)
        start = time.perf_counter()
        send_line(conn, f"OK {size} {self.name}")

        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                conn.sendall(chunk)   # sendall keeps sending until the whole chunk is out
                sent += len(chunk)

        elapsed = time.perf_counter() - start
        with self.lock:
            self.bytes_sent += sent
        self.log(f"sent {safe_name} ({sent} bytes) to {client} in {elapsed:.3f}s")

    # ------------------------------------------------------------------
    def report_loop(self):
        """Send this server's load to the SDN controller over UDP, forever."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while True:
            with self.lock:
                report = {"name": self.name, "active": self.active,
                          "total": self.total, "bytes_sent": self.bytes_sent}
            try:
                sock.sendto(json.dumps(report).encode(), (self.report_to, self.report_port))
            except OSError:
                pass
            time.sleep(self.report_interval)

    # ------------------------------------------------------------------
    @staticmethod
    def _try_send(conn, text):
        try:
            send_line(conn, text)
        except OSError:
            pass


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="TCP content server")
    parser.add_argument("--name", required=True, help="server name, e.g. srv1")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--content-dir", default=os.path.join(here, "content"))
    parser.add_argument("--report-to", default=REPORT_TO)
    parser.add_argument("--report-port", type=int, default=REPORT_PORT)
    parser.add_argument("--report-interval", type=float, default=REPORT_INTERVAL)
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args()

    ContentServer(args.name, args.host, args.port, args.content_dir,
                  report_to=None if args.no_report else args.report_to,
                  report_port=args.report_port,
                  report_interval=args.report_interval).start()


if __name__ == "__main__":
    main()
