import CoreLocation
import Foundation

/// Exchanges commands with the paired Mac through the development app container.
/// A successful Mac receipt is followed by an independent Core Location check.
final class MacCompanion: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = MacCompanion()

    struct Request: Codable {
        let version: Int
        let id: String
        let enabled: Bool
        let latitude: Double
        let longitude: Double
        let name: String
        let createdAt: TimeInterval
    }

    private struct Receipt: Decodable {
        let requestId: String
        let success: Bool
        let error: String?
    }

    @Published private(set) var state: SpoofingState = .off
    @Published private(set) var observedLocation: CLLocation?
    @Published private(set) var isBusy = false

    private let locationManager = CLLocationManager()
    private var timer: Timer?
    private var request: Request?
    private var receipt: Receipt?
    private var lastReceiptData: Data?
    private var lastLocationRequest = Date.distantPast
    private var verificationStarted = Date()
    private let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]

    private override init() {
        super.init()
        locationManager.delegate = self
        locationManager.desiredAccuracy = kCLLocationAccuracyBest
        try? FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
    }

    func start() {
        guard timer == nil else { return }
        if let data = try? Data(contentsOf: requestURL),
           let saved = try? JSONDecoder().decode(Request.self, from: data) {
            request = saved
        }
        readReceipt()
        requestLocation()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.readReceipt()
            if case .on = self.state, Date().timeIntervalSince(self.lastLocationRequest) >= 5 {
                self.requestLocation()
            }
            if self.isBusy {
                if Date().timeIntervalSince(self.lastLocationRequest) >= 2 { self.requestLocation() }
                if self.request != nil, Date().timeIntervalSince(self.verificationStarted) > 30 {
                    self.isBusy = false
                    let reason = self.receipt?.success == true
                        ? "Mac applied the change, but iPhone location verification timed out. Allow location access and tap Retry."
                        : "Mac companion unavailable. Keep the Mac online. For remote access, connect Tailscale on both devices and keep iPhone on Wi-Fi. Otherwise connect by USB or paired Wi-Fi."
                    self.state = .failed(reason: reason)
                }
            }
        }
    }

    func refresh() {
        start()
        readReceipt()
        requestLocation()
    }

    func setLocation(latitude: Double, longitude: Double, name: String) {
        send(enabled: true, latitude: latitude, longitude: longitude, name: name)
    }

    func clearLocation() {
        send(enabled: false, latitude: 0, longitude: 0, name: "")
    }

    func retry() {
        guard let request else { return }
        send(enabled: request.enabled, latitude: request.latitude, longitude: request.longitude, name: request.name)
    }

    private var requestURL: URL { documents.appendingPathComponent("location-request.json") }

    private func send(enabled: Bool, latitude: Double, longitude: Double, name: String) {
        start()
        let command = Request(version: 1, id: UUID().uuidString, enabled: enabled,
                              latitude: latitude, longitude: longitude, name: name,
                              createdAt: Date().timeIntervalSince1970)
        do {
            try JSONEncoder().encode(command).write(to: requestURL, options: .atomic)
            request = command
            receipt = nil
            lastReceiptData = nil
            verificationStarted = Date()
            isBusy = true
            state = .pending(name: name, isClosing: !enabled)
            DiagLog.add("[companion] Sent \(enabled ? "set" : "clear") request \(command.id)")
        } catch {
            isBusy = false
            state = .failed(reason: "Could not send location to Mac: \(error.localizedDescription)")
        }
    }

    private func readReceipt() {
        let url = documents.appendingPathComponent("location-response.json")
        guard let data = try? Data(contentsOf: url), data != lastReceiptData,
              let reply = try? JSONDecoder().decode(Receipt.self, from: data),
              let request, reply.requestId == request.id else { return }
        lastReceiptData = data
        receipt = reply
        DiagLog.add("[companion] Mac acknowledged \(reply.requestId), success=\(reply.success)")
        guard reply.success else {
            isBusy = false
            state = .failed(reason: reply.error ?? "Mac could not apply this location.")
            return
        }
        verificationStarted = Date()
        isBusy = true
        state = .pending(name: request.name, isClosing: !request.enabled)
        requestLocation()
    }

    private func requestLocation() {
        lastLocationRequest = Date()
        switch locationManager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            locationManager.requestLocation()
        case .notDetermined:
            locationManager.requestWhenInUseAuthorization()
        default:
            if isBusy {
                state = .failed(reason: "Allow location access in Settings to verify spoofing.")
                isBusy = false
            }
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        requestLocation()
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last, location.horizontalAccuracy >= 0 else { return }
        observedLocation = location
        let simulated = location.sourceInformation?.isSimulatedBySoftware == true
        DiagLog.add("[companion] Core Location (\(location.coordinate.latitude), \(location.coordinate.longitude)) simulated=\(simulated) accuracy=\(location.horizontalAccuracy)m timestamp=\(location.timestamp)")
        guard let request, receipt?.success == true,
              location.timestamp.timeIntervalSince1970 >= request.createdAt - 2 else { return }
        if !request.enabled {
            if !simulated {
                isBusy = false
                state = .off
                LocationConfiguration.shared.clearCoordinates()
                UserDefaults.standard.removeObject(forKey: "currentLocationName")
                DiagLog.add("[companion] Verified real location restored")
            }
            return
        }
        let target = CLLocation(latitude: request.latitude, longitude: request.longitude)
        if simulated && location.distance(from: target) < 50 {
            isBusy = false
            state = .on(name: request.name)
            DiagLog.add("[companion] Verified target through Core Location")
        } else if case .on = state {
            state = .failed(reason: "iPhone is no longer reporting the selected location. Select it again with your Mac connected.")
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        DiagLog.add("[companion] Location verification: \(error.localizedDescription)")
    }
}
