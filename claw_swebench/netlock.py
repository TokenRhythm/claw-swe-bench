
import fcntl
import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

NETWORK = os.environ.get("SWE_NETLOCK_NETWORK", "swe-locked")
SUBNET = os.environ.get("SWE_NETLOCK_SUBNET", "172.30.0.0/16")
GATEWAY = SUBNET.rsplit(".", 1)[0] + ".1"
CHAIN = "SWE-LOCK"
LOCK_FILE = "/tmp/swe-netlock.lock"
SNIPROXY_CONF = "/etc/sniproxy-swe/sniproxy.conf"
SNIPROXY_UNIT = "sniproxy-swe"


def _run(cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r


def _ipt_has(rule):
    return _run(["iptables", "-w", "5", "-C", *rule]).returncode == 0


def _ipt_ensure(rule, insert_pos=None):
    if _ipt_has(rule):
        return
    if insert_pos is None:
        _run(["iptables", "-w", "5", "-A", *rule], check=True)
    else:
        _run(["iptables", "-w", "5", "-I", rule[0], str(insert_pos), *rule[1:]], check=True)


def ensure_network():
    if _run(["docker", "network", "inspect", NETWORK]).returncode != 0:
        _run(["docker", "network", "create", "--driver", "bridge", "--subnet", SUBNET, NETWORK], check=True)
        logger.info("Created docker network %s (%s)", NETWORK, SUBNET)


def ensure_chain():
    if _run(["iptables", "-w", "5", "-L", CHAIN, "-n"]).returncode != 0:
        _run(["iptables", "-w", "5", "-N", CHAIN], check=True)
    _ipt_ensure(["DOCKER-USER", "-s", SUBNET, "-j", CHAIN], insert_pos=1)
    _ipt_ensure([CHAIN, "-s", SUBNET, "-j", "DROP"])
    _ipt_ensure(["INPUT", "-s", SUBNET, "-p", "tcp", "-m", "multiport", "--dports", "443,18000:18199", "-j", "ACCEPT"], insert_pos=1)
    _ipt_ensure(["INPUT", "-s", SUBNET, "-j", "DROP"], insert_pos=2)
    for line in _run(["iptables", "-w", "5", "-S", CHAIN]).stdout.splitlines():
        if "-j RETURN" in line and line.startswith("-A"):
            _run(["iptables", "-w", "5", "-D", *line.split()[1:]])


def sniproxy_allowed_domains():
    try:
        text = open(SNIPROXY_CONF).read()
    except OSError:
        return set()
    table = re.search(r"table\s+swe_allow\s*\{(.*?)\}", text, re.S)
    if not table:
        return set()
    doms = set()
    for ln in table.group(1).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        pat = ln.split()[0]
        doms.add(pat.strip("^$").replace("\\.", "."))
    return doms


def ensure_sniproxy():
    r = _run(["systemctl", "is-active", SNIPROXY_UNIT])
    if r.stdout.strip() != "active":
        _run(["systemctl", "start", SNIPROXY_UNIT])
        r = _run(["systemctl", "is-active", SNIPROXY_UNIT])
        if r.stdout.strip() != "active":
            raise RuntimeError(f"{SNIPROXY_UNIT} is not running; refusing to start an agent container "
                               f"without the egress proxy (LLM traffic would be dropped)")


def docker_args(whitelist):
    whitelist = (whitelist or "").strip()
    if not whitelist or whitelist.lower() == "off":
        return []
    with open(LOCK_FILE, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            ensure_network()
            ensure_chain()
            ensure_sniproxy()
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
    allowed = sniproxy_allowed_domains()
    args = ["--network", NETWORK, "--dns", "127.0.0.1"]
    for domain in whitelist.split(","):
        domain = domain.strip()
        if not domain or domain.lower() == "on":
            continue
        if domain not in allowed:
            raise RuntimeError(f"whitelist domain {domain!r} is not in the SNI proxy allow table "
                               f"({SNIPROXY_CONF}); add it there and restart {SNIPROXY_UNIT}")
        args += ["--add-host", f"{domain}:{GATEWAY}"]
    logger.info("Network egress lock active: %s", args)
    return args
