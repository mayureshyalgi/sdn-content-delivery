"""
SDN content-delivery controller (Ryu, OpenFlow 1.3).

  1. Virtual IP load balancing: clients connect to VIP 10.0.0.100:9000. On the
     first packet of each TCP connection the controller picks a real server and
     installs NAT flow rules on the client's edge switch.
  2. Policies (LB_POLICY env var): static | round_robin | least_loaded
  3. Socket-SDN integration: servers send UDP load reports to 10.0.0.254:5555.
  4. Availability: a server whose reports stop is marked DOWN.
  5. Loop-free routing: the controller answers ARP itself and forwards along a
     shortest path; when a link changes state it clears rules to re-route.

Run from the project folder:
    LB_POLICY=round_robin ryu-manager controller/lb_controller.py
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

PRIO_MISS = 0       # unknown traffic -> controller
PRIO_DROP = 1       # IPv6 noise -> drop
PRIO_ROUTE = 10     # normal forwarding by destination IP
PRIO_VIP = 30       # per-connection VIP rewrite rules

COOKIE_ROUTE = 0x1
COOKIE_VIP = 0x2

ROUTE_IDLE = 60     # seconds before an unused route rule expires
VIP_IDLE = 15       # seconds before an unused VIP connection rule expires
DEAD_AFTER = 3.0    # seconds without a load report -> server DOWN
STATUS_EVERY = 5    # seconds between status lines in the log

POLICIES = ("static", "round_robin", "least_loaded")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISIONS_CSV = os.path.join(PROJECT_DIR, "results", "controller_decisions.csv")


class ContentLoadBalancer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.policy = os.environ.get("LB_POLICY", "least_loaded").strip()
        if self.policy not in POLICIES:
            raise ValueError("LB_POLICY must be one of %s, got %r" % (POLICIES, self.policy))

        self.datapaths = {}
        self.link_up = [True] * len(cfg.LINKS)
        self.ports_down = set()
        self.port_to_link = {}
        for i, (a, pa, b, pb, _d, _bw) in enumerate(cfg.LINKS):
            self.port_to_link[(a, pa)] = i
            self.port_to_link[(b, pb)] = i

        self.servers = {ip: {"active": 0, "pending": 0, "last": 0.0, "alive": False}
                        for ip in cfg.SERVERS}
        self.rr_index = 0
        self.assignments = {}

        self.arp_table = {ip: h[1] for ip, h in cfg.HOSTS.items()}
        self.arp_table[cfg.VIP] = cfg.VIP_MAC
        self.arp_table[cfg.CTRL_IP] = cfg.CTRL_MAC

        os.makedirs(os.path.dirname(DECISIONS_CSV), exist_ok=True)
        self.logger.info("[START] policy=%s  VIP=%s:%d  reports on %s:%d/udp",
                         self.policy, cfg.VIP, cfg.APP_PORT, cfg.CTRL_IP, cfg.REPORT_PORT)
        hub.spawn(self._health_loop)

    # ---------------- OpenFlow helpers ----------------
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

    # ---------------- switches and links ----------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        self.add_flow(dp, PRIO_MISS, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
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
            return
        down = (msg.reason == ofp.OFPPR_DELETE
                or bool(msg.desc.state & ofp.OFPPS_LINK_DOWN)
                or bool(msg.desc.config & ofp.OFPPC_PORT_DOWN))
        # A link is UP only if BOTH of its ends are up.
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

    # ---------------- shortest path ----------------
    def _neighbors(self, dpid):
        for i, (a, pa, b, pb, delay, _bw) in enumerate(cfg.LINKS):
            if not self.link_up[i]:
                continue
            if a == dpid:
                yield b, pa, delay
            elif b == dpid:
                yield a, pb, delay

    def shortest_path(self, src, dst):
        dist, prev = {src: 0}, {}
        queue = [(0, src)]
        while queue:
            d, u = heapq.heappop(queue)
            if u == dst:
                break
            if d > dist.get(u, float("inf")):
                continue
            for v, port, delay in self._neighbors(u):
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

    # ---------------- packet in ----------------
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

    # ---------------- ARP ----------------
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

    # ---------------- plain routing ----------------
    def handle_route(self, from_dpid, ip, data):
        if ip.dst not in cfg.HOSTS:
            return
        if self.install_route(ip.dst, from_dpid) is None:
            return
        _n, _m, dst_dpid, dst_port = cfg.HOSTS[ip.dst]
        dp = self.datapaths[dst_dpid]
        self.send_packet(dp, [dp.ofproto_parser.OFPActionOutput(dst_port)], data)

    # ---------------- load reports ----------------
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
                parts.append("%s=UP(%d)" % (cfg.host_name(ip), st["active"] + st["pending"]))
            else:
                parts.append("%s=DOWN" % cfg.host_name(ip))
        return " ".join(parts)

    # ---------------- server selection ----------------
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
        return min(alive, key=lambda s: (self.servers[s]["active"] + self.servers[s]["pending"],
                                         cfg.SERVER_DELAY_MS[s]))

    def log_decision(self, client_ip, client_port, server):
        load = self._load_summary()
        self.logger.info("[LB] %s:%d -> %s  (policy=%s | %s)", cfg.host_name(client_ip),
                         client_port, cfg.host_name(server), self.policy, load)
        new_file = not os.path.exists(DECISIONS_CSV)
        try:
            with open(DECISIONS_CSV, "a", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(["timestamp", "policy", "client", "client_port", "server", "loads"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), self.policy,
                            cfg.host_name(client_ip), client_port, cfg.host_name(server), load])
        except OSError as e:
            self.logger.warning("could not write %s: %s", DECISIONS_CSV, e)

    # ---------------- virtual IP ----------------
    def handle_vip_request(self, ip, t, data):
        client_ip, client_port = ip.src, t.src_port
        if client_ip not in cfg.HOSTS:
            return
        key = (client_ip, client_port)
        entry = self.assignments.get(key)
        if entry is None:
            if not t.bits & tcp.TCP_SYN:
                return
            server = self.choose_server()
            if server is None:
                self.logger.warning("[LB] %s:%d rejected: no server is UP",
                                    cfg.host_name(client_ip), client_port)
                return
            self.servers[server]["pending"] += 1
            self._forget_old_assignments()
            self.assignments[key] = {"server": server, "time": time.time()}
            self.log_decision(client_ip, client_port, server)
        else:
            server = entry["server"]

        if not self.install_vip_flows(client_ip, client_port, server):
            return
        _n, s_mac, s_dpid, s_port = cfg.HOSTS[server]
        dp = self.datapaths[s_dpid]
        parser = dp.ofproto_parser
        self.send_packet(dp, [parser.OFPActionSetField(eth_dst=s_mac),
                              parser.OFPActionSetField(ipv4_dst=server),
                              parser.OFPActionOutput(s_port)], data)

    def handle_vip_reply(self, ip, t, data):
        client_ip, client_port = ip.dst, t.dst_port
        server = self.assignments[(client_ip, client_port)]["server"]
        if server != ip.src:
            return
        if not self.install_vip_flows(client_ip, client_port, server):
            return
        _n, _m, c_dpid, c_port = cfg.HOSTS[client_ip]
        dp = self.datapaths[c_dpid]
        parser = dp.ofproto_parser
        self.send_packet(dp, [parser.OFPActionSetField(eth_src=cfg.VIP_MAC),
                              parser.OFPActionSetField(ipv4_src=cfg.VIP),
                              parser.OFPActionOutput(c_port)], data)

    def install_vip_flows(self, client_ip, client_port, server):
        _cn, _cm, c_dpid, c_port = cfg.HOSTS[client_ip]
        _sn, s_mac, s_dpid, s_port = cfg.HOSTS[server]

        forward = self.install_route(server, c_dpid)
        backward = self.install_route(client_ip, s_dpid)
        if forward is None or backward is None:
            return False

        first_hop = forward[0][1]
        out_port = s_port if first_hop is None else first_hop

        dp = self.datapaths[c_dpid]
        parser = dp.ofproto_parser
        self.add_flow(dp, PRIO_VIP,
                      parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=6,
                                      ipv4_src=client_ip, ipv4_dst=cfg.VIP,
                                      tcp_src=client_port, tcp_dst=cfg.APP_PORT),
                      [parser.OFPActionSetField(eth_dst=s_mac),
                       parser.OFPActionSetField(ipv4_dst=server),
                       parser.OFPActionOutput(out_port)],
                      idle=VIP_IDLE, cookie=COOKIE_VIP)
        self.add_flow(dp, PRIO_VIP,
                      parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=6,
                                      ipv4_src=server, ipv4_dst=client_ip,
                                      tcp_src=cfg.APP_PORT, tcp_dst=client_port),
                      [parser.OFPActionSetField(eth_src=cfg.VIP_MAC),
                       parser.OFPActionSetField(ipv4_src=cfg.VIP),
                       parser.OFPActionOutput(c_port)],
                      idle=VIP_IDLE, cookie=COOKIE_VIP)
        return True

    def _forget_old_assignments(self, max_age=600):
        if len(self.assignments) < 500:
            return
        now = time.time()
        for key in [k for k, v in self.assignments.items() if now - v["time"] > max_age]:
            del self.assignments[key]
