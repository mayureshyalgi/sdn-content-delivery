#!/usr/bin/env python3
"""
One-command live demo of the SDN content-delivery project.

    cd ~/sdn-content-delivery
    sudo python3 demo/d1_demo.py

It starts the controller, builds the network, starts the servers and then runs
each test by itself. Before every step it prints what the step shows and waits
for Enter, so the presenter only presses Enter and talks.

Keys at each pause:  Enter = run the step   s = skip it   q = quit
At the end you can open the normal mininet> prompt for extra questions.

Optional: in a second terminal,  tail -f results/demo/controller.log
shows the full controller log live.
"""

import os
import pwd
import re
import signal
import subprocess
import sys
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT)
sys.path.insert(0, os.path.join(PROJECT, "topology"))

from mininet.net import Mininet                       # noqa: E402
from mininet.node import RemoteController, OVSSwitch  # noqa: E402
from mininet.link import TCLink                       # noqa: E402
from mininet.log import setLogLevel                   # noqa: E402
from mininet.clean import cleanup                     # noqa: E402
from mininet.cli import CLI                           # noqa: E402
from topo import ContentTopo, CONTROLLER_IP, CONTROLLER_PORT  # noqa: E402

VIP = "10.0.0.100"
SERVERS = ["srv1", "srv2", "srv3"]
OUT = os.path.join("results", "demo")
CTRL_LOG = os.path.join(OUT, "controller.log")
LB_POLICY = os.environ.get("LB_POLICY", "round_robin")
PATH_POLICY = os.environ.get("PATH_POLICY", "congestion")
STATIC_BASELINE_MBPS = 3.2        # measured: 3 clients, static policy (24-run batch)
CTRL_TAGS = ("[START]", "[SWITCH]", "[HEALTH]", "[LB]", "[LINK]", "[ROUTE]")

# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CYAN, GREEN, YELLOW, RED, MAGENTA = "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m"


def banner(num, title, lines):
    print("\n" + CYAN + "=" * 72 + RESET)
    print(f"{BOLD}{CYAN} STEP {num}: {title}{RESET}")
    print(CYAN + "=" * 72 + RESET)
    for line in lines:
        print(f"  {YELLOW}{line}{RESET}")


def pause(prompt="Press Enter to run  (s = skip, q = quit) "):
    try:
        ans = input(f"\n{DIM}{prompt}{RESET}").strip().lower()
    except EOFError:
        ans = ""
    if ans == "q":
        raise KeyboardInterrupt
    return ans != "s"


def heading(text):
    print(f"\n{BOLD}{MAGENTA}>> {text}{RESET}")


def ok(text):
    print(f"{GREEN}{BOLD}  OK  {text}{RESET}")


def bad(text):
    print(f"{RED}{BOLD}  !!  {text}{RESET}")


def show_cmd(host, command):
    print(f"{DIM}  {host}$ {command}{RESET}")


# ---------------------------------------------------------------------------
# Controller log helpers
# ---------------------------------------------------------------------------
class CtrlLog:
    """Reads the controller log from where we last stopped."""
    def __init__(self, path):
        self.path, self.pos = path, 0

    def new_lines(self):
        try:
            with open(self.path) as f:
                f.seek(self.pos)
                text = f.read()
                self.pos = f.tell()
        except OSError:
            return []
        return text.splitlines()

    def show(self, title="Controller says:", tags=CTRL_TAGS):
        lines = [l for l in self.new_lines() if any(t in l for t in tags)]
        if lines:
            heading(title)
            for l in lines:
                colour = RED if ("DOWN" in l or "no path" in l) else GREEN
                print(f"  {colour}{l.strip()}{RESET}")
        return lines

    def wait_for(self, needles, timeout):
        """Wait until every needle has appeared after the current position."""
        start, deadline = self.pos, time.time() + timeout
        while time.time() < deadline:
            try:
                with open(self.path) as f:
                    f.seek(start)
                    text = f.read()
                if all(n in text for n in needles):
                    return True
            except OSError:
                pass
            time.sleep(0.3)
        return False


def default_ryu():
    user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    return os.path.join(home, "ryu-env", "bin", "ryu-manager")


