# Viva Questions and Answers

Questions that have come up while building the project, plus the ones an examiner is most likely to
ask. Every team member should be able to answer all of these, not just the part they wrote.

New questions are appended as they arise.

---

## A. Reading the test output

### A1. Why does `pingall` report 30 tests?

There are 6 hosts (h1, h2, h3, srv1, srv2, srv3). `pingall` has every host ping every *other* host,
so each of the 6 performs 5 pings: **6 x 5 = 30** ordered pairs. h1 to h2 and h2 to h1 are counted
separately, which is why it is not 15. In the Step 1 sanity test there were only 3 hosts, giving
3 x 2 = 6 tests.

### A2. Why was the first ping to srv1 slower than the rest (20.8 ms vs 13 ms)?

The first packet of a new flow has no matching rule in the switch, so the switch sends it to the
controller and waits for a decision (a `packet_in` / `packet_out` round trip). The controller then
installs a flow rule, so every later packet is forwarded by the switch alone. The extra ~7 ms is
that one-off control-plane detour.

### A3. Why is the measured RTT slightly higher than the configured delays?

The configured one-way delay for h1 to srv1 is 1 + 2 + 2 + 1 = 6 ms, so the expected round trip is
12 ms and we measured about 13 ms. The excess is processing overhead: the hosts are processes inside
a virtual machine, and every packet crosses several virtual interfaces and queueing disciplines. The
relative difference between servers is what matters, and it matches the design.

### A4. Why does throughput top out around 9.3 Mbps on a 10 Mbps link?

The 10 Mbps limit applies to all bits on the wire, including Ethernet, IP and TCP headers and the
acknowledgements travelling the other way. Our figure counts only the file's payload bytes, so
roughly 5-7% overhead is expected. Reaching 9.3 of 10 Mbps means the transfer is link-limited, not
application-limited.

### A5. In Test E, three clients each got about 3.4 Mbps. What was the limit?

Bandwidth, not software. srv1's link is 10 Mbps, and three clients sharing it got roughly a third
each; their combined throughput was almost exactly 10 Mbps. The lock in the server is held only for
a counter update lasting microseconds and has no measurable effect. Meanwhile srv2, srv3 and the
backup path sat idle, which is the waste the controller removes. General rule: when throughput
splits evenly between N users and the total equals a known link capacity, the bottleneck is the
link.

---

## B. Application and sockets

### B1. Why read the protocol header one byte at a time?

TCP is a **byte stream** with no message boundaries: the receiver cannot know where one message
ends unless the protocol says so. Our header ends at a newline. If we read a large block looking for
that newline, the block would also swallow the first bytes of the file that follows, and those bytes
would be lost from the body. Reading one byte at a time up to the newline guarantees we stop exactly
at the boundary. The body is then read in large chunks, because its length is known from the header,
so there is no performance penalty.

### B2. Why loop when receiving the file instead of calling `recv()` once?

A single `recv(n)` may return fewer than `n` bytes; it returns whatever has arrived so far. We
therefore loop until the exact number of bytes promised in the `OK <size>` header has been received.
Not doing this is the most common bug in socket programming.

### B3. How does the server handle several clients at once?

The main thread only accepts connections. Each accepted connection is handed to a new thread that
serves that client and exits. Shared counters (`active`, `total`, `bytes_sent`) are protected by a
lock because several threads update them. Test E proves it worked: the server log recorded
`active=3`, meaning three transfers were in progress simultaneously.

### B4. Why does the server put its own name in every reply?

Because the controller hides the real server behind the virtual IP, the client cannot tell from the
network which server answered. The `OK <size> <server_name>` header is the application's own way of
reporting it, and it is what makes the load-balancing demo visible.

### B5. What errors does the application handle?

Missing file (`ERR 404`), malformed request (`ERR 400`), internal failure (`ERR 500`), client
disconnecting mid-transfer (caught as a connection error, the thread exits cleanly), and client
timeouts. The client retries failed requests and records the failure in its CSV. The server also
rejects file names containing path separators, so `GET ../../etc/passwd` cannot escape the content
directory.

