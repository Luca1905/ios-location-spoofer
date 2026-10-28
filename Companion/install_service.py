#!/usr/bin/env python3
"""Install this user's companion as a login service, with its remote configuration."""
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

LABEL = "com.luca1905.location-spoofer.companion"


def install():
    if not CONFIG.is_file():
        raise RuntimeError("Configure remote access first: python3 companion.py --configure-tailscale")
    root = CONFIG.parent
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
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [interpreter, "-u", str(root / "companion.py")],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(root / "companion.log"),
        "StandardErrorPath": str(root / "companion-error.log"),
        # GUI app PATH may omit the Tailscale/Homebrew command-line tools.
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    # Stop the prior standalone copy before launchd starts the installed copy.
    # Verify the exact script path before signalling a process holding the lock.
    owners = subprocess.run(["/usr/sbin/lsof", "-t", str(Path(tempfile.gettempdir()) / "location-spoofer-companion.lock")],
                            capture_output=True, text=True).stdout.split()
    expected = (str(Path(__file__).with_name("companion.py").resolve()), str(root / "companion.py"))
    for owner in set(owners):
        pid = int(owner)
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        if any(script in command for script in expected):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            for _ in range(250):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("The previous companion is still stopping; close its Terminal and retry setup.")
        else:
            raise RuntimeError("Another companion owns the device lock. Close it before installing this copy.")
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    subprocess.run(["launchctl", "kickstart", f"{domain}/{LABEL}"], check=True)
    print("Remote companion installed. It starts at login and keeps this Mac awake while running.")
    print(f"Logs: {root / 'companion.log'}")


if __name__ == "__main__":
    install()
