#!/usr/bin/env python3
"""Manage each phone's Mac companion without affecting the other phones."""
import argparse
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

from install_service import install, service_label
from tailscale_bridge import CONFIG, tailnet_host


@dataclass
class Phone:
    path: Path
    config: dict
    name: str
    slug: str
    label: str

    @property
    def plist(self):
        return Path.home() / "Library/LaunchAgents" / f"{self.label}.plist"

    @property
    def target(self):
        return f"gui/{os.getuid()}/{self.label}"


def discover(root):
    phones = []
    for path in sorted(root.rglob("tailscale.json")):
        config = json.loads(path.read_text())
        tailnet_host(config["peer"])
        slug = "default" if path.parent == root else str(path.parent.relative_to(root))
        name = config.get("service", {}).get("originalHost", slug).removesuffix(".").removesuffix(".local").replace("-", " ")
        phones.append(Phone(path, config, name, slug, service_label(path, config["device"])))
    return phones


def select(phones, selector):
    if selector is None:
        return phones
    key = selector.casefold()
    matches = [p for p in phones if key in {
        p.name.casefold(), p.slug.casefold(), p.label.casefold(),
        p.config["device"].casefold(), p.config["peer"].casefold(),
        p.config["peer"].split(".")[0].casefold(),
    }]
    if not matches:
        matches = [p for p in phones if key in p.name.casefold() or key in p.slug.casefold()]
    if len(matches) != 1:
        raise ValueError(f"Phone {selector!r} is unknown or ambiguous. Run 'location-spoofer status' and use its name or ID.")
    return matches


def command(args, check=False):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"Command failed: {args[0]}")
    return result


def status(phone):
    result = command(["launchctl", "print", phone.target])
    state = re.search(r"^\s*state = (.+)$", result.stdout, re.M)
    pid = re.search(r"^\s*pid = (\d+)$", result.stdout, re.M)
    return {"name": phone.name, "id": phone.slug, "device": phone.config["device"],
            "peer": phone.config["peer"], "service": phone.label,
            "state": state[1] if state else "unloaded", "pid": int(pid[1]) if pid else None,
            "configuration": str(phone.path), "installed": phone.plist.is_file()}


def pairing_probe(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except (TimeoutError, socket.timeout):
        return "timeout"
    except OSError as error:
        return str(error)


def diagnose(phone, tailnet):
    report = status(phone)
    peer = next((p for p in tailnet.get("Peer", {}).values()
                 if phone.config["peer"].rstrip(".").casefold() in
                 [str(p.get("DNSName", "")).rstrip(".").casefold(), *p.get("TailscaleIPs", [])]), {})
    report.update(macTailscale=tailnet.get("BackendState", "unavailable"),
                  phoneOnline=peer.get("Online") is True,
                  phoneAddresses=peer.get("TailscaleIPs", []),
                  pairingPort=phone.config["service"]["port"])
    report["pairingConnection"] = pairing_probe(phone.config["peer"], report["pairingPort"])
    problems = []
    if report["state"] != "running":
        problems.append("Companion is stopped. Run start for this phone, or install if its LaunchAgent is missing.")
    if report["macTailscale"] != "Running":
        problems.append("Connect Tailscale on this Mac.")
    if not report["phoneOnline"]:
        problems.append("Phone is offline in this tailnet. Open Tailscale on the phone and connect it.")
    if report["pairingConnection"] != "open":
        problems.append("Apple pairing service is unreachable. Join Wi-Fi on the phone (cellular or hosting a hotspot is insufficient), unlock it, and check Developer Mode. If it persists, refresh pairing discovery while locally connected.")
    address = phone.config.get("listenAddress")
    if address and address not in tailnet.get("Self", {}).get("TailscaleIPs", []):
        problems.append("Saved relay address is no longer assigned to this Mac; refresh this phone's setup.")
    bridge = phone.path.with_name("tailscale_bridge.py")
    report["dynamicTunnelPorts"] = bridge.is_file() and "watch_tunnel_ports" in bridge.read_text()
    if not report["dynamicTunnelPorts"]:
        problems.append("Install the current companion to enable negotiated tunnel ports.")
    log = phone.path.with_name("companion.log")
    if log.is_file():
        with log.open(errors="replace") as stream:
            events = [line.strip() for line in deque(stream, maxlen=200)
                      if line.startswith(("Applied ", "Cleared ", "Following Apple tunnel port"))]
        report["lastRecordedActivity"] = events[-1] if events else None
    report["problems"] = problems
    report["transportReachable"] = not problems
    return report


def control(phone, action):
    if action == "install":
        install(phone.path)
        return
    loaded = command(["launchctl", "print", phone.target]).returncode == 0
    if action == "stop":
        if loaded:
            command(["launchctl", "bootout", phone.target], check=True)
        return
    if not loaded:
        if not phone.plist.is_file():
            raise ValueError("LaunchAgent is missing. Run install for this phone first.")
        command(["launchctl", "enable", phone.target], check=True)
        command(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(phone.plist)], check=True)
    args = ["launchctl", "kickstart"]
    if action == "restart":
        args.append("-k")
    command([*args, phone.target], check=True)


