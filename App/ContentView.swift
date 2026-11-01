import SwiftUI
import NetworkExtension

struct ContentView: View {
    var body: some View {
        MapHomeView()
            .task { retireLegacyConfiguration() }
    }

    private func retireLegacyConfiguration() {
        for key in ["useMacCompanion", "firstSetupCompleted", "certDownloaded", "certInstalled", "certTrusted"] {
            UserDefaults.standard.removeObject(forKey: key)
        }
        // Retire only this app's old tunnel. Never stop or remove Tailscale.
        NETunnelProviderManager.loadAllFromPreferences { managers, error in
            if let error {
                DiagLog.add("[migration] Could not inspect old app VPN: \(error.localizedDescription)")
                return
            }
            for manager in managers ?? [] {
                guard let configuration = manager.protocolConfiguration as? NETunnelProviderProtocol,
                      configuration.providerBundleIdentifier == "com.luca1905.location-spoofer.tunnel" else { continue }
                manager.connection.stopVPNTunnel()
                manager.removeFromPreferences { error in
                    if let error {
                        DiagLog.add("[migration] Could not remove old app VPN: \(error.localizedDescription)")
                    } else {
                        DiagLog.add("[migration] Removed old app VPN")
                    }
                }
            }
        }
    }
}

#Preview {
    ContentView()
}