# ---------------------------------------------------------------------------
# Small actions
# ---------------------------------------------------------------------------
def client_cmd(name, server, fname, count=1, extra=""):
    return (f"python3 app/client.py --name={name} --server {server} "
            f"--file {fname} --count {count} --interval 0 {extra}").strip()


def run_on(net, host, command):
    show_cmd(host, command)
    out = net.get(host).cmd(command)
    for line in out.strip().splitlines():
        print("  " + line)
    return out


def start_server(net, name):
    log = open(os.path.join(OUT, f"{name}.log"), "a")
    return net.get(name).popen(["python3", "app/server.py", f"--name={name}"],
                               stdout=log, stderr=subprocess.STDOUT)


def vip_flows(switch="s1"):
    """The VIP rewrite rules on a switch, with the noisy fields removed."""
    out = subprocess.run(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", switch],
                         capture_output=True, text=True).stdout
    rules = []
    for line in out.splitlines():
        if VIP not in line:
            continue
        line = re.sub(r"(cookie|duration|table|idle_age|hard_age|idle_timeout|"
                      r"reset_counts)=[^, ]*,?\s*", "", line.strip())
        rules.append(line)
    return rules


# ---------------------------------------------------------------------------
# The demo steps
# ---------------------------------------------------------------------------
def step_controller(ryu, state):
    banner(1, "Start the SDN controller (Ryu, OpenFlow 1.3)", [
        "The controller is the 'brain'. It writes the forwarding rules into the switches.",
        f"Server choice policy = {LB_POLICY},  path policy = {PATH_POLICY}.",
    ])
    pause("Press Enter to start the controller ")
    env = dict(os.environ, LB_POLICY=LB_POLICY, PATH_POLICY=PATH_POLICY,
               LB_DECISIONS_CSV=os.path.join(OUT, "lb_decisions.csv"))
    state["ctrl_log_file"] = open(CTRL_LOG, "w")
    state["ctrl"] = subprocess.Popen([ryu, "controller/lb_controller.py"],
                                     stdout=state["ctrl_log_file"], stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True)
    if not state["log"].wait_for(["[START]"], 25):
        raise RuntimeError("controller did not start - see results/demo/controller.log")
    time.sleep(0.5)
    state["log"].show()
    ok("controller running and listening for switches on port 6653")


