# SDN-Based Content Delivery with Dynamic Server Selection

**Architecture and Design Document (Deliverable 1)**

Course mini-project: socket programming integrated with Software-Defined Networking.

---

## 1. Problem statement

Implement multiple content servers and clients where the SDN controller dynamically selects or
reroutes traffic toward suitable servers based on network conditions.

Required capabilities: TCP content requests, multiple content servers, server availability
tracking, network-condition monitoring, server selection, dynamic modification of SDN forwarding,
simulated server and path failures, and measurement of response time and throughput.

## 2. Objectives

1. Build a content service over **low-level TCP sockets** (no HTTP library or framework) with a
   custom application protocol, concurrency, and error handling.
2. Deploy **three content servers** holding identical content, plus **three clients**, inside a
   Mininet topology with **two alternative paths** between them.
3. Write an **SDN controller** that exposes one virtual service address and decides, per TCP
   connection, which real server should answer it.
4. Make that decision depend on **live conditions**: which servers are alive, how loaded each one
   is, how close it is, and which network paths are available.
5. Implement the decision in the data plane by **installing and modifying OpenFlow rules**.
6. Demonstrate **robustness** under server failure and path failure.
7. **Measure** response time and throughput, and compare dynamic selection against a static
   baseline under identical conditions.

### Non-goals

Session persistence across server failure (a transfer interrupted mid-flight is retried by the
client, not migrated), content replication or consistency, encryption, and authentication. These
are out of scope for the assignment and are noted here to bound the design.

## 3. System architecture

### 3.1 Topology

~~~
                         SDN CONTROLLER  (Ryu, OpenFlow 1.3)
                         controller/lb_controller.py
                                   |
          OpenFlow control channel (TCP 6653) to all four switches
                                   |
                 +---- s2 ----+        fast path  (2 ms per hop, 20 Mbps)
   h1 -+         |            |         +- srv1  10.0.0.11  (1 ms)
   h2 -+-- s1 ---+            +-- s4 ---+- srv2  10.0.0.12  (5 ms)
   h3 -+         |            |         +- srv3  10.0.0.13  (10 ms)
                 +---- s3 ----+        slow path (10 ms per hop, 20 Mbps)
   clients 10.0.0.1-3                     content servers
~~~

| Element | Value | Purpose |
|---|---|---|
| Client links (h to s1) | 10 Mbps, 1 ms | Access links |
| Server links (s4 to srv) | 10 Mbps, 1/5/10 ms | Different **proximity** per server |
| Fast path (s1-s2-s4) | 20 Mbps, 2 ms per hop | Default route |
| Slow path (s1-s3-s4) | 20 Mbps, 10 ms per hop | Backup route for rerouting |
| Virtual IP (VIP) | 10.0.0.100:9000 | The only address clients ever use |
| Controller report address | 10.0.0.254:5555/udp | Where servers send load reports |

**Design rationale.** Core links are deliberately **faster** than host links so that each server's
own 10 Mbps link is the bottleneck; otherwise spreading clients across servers would not help,
because all traffic would still cross one shared core link. The three different server delays give
the controller a genuine proximity choice. The two disjoint paths make path failure and rerouting
possible: with a single path there would be nothing to reroute to.

### 3.2 Components

| Component | File | Responsibility |
|---|---|---|
| Content server | `app/server.py` | Multithreaded TCP server; serves files; counts active connections; sends UDP load reports |
| Content client | `app/client.py` | Requests files over TCP; measures connect time, time-to-first-byte, total response time, throughput; retries on failure; writes CSV |
| Protocol library | `app/protocol.py` | Message framing, line-based header I/O, exact-length body reads |
| Content generator | `app/make_content.py` | Creates the 1 KB / 1 MB / 5 MB test files |
| Topology | `topology/topo.py` | Builds the Mininet network with fixed IPs, MACs, DPIDs and port numbers |
| SDN controller | `controller/lb_controller.py` | ARP handling, path computation, server selection, flow-rule installation, health checking, rerouting |
| Network map | `controller/net_config.py` | Single source of truth for addresses, switch IDs, ports, link delays |

Fixed MACs, DPIDs and port numbers matter: the controller's flow rules reference specific output
ports, so the mapping must be identical on every run.

### 3.3 Application protocol

Line-based text header, optionally followed by a binary body. Chosen for simplicity of parsing and
ease of inspection in logs and packet captures.

| Direction | Message | Meaning |
|---|---|---|
| Client to Server | `GET <filename>` | Request a file |
| Client to Server | `PING` | Health/liveness probe |
| Server to Client | `OK <size> <server_name>` + body | Success, followed by exactly that many bytes |
| Server to Client | `PONG <server_name> <active>` | Alive, with current connection count |
| Server to Client | `ERR <code> <message>` | 400 bad request, 404 file not found, 500 server error |

