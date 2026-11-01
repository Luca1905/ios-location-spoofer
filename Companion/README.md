# Mac companion

Select locations in the iPhone app. The companion applies them using Apple's
paired-device developer tools, then the app verifies the result through Core
Location. The app uses the companion exclusively; no location-server certificate
or app VPN is required.

## Mac CLI

From the checkout, run `./location-spoofer`. To install a command independent of
the checkout (without restarting either phone's service):

```sh
./location-spoofer install-cli
location-spoofer status
location-spoofer doctor
```

The command lives at `~/.local/bin/location-spoofer`; add `~/.local/bin` to your
PATH if needed. Its scripts live in Application Support alongside the services.
Use a phone's printed name, configuration ID, UDID, or full service label:

```sh
location-spoofer doctor 'Phone Two'
location-spoofer logs 'Phone Two' --lines 40
location-spoofer logs 'Phone Two' --follow
location-spoofer restart 'Phone Two'
location-spoofer stop 'Phone Two'
location-spoofer start 'Phone Two'
location-spoofer install 'Phone Two'
location-spoofer status --json
```

`status` and `doctor` inspect all phones unless one is selected. Every command
that changes a service requires exactly one phone; ambiguous selectors fail
without changing anything. `install` refreshes that phone's installed scripts
and LaunchAgent. `start`, `stop`, and `restart` operate its existing LaunchAgent.
Stopping a companion may clear the simulation it applied.

`doctor` checks the LaunchAgent, Mac/phone Tailscale state, Apple pairing port,
saved relay address, and negotiated-port support. A running process or online
Tailscale peer alone is insufficient. Exit codes are 0 for reachable transport,
1 for connection problems, and 2 for command/configuration errors. Confirm
**Active** in the iPhone app for actual Core Location verification.

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
`launchd` restarts a crashed companion automatically. Its relay releases its
ports when its owning companion exits, so a restart does not leave competing
relay processes behind. Rerun the installer to refresh and restart a service;
it also removes any stale relay belonging to that phone.

On the iPhone, select a location and tap **Set Location**.
Wait for **Active**, which requires both a matching Mac receipt
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

### Keep two phones connected

Each additional phone needs its own configuration file, login service, and relay
address on this Mac. The original phone can keep the default LAN address; a
second phone can use the Mac's Tailscale IPv4 address from `tailscale ip -4`.
The address must already be assigned to this Mac. Each relay accepts connections
only from this Mac, including when it binds to the Tailscale interface.

For the second phone, choose a separate directory under Application Support and
pass the same absolute configuration path to both commands:

```sh
python3 Companion/companion.py --device 'second iPhone UDID' \
  --config "$HOME/Library/Application Support/Location Spoofer/second-phone/tailscale.json" \
  --configure-tailscale --tailscale-peer 'second-phone.your-tailnet.ts.net' \
  --service-identifier 'second phone Bonjour instance identifier' \
  --listen-address 'this Mac Tailscale IPv4 address'
python3 Companion/install_service.py \
  --config "$HOME/Library/Application Support/Location Spoofer/second-phone/tailscale.json"
```

The installer prints the service label and log path for that phone. Installing
or restarting its service leaves the other phone's service and configuration in
place. Device locks and Bonjour hostnames are separate for each phone. Two
relays cannot share an IPv4 address because Apple's tunnel ports can overlap.
Use each phone's printed service label for the stop/start commands below.

### Transport

Tailscale routes IP traffic but does not forward Bonjour discovery. The bridge
re-advertises the previously paired iPhone on the Mac's local interface and
relays Apple's pairing and subsequent TCP/UDP tunnel traffic to the iPhone's
Tailscale address. Apple authenticates and encrypts its pairing protocol end to
end. Only clients originating on this Mac may use the relay.

The initial relayed ports are TCP/UDP 49152 and 55000–56000. Apple's subsequent
tunnel port is dynamic and not a published stable API. The relay follows the
negotiated endpoint in macOS's RemotePairing log and opens a bounded 128-port
window around it, including ports outside that initial range. A first connection
may need a retry while that window opens. Each phone follows only endpoints for
its own relay address. Old dynamic listeners are released while active TCP/UDP
flows are preserved. At most 4096 ports are relayed per phone. The log reports
negotiated windows and local port conflicts. `--tunnel-ports '49152,other-range'`
can still configure a fixed window if macOS changes its endpoint log format.
A phone reset or changed Bonjour pairing record may require setup again while
the phone is locally available.

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

Only one companion runs per phone. Ctrl+C stops a terminal companion and clears
simulation if that process applied it and the phone remains reachable. Simulation
can persist after a disconnect. To clear it manually once reconnected:

```sh
python3 Companion/companion.py --device 'your iPhone name or UDID' --clear
```

## Validation

```sh
python3 -m unittest discover -s Companion -v
xcodebuild -project location-spoofer.xcodeproj -scheme location-spoofer \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

The automated transport tests exercise actual TCP and UDP forwarding, half-close
handling, client separation, local-client restrictions, discovery parsing, and
configuration bounds. Simulator UI verification covers app controls and Core
Location independently of the physical phone. Neither a simulator run nor a
local socket test proves a physical phone's connection from another network.
