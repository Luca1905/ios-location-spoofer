# Mac companion

Select locations in the iPhone app. The companion applies them using Apple's
paired-device developer tools, then the app verifies the result through Core
Location. The default method needs no location-server certificate.

## Remote access over Tailscale

Requirements:

- This Mac has Xcode, Python 3, and Tailscale connected.
- The iPhone is paired with this Mac and Developer Mode is enabled.
- The iPhone has Tailscale connected and is joined to Wi-Fi at its current
  location. It can be a different Wi-Fi network from the Mac's.
- Tailnet access rules permit this Mac to reach the iPhone's developer services.
- The Mac stays powered on, logged in, and connected to the internet.

While the paired iPhone is still available locally, double-click
**Setup Remote Companion.command**. This saves the phone's Bonjour pairing
record and Tailscale address, installs a service for the current Mac user, and
starts it. Setup does not change tailnet access rules or expose a public API.

The service starts at login and prevents idle system sleep while it is running.
It waits and reconnects when the phone is offline. Closing Terminal does not stop
this installed service. Logs and configuration are in
`~/Library/Application Support/Location Spoofer/`.

On the iPhone, leave **Use Mac Companion** enabled. Select a location and tap
**Set Location**. Wait for **Active**, which requires both a matching Mac receipt
and a recent software-simulated Core Location reading at the target. Tap
**Disable** to clear simulation; the app waits for a non-simulated reading before
showing it as off. Unlock the phone if Apple's developer service requests it.

This is a Wi-Fi remote connection. Cellular-only operation is not covered by this
setup. iOS apps may detect Apple's software-simulated locations.

For multiple iPhones or a specific tailnet peer, run setup explicitly:

```sh
python3 Companion/companion.py --device 'your iPhone UDID' \
  --configure-tailscale --tailscale-peer 'iphone.your-tailnet.ts.net'
python3 Companion/install_service.py
```

If several phones advertise pairing services, pass `--service-identifier` with
this phone's `_remotepairing._tcp` Bonjour instance identifier.

### Transport

Tailscale routes IP traffic but does not forward Bonjour discovery. The bridge
re-advertises the previously paired iPhone on the Mac's local interface and
relays Apple's pairing and subsequent TCP/UDP tunnel traffic to the iPhone's
Tailscale address. Apple authenticates and encrypts its pairing protocol end to
end. Only clients originating on this Mac may use the relay.

The default relayed ports are TCP/UDP 49152 and 55000–56000. Apple's subsequent
tunnel port is dynamic and not a published stable API. If an iOS update allocates
outside that window, rerun setup with `--tunnel-ports '49152,other-range'` and
reinstall the service. At most 4096 ports may be selected. The log reports local
port conflicts. A phone reset or changed Bonjour pairing record may require
setup again while the phone is locally available.

To stop the installed service:

```sh
launchctl bootout "gui/$(id -u)/com.luca1905.location-spoofer.companion"
```

To start it again:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.luca1905.location-spoofer.companion.plist"
```

To remove it permanently, stop it and remove that LaunchAgent plist. The scripts
and configuration in Application Support can then also be removed.

## USB or local paired Wi-Fi

Double-click **Start Companion.command** and leave Terminal open. This uses the
saved remote configuration if present; otherwise it automatically chooses the
single connected paired physical iPhone. To force local-only mode:

```sh
python3 Companion/companion.py --device 'your iPhone name or UDID' --local-only
```

Only one companion runs at a time. Ctrl+C stops a terminal companion and clears
simulation if that process applied it and the phone remains reachable. Simulation
can persist after a disconnect. To clear it manually once reconnected:

```sh
python3 Companion/companion.py --device 'your iPhone name or UDID' --clear
```

## Validation

```sh
python3 -m unittest discover -s Companion -v
bash GoSpoofer/make.sh --simulator
```

The automated transport tests exercise actual TCP and UDP forwarding, half-close
handling, client separation, local-client restrictions, discovery parsing, and
configuration bounds. Simulator UI verification covers app controls and Core
Location independently of the physical phone. Neither a simulator run nor a
local socket test proves a physical phone's connection from another network.