Two deliberate details:

- The **server name is included in every reply**. Because the controller hides the real server
  behind the VIP, this is how a client (and the demo audience) can tell which server actually
  answered.
- The header is read **one byte at a time** up to the newline. A larger read could consume the
  beginning of the body that follows, since TCP is a byte stream with no message boundaries.

### 3.4 Load-report protocol (socket to SDN integration)

Each server sends a UDP datagram to `10.0.0.254:5555` once per second:

~~~
{"name": "srv1", "active": 2, "total": 57, "bytes_sent": 298844160}
~~~

No host owns `10.0.0.254`, so these packets miss every forwarding rule and are delivered to the
controller as `packet_in` events. The controller parses them and maintains, per server: reported
active connections, a pending counter for clients assigned since the last report, and the timestamp
of the last report. This is the mechanism by which **application state influences network
behaviour**, which is precisely what the Socket-SDN Integration criterion asks for.

## 4. Communication flow

### 4.1 A new content request

~~~
 1. Client resolves the VIP        : ARP "who has 10.0.0.100?"  -> controller replies with VIP_MAC
                                     (the controller answers ARP itself; nothing is flooded)
 2. Client sends TCP SYN to VIP:9000
 3. s1 has no matching rule        -> packet_in to controller
 4. Controller selects a server    : policy + liveness + load + proximity
 5. Controller computes paths      : Dijkstra over link delays, excluding down links
 6. Controller installs rules      :
       - priority 10 on each switch along the path: forward by destination IP
       - priority 30 on s1 (client edge), per connection:
             client -> VIP       : rewrite ip_dst/eth_dst to the chosen server, output to next hop
             server -> client    : rewrite ip_src/eth_src back to the VIP, output to the client port
 7. Controller forwards the SYN itself, already rewritten, so no packet is lost
 8. Every later packet of this connection is switched in the datapath, never via the controller
 9. Client sends "GET medium.bin"; server replies "OK <size> srv2" plus the data
10. Rules expire 15 s after the connection goes idle
~~~

The controller therefore touches only the **first packet** of each connection. Test G confirms this:
the return flow rule had counted about 5.37 MB for a 5 MB transfer, so the bulk data never reached
the control plane.

### 4.2 Connection affinity

The controller keys assignments on `(client IP, client TCP source port)`, so every packet of one
connection goes to the same server even if rules expire and are reinstalled mid-transfer. Each
*new* connection is a fresh decision, which is what allows load to spread as conditions change.

## 5. Network conditions monitored

| Condition | Source | Used for |
|---|---|---|
| Server liveness | UDP load reports; a server silent for 3 s is marked DOWN | Excluding dead servers from selection |
| Server load | `active` field of the load report, plus pending assignments | `least_loaded` selection |
| Server proximity | Known per-server link delay (1/5/10 ms) | Tie-breaking between equally loaded servers |
| Path availability | OpenFlow `PortStatus` events on switch-to-switch links | Rerouting; excluding failed links from path computation |
| Path cost | Configured link delays | Shortest-path selection (prefers the fast path) |

The pending counter exists because load reports arrive only once per second. Without it, a burst of
connections arriving within the same second would all see stale zeros and be sent to the same
server.

## 6. Server selection policies

Selected at start-up with the `LB_POLICY` environment variable, so all three run from identical code
and can be compared fairly.

| Policy | Rule | Role in the project |
|---|---|---|
| `static` | Always the first server | **Baseline**, equivalent to no controller intelligence |
| `round_robin` | Rotate over live servers | Simple distribution, ignores load |
| `least_loaded` | Fewest (active + pending) connections; ties broken by lowest delay | The proposed approach |

`round_robin` and `least_loaded` skip servers that are currently DOWN. `static` deliberately does
not: it keeps sending traffic to srv1 even after srv1 fails, exactly as a network with no controller
intelligence would. That is what makes it a fair baseline for the failure experiments.

## 7. Expected network behaviour

| Situation | Expected behaviour |
|---|---|
| Single client, idle system | Served by the nearest server; throughput close to the 10 Mbps link limit |
| Three concurrent clients, `static` | All land on one server; each receives roughly one third of 10 Mbps |
| Three concurrent clients, `least_loaded` | Spread across three servers; each approaches the full 10 Mbps |
| A server process dies | Marked DOWN within 3 s; no new connections sent to it; clients see no errors |
| That server returns | Marked UP on its first report; rejoins the candidate set |
| Fast path link fails | PortStatus fires; affected rules are cleared; new paths computed over the slow path; latency rises but connectivity survives |
| Topology loop | No broadcast storm: ARP is answered by the controller and traffic follows computed unicast paths, so neither path needs to be blocked |