def install_cli(root, destination=None):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("cli.py", "install_service.py", "companion.py", "tailscale_bridge.py"):
        source, target = Path(__file__).with_name(name), root / name
        if source.resolve() != target.resolve():
            temporary = target.with_suffix(".tmp")
            shutil.copyfile(source, temporary)
            temporary.chmod(0o700)
            temporary.replace(target)
    destination = destination or Path.home() / ".local/bin/location-spoofer"
    if destination.exists() and "from cli import main" not in destination.read_text():
        raise ValueError(f"Refusing to replace another command: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(f"#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, {str(root)!r})\nfrom cli import main\nraise SystemExit(main())\n")
    destination.chmod(0o700)
    return destination


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("status", "doctor", "start", "stop", "restart", "install", "logs"):
        child = commands.add_parser(name)
        child.add_argument("phone", nargs="?" if name in ("status", "doctor") else None,
                           help="Phone name, configuration ID, UDID, or service label")
        if name in ("status", "doctor"):
            child.add_argument("--json", action="store_true")
        if name == "logs":
            child.add_argument("--lines", type=int, default=30)
            child.add_argument("--follow", action="store_true")
    commands.add_parser("install-cli", help="Install ~/.local/bin/location-spoofer without restarting services")
    args = parser.parse_args(argv)
    root = root or CONFIG.parent
    try:
        if args.action == "install-cli":
            print(install_cli(root))
            return 0
        phones = select(discover(root), args.phone)
        if not phones:
            raise ValueError(f"No configured phones in {root}. See Companion/README.md for setup.")
        if args.action in ("status", "doctor"):
            tailnet = {}
            if args.action == "doctor":
                result = command(["tailscale", "status", "--json"])
                tailnet = json.loads(result.stdout) if result.returncode == 0 else {}
            reports = [diagnose(p, tailnet) if args.action == "doctor" else status(p) for p in phones]
            if args.json:
                print(json.dumps(reports, indent=2))
            else:
                for item in reports:
                    print(f"{item['name']} [{item['id']}]: {item['state']}" + (f" (PID {item['pid']})" if item['pid'] else ""))
                    print(f"  Service: {item['service']}\n  Device: {item['device']}\n  Peer: {item['peer']}")
                    if args.action == "doctor":
                        print(f"  Tailscale: Mac {item['macTailscale']}, phone {'online' if item['phoneOnline'] else 'offline'}")
                        print(f"  Apple pairing: {item['pairingPort']} {item['pairingConnection']}; negotiated ports: {'enabled' if item['dynamicTunnelPorts'] else 'missing'}")
                        for problem in item["problems"]:
                            print(f"  Next: {problem}")
                        if not item["problems"]:
                            print("  Transport reachable. Confirm Active in the iPhone app to verify its location.")
            return int(args.action == "doctor" and any(r["problems"] for r in reports))
        phone = phones[0]
        if args.action == "logs":
            if not 1 <= args.lines <= 10000:
                raise ValueError("Choose 1–10000 log lines.")
            paths = [phone.path.with_name(n) for n in ("companion.log", "companion-error.log")]
            paths = [str(p) for p in paths if p.is_file()]
            if not paths:
                raise ValueError("No logs yet. Start this phone's companion first.")
            return subprocess.call(["tail", "-n", str(args.lines), *(["-F"] if args.follow else []), *paths])
        control(phone, args.action)
        print(f"{phone.name}: {args.action} completed ({phone.label})")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"location-spoofer: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