### B6. Why did the first concurrency test fail to prove anything?

Each 5 MB download finished in about 4.4 seconds, faster than the next command could be typed, so
the three downloads ran one after another and the server log showed only `active=1`. The fix was to
make the background clients download five times in a row so the transfers genuinely overlapped. The
lesson: verify concurrency from the **server's** log, not from the client's wall-clock timing.

---

## C. Topology design

### C1. Why are there two paths between the client switch and the server switch?

To make path failure and rerouting possible. With a single path there is nothing to reroute to, and
the "simulate path failure" requirement could not be demonstrated.

### C2. Why do the three servers have different link delays (1, 5 and 10 ms)?

To give the controller a genuine **proximity** difference. If all servers were equidistant, "select
the closest server" would be meaningless. It is also the tie-breaker in `least_loaded`: without it,
the policy would have only one decision criterion. The difference is measurable: h1 to srv1 is about
16 ms RTT, h1 to srv3 about 39 ms.

### C3. Why are the core links 20 Mbps while host links are 10 Mbps?

So that each server's own link is the bottleneck, which is the realistic case. If the core were also
10 Mbps, traffic for all three servers would still cross one shared 10 Mbps link, and Test H would
have shown about 3.3 Mbps per client even with perfect load balancing. The controller would have
looked useless for a reason unrelated to its logic. Moving the bottleneck to the server links makes
the experiment measure the thing being tested.

### C4. Why fix the IP addresses, MAC addresses, switch IDs and port numbers?

The controller's flow rules refer to specific output ports ("send out port 4") and specific
addresses. If Mininet assigned these differently on each run, the controller would send traffic out
of the wrong port. Fixing them also makes the logs readable and the experiments repeatable.

---

## D. SDN controller

### D1. What is the difference between the control plane and the data plane here?

The control plane decides the rules; the data plane applies them to the traffic. In this project the
Ryu controller is the control plane and the four Open vSwitch switches are the data plane. The
controller sees only the first packet of each connection; everything after that is switched without
it. Test G shows this concretely: the return flow rule had counted about 5.37 MB for a 5 MB
transfer, so the bulk data never reached the controller. In short, the controller is on the
connection setup path, not on the data path.

### D2. Why use a virtual IP instead of letting clients choose a server?

The client is blind: it cannot see server load, server failures, link state or other clients'
traffic. The controller sees all of these, through UDP load reports, OpenFlow port events and its
view of every connection. Moving the decision into the network therefore uses information no single
client has. The client also needs to know only one address, so servers can be added, removed or fail
without changing the client.

### D3. Nobody owns 10.0.0.100. How does it work?

The controller answers ARP requests for it with an invented MAC address, so the client sends packets
to it. Those packets reach switch s1, where the controller has installed rules that rewrite the
destination to a real server. The server's replies get their source rewritten back to 10.0.0.100
before reaching the client. Without that return rewrite the client would receive a reply from an
address it never contacted and discard it. The address exists only as a rule in the switch.

### D4. What exactly do the flow rules do?

For each connection, two rules are installed on the client's edge switch:

| Direction | Match | Action |
|---|---|---|
| Client to VIP | `src=10.0.0.2, dst=10.0.0.100, tcp_dst=9000` | set `ip_dst`/`eth_dst` to the chosen server, output to next hop |
| Server to client | `src=10.0.0.11, dst=10.0.0.2, tcp_src=9000` | set `ip_src`/`eth_src` back to the VIP, output to the client port |

Plus lower-priority rules that forward by destination IP along the computed path, an IPv6 drop rule,
and the table-miss rule that sends unmatched packets to the controller.

### D5. How does the controller learn how busy each server is?

Each server sends a UDP datagram to 10.0.0.254:5555 once per second containing its name, its current
active connection count, total connections served and bytes sent. No host owns that address, so the
packets reach the controller as `packet_in` events. This is the project's socket-to-SDN link:
application state directly shapes forwarding decisions.

