"""
Network map used by the SDN controller.

Keep this in sync with topology/topo.py (IPs, MACs, switch IDs, port numbers).

                 +---- s2 ----+        fast path  (2 ms per link, 20 Mbps)
   h1 -+         |            |         +- srv1  (1 ms)
   h2 -+-- s1 ---+            +-- s4 ---+- srv2  (5 ms)
   h3 -+         |            |         +- srv3  (10 ms)
                 +---- s3 ----+        slow path (10 ms per link, 20 Mbps)
"""

# Virtual service address: clients only ever connect to this IP.
# No host owns it; the controller answers ARP for it and rewrites traffic.
VIP = "10.0.0.100"
VIP_MAC = "00:00:00:00:00:64"

# Address that servers send UDP load reports to. The switches hand these
# packets to the controller (it does not belong to any host either).
CTRL_IP = "10.0.0.254"
CTRL_MAC = "00:00:00:00:00:fe"

APP_PORT = 9000      # TCP port of the content service
REPORT_PORT = 5555   # UDP port for server load reports

# ip: (name, mac, switch dpid, switch port)
HOSTS = {
    "10.0.0.1":  ("h1",   "00:00:00:00:00:01", 1, 1),
    "10.0.0.2":  ("h2",   "00:00:00:00:00:02", 1, 2),
    "10.0.0.3":  ("h3",   "00:00:00:00:00:03", 1, 3),
    "10.0.0.11": ("srv1", "00:00:00:00:00:11", 4, 1),
    "10.0.0.12": ("srv2", "00:00:00:00:00:12", 4, 2),
    "10.0.0.13": ("srv3", "00:00:00:00:00:13", 4, 3),
    # Background-traffic hosts (not content servers): used to congest the core
    "10.0.0.21": ("sink", "00:00:00:00:00:21", 1, 6),
    "10.0.0.22": ("gen",  "00:00:00:00:00:22", 4, 6),
}

# Content servers, in order (static policy always uses the first one)
SERVERS = ["10.0.0.11", "10.0.0.12", "10.0.0.13"]

# One-way delay from s4 to each server (used as the proximity tie-breaker)
SERVER_DELAY_MS = {"10.0.0.11": 1, "10.0.0.12": 5, "10.0.0.13": 10}

# Switch-to-switch links: (dpid_a, port_a, dpid_b, port_b, delay_ms, bandwidth_mbps)
LINKS = [
    (1, 4, 2, 1, 2, 20),    # s1 - s2  fast path
    (2, 2, 4, 4, 2, 20),    # s2 - s4  fast path
    (1, 5, 3, 1, 10, 20),   # s1 - s3  slow path
    (3, 2, 4, 5, 10, 20),   # s3 - s4  slow path
]


def host_name(ip):
    return HOSTS[ip][0] if ip in HOSTS else ip
