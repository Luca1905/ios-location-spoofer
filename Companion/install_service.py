#!/usr/bin/env python3
"""Install this user's companion as a login service, with its remote configuration."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import signal
import tempfile
import time

from tailscale_bridge import CONFIG
from companion import lock_path

LABEL = "com.luca1905.location-spoofer.companion"


def process_command(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def stop_owned_process(pid, expected):
    command = process_command(pid)
    if not command:
        return  # It can exit between lsof's snapshot and ps.
    if not any(script in command for script in expected):
        raise RuntimeError("Another companion owns the device lock. Close it before installing this copy.")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(250):
        current = process_command(pid)
        if not current or current != command:
            return
        time.sleep(0.1)
    raise RuntimeError("The previous companion is still stopping; close its Terminal and retry setup.")


def stop_orphaned_bridges(root, config_path):
    # A prior interrupted shutdown can leave the relay holding Apple's ports.
    # Match this phone's exact script AND configuration, leaving other phones alone.
    expected = f"{root / 'tailscale_bridge.py'} --config {config_path}"
    processes = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    for line in processes.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and expected in fields[1]:
            stop_owned_process(int(fields[0]), (expected,))


def service_label(config_path, device):
    if config_path.resolve() == CONFIG.resolve():
        return LABEL
    return f"{LABEL}.{lock_path(device).stem.removeprefix('location-spoofer-companion-')}"


def install(config_path=CONFIG):
    config_path = config_path.expanduser().resolve()
    if not config_path.is_file():
        raise RuntimeError("Configure remote access first: python3 companion.py --configure-tailscale")
    device = json.loads(config_path.read_text())["device"]
    label = service_label(config_path, device)
    root = config_path.parent
    root.mkdir(parents=True, exist_ok=True)
    for name in ("companion.py", "tailscale_bridge.py"):
        source = Path(__file__).with_name(name)
        destination = root / name
        if source.resolve() != destination.resolve():
            temporary = destination.with_suffix(".tmp")
            shutil.copyfile(source, temporary)
            temporary.chmod(0o700)
            temporary.replace(destination)
    interpreter = shutil.which("python3") or sys.executable
    plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [interpreter, "-u", str(root / "companion.py"), "--config", str(config_path)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(root / "companion.log"),
        "StandardErrorPath": str(root / "companion-error.log"),
        # GUI app PATH may omit the Tailscale/Homebrew command-line tools.
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    # Stop the prior standalone copy before launchd starts the installed copy.
    # Verify the exact script path before signalling a process holding the lock.
    locks = [lock_path(device)]
    if label == LABEL:
        locks.append(Path(tempfile.gettempdir()) / "location-spoofer-companion.lock")
    owners = subprocess.run(["/usr/sbin/lsof", "-t", *map(str, locks)],
                            capture_output=True, text=True).stdout.split()
    expected = (str(Path(__file__).with_name("companion.py").resolve()), str(root / "companion.py"))
    for owner in set(owners):
        stop_owned_process(int(owner), expected)
    stop_orphaned_bridges(root, config_path)
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    subprocess.run(["launchctl", "kickstart", f"{domain}/{label}"], check=True)
    print("Remote companion installed. It starts at login and keeps this Mac awake while running.")
    print(f"Logs: {root / 'companion.log'}")
    print(f"Service: {label}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    install(parser.parse_args().config)
