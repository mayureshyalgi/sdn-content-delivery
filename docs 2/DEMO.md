# Live Demo Script (about 12 minutes)

Three presenters, one laptop. Each part says **who drives**, **what to type**, **what the audience should
see**, and **what to say**. Rehearse it end to end at least twice.

Before the examiner arrives:

- Run the whole script once. Leave the VM **plugged in and awake**.
- Have three terminal tabs open, all in `~/sdn-content-delivery`:
  **T1** controller, **T2** Mininet, **T3** spare (for `ovs-ofctl`).
- Open `docs/REPORT.md` on GitHub in a browser tab, scrolled to Section 5 (graphs), as the fallback.
- Increase the terminal font size (**Ctrl + +**) so the examiner can read it.

If anything fails live, don't debug in front of the examiner: say what should have happened and show the
matching screenshot in `docs/evidence/EVIDENCE.md` or the graph in `docs/REPORT.md`.

---

## Part 1 — The problem and the design (2 min) · Presenter 1

No commands. Show the topology diagram at the top of `README.md`.

**Say:** three identical content servers, three clients, one virtual IP. The controller decides per TCP
connection which server answers and which path traffic takes, using server load (UDP reports from the
servers) and network state (OpenFlow port statistics and link events). Two paths so we can reroute;
different server delays so proximity means something; core links faster than host links so each server's
link is the real bottleneck.

## Part 2 — Start the system (2 min) · Presenter 2

```bash
# T2
sudo mn -c
# T1
source ~/ryu-env/bin/activate
LB_POLICY=least_loaded PATH_POLICY=congestion ryu-manager controller/lb_controller.py
# T2
sudo python3 topology/topo.py
```

**Point at T1:** `[START] policy=least_loaded path_policy=congestion`, four `[SWITCH] connected` lines,
and `srv1=DOWN srv2=DOWN srv3=DOWN` (no servers yet, so health checking already works).

```
mininet> pingall
```

**Say:** 56/56 immediately. The topology has a loop, but our controller answers ARP itself and forwards
along computed paths, so nothing is flooded and no spanning tree is needed.

```
srv1 python3 app/server.py --name=srv1 > results/srv1.log 2>&1 &
srv2 python3 app/server.py --name=srv2 > results/srv2.log 2>&1 &
srv3 python3 app/server.py --name=srv3 > results/srv3.log 2>&1 &
```

**Point at T1:** `[HEALTH] srv1 is UP` ×3 — the servers' own UDP sockets reporting to the controller.

## Part 3 — Load balancing through the virtual IP (2 min) · Presenter 1

```
h2 python3 app/client.py --name=h2 --server 10.0.0.100 --file large.bin --count 2 --interval 0 > results/demo_h2.txt &
h3 python3 app/client.py --name=h3 --server 10.0.0.100 --file large.bin --count 2 --interval 0 > results/demo_h3.txt &
h1 python3 app/client.py --name=h1 --server 10.0.0.100 --file large.bin --count 2 --interval 0
```

**While it runs, in T3:**

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep 10.0.0.100
```

**Say:** clients only ever contact 10.0.0.100, which no host owns. Read one rule aloud: traffic to the VIP
has its destination rewritten to a real server; the reverse rule rewrites replies back to the VIP. The
byte counters keep growing in the switch: the controller saw only the first packet.

**Then:**

```
h2 cat results/demo_h2.txt
h3 cat results/demo_h3.txt
```

**Point at:** the `[LB]` lines in T1 sending h1, h2, h3 to different servers, and each client near
9 Mbps instead of ~3.3 Mbps if they all shared one server (our static baseline).

## Part 4 — Server failure (2 min) · Presenter 3

```
srv2 pkill -f name=srv2
```

**Point at T1** within 3 s: `[HEALTH] srv2 is DOWN`.

```
h1 python3 app/client.py --name=h1 --server 10.0.0.100 --file medium.bin --count 3
```

**Say:** every request succeeds on a live server; srv2 is never chosen. Then bring it back:

```
srv2 python3 app/server.py --name=srv2 > results/srv2.log 2>&1 &
```

`[HEALTH] srv2 is UP`. In the automated runs, killing srv1 lost 36 of 36 downloads with the static policy
and 0 of 36 with ours.

## Part 5 — Link failure and rerouting (2 min) · Presenter 3

```
h1 ping -c 3 srv1
h1 python3 app/client.py --name=h1 --server 10.0.0.100 --file large.bin --count 3 --interval 0 > results/demo_link.txt &
link s1 s2 down
h1 ping -c 3 srv1
sh ovs-ofctl -O OpenFlow13 dump-flows s1 | grep s1-eth5
```

**Point at:** `[LINK] s1-s2 is DOWN` in T1, ping rising from ~14 ms to ~55 ms, and rules now outputting
to `s1-eth5` (the backup path).

```
link s1 s2 up
h1 cat results/demo_link.txt
```

**Say:** all downloads completed. We once caught a transfer mid-flight: part of its bytes were counted on
the old rule and the rest on the rebuilt backup-path rule. We also found and fixed a bug here: the link
"flapped" because each end reports separately; a link is now UP only when both ends are.

## Part 6 — Congestion-aware routing (2 min) · Presenter 2

```
sink iperf -s -u > results/demo_sink.txt &
gen iperf -c sink -u -b 25M -t 40 > results/demo_flood.txt &
```

Wait for T1 to show `[MONITOR] s1-s2=100% ... s1-s3=0%` (about 4 s), then:

```
h1 python3 app/client.py --name=h1 --server 10.0.0.100 --file large.bin --count 2 --interval 0
```

**Point at:** `[LB] h1:... -> srv1 via s1-s3-s4`, then `s1-s3` utilisation rising as h1's traffic moves
there, and h1 at ~9 Mbps while the flood is still running.

**Say:** with congestion-awareness off, the same flood made the first request fail twice and wait up to
43 s; with it on, the worst wait was 5.4 s.

## Close (30 s) · Presenter 1

Open `docs/REPORT.md` → Section 5 and Appendix A (rubric mapping). Summarise: 2.5× throughput under
load, 7.9× lower worst-case wait under congestion, no lost requests on server failure, link failure
invisible to clients — all measured over 24 automated runs.

```
exit
```
then **Ctrl + C** in T1.
