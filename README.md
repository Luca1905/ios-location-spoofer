# iOS Location Spoofer

Choose a location in the iPhone app; a paired Mac applies it using Apple's
developer tools. The iPhone independently verifies the result through Core
Location before showing **Active**.

The app uses the Mac companion exclusively. The old VPN/certificate setup,
method switch, and location-service restart tutorials have been removed. The app
no longer embeds the packet-tunnel extension or requires the Go proxy to build.
An upgrade removes only this app's old VPN configuration and obsolete setup
flags, preserving favorites, recent locations, and companion requests. Previously
installed certificate profiles can be removed manually in iOS Settings.

## Setup and use

See [Mac companion setup](Companion/README.md) for pairing, Developer Mode,
Tailscale, automatic startup after login, and independent services for two or
more phones.

1. Pair the iPhone with the Mac and enable Developer Mode.
2. Configure and start the Mac companion.
3. For remote use, connect Tailscale on both devices and join a Wi-Fi network on
   the iPhone. Keep the Mac powered on, signed in, and online.
4. Select a location in the app and tap **Set Location**. Wait for **Active**.
5. Tap **Disable** to restore the real location; the app verifies restoration.

## Mac CLI

```sh
./location-spoofer install-cli
location-spoofer status
location-spoofer doctor 'Phone Two'
location-spoofer logs 'Phone Two'
location-spoofer restart 'Phone Two'
```

Names and configuration IDs are printed by `status`. Start/stop/restart/install
commands always target one explicit phone, leaving the others alone. See the
[CLI reference](Companion/README.md#mac-cli) for JSON output and exit codes.

## Build and validate

Open `location-spoofer.xcodeproj` in Xcode, select the `location-spoofer` scheme,
and configure your signing team to run on a personal iPhone. The app requires
iOS 18 or later. No Go build or Swift package dependencies are needed.

```sh
python3 -m unittest discover -s Companion -v
xcodebuild -project location-spoofer.xcodeproj -scheme location-spoofer \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

The Python tests cover two-phone isolation, CLI selection and diagnostics,
installation and restart recovery, negotiated tunnel ports, and actual local
TCP/UDP forwarding. Simulator tests cover app behavior and Core Location using a
simulator transport adapter. A physical Tailscale path must be verified on the
phone itself. Apps may detect Apple's software-simulated locations.

`Tunnel/` and `GoSpoofer/` retain the earlier location-server research sources;
they are not part of the app target.