## 8. Results obtained (initial testing)

Full per-request data is in `docs/evidence/RESULTS_SUMMARY.md`; screenshots and what each one proves
are in `docs/evidence/EVIDENCE.md`.

| Test | Result |
|---|---|
| Connectivity | 0% loss, 30/30, immediately (no spanning-tree convergence delay) |
| Proximity (ICMP) | h1 to srv1 about 16 ms, h1 to srv3 about 39 ms |
| Single download, 1 MB | srv1 902 ms / 9.29 Mbps; srv3 973 ms / 8.62 Mbps |
| Concurrency | One server served three clients simultaneously (`active=3` in its log) |
| Error handling | Missing file gives ERR 404; both sides continued normally |
| Round-robin via VIP | 6/6 requests served srv1, srv2, srv3, srv1, srv2, srv3; avg 9.01 Mbps |
| Flow rules | Bidirectional VIP rewrite pairs present on s1; 5.37 MB counted in the datapath |
| **Static vs dynamic** | Mean per-client throughput **4.71 to 8.92 Mbps**; mean response time **10.44 s to 4.81 s** for a 5 MB file with three concurrent clients |
| Server failure | Detected within 3 s; 4/4 subsequent requests succeeded at 9.28 Mbps; zero client-visible errors |

The "fast path link fails" row is expected behaviour from the design; it is demonstrated and measured
in Deliverable 2.

## 9. How to reproduce

~~~
# Terminal 2 - clean any previous run (this also kills a running controller)
sudo mn -c

# Terminal 1 - start the controller (policy: static | round_robin | least_loaded)
cd ~/sdn-content-delivery
source ~/ryu-env/bin/activate
LB_POLICY=least_loaded ryu-manager controller/lb_controller.py

# Terminal 2 - start the network
cd ~/sdn-content-delivery
sudo python3 topology/topo.py

# At the mininet> prompt
srv1 python3 app/server.py --name=srv1 > results/srv1.log 2>&1 &
srv2 python3 app/server.py --name=srv2 > results/srv2.log 2>&1 &
srv3 python3 app/server.py --name=srv3 > results/srv3.log 2>&1 &
h1 python3 app/client.py --name=h1 --server 10.0.0.100 --file medium.bin --count 6
~~~

Order matters: cleanup, then controller, then topology. Use `--name=h1` with an equals sign, because
the Mininet CLI substitutes a bare host name with its IP address.

## 10. Repository layout

~~~
sdn-content-delivery/
|-- app/            server.py, client.py, protocol.py, make_content.py
|-- controller/     lb_controller.py, net_config.py
|-- topology/       topo.py
|-- results/        CSV measurements, server logs, controller_decisions.csv
|-- tests/          failure-injection and experiment scripts
`-- docs/
    |-- ARCHITECTURE.md          this document
    |-- VIVA_QA.md               questions and answers for the viva
    `-- evidence/
        |-- EVIDENCE.md          every screenshot and what it proves
        |-- RESULTS_SUMMARY.md   all measured data in tables
        `-- images/
~~~

## Appendix A - Demo sequence

1. Start controller with `round_robin`; show the four switches connecting and all servers reported
   DOWN before they are launched.
2. `pingall` gives 0% loss immediately; note that a looped topology needs no spanning tree here.
3. Start the three servers; show `[HEALTH] ... is UP` appearing as the first UDP reports arrive.
4. Six client requests to the VIP; show the rotation in the client output and the matching `[LB]`
   decisions in the controller log.
5. `ovs-ofctl -O OpenFlow13 dump-flows s1` during a transfer; explain one rewrite pair and point out
   the byte counters proving the data path bypasses the controller.
6. Restart with `least_loaded`; run three concurrent clients; compare throughput with the static
   baseline figures.
7. Kill a server; show detection within 3 s and uninterrupted client success; restart it and show
   recovery.
8. Bring a core link down; show the reroute and the resulting latency change.

## Appendix B - Team roles

| Member | Primary responsibility |
|---|---|
| Member 1 | Application and sockets: server, client, protocol, load reporting, measurement instrumentation |
| Member 2 | SDN controller: ARP and routing, selection policies, flow-rule management, health checking, rerouting |
| Member 3 | Topology, test environment, failure injection, experiments, graphs, documentation |

Problem understanding, integration testing, the demo and the viva are shared; each member is
expected to be able to explain the whole system.
