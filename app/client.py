#!/usr/bin/env python3
"""
TCP content client.

Requests files from a content server and measures:
  - connect time      (TCP handshake)
  - time to first byte (until the header arrives)
  - total response time (until the last byte arrives)
  - throughput         (Mbps)

  - time to content   (from asking to having the file, INCLUDING failed attempts
                        and the pauses between retries: what a user actually waits)

Results are printed and can be appended to a CSV file for later graphs.

Usage (inside Mininet, from the project folder):
    h1 python3 app/client.py --name h1 --server 10.0.0.11 --file medium.bin --count 3
    h1 python3 app/client.py --name h1 --server 10.0.0.11 --ping
"""

import argparse
import csv
import os
import socket
import time

from protocol import DEFAULT_PORT, send_line, recv_line, recv_exact

CSV_FIELDS = ["timestamp", "start_epoch", "client", "server", "served_by", "file", "bytes",
              "connect_ms", "ttfb_ms", "total_ms", "throughput_mbps",
              "status", "attempts", "time_to_content_ms"]


def open_connection(server, port, timeout):
    """Create a TCP socket and connect it (low-level socket calls)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((server, port))
    return sock


def request_file(server, port, filename, timeout):
    """Download one file and return a dict of measurements."""
    t_start = time.perf_counter()
    sock = open_connection(server, port, timeout)
    try:
        t_connected = time.perf_counter()
        send_line(sock, f"GET {filename}")

        header = recv_line(sock)
        t_first_byte = time.perf_counter()
        parts = header.split()

        if len(parts) == 3 and parts[0] == "OK":
            size, served_by = int(parts[1]), parts[2]
            received = recv_exact(sock, size)
            t_end = time.perf_counter()
        elif parts and parts[0] == "ERR":
            raise RuntimeError(f"server error: {header}")
        else:
            raise RuntimeError(f"unexpected reply: {header!r}")
    finally:
        sock.close()

    total_s = t_end - t_start
    return {
        "served_by": served_by,
        "bytes": received,
        "connect_ms": round((t_connected - t_start) * 1000, 2),
        "ttfb_ms": round((t_first_byte - t_start) * 1000, 2),
        "total_ms": round(total_s * 1000, 2),
        "throughput_mbps": round(received * 8 / total_s / 1e6, 3) if total_s > 0 else 0,
    }


def ping_server(server, port, timeout):
    """Send PING and print the reply (quick health check)."""
    t_start = time.perf_counter()
    sock = open_connection(server, port, timeout)
    try:
        send_line(sock, "PING")
        reply = recv_line(sock)
    finally:
        sock.close()
    rtt_ms = (time.perf_counter() - t_start) * 1000
    print(f"{server}: {reply}  ({rtt_ms:.1f} ms)")


def append_csv(path, row):
    new_file = not os.path.exists(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="TCP content client")
    parser.add_argument("--server", required=True, help="server IP address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--name", default="client", help="client name for logs, e.g. h1")
    parser.add_argument("--file", default="medium.bin", help="file to request")
    parser.add_argument("--count", type=int, default=1, help="number of requests")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between requests")
    parser.add_argument("--retries", type=int, default=2, help="retries per failed request")
    parser.add_argument("--timeout", type=float, default=10.0, help="socket timeout (s)")
    parser.add_argument("--csv", help="append results to this CSV file")
    parser.add_argument("--ping", action="store_true", help="only send a PING health check")
    args = parser.parse_args()

    if args.ping:
        try:
            ping_server(args.server, args.port, args.timeout)
        except (OSError, RuntimeError) as e:
            print(f"{args.server}: no reply ({e})")
        return

    successes, times, rates, waits = 0, [], [], []

    for i in range(1, args.count + 1):
        request_start = time.time()
        t_request = time.perf_counter()
        row = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
               "start_epoch": round(request_start, 3), "client": args.name,
               "server": args.server, "file": args.file}

        for attempt in range(1, args.retries + 2):
            try:
                result = request_file(args.server, args.port, args.file, args.timeout)
                row.update(result, status="ok", attempts=attempt)
                break
            except (OSError, RuntimeError, ValueError) as e:
                # OSError covers timeouts, refused and reset connections
                print(f"[{args.name}] request {i} attempt {attempt} failed: {e}")
                row.update(served_by="-", bytes=0, connect_ms="", ttfb_ms="",
                           total_ms="", throughput_mbps="",
                           status=f"failed: {e}", attempts=attempt)
                if attempt <= args.retries:
                    time.sleep(1)

        row["time_to_content_ms"] = round((time.perf_counter() - t_request) * 1000, 2)

        if row["status"] == "ok":
            successes += 1
            waits.append(row["time_to_content_ms"])
            times.append(row["total_ms"])
            rates.append(row["throughput_mbps"])
            retried = f" after {row['attempts']} attempts, waited {row['time_to_content_ms']} ms" \
                if row["attempts"] > 1 else ""
            print(f"[{args.name}] #{i} {args.file} from {row['served_by']}: "
                  f"{row['bytes']} bytes, response {row['total_ms']} ms, "
                  f"throughput {row['throughput_mbps']} Mbps{retried}")

        if args.csv:
            append_csv(args.csv, row)
        if i < args.count:
            time.sleep(args.interval)

    print(f"[{args.name}] summary: {successes}/{args.count} succeeded", end="")
    if times:
        print(f", avg response {sum(times)/len(times):.1f} ms, "
              f"avg throughput {sum(rates)/len(rates):.2f} Mbps, "
              f"avg time to content {sum(waits)/len(waits):.1f} ms")
    else:
        print()


if __name__ == "__main__":
    main()