### D6. Why keep a "pending" counter as well as the reported load?

Reports arrive only once per second. If four clients connect within the same second, all four would
see `srv1=0, srv2=0, srv3=0` and all four would go to srv1. The pending counter records clients
assigned since the last report, so after the first assignment srv1 shows 1 and the next client goes
elsewhere. It is reset when a fresh report arrives.

### D7. How does the controller detect that a server has died?

By absence of reports. If a server has not reported for 3 seconds (`DEAD_AFTER`), it is marked DOWN
and excluded from selection until it reports again. In Test I, killing srv2 produced
`[HEALTH] srv2 is DOWN` within 3 seconds, and restarting it produced `[HEALTH] srv2 is UP`.

### D8. Why does the project need a static policy at all?

As the baseline for comparison. The rubric asks for the proposed approach to be compared with a
static approach, and running both from the same code on the same topology makes the comparison fair:
only the decision rule differs.

### D9. Why did every request go to srv1 after srv2 died, instead of alternating with srv3?

Because `least_loaded` broke a tie. With srv2 down and both srv1 and srv3 idle (0 connections each),
load was tied, so the policy fell through to its second criterion, proximity: srv1 is 1 ms away
versus srv3 at 10 ms. Each request finished before the next began, so srv1 was back at 0 and won
every time. The decision used both criteria: load first, closeness as the tie-break.

### D10. How are connections kept on the same server?

The controller records the assignment keyed by `(client IP, client source port)`. If rules expire or
are cleared mid-transfer, the same connection is re-mapped to the same server. Each *new* connection
is a fresh decision, which is what allows load to redistribute over time.

### D11. How does the controller choose a path?

Dijkstra's algorithm over the switch graph, using the configured link delays as weights and skipping
links that are currently down. The fast path (2 + 2 = 4 ms) therefore wins over the slow path
(10 + 10 = 20 ms) while both are available.

### D12. Does the static policy avoid dead servers?

No, deliberately. `round_robin` and `least_loaded` skip servers marked DOWN, but `static` always
returns srv1 without checking liveness, exactly as a network with no controller intelligence would.
If the baseline avoided failures, the failure experiments would compare two intelligent systems
rather than intelligent versus static, and the comparison would understate the benefit.

### D13. A server dies, and separately a link is cut. How do the responses differ?

| What fails | How it is detected | What the controller does |
|---|---|---|
| A server | Its UDP reports stop for 3 s | Marks it DOWN and chooses a **different server** |
| A link | The switch sends an OpenFlow `PortStatus` event | Clears affected rules and computes a **different path** |

A dead server does not need a new path, and a dead link does not need a new server.

---

## E. The loop and STP

### E1. What is a broadcast storm, and why does a loop cause one?

A broadcast (for example ARP: "who has 10.0.0.11?") has no single destination, so a basic switch
**floods** it out of every port except the one it arrived on. In a loop, those copies come back
around and are flooded again, multiplying each lap until the network is saturated. The network fails
within seconds rather than degrading slowly.

### E2. Why did the early tests need the STP controller?

The two paths form a loop (s1 to s2 to s4 to s3 to s1). Ryu's STP app detects the loop and blocks
one path, which is why `pingall` failed until about 40 seconds of convergence had passed.

### E3. Why doesn't your own controller need STP?

It never floods. It answers every ARP request itself from its known address table, and it forwards
IP traffic along a single path computed with Dijkstra rather than broadcasting. With nothing being
flooded, the loop cannot carry a storm. `pingall` therefore succeeded immediately.

### E4. STP would have worked. Why is it a bad fit for this project?

STP permanently blocks one of the two paths. The project needs both paths available so that when a
link fails, the controller can reroute onto the other one. STP's cure removes exactly the redundancy
the project exists to demonstrate.

---

## F. Results and evaluation

### F1. What is the headline result?

With three clients each downloading 5 MB files concurrently:

| Metric | Static (all on one server) | Dynamic (least-loaded) |
|---|---|---|
| Mean response time | 10.44 s | 4.81 s |
| Mean throughput per client | 4.71 Mbps | 8.92 Mbps |

