import SwiftUI
import MapKit
/// App 启动时单次定位用户真实位置(GCJ-02 在中国大陆,iOS 自动偏移)。
/// 拿到位置就调 onLocation 回调,失败/拒权默默忽略,保持地图默认区域。
private final class LocationFetcher: NSObject, ObservableObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    var onLocation: ((CLLocationCoordinate2D) -> Void)?

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyKilometer
    }

    func requestOneShot() {
        manager.requestWhenInUseAuthorization()
        switch manager.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways:
            manager.requestLocation()
        default:
            break  // 等 locationManagerDidChangeAuthorization 触发
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if manager.authorizationStatus == .authorizedWhenInUse || manager.authorizationStatus == .authorizedAlways {
            manager.requestLocation()
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let loc = locations.last else { return }
        DiagLog.add("[location] iOS reported (\(loc.coordinate.latitude), \(loc.coordinate.longitude)) accuracy=\(loc.horizontalAccuracy)m timestamp=\(loc.timestamp)")
        DispatchQueue.main.async {
            self.onLocation?(loc.coordinate)
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        DiagLog.add("[location] Failed: \(error.localizedDescription)")
    }
}

/// 包装 MKLocalSearchCompleter 做搜索框实时输入联想。
/// completer 回调默认主线程,@Published 更新走 main async 保险。
private final class SearchCompleterHelper: NSObject, ObservableObject, MKLocalSearchCompleterDelegate {
    private let completer = MKLocalSearchCompleter()
    @Published var results: [MKLocalSearchCompletion] = []

    override init() {
        super.init()
        completer.delegate = self
        completer.resultTypes = [.pointOfInterest, .address]
    }

    func updateQuery(_ fragment: String) {
        if fragment.isEmpty {
            results = []
            return
        }
        completer.queryFragment = fragment
    }

    func clear() {
        results = []
    }

    func completerDidUpdateResults(_ completer: MKLocalSearchCompleter) {
        DispatchQueue.main.async {
            self.results = completer.results
        }
    }

    func completer(_ completer: MKLocalSearchCompleter, didFailWithError error: Error) {
        DispatchQueue.main.async {
            self.results = []
        }
    }
}

struct MapHomeView: View {
    @ObservedObject private var companion = MacCompanion.shared
    @Environment(\.scenePhase) private var scenePhase
    @State private var mapSelection: MapSelection<MKMapItem>?
    @State private var cameraPosition: MapCameraPosition = .region(
        MKCoordinateRegion(
            center: CLLocationCoordinate2D(latitude: 39.9042, longitude: 116.4074),
            span: MKCoordinateSpan(latitudeDelta: 0.05, longitudeDelta: 0.05)
        )
    )
    @State private var searchText: String = ""
    @State private var selectedCoordinate: CLLocationCoordinate2D?
    @State private var selectedLocationName: String?
    @State private var showLocationSheet = false
    @State private var showDisableGuide = false
    @State private var savedLocations: [SavedLocation] = []
    @State private var recentLocations: [SavedLocation] = []
    @State private var showFavoritesSheet = false
    @State private var showSettings = false
    @State private var spoofingState: SpoofingState = .off
    @State private var justFavorited: Bool = false
    @State private var isSpoofing: Bool = false
    @StateObject private var locationFetcher = LocationFetcher()
    @StateObject private var searchCompleter = SearchCompleterHelper()
    @State private var lastKnownUserCoord: CLLocationCoordinate2D? = nil

    var body: some View {
        ZStack {
            // 全屏地图
            MapReader { proxy in
                Map(position: $cameraPosition, selection: $mapSelection) {
                    if let coord = selectedCoordinate {
                        Marker("Target Location", coordinate: coord)
                            .tint(.red)
                    }
                    UserAnnotation()
                }
                .mapStyle(.standard(pointsOfInterest: .all))
                .mapFeatureSelectionAccessory(.automatic)
                .ignoresSafeArea()
                .onTapGesture(coordinateSpace: .global) { tapLocation in
                    // 空白点击才取坐标(POI 内置选中由 selection 绑定处理)
                    if let coordinate = proxy.convert(tapLocation, from: .global) {
                        selectCoordinate(coordinate)
                    }
                }
                .onChange(of: mapSelection) { _, newSelection in
                    handleMapSelection(newSelection)
                }
            }

            // 底部叠层:右下角"回到我的位置"按钮 + 底部卡片
            VStack(spacing: 0) {
                Spacer()
                HStack {
                    Spacer()
                    if let userCoord = lastKnownUserCoord {
                        Button(action: {
                            withAnimation {
                                cameraPosition = .region(MKCoordinateRegion(
                                    center: userCoord,
                                    span: MKCoordinateSpan(latitudeDelta: 0.05, longitudeDelta: 0.05)
                                ))
                            }
                        }) {
                            Image(systemName: "location.fill")
                                .font(.title3)
                                .foregroundColor(.blue)
                                .padding(12)
                                .background(.regularMaterial)
                                .clipShape(Circle())
                                .shadow(radius: 3)
                        }
                    }
                }
                .padding(.trailing, 12)
                .padding(.bottom, 8)

                bottomCard
            }
        }
        .sheet(isPresented: $showLocationSheet) {
            locationSheet
                .presentationDetents([.height(220)])
        }
        .alert("Disable Spoofing", isPresented: $showDisableGuide) {
            Button("Cancel", role: .cancel) { }
            Button("Disable", role: .destructive) {
                disableSpoofing()
            }
        } message: {
            Text("Your Mac will stop location simulation and restore your real location.")
        }
        .sheet(isPresented: $showFavoritesSheet) {
            favoritesSheet
        }
        .sheet(isPresented: $showSettings) {
            SettingsView()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                locationFetcher.requestOneShot()
                companion.refresh()
            }
        }
        .onReceive(companion.$state) { state in
            spoofingState = state
            isSpoofing = companion.isBusy
            if case .on = state, let location = companion.observedLocation {
                cameraPosition = .region(MKCoordinateRegion(center: location.coordinate,
                    span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01)))
            }
        }
        .onReceive(companion.$observedLocation) { location in
            guard let location else { return }
            lastKnownUserCoord = location.coordinate
            if case .on = companion.state {
                withAnimation {
                    cameraPosition = .region(MKCoordinateRegion(center: location.coordinate,
                        span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01)))
                }
            }
        }
        .onAppear {
            loadSavedLocations()
            loadRecentLocations()
            // 启动时单次定位到用户真实位置,移地图镜头过去(失败/拒权保持默认)
            locationFetcher.onLocation = { coord in
                lastKnownUserCoord = coord
                withAnimation {
                    cameraPosition = .region(MKCoordinateRegion(
                        center: coord,
                        span: MKCoordinateSpan(latitudeDelta: 0.05, longitudeDelta: 0.05)
                    ))
                }
            }
            locationFetcher.requestOneShot()
            companion.start()
            spoofingState = companion.state
        }
    }
    // MARK: - 子视图


    // 底部卡片:搜索栏 + 输入联想 + 状态信息 + 主操作。
    // 顶圆角 + .regularMaterial 背景 + 阴影,底部贴屏幕边(ignoresSafeArea bottom)。
    private var bottomCard: some View {
        VStack(spacing: 12) {
            searchBar
            if !searchCompleter.results.isEmpty && !searchText.isEmpty {
                suggestionsList
            }
            statusSection
        }
        .padding(.top, 16)
        .padding(.bottom, 8)
        .background {
            UnevenRoundedRectangle(topLeadingRadius: 20, topTrailingRadius: 20)
                .fill(.regularMaterial)
                .ignoresSafeArea(edges: .bottom)
                .shadow(color: .black.opacity(0.1), radius: 8, y: -4)
        }
    }

    // 搜索联想列表(原本在 body,移到 bottomCard 内)
    private var suggestionsList: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(searchCompleter.results.prefix(5), id: \.self) { completion in
                Button(action: { selectCompletion(completion) }) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(completion.title)
                            .font(.body)
                            .foregroundColor(.primary)
                        if !completion.subtitle.isEmpty {
                            Text(completion.subtitle)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                }
                .buttonStyle(.plain)
                Divider().padding(.leading, 14)
            }
        }
        .background(Color(UIColor.systemBackground).opacity(0.4))
        .cornerRadius(12)
        .padding(.horizontal, 12)
    }

    // 状态信息(圆点+文案)+ 主操作(.on 关闭定位 / .failed 重试)
    private var statusSection: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                Circle()
                    .fill(indicatorColor)
                    .frame(width: 10, height: 10)
                VStack(alignment: .leading, spacing: 2) {
                    Text(statusPrimaryText)
                        .font(.body)
                        .fontWeight(.semibold)
                    if let sub = statusSubText {
                        Text(sub)
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
                Spacer()
            }

            if case .on = spoofingState {
                Button(action: { showDisableGuide = true }) {
                    Text("Disable")
                        .font(.body)
                        .fontWeight(.semibold)
                        .frame(maxWidth: .infinity)
                        .padding(14)
                        .background(Color.red)
                        .foregroundColor(.white)
                        .cornerRadius(12)
                }
            } else if case .failed = spoofingState {
                Button(action: {
                    companion.retry()
                }) {
                    Text("Retry")
                        .font(.body)
                        .fontWeight(.semibold)
                        .frame(maxWidth: .infinity)
                        .padding(14)
                        .background(Color.blue)
                        .foregroundColor(.white)
                        .cornerRadius(12)
                }
            }
        }
        .padding(.horizontal, 16)
    }

    // 状态文案(MapHomeView 内重写,不动 SpoofingState.swift)
    private var statusPrimaryText: String {
        switch spoofingState {
        case .off: return "Select a location to start spoofing"
        case .pending(_, let isClosing): return isClosing ? "Disabling…" : "Activating…"
        case .on(let name): return "Active: \(name)"
        case .failed(let reason): return "Failed: \(reason)"
        }
    }

    private var statusSubText: String? {
        switch spoofingState {
        case .pending: return "Waiting for Mac and iPhone location verification"
        case .on: return "Verified on iPhone · Mac companion"
        default: return "Keep your Mac companion online"
        }
    }
    private var indicatorColor: Color {
        switch spoofingState {
        case .off:      return .gray
        case .pending:  return .yellow
        case .on:       return .blue
        case .failed:   return .red
        }
    }


    private var searchBar: some View {
        HStack(spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundColor(.secondary)
                TextField("Search a place or tap the map", text: $searchText)
                    .textFieldStyle(.plain)
                    .submitLabel(.search)
                    .onSubmit { performSearch() }
                    .onChange(of: searchText) { _, new in searchCompleter.updateQuery(new) }
                if !searchText.isEmpty {
                    Button(action: { searchText = "" }) {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundColor(.secondary)
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(.regularMaterial)
            .cornerRadius(12)

            Button(action: { showFavoritesSheet = true }) {
                Image(systemName: "star.fill")
                    .font(.title3)
                    .foregroundColor(.yellow)
                    .padding(12)
                    .background(.regularMaterial)
                    .clipShape(Circle())
            }
            Button(action: { showSettings = true }) {
                Image(systemName: "gearshape.fill")
                    .font(.title3)
                    .foregroundColor(.primary)
                    .padding(12)
                    .background(.regularMaterial)
                    .clipShape(Circle())
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 24)
    }

    private var locationSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Image(systemName: "mappin.circle.fill")
                    .foregroundColor(.red)
                    .font(.title2)
                VStack(alignment: .leading, spacing: 4) {
                    Text(selectedLocationName ?? "Unknown location")
                        .font(.headline)
                    if let coord = selectedCoordinate {
                        Text(String(format: "%.4f, %.4f", coord.latitude, coord.longitude))
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
                Spacer()
            }

            Spacer()

            HStack(spacing: 12) {
                Button(action: { addCurrentSelectionToFavorites() }) {
                    HStack {
                        Image(systemName: justFavorited ? "checkmark.circle.fill" : "star")
                        Text(justFavorited ? "Saved" : "Save")
                            .fontWeight(justFavorited ? .semibold : .regular)
                    }
                    .frame(maxWidth: .infinity)
                    .padding()
                    .background(justFavorited ? Color.green.opacity(0.15) : Color(UIColor.tertiarySystemBackground))
                    .foregroundColor(justFavorited ? Color.green : Color.primary)
                    .cornerRadius(12)
                    .animation(.easeInOut(duration: 0.2), value: justFavorited)
                }
                .disabled(justFavorited)
                Button(action: { setAsLocation() }) {
                    Text(isSpoofing ? "Activating…" : "Set Location")
                        .fontWeight(.semibold)
                        .frame(maxWidth: .infinity)
                        .padding()
                        .background(isSpoofing ? Color.gray : Color.blue)
                        .foregroundColor(.white)
                        .cornerRadius(12)
                }
                .disabled(isSpoofing)
            }
        }
        .padding()
    }

    private var favoritesSheet: some View {
        NavigationView {
            List {
                // 最近使用
                if !recentLocations.isEmpty {
                    Section("Recent") {
                        ForEach(recentLocations) { saved in
                            Button(action: { selectFavorite(saved) }) {
                                favoriteRow(saved: saved, iconName: "clock", iconColor: .orange)
                            }
                        }
                    }
                }

                // 我的收藏
                Section("Favorites") {
                    if savedLocations.isEmpty {
                        Text("No saved locations yet")
                            .foregroundColor(.secondary)
                    } else {
                        ForEach(savedLocations) { saved in
                            Button(action: { selectFavorite(saved) }) {
                                favoriteRow(saved: saved, iconName: "star.fill", iconColor: .yellow)
                            }
                            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                Button(role: .destructive) {
                                    deleteFavorite(saved)
                                } label: {
                                    Label("Delete", systemImage: "trash")
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Saved Locations")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Done") { showFavoritesSheet = false }
                }
            }
        }
    }

    @ViewBuilder
    private func favoriteRow(saved: SavedLocation, iconName: String, iconColor: Color) -> some View {
        HStack {
            Image(systemName: iconName)
                .foregroundColor(iconColor)
            VStack(alignment: .leading) {
                Text(saved.name)
                    .foregroundColor(.primary)
                Text(String(format: "%.4f, %.4f", saved.latitude, saved.longitude))
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Spacer()
        }
    }

    // MARK: - 动作

    private func selectCoordinate(_ coordinate: CLLocationCoordinate2D) {
        selectedCoordinate = coordinate
        selectedLocationName = nil
        showLocationSheet = true

        let geocoder = CLGeocoder()
        let location = CLLocation(latitude: coordinate.latitude, longitude: coordinate.longitude)
        geocoder.reverseGeocodeLocation(location) { placemarks, _ in
            if let placemark = placemarks?.first {
                let name = [placemark.name, placemark.locality, placemark.country]
                    .compactMap { $0 }
                    .joined(separator: ", ")
                DispatchQueue.main.async {
                    self.selectedLocationName = name.isEmpty ? "Unknown location" : name
                }
            }
        }
    }

    private func handleMapSelection(_ selection: MapSelection<MKMapItem>?) {
        guard let selection else { return }

        // 自定义 Marker 走 .value;POI 走 .feature
        if let mapItem = selection.value {
            applyMapItem(mapItem)
            return
        }
        guard let feature = selection.feature else { return }

        // POI:用 MKMapItemRequest 异步换成 MKMapItem,取 name + coordinate
        Task { @MainActor in
            do {
                let mapItem = try await MKMapItemRequest(feature: feature).mapItem
                applyMapItem(mapItem)
            } catch {
                // 兜底:直接用 feature 自带字段
                applySelectedLocation(
                    name: feature.title ?? "Unknown place",
                    coordinate: feature.coordinate
                )
            }
        }
    }

    private func applyMapItem(_ mapItem: MKMapItem) {
        applySelectedLocation(
            name: mapItem.name ?? "Unknown place",
            coordinate: mapItem.placemark.coordinate
        )
    }

    private func applySelectedLocation(name: String, coordinate: CLLocationCoordinate2D) {
        selectedCoordinate = coordinate
        selectedLocationName = name
        showLocationSheet = true
        withAnimation {
            cameraPosition = .region(MKCoordinateRegion(
                center: coordinate,
                span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01)
            ))
        }
    }

    private func performSearch() {
        guard !searchText.isEmpty else { return }
        let request = MKLocalSearch.Request()
        request.naturalLanguageQuery = searchText
        let search = MKLocalSearch(request: request)
        search.start { response, _ in
            if let item = response?.mapItems.first {
                let coord = item.placemark.coordinate
                DispatchQueue.main.async {
                    self.cameraPosition = .region(MKCoordinateRegion(
                        center: coord,
                        span: MKCoordinateSpan(latitudeDelta: 0.05, longitudeDelta: 0.05)
                    ))
                    self.selectedCoordinate = coord
                    self.selectedLocationName = item.name
                    self.showLocationSheet = true
                }
            }
        }
    }

    /// 用户从联想列表选中某条 → 用 MKLocalSearch 解析成 MKMapItem → 移镜头 + 弹位置抽屉。
    /// 选中后清空 searchText 与 completer.results,自然收起联想列表与键盘。
    private func selectCompletion(_ completion: MKLocalSearchCompletion) {
        let request = MKLocalSearch.Request(completion: completion)
        let search = MKLocalSearch(request: request)
        search.start { response, _ in
            guard let item = response?.mapItems.first else { return }
            DispatchQueue.main.async {
                let coord = item.placemark.coordinate
                withAnimation {
                    self.cameraPosition = .region(MKCoordinateRegion(
                        center: coord,
                        span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01)
                    ))
                }
                self.selectedCoordinate = coord
                self.selectedLocationName = item.name ?? completion.title
                self.showLocationSheet = true
                self.searchText = ""
                self.searchCompleter.clear()
                UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
            }
        }
    }

    private func setAsLocation() {
        guard let coord = selectedCoordinate else { return }
        let name = selectedLocationName ?? "Unknown location"
        let converted = CoordinateConverter.gcj02ToWgs84(lat: coord.latitude, lng: coord.longitude)
        LocationConfiguration.shared.setCoordinates(latitude: converted.latitude, longitude: converted.longitude)
        UserDefaults.standard.set(name, forKey: "currentLocationName")
        addToRecentLocations(name: name, latitude: coord.latitude, longitude: coord.longitude)
        showLocationSheet = false
        companion.setLocation(latitude: converted.latitude, longitude: converted.longitude, name: name)
    }

    private func loadRecentLocations() {
        if let data = UserDefaults.standard.data(forKey: "recentLocations"),
           let decoded = try? JSONDecoder().decode([SavedLocation].self, from: data) {
            recentLocations = decoded
        }
    }

    private func saveRecentLocations() {
        if let encoded = try? JSONEncoder().encode(recentLocations) {
            UserDefaults.standard.set(encoded, forKey: "recentLocations")
        }
    }

    private func addToRecentLocations(name: String, latitude: Double, longitude: Double) {
        // 去重:相同 name 移到顶部
        recentLocations.removeAll { $0.name == name }
        let new = SavedLocation(name: name, latitude: latitude, longitude: longitude)
        recentLocations.insert(new, at: 0)
        // 限制最多 5 个
        if recentLocations.count > 5 {
            recentLocations = Array(recentLocations.prefix(5))
        }
        saveRecentLocations()
    }

    private func disableSpoofing() {
        companion.clearLocation()
    }

    private func loadSavedLocations() {
        if let data = UserDefaults.standard.data(forKey: "savedLocations"),
           let decoded = try? JSONDecoder().decode([SavedLocation].self, from: data) {
            savedLocations = decoded
        }
    }

    private func saveSavedLocations() {
        if let encoded = try? JSONEncoder().encode(savedLocations) {
            UserDefaults.standard.set(encoded, forKey: "savedLocations")
        }
    }

    private func addCurrentSelectionToFavorites() {
        guard let coord = selectedCoordinate,
              let name = selectedLocationName else { return }
        // 避免重复
        if savedLocations.contains(where: { $0.name == name }) {
            // 已存在也给反馈,让用户知道点击生效
            triggerFavoritedFeedback()
            return
        }
        let new = SavedLocation(
            name: name,
            latitude: coord.latitude,
            longitude: coord.longitude
        )
        savedLocations.append(new)
        saveSavedLocations()
        triggerFavoritedFeedback()
    }

    private func triggerFavoritedFeedback() {
        // 触感反馈
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.impactOccurred()
        // 视觉反馈
        justFavorited = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            justFavorited = false
        }
    }

    private func selectFavorite(_ saved: SavedLocation) {
        let coord = CLLocationCoordinate2D(latitude: saved.latitude, longitude: saved.longitude)
        selectedCoordinate = coord
        selectedLocationName = saved.name
        cameraPosition = .region(MKCoordinateRegion(
            center: coord,
            span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01)
        ))
        showFavoritesSheet = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            showLocationSheet = true
        }
    }

    private func deleteFavorite(_ saved: SavedLocation) {
        savedLocations.removeAll { $0.id == saved.id }
        saveSavedLocations()
    }

}
