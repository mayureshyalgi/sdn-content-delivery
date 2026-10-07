#!/usr/bin/env python3
"""
SDN Content Delivery Project - Mininet Topology

                 +---- s2 ----+        fast path  (2 ms per link)
   h1 -+         |            |         +- srv1  (1 ms)
   h2 -+-- s1 ---+            +-- s4 ---+- srv2  (5 ms)
   h3 -+         |            |         +- srv3  (10 ms)
                 +---- s3 ----+        slow path (10 ms per link)
   clients                                servers

Run (with the Ryu controller already running in another terminal):
    sudo python3 topo.py
"""

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

# ---------------------------------------------------------------
# Settings (shared with the controller - keep these in sync!)
# ---------------------------------------------------------------
CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653
LINK_BW = 10                # Mbps, host links (clients and servers)
CORE_BW = 20                # Mbps, switch-to-switch links

CLIENTS = {                 # name: (IP, MAC)
    "h1": ("10.0.0.1", "00:00:00:00:00:01"),
    "h2": ("10.0.0.2", "00:00:00:00:00:02"),
    "h3": ("10.0.0.3", "00:00:00:00:00:03"),
}

SERVERS = {                 # name: (IP, MAC, delay to s4)
    "srv1": ("10.0.0.11", "00:00:00:00:00:11", "1ms"),
    "srv2": ("10.0.0.12", "00:00:00:00:00:12", "5ms"),
    "srv3": ("10.0.0.13", "00:00:00:00:00:13", "10ms"),
}

# ---------------------------------------------------------------
# Port map (fixed so the controller always knows which port is which)
#   s1: 1=h1  2=h2  3=h3  4=s2  5=s3
#   s2: 1=s1  2=s4
#   s3: 1=s1  2=s4
#   s4: 1=srv1  2=srv2  3=srv3  4=s2  5=s3
# ---------------------------------------------------------------


class ContentTopo(Topo):
    def build(self):
        # Switches (fixed DPIDs so the controller can identify them)
        s1 = self.addSwitch("s1", dpid="0000000000000001", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", dpid="0000000000000002", protocols="OpenFlow13")
        s3 = self.addSwitch("s3", dpid="0000000000000003", protocols="OpenFlow13")
        s4 = self.addSwitch("s4", dpid="0000000000000004", protocols="OpenFlow13")

        # Clients -> s1 (ports 1, 2, 3)
        for port, (name, (ip, mac)) in enumerate(CLIENTS.items(), start=1):
            host = self.addHost(name, ip=ip + "/24", mac=mac)
            self.addLink(host, s1, port2=port, bw=LINK_BW, delay="1ms")

        # Servers -> s4 (ports 1, 2, 3)
        for port, (name, (ip, mac, delay)) in enumerate(SERVERS.items(), start=1):
            host = self.addHost(name, ip=ip + "/24", mac=mac)
            self.addLink(host, s4, port2=port, bw=LINK_BW, delay=delay)

        # Fast path: s1 -> s2 -> s4
        self.addLink(s1, s2, port1=4, port2=1, bw=CORE_BW, delay="2ms")
        self.addLink(s2, s4, port1=2, port2=4, bw=CORE_BW, delay="2ms")

        # Slow backup path: s1 -> s3 -> s4
        self.addLink(s1, s3, port1=5, port2=1, bw=CORE_BW, delay="10ms")
        self.addLink(s3, s4, port1=2, port2=5, bw=CORE_BW, delay="10ms")


def run():
    net = Mininet(
        topo=ContentTopo(),
        switch=OVSSwitch,
        link=TCLink,
        controller=None,
        autoSetMacs=False,
    )
    net.addController("c0", controller=RemoteController,
                      ip=CONTROLLER_IP, port=CONTROLLER_PORT)

    net.start()
    info("\n*** Network is up. Clients: h1 h2 h3 | Servers: srv1 srv2 srv3\n")
    info("*** Give the controller a few seconds before testing.\n\n")
    CLI(net)
    net.stop()


# Allows: sudo mn --custom topo.py --topo contenttopo ...
topos = {"contenttopo": ContentTopo}

if __name__ == "__main__":
    setLogLevel("info")
    run()
