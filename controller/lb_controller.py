"""
SDN content-delivery controller (Ryu, OpenFlow 1.3).

What it does
  1. Virtual IP load balancing: clients connect to VIP 10.0.0.100:9000. On the
     first packet of each TCP connection the controller picks a real server and
     installs NAT flow rules on the client's edge switch:
        client -> VIP     becomes  client -> server   (dst IP/MAC rewritten)
        server -> client  becomes  VIP -> client      (src IP/MAC rewritten)
  2. Server selection policies (set with the LB_POLICY environment variable):
        static        always srv1 (baseline, like having no controller logic)
        round_robin   srv1, srv2, srv3, srv1, ...
        least_loaded  fewest active connections, ties broken by proximity
  3. Socket-SDN integration: every server sends a UDP load report to
     10.0.0.254:5555 each second. The switches pass these to the controller,
     which uses them for least_loaded and for health checking.
  4. Availability: a server whose reports stop for DEAD_AFTER seconds is marked
     DOWN and receives no new clients until it reports again.
  5. Routing without loops: the controller answers every ARP request itself
     (no flooding) and forwards IP traffic along a shortest path (by link delay)
     computed from its network map. When a switch-to-switch link goes down or
     comes back, it clears the dynamic rules so traffic is re-routed.
  6. Congestion awareness: every MONITOR_INTERVAL seconds the controller reads
     each switch's port counters and computes how full every link is. Content
     connections are given their own path, chosen by delay plus a penalty for
     links above UTIL_THRESHOLD, so a busy fast path loses to an idle slow one
     (set PATH_POLICY=delay to switch this off for comparison). Background
     traffic still follows the plain shortest path.

Run from the project folder:
    LB_POLICY=least_loaded PATH_POLICY=congestion ryu-manager controller/lb_controller.py
"""

import csv
import heapq
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import net_config as cfg  # noqa: E402

from ryu.base import app_manager  # noqa: E402
from ryu.controller import ofp_event  # noqa: E402
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls  # noqa: E402
from ryu.lib import hub  # noqa: E402
from ryu.lib.packet import packet, ethernet, arp, ipv4, tcp, udp, ether_types  # noqa: E402
from ryu.ofproto import ofproto_v1_3  # noqa: E402

# Flow rule priorities (higher wins)
PRIO_MISS = 0       # unknown traffic -> controller
PRIO_DROP = 1       # IPv6 noise -> drop
PRIO_ROUTE = 10     # normal forwarding by destination IP
PRIO_PATH = 20      # per-connection path rules on transit switches
PRIO_VIP = 30       # per-connection VIP rewrite rules

# Cookies tag our rules so we can delete them in groups
COOKIE_ROUTE = 0x1
COOKIE_VIP = 0x2

ROUTE_IDLE = 60     # seconds before an unused route rule expires
VIP_IDLE = 15       # seconds before an unused VIP connection rule expires
DEAD_AFTER = 3.0    # seconds without a load report -> server DOWN
STATUS_EVERY = 5    # seconds between status lines in the log

POLICIES = ("static", "round_robin", "least_loaded")
PATH_POLICIES = ("delay", "congestion")

MONITOR_INTERVAL = 2.0       # seconds between port-statistics requests
UTIL_THRESHOLD = 0.7         # a link above 70% utilisation counts as congested
CONGESTION_PENALTY_MS = 100  # extra path cost for crossing a congested link

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISIONS_CSV = os.environ.get("LB_DECISIONS_CSV",
                               os.path.join(PROJECT_DIR, "results", "lb_decisions.csv"))