Mean response time fell by 54% and per-client throughput rose by 89%, with no change to the
application code; only the controller's decisions differ.

### F2. Why was h1's first download slower than its later ones in Test H?

All three clients started within the same moment, so before any load report had differentiated the
servers, two clients briefly shared one server. Once the reports updated, the loads diverged and h1's
remaining downloads ran at 9.4 Mbps. It illustrates why the pending counter matters and why the
report interval limits how quickly the controller can react.

### F3. What metrics does the client measure, and why those?

Connect time (TCP handshake, sensitive to path latency), time to first byte (handshake plus server
processing), total response time (user-visible latency) and throughput (payload bits per second).
Together they separate network delay from server delay, which matters when comparing a near but busy
server against a distant but idle one.

### F4. What are the limitations of your evaluation?

Measurements come from a single virtual machine, so absolute numbers include emulation overhead;
each scenario was run a small number of times rather than averaged over many repetitions; the
workload is uniform (identical file sizes and request patterns) rather than realistic traffic; and
the static baseline in D1 came from an earlier configuration, so a fresh static run under the final
topology is the cleanest comparison. Stating these shows the results are understood rather than
oversold.

---

## G. Scope and alternatives

### G1. Why did you not implement session migration when a server dies mid-transfer?

TCP connections are bound to a specific endpoint; moving one to a different server would require
replicating TCP state, which is outside the scope of this project. Instead the client retries, and
the controller sends the retry to a healthy server.

### G2. Could the controller have used switch statistics instead of server reports?

Yes. OpenFlow port and flow statistics show link utilisation, which is a good way to monitor
*network* conditions, and Deliverable 2 adds exactly that. Application-level reports were chosen
first because they expose something the network cannot see, namely how many connections a server is
actually handling, and because the rubric specifically asks for interaction between the socket
application and the SDN network.

### G3. Why Ryu rather than POX or ONOS?

Ryu is Python-based, supports OpenFlow 1.3 (which is needed for `set_field` address rewriting), and
is well documented for the kind of per-flow control this project requires. ONOS would have been
heavier to deploy for a single-machine emulation.

---

## H. Link failure and rerouting

### H1. How do you know a transfer survived the failure mid-flight?

The controller log shows connection 43578 began before `[LINK] s1-s2 is DOWN`. Its rewrite rules on
s1 were reinstalled after the cut, outputting to `s1-eth5` (the backup path), and its return rule
counted 5,148,034 bytes against 5,367,050 for a complete transfer. The missing ~219 KB crossed the
fast path before the cut, on a rule the controller then deleted; the rest crossed the backup path.
The download still completed, only ~18 ms slower than an undisturbed one.

### H2. Why did throughput barely change on the slow path when latency quadrupled?

The bottleneck is srv1's 10 Mbps link, not the path. TCP keeps enough data in flight to fill the
pipe over the longer round trip: the bandwidth-delay product at 10 Mbps and ~55 ms is about 69 KB,
well inside Linux's default window. Higher latency hurts small requests and TCP's ramp-up, which is
why downloads took ~2-3% longer, but not steady-state throughput.

### H3. Why was the first ping after the cut 390 ms?

Cutting the link made the controller delete all dynamic rules so that nothing kept pointing at the
dead link. The next packet therefore matched only the table-miss rule, went to the controller,
waited for a new path to be computed and installed, and was then forwarded. Every later ping hit
the new rules directly and took about 55 ms.

### H4. Describe a bug you found and fixed.

One `link s1 s2 down` produced `DOWN`, `UP`, `DOWN` in the controller log. A link has two ends,
each reported separately by its own switch, and the controller set the link's state from whichever
end reported last, so a status update from the far end briefly marked it UP. For those
milliseconds traffic could have been routed into a dead link. The fix tracks every port's state and
treats a link as UP only when both ends are up. We confirmed it by replaying the exact event
sequence in a unit test and by re-running the experiment, which then showed one DOWN and one UP.