def step_network(state):
    banner(2, "Build the Mininet network", [
        "4 switches (s1..s4), 3 clients (h1 h2 h3), 3 content servers (srv1 srv2 srv3).",
        "Two paths between the clients and servers: fast s1-s2-s4, slow s1-s3-s4.",
        "Then 'pingall': every host pings every other host through OUR controller.",
    ])
    pause("Press Enter to build the network ")
    net = Mininet(topo=ContentTopo(), switch=OVSSwitch, link=TCLink,
                  controller=None, autoSetMacs=False)
    net.addController("c0", controller=RemoteController, ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    net.start()
    state["net"] = net
    if not net.waitConnected(timeout=20):
        raise RuntimeError("switches did not connect to the controller")
    time.sleep(1)
    state["log"].show(tags=("[SWITCH]",))
    heading("pingall")
    loss = net.pingAll(timeout="1")
    if loss == 0:
        ok("0% packet loss: all hosts reach each other (no flooding, no STP needed)")
    else:
        bad(f"{loss}% packet loss")
    state["log"].new_lines()          # skip the routing noise from pingall


def step_servers(state):
    banner(3, "Start the 3 content servers", [
        "Each server is a TCP socket server on port 9000 (one thread per client).",
        "Each server also sends a UDP load report to the controller every second.",
        "The controller marks a server UP when its reports arrive.",
    ])
    pause("Press Enter to start the servers ")
    state["servers"] = {s: start_server(state["net"], s) for s in SERVERS}
    if not state["log"].wait_for([f"[HEALTH] {s} is UP" for s in SERVERS], 20):
        bad("not all servers reported UP")
    time.sleep(0.3)
    state["log"].show(tags=("[HEALTH]",))
    ok("all 3 servers UP - socket layer is talking to the SDN controller")


def step_tcp(state):
    banner(4, "TCP socket protocol (client <-> one server directly)", [
        "Our own text protocol over TCP:  GET <file>  ->  OK <size> <server> + data",
        "PING -> PONG   |   missing file -> ERR 404",
    ])
    if not pause():
        return
    net = state["net"]
    heading("PING health check")
    run_on(net, "h1", "python3 app/client.py --server 10.0.0.11 --ping")
    heading("Download a 1 KB file from srv1")
    run_on(net, "h1", client_cmd("h1", "10.0.0.11", "small.txt"))
    heading("Ask for a file that does not exist")
    run_on(net, "h1", client_cmd("h1", "10.0.0.11", "nothing.txt", extra="--retries 0"))
    ok("server answers GET, PING and returns ERR 404 for a missing file")
    state["log"].new_lines()


def step_vip(state):
    banner(5, "Virtual IP 10.0.0.100 - the controller picks the server", [
        "Clients only know ONE address: 10.0.0.100. No machine owns it.",
        "On every new TCP connection the controller chooses a server and a path.",
        "Watch: 3 requests from h1 go to 3 different servers.",
    ])
    if not pause():
        return
    run_on(state["net"], "h1", client_cmd("h1", VIP, "medium.bin", count=3))
    state["log"].show(tags=("[LB]",))
    ok("same address, different servers - chosen by the controller")


def step_flows(state):
    banner(6, "The flow rules inside switch s1", [
        "The controller saw only the FIRST packet of each connection.",
        "It then installed rules that rewrite 10.0.0.100 <-> real server address.",
        "All other packets are handled by the switch alone (n_packets keeps growing).",
    ])
    if not pause():
        return
    net = state["net"]
    show_cmd("h1", client_cmd("h1", VIP, "large.bin") + " &")
    proc = net.get("h1").popen(client_cmd("h1", VIP, "large.bin").split(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True)
    time.sleep(2)
    heading("ovs-ofctl dump-flows s1   (only the virtual-IP rules)")
    rules = vip_flows("s1")
    for r in rules[:6]:
        print("  " + r)
    if not rules:
        bad("no VIP rules found (the download may have finished already)")
    print(f"\n  {YELLOW}nw_dst={VIP} ... set_field:10.0.0.1x->ip_dst  = request rewritten to a real server{RESET}")
    print(f"  {YELLOW}nw_src=10.0.0.1x ... set_field:{VIP}->ip_src = reply rewritten back to the VIP{RESET}")
    out, _ = proc.communicate(timeout=60)
    heading("download result")
    for line in out.strip().splitlines():
        print("  " + line)
    state["log"].show(tags=("[LB]",))


def step_concurrent(state):
    banner(7, "3 clients download 5 MB at the same time", [
        "The controller spreads them over the 3 servers, so each gets a full link.",
        f"Measured baseline (static policy, all on srv1): about {STATIC_BASELINE_MBPS} Mbps each.",
    ])
    if not pause():
        return
    net = state["net"]
    procs = {}
    for h in ("h1", "h2", "h3"):
        show_cmd(h, client_cmd(h, VIP, "large.bin") + " &")
        procs[h] = net.get(h).popen(client_cmd(h, VIP, "large.bin").split(),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    universal_newlines=True)
    rates = []
    for h, p in procs.items():
        out, _ = p.communicate(timeout=90)
        for line in out.strip().splitlines():
            print("  " + line)
            m = re.search(r"throughput ([\d.]+) Mbps", line)
            if m and "summary" not in line:
                rates.append(float(m.group(1)))
    state["log"].show(tags=("[LB]",))
    if rates:
        avg = sum(rates) / len(rates)
        ok(f"average {avg:.1f} Mbps per client  vs  ~{STATIC_BASELINE_MBPS} Mbps with one "
           f"static server  ({avg / STATIC_BASELINE_MBPS:.1f}x better)")


def step_server_failure(state):
    banner(8, "Server failure: srv2 crashes", [
        "We kill srv2. Its UDP reports stop, so after 3 s the controller marks it DOWN.",
        "New requests are then sent only to the servers that are still alive.",
    ])
    if not pause():
        return
    net, log = state["net"], state["log"]
    log.new_lines()
    show_cmd("srv2", "pkill -f name=srv2")
    net.get("srv2").cmd("pkill -f 'name=srv2'")
    print(f"  {DIM}waiting for the controller to notice...{RESET}")
    log.wait_for(["[HEALTH] srv2 is DOWN"], 10)
    log.show(tags=("[HEALTH]",))
    heading("h1 downloads 3 files through the virtual IP")
    run_on(net, "h1", client_cmd("h1", VIP, "medium.bin", count=3))
    log.show(tags=("[LB]",))
    ok("every request succeeded - srv2 was never chosen")

    pause("Press Enter to bring srv2 back ")
    show_cmd("srv2", "python3 app/server.py --name=srv2 &")
    state["servers"]["srv2"] = start_server(net, "srv2")
    log.wait_for(["[HEALTH] srv2 is UP"], 10)
    log.show(tags=("[HEALTH]",))
    ok("srv2 recovered and is used again automatically")


def ping_summary(net):
    out = net.get("h1").cmd("ping -c 3 -W 2 10.0.0.11")
    show_cmd("h1", "ping -c 3 srv1")
    for line in out.splitlines():
        if "packet loss" in line or "rtt" in line:
            print("  " + line)


def step_link_failure(state):
    banner(9, "BONUS - link failure: cut the fast link s1-s2", [
        "The switch tells the controller a port went down (OpenFlow PortStatus).",
        "The controller recomputes routes over the slow path s1-s3-s4.",
    ])
    if not pause():
        return
    net, log = state["net"], state["log"]
    log.new_lines()
    heading("before: fast path")
    ping_summary(net)
    show_cmd("mininet", "link s1 s2 down")
    net.configLinkStatus("s1", "s2", "down")
    log.wait_for(["[LINK]"], 5)
    time.sleep(1)
    log.show(tags=("[LINK]",))
    heading("after: traffic still flows, over the slower backup path")
    ping_summary(net)
    pause("Press Enter to repair the link ")
    show_cmd("mininet", "link s1 s2 up")
    net.configLinkStatus("s1", "s2", "up")
    log.wait_for(["is UP"], 5)
    time.sleep(1)
    log.show(tags=("[LINK]",))
    ok("link failure rerouted automatically; nothing was lost")


# ---------------------------------------------------------------------------
def give_back_to_user(path):
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None:
        return
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.chown(name, int(uid), int(gid))
            except OSError:
                pass


def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo:  sudo python3 demo/d1_demo.py")
    ryu = os.environ.get("RYU", default_ryu())
    if not os.path.exists(ryu):
        sys.exit(f"ryu-manager not found at {ryu}  (set RYU=/path/to/ryu-manager)")
    if not os.path.exists(os.path.join("app", "content", "large.bin")):
        sys.exit("Test files missing: run  python3 app/make_content.py  first")

    os.makedirs(OUT, exist_ok=True)
    setLogLevel("warning")
    print(f"{BOLD}SDN Content Delivery - live demo{RESET}")
    print(f"{DIM}Cleaning up any old Mininet/controller...{RESET}")
    subprocess.run(["pkill", "-f", "ryu-manager"], capture_output=True)
    cleanup()

    state = {"log": CtrlLog(CTRL_LOG), "net": None, "ctrl": None, "servers": {}}
    try:
        step_controller(ryu, state)
        step_network(state)
        step_servers(state)
        for step in (step_tcp, step_vip, step_flows, step_concurrent,
                     step_server_failure, step_link_failure):
            step(state)
        print(f"\n{BOLD}{GREEN}Demo finished.{RESET}")
        ans = input(f"{DIM}Type c + Enter to open the mininet> prompt for questions, "
                    f"or just Enter to stop everything: {RESET}").strip().lower()
        if ans == "c":
            print(f"{DIM}Type 'exit' when done.{RESET}")
            CLI(state["net"])
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopping...{RESET}")
    except Exception as e:
        bad(f"demo stopped: {e}")
        print(f"{DIM}Full controller log: {CTRL_LOG}{RESET}")
    finally:
        if state["net"] is not None:
            state["net"].stop()
        if state["ctrl"] is not None and state["ctrl"].poll() is None:
            os.killpg(state["ctrl"].pid, signal.SIGTERM)
        cleanup()
        give_back_to_user(OUT)
        print(f"{DIM}Everything stopped. Logs are in {OUT}/{RESET}")


if __name__ == "__main__":
    main()