class ContentLoadBalancer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.policy = os.environ.get("LB_POLICY", "least_loaded").strip()
        if self.policy not in POLICIES:
            raise ValueError(f"LB_POLICY must be one of {POLICIES}, got {self.policy!r}")
        self.path_policy = os.environ.get("PATH_POLICY", "congestion").strip()
        if self.path_policy not in PATH_POLICIES:
            raise ValueError(f"PATH_POLICY must be one of {PATH_POLICIES}, got {self.path_policy!r}")

        self.datapaths = {}                                   # dpid -> datapath
        self.link_up = [True] * len(cfg.LINKS)                # state of each switch link
        self.ports_down = set()                                # (dpid, port) currently down
        self.port_to_link = {}                                # (dpid, port) -> link index
        for i, (a, pa, b, pb, _d, _bw) in enumerate(cfg.LINKS):
            self.port_to_link[(a, pa)] = i
            self.port_to_link[(b, pb)] = i

        # Link monitoring, filled from OpenFlow port statistics
        self.port_tx = {}                                     # (dpid, port) -> (tx_bytes, time)
        self.port_bps = {}                                    # (dpid, port) -> transmit rate, bit/s
        self.link_util = [0.0] * len(cfg.LINKS)               # fraction of capacity in use

        # Server state, filled by UDP load reports
        self.servers = {ip: {"active": 0, "pending": 0, "last": 0.0, "alive": False}
                        for ip in cfg.SERVERS}
        self.rr_index = 0
        self.assignments = {}      # (client_ip, client_port) -> {"server", "time"}

        # Every IP the controller answers ARP for
        self.arp_table = {ip: h[1] for ip, h in cfg.HOSTS.items()}
        self.arp_table[cfg.VIP] = cfg.VIP_MAC
        self.arp_table[cfg.CTRL_IP] = cfg.CTRL_MAC

        os.makedirs(os.path.dirname(DECISIONS_CSV), exist_ok=True)
        self.logger.info("[START] policy=%s  path_policy=%s  VIP=%s:%d  reports on %s:%d/udp",
                         self.policy, self.path_policy, cfg.VIP, cfg.APP_PORT,
                         cfg.CTRL_IP, cfg.REPORT_PORT)
        hub.spawn(self._health_loop)
        hub.spawn(self._monitor_loop)

    # ================================================================
    # OpenFlow helpers
    # ================================================================
    def add_flow(self, dp, priority, match, actions, idle=0, cookie=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, priority=priority,
                                      match=match, instructions=inst, idle_timeout=idle))

    def send_packet(self, dp, actions, data):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=ofp.OFPP_CONTROLLER,
                                        actions=actions, data=data))

    def delete_flows(self, cookie):
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie,
                                          cookie_mask=0xFFFFFFFFFFFFFFFF,
                                          table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                                          out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                          match=parser.OFPMatch()))

    # ================================================================
    # Switch connection and link state
    # ================================================================
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        # Table-miss: send unknown packets to the controller (whole packet)
        self.add_flow(dp, PRIO_MISS, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        # IPv6 is not used in this project; drop its background chatter
        self.add_flow(dp, PRIO_DROP, parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6), [])
        self.logger.info("[SWITCH] s%d connected", dp.id)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        key = (dp.id, msg.desc.port_no)
        idx = self.port_to_link.get(key)
        if idx is None:
            return  # a host port, not a switch-to-switch link
        down = (msg.reason == ofp.OFPPR_DELETE
                or bool(msg.desc.state & ofp.OFPPS_LINK_DOWN)
                or bool(msg.desc.config & ofp.OFPPC_PORT_DOWN))
        # A link is UP only if BOTH of its ends are up. Deciding from whichever
        # end reported last made the link appear to flap DOWN-UP-DOWN.
        if down:
            self.ports_down.add(key)
        else:
            self.ports_down.discard(key)
        a, pa, b, pb, _d, _bw = cfg.LINKS[idx]
        now_up = (a, pa) not in self.ports_down and (b, pb) not in self.ports_down
        if self.link_up[idx] == now_up:
            return
        self.link_up[idx] = now_up
        down = not now_up
        self.logger.info("[LINK] s%d-s%d is %s -> clearing dynamic rules to re-route",
                         a, b, "DOWN" if down else "UP")
        self.delete_flows(COOKIE_ROUTE)
        self.delete_flows(COOKIE_VIP)

    # ================================================================
    # Path computation (Dijkstra on link delay, only links that are up)
    # ================================================================
    def _neighbors(self, dpid, congestion=False):
        for i, (a, pa, b, pb, delay, _bw) in enumerate(cfg.LINKS):
            if not self.link_up[i]:
                continue
            cost = delay
            if congestion and self.link_util[i] >= UTIL_THRESHOLD:
                cost += CONGESTION_PENALTY_MS
            if a == dpid:
                yield b, pa, cost
            elif b == dpid:
                yield a, pb, cost

    def port_between(self, u, v):
        """Port on switch u that leads to neighbouring switch v (over a link that is up)."""
        for i, (a, pa, b, pb, _d, _bw) in enumerate(cfg.LINKS):
            if self.link_up[i] and (a, b) == (u, v):
                return pa
            if self.link_up[i] and (b, a) == (u, v):
                return pb
        return None

    def shortest_path(self, src, dst, congestion=False):
        """Return [(dpid, out_port_to_next_switch), ...] ending with (dst, None), or None."""
        dist, prev = {src: 0}, {}
        queue = [(0, src)]
        while queue:
            d, u = heapq.heappop(queue)
            if u == dst:
                break
            if d > dist.get(u, float("inf")):
                continue
            for v, port, delay in self._neighbors(u, congestion):
                nd = d + delay
                if nd < dist.get(v, float("inf")):
                    dist[v], prev[v] = nd, (u, port)
                    heapq.heappush(queue, (nd, v))
        if dst not in dist:
            return None
        hops, node, out_port = [], dst, None
        while True:
            hops.append((node, out_port))
            if node == src:
                break
            node, out_port = prev[node]
        return list(reversed(hops))

    def install_route(self, dst_ip, from_dpid):
        """Install destination-based rules for dst_ip on every switch from from_dpid to dst."""
        _name, _mac, dst_dpid, dst_port = cfg.HOSTS[dst_ip]
        path = self.shortest_path(from_dpid, dst_dpid)
        if path is None:
            self.logger.warning("[ROUTE] no path from s%d to %s", from_dpid, cfg.host_name(dst_ip))
            return None
        for dpid, out_port in path:
            dp = self.datapaths.get(dpid)
            if dp is None:
                return None
            parser = dp.ofproto_parser
            port = dst_port if out_port is None else out_port
            self.add_flow(dp, PRIO_ROUTE,
                          parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_dst=dst_ip),
                          [parser.OFPActionOutput(port)], idle=ROUTE_IDLE, cookie=COOKIE_ROUTE)
        return path

    # ================================================================
    # Link monitoring (OpenFlow port statistics)
    # ================================================================
    def _monitor_loop(self):
        samples = 0
        while True:
            hub.sleep(MONITOR_INTERVAL)
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            samples += 1
            if samples % 2 == 0:
                self.logger.info("[MONITOR] %s", self._util_summary())

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        for st in ev.msg.body:
            key = (dpid, st.port_no)
            prev = self.port_tx.get(key)
            self.port_tx[key] = (st.tx_bytes, now)
            if prev is not None and now > prev[1]:
                self.port_bps[key] = max(0.0, (st.tx_bytes - prev[0]) * 8 / (now - prev[1]))
        # A link's load is the busier of its two directions
        for i, (a, pa, b, pb, _d, bw) in enumerate(cfg.LINKS):
            busiest = max(self.port_bps.get((a, pa), 0.0), self.port_bps.get((b, pb), 0.0))
            self.link_util[i] = busiest / (bw * 1e6)

    def _util_summary(self):
        parts = []
        for i, (a, _pa, b, _pb, _d, _bw) in enumerate(cfg.LINKS):
            state = "%d%%" % round(self.link_util[i] * 100) if self.link_up[i] else "DOWN"
            parts.append("s%d-s%d=%s" % (a, b, state))
        return " ".join(parts)

    # ================================================================
    # Packet-in dispatcher
    # ================================================================
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self.handle_arp(dp, in_port, eth, pkt.get_protocol(arp.arp))
            return

        ip = pkt.get_protocol(ipv4.ipv4)
        if ip is None:
            return
        t = pkt.get_protocol(tcp.tcp)
        u = pkt.get_protocol(udp.udp)

        if ip.dst == cfg.CTRL_IP:
            if u is not None and u.dst_port == cfg.REPORT_PORT:
                self.handle_report(ip.src, pkt)
            return

        if ip.dst == cfg.VIP:
            if t is not None and t.dst_port == cfg.APP_PORT:
                self.handle_vip_request(ip, t, msg.data)
            return

        if (t is not None and ip.src in self.servers and t.src_port == cfg.APP_PORT
                and (ip.dst, t.dst_port) in self.assignments):
            self.handle_vip_reply(ip, t, msg.data)
            return

        self.handle_route(dp.id, ip, msg.data)

    # ================================================================
    # ARP: answer everything ourselves, never flood
    # ================================================================
    def handle_arp(self, dp, in_port, eth, a):
        if a is None or a.opcode != arp.ARP_REQUEST:
            return
        mac = self.arp_table.get(a.dst_ip)
        if mac is None:
            return
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(dst=eth.src, src=mac,
                                             ethertype=ether_types.ETH_TYPE_ARP))
        reply.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=mac, src_ip=a.dst_ip,
                                   dst_mac=a.src_mac, dst_ip=a.src_ip))
        reply.serialize()
        self.send_packet(dp, [dp.ofproto_parser.OFPActionOutput(in_port)], reply.data)

    # ================================================================
    # Normal IP forwarding between real hosts
    # ================================================================
    def handle_route(self, from_dpid, ip, data):
        if ip.dst not in cfg.HOSTS:
            return
        if self.install_route(ip.dst, from_dpid) is None:
            return
        _n, _m, dst_dpid, dst_port = cfg.HOSTS[ip.dst]
        dp = self.datapaths[dst_dpid]
        self.send_packet(dp, [dp.ofproto_parser.OFPActionOutput(dst_port)], data)

    # ================================================================
    # Server load reports (UDP socket from the servers)
    # ================================================================
    def handle_report(self, src_ip, pkt):
        state = self.servers.get(src_ip)
        payload = pkt.protocols[-1]
        if state is None or not isinstance(payload, (bytes, bytearray)):
            return
        try:
            report = json.loads(payload.decode())
        except ValueError:
            return
        state["active"] = int(report.get("active", 0))
        state["pending"] = 0
        state["last"] = time.time()
        if not state["alive"]:
            state["alive"] = True
            self.logger.info("[HEALTH] %s is UP (load reports received)", cfg.host_name(src_ip))

    def _health_loop(self):
        ticks = 0
        while True:
            hub.sleep(1)
            ticks += 1
            now = time.time()
            for ip, st in self.servers.items():
                if st["alive"] and now - st["last"] >= DEAD_AFTER:
                    st["alive"] = False
                    self.logger.info("[HEALTH] %s is DOWN (no report for %.0fs)",
                                     cfg.host_name(ip), DEAD_AFTER)
            if ticks % STATUS_EVERY == 0:
                self.logger.info("[STATUS] %s", self._load_summary())

    def _load_summary(self):
        parts = []
        for ip, st in self.servers.items():
            if st["alive"]:
                parts.append(f"{cfg.host_name(ip)}=UP({st['active'] + st['pending']})")
            else:
                parts.append(f"{cfg.host_name(ip)}=DOWN")
        return " ".join(parts)

    # ================================================================
    # Server selection
    # ================================================================
    def choose_server(self):
        if self.policy == "static":
            return cfg.SERVERS[0]
        now = time.time()
        alive = [s for s in cfg.SERVERS if now - self.servers[s]["last"] < DEAD_AFTER]
        if not alive:
            return None
        if self.policy == "round_robin":
            server = alive[self.rr_index % len(alive)]
            self.rr_index += 1
            return server
        # least_loaded: reported active connections + clients sent since the last
        # report (so a burst of new clients is not all sent to the same server)
        return min(alive, key=lambda s: (self.servers[s]["active"] + self.servers[s]["pending"],
                                         cfg.SERVER_DELAY_MS[s]))

    def log_decision(self, client_ip, client_port, server, path):
        load = self._load_summary()
        route = "-".join("s%d" % dpid for dpid, _p in path)
        self.logger.info("[LB] %s:%d -> %s via %s  (policy=%s | %s)", cfg.host_name(client_ip),
                         client_port, cfg.host_name(server), route, self.policy, load)
        new_file = not os.path.exists(DECISIONS_CSV)
        try:
            with open(DECISIONS_CSV, "a", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(["timestamp", "policy", "path_policy", "client", "client_port",
                                "server", "path", "loads", "link_util"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), self.policy, self.path_policy,
                            cfg.host_name(client_ip), client_port, cfg.host_name(server),
                            route, load, self._util_summary()])
        except OSError as e:
            self.logger.warning("could not write %s: %s", DECISIONS_CSV, e)

    # ================================================================
    # Virtual IP handling
    # ================================================================
    def handle_vip_request(self, ip, t, data):
        client_ip, client_port = ip.src, t.src_port
        if client_ip not in cfg.HOSTS:
            return
        key = (client_ip, client_port)
        entry = self.assignments.get(key)
        new = entry is None
        if new:
            if not t.bits & tcp.TCP_SYN:
                return  # middle of a connection we never saw start; ignore
            server = self.choose_server()
            if server is None:
                self.logger.warning("[LB] %s:%d rejected: no server is UP",
                                    cfg.host_name(client_ip), client_port)
                return
            self.servers[server]["pending"] += 1
            self._forget_old_assignments()
            self.assignments[key] = {"server": server, "time": time.time()}
        else:
            server = entry["server"]

        path = self.install_vip_flows(client_ip, client_port, server)
        if path is None:
            return
        if new:
            self.log_decision(client_ip, client_port, server, path)
        # Deliver this first packet straight to the server, already rewritten
        _n, s_mac, s_dpid, s_port = cfg.HOSTS[server]
        dp = self.datapaths[s_dpid]
        parser = dp.ofproto_parser
        self.send_packet(dp, [parser.OFPActionSetField(eth_dst=s_mac),
                              parser.OFPActionSetField(ipv4_dst=server),
                              parser.OFPActionOutput(s_port)], data)

    def handle_vip_reply(self, ip, t, data):
        """A server reply reached the controller (e.g. rules were cleared after a link change)."""
        client_ip, client_port = ip.dst, t.dst_port
        server = self.assignments[(client_ip, client_port)]["server"]
        if server != ip.src:
            return
        if self.install_vip_flows(client_ip, client_port, server) is None:
            return
        _n, _m, c_dpid, c_port = cfg.HOSTS[client_ip]
        dp = self.datapaths[c_dpid]
        parser = dp.ofproto_parser
        self.send_packet(dp, [parser.OFPActionSetField(eth_src=cfg.VIP_MAC),
                              parser.OFPActionSetField(ipv4_src=cfg.VIP),
                              parser.OFPActionOutput(c_port)], data)

    def install_vip_flows(self, client_ip, client_port, server):
        """
        Give this one connection its own path: per-connection rules on every transit
        switch in both directions, plus the two address-rewrite rules on the client's
        edge switch. Returns the path, or None if there is none.
        """
        _cn, _cm, c_dpid, c_port = cfg.HOSTS[client_ip]
        _sn, s_mac, s_dpid, s_port = cfg.HOSTS[server]

        path = self.shortest_path(c_dpid, s_dpid,
                                  congestion=(self.path_policy == "congestion"))
        if path is None:
            self.logger.warning("[ROUTE] no path from %s to %s",
                                cfg.host_name(client_ip), cfg.host_name(server))
            return None
        if any(dpid not in self.datapaths for dpid, _p in path):
            return None

        fwd = dict(eth_type=ether_types.ETH_TYPE_IP, ip_proto=6, ipv4_src=client_ip,
                   ipv4_dst=server, tcp_src=client_port, tcp_dst=cfg.APP_PORT)
        rev = dict(eth_type=ether_types.ETH_TYPE_IP, ip_proto=6, ipv4_src=server,
                   ipv4_dst=client_ip, tcp_src=cfg.APP_PORT, tcp_dst=client_port)
        for i in range(1, len(path)):
            dpid, out = path[i]
            dp = self.datapaths[dpid]
            parser = dp.ofproto_parser
            toward_server = s_port if out is None else out
            toward_client = self.port_between(dpid, path[i - 1][0])
            self.add_flow(dp, PRIO_PATH, parser.OFPMatch(**fwd),
                          [parser.OFPActionOutput(toward_server)], idle=VIP_IDLE, cookie=COOKIE_VIP)
            self.add_flow(dp, PRIO_PATH, parser.OFPMatch(**rev),
                          [parser.OFPActionOutput(toward_client)], idle=VIP_IDLE, cookie=COOKIE_VIP)

        first_hop = path[0][1]
        out_port = s_port if first_hop is None else first_hop

        dp = self.datapaths[c_dpid]
        parser = dp.ofproto_parser
        # client -> VIP  ==>  client -> server
        self.add_flow(dp, PRIO_VIP,
                      parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=6,
                                      ipv4_src=client_ip, ipv4_dst=cfg.VIP,
                                      tcp_src=client_port, tcp_dst=cfg.APP_PORT),
                      [parser.OFPActionSetField(eth_dst=s_mac),
                       parser.OFPActionSetField(ipv4_dst=server),
                       parser.OFPActionOutput(out_port)],
                      idle=VIP_IDLE, cookie=COOKIE_VIP)
        # server -> client  ==>  VIP -> client
        self.add_flow(dp, PRIO_VIP,
                      parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=6,
                                      ipv4_src=server, ipv4_dst=client_ip,
                                      tcp_src=cfg.APP_PORT, tcp_dst=client_port),
                      [parser.OFPActionSetField(eth_src=cfg.VIP_MAC),
                       parser.OFPActionSetField(ipv4_src=cfg.VIP),
                       parser.OFPActionOutput(c_port)],
                      idle=VIP_IDLE, cookie=COOKIE_VIP)
        return path

    def _forget_old_assignments(self, max_age=600):
        if len(self.assignments) < 500:
            return
        now = time.time()
        for key in [k for k, v in self.assignments.items() if now - v["time"] > max_age]:
            del self.assignments[key]
