import SwiftUI

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var showResetConfirm = false
    @State private var showCopiedToast = false
    @ObservedObject private var diagLog = DiagLog.shared

    var body: some View {
        NavigationView {
            List {
                Section("Mac Companion") {
                    Text("Choose a location while your Mac companion is running. Connect by USB, paired Wi-Fi, or Tailscale.")
                    Text("For remote access, connect Tailscale on both devices and join a Wi-Fi network on this iPhone. Keep the Mac powered on and signed in.")
                        .font(.footnote)
                        .foregroundColor(.secondary)
                }
                // 关于
                Section("About") {
                    HStack {
                        Text("Location Spoofer")
                        Spacer()
                        Text("Build \(buildNumber)")
                            .foregroundColor(.secondary)
                    }
                    HStack {
                        Text("Version")
                        Spacer()
                        Text(appVersion)
                            .foregroundColor(.secondary)
                    }
                }

                // 诊断信息
                Section("Diagnostics (use when contacting support)") {
                    diagnosticRow(label: "Location Method", value: "Mac Companion")
                    diagnosticRow(label: "Companion Status", value: MacCompanion.shared.state.primaryText)
                    diagnosticRow(label: "Current Spoofed Location", value: currentLocationText)
                    diagnosticRow(label: "Current Coordinates", value: currentCoordinatesText)

                    Button(action: { copyDiagnostic() }) {
                        HStack {
                            Image(systemName: "doc.on.doc")
                            Text(showCopiedToast ? "Copied" : "Copy Diagnostics (incl. flow logs)")
                            Spacer()
                        }
                        .foregroundColor(showCopiedToast ? .green : .blue)
                    }
                }

                // 流程日志
                Section(header: HStack {
                    Text("Flow Logs (most recent \(diagLog.entries.count))")
                    Spacer()
                    Button("Clear") { diagLog.clear() }
                        .font(.caption)
                        .foregroundColor(.red)
                        .disabled(diagLog.entries.isEmpty)
                }) {
                    if diagLog.entries.isEmpty {
                        Text("No logs yet. Run \"Set as Location\" once and come back.")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    } else {
                        ForEach(diagLog.entries.reversed()) { entry in
                            Text(entry.formatted)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .lineLimit(nil)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }

                // 危险操作
                Section("Advanced") {
                    Button(role: .destructive, action: { showResetConfirm = true }) {
                        HStack {
                            Image(systemName: "arrow.counterclockwise")
                            Text("Reset App (clears all settings and favorites)")
                        }
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .alert("Confirm Reset App?", isPresented: $showResetConfirm) {
                Button("Cancel", role: .cancel) { }
                Button("Reset", role: .destructive) { resetApp() }
            } message: {
                Text("This asks your Mac to restore your real location and clears this app's settings and favorites.")
            }
        }
    }

    // MARK: - 诊断信息收集

    @ViewBuilder
    private func diagnosticRow(label: String, value: String) -> some View {
        HStack {
            Text(label)
            Spacer()
            Text(value)
                .foregroundColor(.secondary)
                .font(.system(.body, design: .monospaced))
        }
    }

    private var appVersion: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown"
    }

    private var buildNumber: String {
        Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "unknown"
    }

    private var currentLocationText: String {
        UserDefaults.standard.string(forKey: "currentLocationName") ?? "None"
    }

    private var currentCoordinatesText: String {
        guard let coords = LocationConfiguration.shared.currentCoordinates else {
            return "None"
        }
        return String(format: "%.6f, %.6f", coords.latitude, coords.longitude)
    }

    private func copyDiagnostic() {
        let logBlock = diagLog.entries.isEmpty
            ? "(no logs)"
            : diagLog.entries.map { $0.formatted }.joined(separator: "\n")

        let info = """
        Location Spoofer Diagnostics
        ─────────────
        App version: \(appVersion)
        Build: \(buildNumber)
        Location method: Mac Companion
        Companion status: \(MacCompanion.shared.state.primaryText)
        Current spoofed location: \(currentLocationText)
        Current coordinates: \(currentCoordinatesText)

        ─── Flow Logs ───
        \(logBlock)
        """
        UIPasteboard.general.string = info
        showCopiedToast = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            showCopiedToast = false
        }
    }

    // MARK: - 重置 App

    private func resetApp() {
        MacCompanion.shared.clearLocation()
        // 清坐标
        LocationConfiguration.shared.clearCoordinates()
        // 清所有 UserDefaults key
        let keys = [
            "currentLocationName", "savedLocations", "recentLocations"
        ]
        for key in keys {
            UserDefaults.standard.removeObject(forKey: key)
        }
        // 清诊断日志
        DiagLog.shared.clear()
        dismiss()
    }
}
