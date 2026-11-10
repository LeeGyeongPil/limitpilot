// LimitPilot menu bar app.
//
// Reads the state file the daemon writes (no network, no credentials) and shows the
// active account's session usage as a gauge icon + "42%". Every change (switch, ON/OFF,
// delete, settings) runs the limitpilot CLI with an argument array — no shell — so the
// CLI's safety checks apply. Quitting the app only closes the UI; the daemon keeps
// running unless the user picks "Quit and turn off watching…" (hold Option).
import Cocoa

// ---------------------------------------------------------------- 경로·실행 파일

let launchArgs = CommandLine.arguments
let demoMode = launchArgs.contains("--demo")   // 연습 모드: 임시 폴더의 가짜 계정을 보여 준다

let stateDir: String = {
    if demoMode {
        return (NSTemporaryDirectory() as NSString).appendingPathComponent("limitpilot-demo/state")
    }
    if let home = ProcessInfo.processInfo.environment["LIMITPILOT_HOME"], !home.isEmpty {
        return home
    }
    return NSString(string: "~/.limitpilot").expandingTildeInPath
}()
let statePath = (stateDir as NSString).appendingPathComponent("state.json")

/// 번들 안의 limitpilot.pyz (widget build 가 넣는다)
let bundledPyz: String? = {
    guard let res = Bundle.main.resourcePath else { return nil }
    let p = (res as NSString).appendingPathComponent("limitpilot.pyz")
    return FileManager.default.fileExists(atPath: p) ? p : nil
}()

/// pyz 를 돌릴 파이썬 — 빌드한 인터프리터(Info.plist) → 흔한 설치 위치 순서
let pythonPath: String? = {
    var candidates: [String] = []
    if let built = Bundle.main.object(forInfoDictionaryKey: "LimitPilotPython") as? String {
        candidates.append(built)
    }
    candidates += ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
    return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
}()

/// pyz 가 없을 때 쓰는 설치된 CLI(pipx / uv tool)
let installedCLI: String? = {
    let candidates = ["~/.local/bin/limitpilot", "/opt/homebrew/bin/limitpilot", "/usr/local/bin/limitpilot"]
        .map { NSString(string: $0).expandingTildeInPath }
    return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
}()

/// CLI 실행 형태 (실행 파일, 인자 배열). 셸을 거치지 않는다.
func cliInvocation(_ args: [String], lang: String) -> (String, [String])? {
    var full = ["--lang", lang]
    if demoMode { full.append("--demo") }
    full += args
    if let pyz = bundledPyz, let py = pythonPath { return (py, [pyz] + full) }
    if let cli = installedCLI { return (cli, full) }
    return nil
}

// ---------------------------------------------------------------- 상태 읽기

func loadState() -> [String: Any]? {
    guard let data = FileManager.default.contents(atPath: statePath) else { return nil }
    return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
}

// 포매터는 한 번만 만든다(5초마다 새로 만들지 않게)
let isoParser: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    f.timeZone = TimeZone(identifier: "UTC")
    return f
}()
let localFormatter: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "M/d HH:mm"
    return f
}()

// resets_at 은 마이크로초가 붙은 UTC ISO — 초 단위(19자)까지만 잘라 읽는다
func parseReset(_ iso: Any?) -> Date? {
    guard let s = iso as? String, s.count >= 19 else { return nil }
    return isoParser.date(from: String(s.prefix(19)))
}

func fmtLocal(_ d: Date?) -> String {
    guard let d = d else { return "?" }
    return localFormatter.string(from: d)
}

func pctOf(_ slot: [String: Any]?, _ window: String) -> Double? {
    guard let u = slot?["usage"] as? [String: Any],
          let w = u[window] as? [String: Any],
          let p = w["pct"] as? Double else { return nil }
    return p
}

func resetOf(_ slot: [String: Any]?, _ window: String) -> Date? {
    guard let u = slot?["usage"] as? [String: Any],
          let w = u[window] as? [String: Any] else { return nil }
    return parseReset(w["resets_at"])
}

func isEnabled(_ slot: [String: Any]) -> Bool { (slot["enabled"] as? Bool) ?? true }
func isLimitedToken(_ slot: [String: Any]) -> Bool { (slot["kind"] as? String) == "setup-token" }
func isGateway(_ slot: [String: Any]) -> Bool { (slot["kind"] as? String) == "gateway" }

// 엔터프라이즈(월 달러 한도·수동 전용) — 데몬의 is_enterprise_slot 과 같은 신호
func isEnterprise(_ slot: [String: Any]) -> Bool {
    guard let oa = slot["oauthAccount"] as? [String: Any] else { return false }
    return (oa["organizationType"] as? String) == "claude_enterprise"
}

func spendOf(_ slot: [String: Any]?) -> (used: Double, limit: Double, currency: String, pct: Double)? {
    guard let u = slot?["usage"] as? [String: Any],
          let sp = u["spend"] as? [String: Any],
          let used = sp["used"] as? Double,
          let limit = sp["limit"] as? Double else { return nil }
    let cur = (sp["currency"] as? String) ?? "USD"
    let pct = (sp["pct"] as? Double) ?? (limit > 0 ? used / limit * 100 : 0)
    return (used, limit, cur, pct)
}

func fmtMoney(_ v: Double, _ currency: String) -> String {
    let sym = currency == "USD" ? "$" : (currency + " ")
    let f = NumberFormatter()
    f.numberStyle = .decimal
    f.maximumFractionDigits = 2
    f.minimumFractionDigits = 2
    return sym + (f.string(from: NSNumber(value: v)) ?? String(format: "%.2f", v))
}

// ---------------------------------------------------------------- 메뉴바 아이콘

/// 단색 템플릿 게이지 — 270° 호(바탕) + 사용률만큼 채운 호 + 바늘. 다크/라이트 자동.
func gaugeImage(percent: Double?) -> NSImage {
    let size = NSSize(width: 18, height: 18)
    let image = NSImage(size: size, flipped: false) { rect in
        let c = NSPoint(x: rect.midX, y: rect.midY - 0.5)
        let r: CGFloat = 7.0
        let start: CGFloat = 225, sweep: CGFloat = 270     // 왼쪽 아래 → 오른쪽 아래(시계 방향)
        let track = NSBezierPath()
        track.appendArc(withCenter: c, radius: r, startAngle: start, endAngle: start - sweep, clockwise: true)
        track.lineWidth = 1.6
        track.lineCapStyle = .round
        NSColor.black.withAlphaComponent(0.35).setStroke()
        track.stroke()
        let frac = CGFloat(min(max((percent ?? 0) / 100.0, 0), 1))
        if frac > 0 {
            let fill = NSBezierPath()
            fill.appendArc(withCenter: c, radius: r, startAngle: start,
                           endAngle: start - sweep * frac, clockwise: true)
            fill.lineWidth = 2.4
            fill.lineCapStyle = .round
            NSColor.black.setStroke()
            fill.stroke()
        }
        let angle = (start - sweep * frac) * .pi / 180
        let needle = NSBezierPath()
        needle.move(to: c)
        needle.line(to: NSPoint(x: c.x + cos(angle) * (r - 2.2), y: c.y + sin(angle) * (r - 2.2)))
        needle.lineWidth = 1.5
        needle.lineCapStyle = .round
        NSColor.black.setStroke()
        needle.stroke()
        NSBezierPath(ovalIn: NSRect(x: c.x - 1.4, y: c.y - 1.4, width: 2.8, height: 2.8)).fill()
        return true
    }
    image.isTemplate = true
    image.accessibilityDescription = "LimitPilot"
    return image
}

// ---------------------------------------------------------------- 앱

class App: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var timer: Timer?
    let intervalSec: Double = 5         // 상태 파일을 다시 읽는 주기(로컬 파일, 네트워크 0)
    let staleAfterSec: Double = 420     // 이보다 오래된 데이터는 제목에 '…' 로 표시
    var lang: String = Locale.preferredLanguages.first?.hasPrefix("ko") == true ? "ko" : "en"

    // 모든 UI 문자열은 이 함수를 거친다(settings.language: ko | en)
    func L(_ ko: String, _ en: String) -> String { lang == "en" ? en : ko }

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.font = NSFont.monospacedDigitSystemFont(ofSize: 12.5, weight: .medium)
        item.button?.imagePosition = .imageLeading
        item.menu = NSMenu()
        item.menu!.autoenablesItems = false
        refresh()
        reschedule()
        if !demoMode { syncDaemon() }
    }

    // 앱(새 빌드)을 열면 데몬 사본을 이 앱의 코드로 맞춘다. 데몬이 없으면 아무 일도 없다.
    func syncDaemon() {
        guard let (exe, args) = cliInvocation(["sync-daemon"], lang: lang) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
    }

    func reschedule() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: intervalSec, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        if let t = timer { RunLoop.main.add(t, forMode: .common) }  // 메뉴가 열려 있어도 갱신
    }

    func settings(_ st: [String: Any]) -> [String: Any] { st["settings"] as? [String: Any] ?? [:] }

    func thresholdOf(_ st: [String: Any]) -> Double {
        let s = settings(st)
        if let t = s["threshold"] as? Double { return t }
        if let t = s["threshold"] as? Int { return Double(t) }
        return 90
    }

    func info(_ menu: NSMenu, _ title: String, indent: Int = 0, dim: Bool = false) {
        let mi = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        mi.isEnabled = false
        mi.indentationLevel = indent
        if dim {
            mi.attributedTitle = NSAttributedString(string: title, attributes: [
                .foregroundColor: NSColor.tertiaryLabelColor,
                .font: NSFont.menuFont(ofSize: 13),
            ])
        }
        menu.addItem(mi)
    }

    func header(_ menu: NSMenu, _ title: String, dim: Bool = false, checked: Bool = false) {
        let mi = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        mi.isEnabled = false
        mi.state = checked ? .on : .off
        mi.attributedTitle = NSAttributedString(string: title, attributes: [
            .font: NSFont.menuBarFont(ofSize: 13).withBold(),
            .foregroundColor: dim ? NSColor.tertiaryLabelColor : NSColor.labelColor,
        ])
        menu.addItem(mi)
    }

    func action(_ menu: NSMenu, _ title: String, _ selector: Selector, key: String = "",
                indent: Int = 0, object: Any? = nil) -> NSMenuItem {
        let mi = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        mi.target = self
        mi.indentationLevel = indent
        mi.representedObject = object
        menu.addItem(mi)
        return mi
    }

    func slotKeys(_ slots: [String: Any]) -> [String] {
        return slots.keys.filter { slots[$0] is [String: Any] }
            .sorted { (Int($0) ?? 0) < (Int($1) ?? 0) }
    }

    func stateAge() -> TimeInterval? {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: statePath),
              let m = attrs[.modificationDate] as? Date else { return nil }
        return Date().timeIntervalSince(m)
    }

    /// 메뉴바 제목: 게이지 아이콘 + 활성 계정 세션 % — 임계 근처에서만 색으로 강조
    func setTitle(percent: Double?, text: String, threshold: Double, stale: Bool) {
        item.button?.image = gaugeImage(percent: percent)
        var attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12.5, weight: .medium),
        ]
        if let p = percent, p >= 100 {
            attrs[.foregroundColor] = NSColor.systemRed
        } else if let p = percent, p >= threshold - 5 {
            attrs[.foregroundColor] = NSColor.systemOrange
        } else if stale {
            attrs[.foregroundColor] = NSColor.secondaryLabelColor
        }
        let shown = " " + text + (stale ? " …" : "") + (demoMode ? " DEMO" : "")
        item.button?.attributedTitle = NSAttributedString(string: shown, attributes: attrs)
        item.button?.toolTip = stale
            ? L("데이터가 오래됐습니다 — 감시 데몬이 멈췄을 수 있습니다", "Data is stale — the daemon may have stopped")
            : "LimitPilot"
    }

    @objc func refresh() {
        guard let st = loadState(), let slots = st["slots"] as? [String: Any] else {
            setTitle(percent: nil, text: "—", threshold: 90, stale: false)
            rebuildMenu(st: [:], slots: [:], active: nil)
            return
        }
        if let code = settings(st)["language"] as? String, code == "ko" || code == "en" { lang = code }
        let active = st["activeSlot"] as? Int
        let threshold = thresholdOf(st)
        let stale = (stateAge() ?? 0) > staleAfterSec
        var percent: Double? = nil
        var text = "—"
        if let a = active, let slot = slots[String(a)] as? [String: Any] {
            if isLimitedToken(slot) {
                text = L("제한 토큰", "Limited")
            } else if isEnterprise(slot) {
                if let sp = spendOf(slot) { text = fmtMoney(sp.used, sp.currency); percent = sp.pct }
            } else if isGateway(slot) {
                text = slot["alias"] as? String ?? "Gateway"
            } else if let p = pctOf(slot, "five_hour") {
                percent = p
                text = "\(Int(p.rounded()))%"
            }
        }
        setTitle(percent: percent, text: text, threshold: threshold, stale: stale)
        rebuildMenu(st: st, slots: slots, active: active)
    }

    func ageText(_ age: Int) -> String {
        if lang == "en" {
            return age >= 86400 ? "\(age / 86400)d" : age >= 3600 ? "\(age / 3600)h" : age >= 60 ? "\(age / 60)m" : "\(age)s"
        }
        return age >= 86400 ? "\(age / 86400)일" : age >= 3600 ? "\(age / 3600)시간" : age >= 60 ? "\(age / 60)분" : "\(age)초"
    }

    func daemonRegistered() -> Bool {
        let paths = [
            NSString(string: "~/Library/LaunchAgents/io.github.limitpilot.daemon.plist").expandingTildeInPath,
            (stateDir as NSString).appendingPathComponent("io.github.limitpilot.daemon.plist"),
        ]
        return paths.contains { FileManager.default.fileExists(atPath: $0) }
    }

    func rebuildMenu(st: [String: Any], slots: [String: Any], active: Int?) {
        let menu = item.menu!
        menu.removeAllItems()
        menu.autoenablesItems = false
        if demoMode {
            info(menu, L("DEMO — 연습용 목(mock) 모드: 가짜 계정", "DEMO — practice (mock) mode: fake accounts"))
            menu.addItem(.separator())
        }

        let keys = slotKeys(slots)
        if keys.count < 2 {
            // 처음 쓰는 사람용 안내 — CLI 명령을 외울 필요 없이 메뉴에서 바로
            header(menu, L("시작하기", "Getting started"))
            info(menu, (keys.isEmpty ? "○ " : "✓ ") + L("1. 지금 로그인된 계정 등록", "1. Register the signed-in account"), indent: 1)
            info(menu, "○ " + L("2. 다른 계정을 브라우저로 추가", "2. Add another account in the browser"), indent: 1)
            info(menu, (daemonRegistered() ? "✓ " : "○ ") + L("3. 감시 켜기", "3. Turn on watching"), indent: 1)
            if keys.isEmpty {
                _ = action(menu, L("현재 로그인 계정 등록", "Register current login"), #selector(addCurrent), indent: 1)
            }
            _ = action(menu, L("브라우저로 계정 추가…", "Add account in browser…"), #selector(openAddPanel), indent: 1)
            menu.addItem(.separator())
        }

        for key in keys {
            guard let slot = slots[key] as? [String: Any], let n = Int(key) else { continue }
            let email = slot["email"] as? String ?? "?"
            let isActive = String(active ?? -1) == key
            let on = isEnabled(slot)
            let limited = isLimitedToken(slot)
            let ent = isEnterprise(slot)
            let gateway = isGateway(slot)
            let manualOnly = ent || gateway
            let needsLogin = slot["quarantine"] is [String: Any]
            header(menu, slot["alias"] as? String ?? email, dim: !on && !manualOnly, checked: isActive)
            if ent {
                info(menu, L("엔터프라이즈 · 수동 전환만", "Enterprise · manual switching only"), indent: 1, dim: true)
            }
            if gateway {
                info(menu, L("서드파티 게이트웨이 · 수동 전용", "Third-party gateway · manual only"), indent: 1, dim: true)
                info(menu, slot["modelId"] as? String ?? "?", indent: 1)
            }
            if limited {
                info(menu, L("제한 토큰 — 삭제 후 브라우저로 다시 추가", "Limited token — delete, then add again in the browser"), indent: 1)
            } else if needsLogin {
                info(menu, L("다시 로그인 필요 — 브라우저로 다시 추가하세요", "Needs login — add it again in the browser"), indent: 1)
            }
            if gateway {
                // 게이트웨이는 구독 사용량이 없다
            } else if ent, !limited {
                if let sp = spendOf(slot) {
                    info(menu, "\(L("사용", "Spend")) \(fmtMoney(sp.used, sp.currency)) / \(fmtMoney(sp.limit, sp.currency)) (\(String(format: "%.1f", sp.pct))%)", indent: 1)
                } else {
                    info(menu, L("사용 금액 데이터 없음", "No spend data"), indent: 1, dim: true)
                }
            } else if !limited {
                if let p = pctOf(slot, "five_hour") {
                    info(menu, "\(L("세션 5시간", "Session 5h")) \(Int(p.rounded()))% · \(L("리셋", "resets")) \(fmtLocal(resetOf(slot, "five_hour")))", indent: 1, dim: !on)
                }
                if let p = pctOf(slot, "seven_day") {
                    info(menu, "\(L("주간 7일", "Weekly 7d")) \(Int(p.rounded()))% · \(L("리셋", "resets")) \(fmtLocal(resetOf(slot, "seven_day")))", indent: 1, dim: !on)
                }
            }
            if !limited, let fetched = slot["usageFetchedAt"] as? Double {
                let age = max(0, Int(Date().timeIntervalSince1970 - fetched))
                info(menu, L("데이터 \(ageText(age)) 전", "Data \(ageText(age)) ago"), indent: 1, dim: true)
            }
            if !isActive && !limited && !needsLogin && (on || manualOnly) {
                let sw = action(menu, L("이 계정으로 전환", "Switch to this account"), #selector(activateSlot(_:)),
                                indent: 1, object: ["slot": n, "email": email])
                if #available(macOS 11.0, *) {
                    sw.image = NSImage(systemSymbolName: "arrow.right.circle", accessibilityDescription: nil)
                }
            }
            let manage = NSMenuItem(title: L("관리", "Manage"), action: nil, keyEquivalent: "")
            manage.indentationLevel = 1
            let sub = NSMenu()
            sub.autoenablesItems = false
            if !manualOnly {
                let toggle = NSMenuItem(
                    title: on ? L("끄기 (자동 전환 후보에서 제외)", "Turn off (never switch to it)")
                              : L("켜기 (자동 전환 후보에 포함)", "Turn on (may switch to it)"),
                    action: #selector(toggleSlot(_:)), keyEquivalent: "")
                toggle.target = self
                toggle.representedObject = ["slot": n, "action": on ? "disable" : "enable"]
                toggle.isEnabled = !isActive && !limited
                sub.addItem(toggle)
            }
            let del = NSMenuItem(title: L("삭제…", "Delete…"), action: #selector(deleteSlot(_:)), keyEquivalent: "")
            del.target = self
            del.representedObject = ["slot": n, "email": email]
            del.isEnabled = !isActive
            sub.addItem(del)
            manage.submenu = sub
            menu.addItem(manage)
            menu.addItem(.separator())
        }

        // 감시·자동 전환 상태 — 감시(데몬)와 자동 전환(옵트인)은 따로 켜고 끈다
        let threshold = Int(thresholdOf(st).rounded())
        let autoOn = (settings(st)["autoSwitch"] as? Bool) ?? false
        let registered = daemonRegistered()
        let running = registered && (stateAge() ?? .infinity) <= 180
        if !registered {
            info(menu, L("감시 꺼짐", "Watching is off"))
            _ = action(menu, L("감시 켜기 (알림만)", "Turn on watching (notify only)"), #selector(installDaemon))
        } else if !running {
            info(menu, L("⚠︎ 감시가 멈춰 있습니다", "⚠︎ Watching has stopped"))
            _ = action(menu, L("감시 다시 켜기", "Restart watching"), #selector(installDaemon))
        } else {
            info(menu, L("● 감시 중 · 임계 \(threshold)%", "● Watching · threshold \(threshold)%"), dim: true)
        }
        if autoOn {
            info(menu, L("자동 전환 켜짐", "Auto-switch is on"), dim: true)
            _ = action(menu, L("자동 전환 끄기 (알림만)", "Turn off auto-switch (notify only)"), #selector(autoSwitchOff))
        } else {
            info(menu, L("자동 전환 꺼짐 — 알림만", "Auto-switch is off — notify only"), dim: true)
            _ = action(menu, L("자동 전환 켜기…", "Turn on auto-switch…"), #selector(autoSwitchOn))
        }

        let langItem = NSMenuItem(title: L("언어", "Language"), action: nil, keyEquivalent: "")
        let langSub = NSMenu()
        langSub.autoenablesItems = false
        for (code, name) in [("ko", "한국어"), ("en", "English")] {
            let li = NSMenuItem(title: name, action: #selector(setLanguage(_:)), keyEquivalent: "")
            li.target = self
            li.representedObject = code
            li.state = lang == code ? .on : .off
            langSub.addItem(li)
        }
        langItem.submenu = langSub
        menu.addItem(langItem)
        menu.addItem(.separator())
        _ = action(menu, L("대시보드 열기", "Open dashboard"), #selector(openDashboard), key: "d")
        _ = action(menu, L("새로고침", "Refresh"), #selector(refresh), key: "r")
        menu.addItem(.separator())
        // "종료" 는 이 메뉴바 앱만 닫는다 — 감시 데몬은 계속 돈다.
        _ = action(menu, L("LimitPilot 종료 (감시는 계속)", "Quit LimitPilot (watching continues)"),
                   #selector(quit), key: "q")
        // Option 키를 누르면 바뀌는 항목: 감시 데몬까지 끄고 종료
        let quitAll = action(menu, L("감시도 끄고 종료…", "Quit and turn off watching…"),
                             #selector(quitAndStopWatching), key: "q")
        quitAll.keyEquivalentModifierMask = [.command, .option]
        quitAll.isAlternate = true
    }

    func alertFailure(_ op: String, _ detail: String?) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = L("\(op) 실패", "\(op) failed")
        alert.informativeText = (detail?.isEmpty == false) ? detail!
            : L("요청을 완료하지 못했습니다. 잠시 후 다시 시도하세요.", "Could not complete the request. Try again shortly.")
        alert.alertStyle = .warning
        alert.addButton(withTitle: L("확인", "OK"))
        alert.runModal()
    }

    /// CLI 를 인자 배열로 실행(셸 없음). 끝나면 성공 문구 또는 오류 창을 보여 준다.
    func runCLI(_ args: [String], operation: String, success: String? = nil, wait: Bool = true,
                then: (() -> Void)? = nil) {
        guard let (exe, full) = cliInvocation(args, lang: lang) else {
            alertFailure(operation, L("limitpilot 실행 파일을 찾지 못했습니다 — `limitpilot widget install` 로 다시 설치하세요.",
                                      "Could not find limitpilot — reinstall with `limitpilot widget install`."))
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = full
        let output = Pipe()
        p.standardOutput = wait ? output : FileHandle.nullDevice
        p.standardError = wait ? output : FileHandle.nullDevice
        if wait {
            p.terminationHandler = { [weak self] process in
                let data = output.fileHandleForReading.readDataToEndOfFile()
                let detail = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if process.terminationStatus == 0 {
                        if let message = success {
                            self.item.button?.attributedTitle = NSAttributedString(string: " ✓ " + message)
                        }
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { self.refresh() }
                        then?()
                    } else {
                        self.refresh()
                        self.alertFailure(operation, detail)
                    }
                }
            }
        }
        do {
            try p.run()
        } catch {
            alertFailure(operation, error.localizedDescription)
        }
    }

    func confirm(_ title: String, _ body: String, danger: String) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.alertStyle = .warning
        // 기본 버튼(Return)은 '취소' — 실수로 위험한 동작을 실행하지 않게
        alert.addButton(withTitle: L("취소", "Cancel"))
        let go = alert.addButton(withTitle: danger)
        if #available(macOS 11.0, *) { go.hasDestructiveAction = true }
        return alert.runModal() == .alertSecondButtonReturn
    }

    @objc func installDaemon() {
        runCLI(["install-daemon"], operation: L("감시 켜기", "Turn on watching"),
               success: L("감시 켜짐", "Watching on"))
    }

    @objc func autoSwitchOn() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = L("자동 전환을 켤까요?", "Turn on automatic switching?")
        alert.informativeText = L("활성 계정이 임계에 닿으면 감시 데몬이 등록된 다른 계정으로 Claude Code 로그인을 바꿉니다. 각 계정의 이용 약관·요금제 조건을 지켜 본인 계정에만 쓰세요.",
                                  "When the active account reaches its threshold, the daemon switches Claude Code to another registered account. Use it only with your own accounts and follow each account's terms and plan conditions.")
        alert.addButton(withTitle: L("취소", "Cancel"))
        alert.addButton(withTitle: L("켜기", "Turn on"))
        if alert.runModal() == .alertSecondButtonReturn {
            runCLI(["config", "auto-switch", "on"], operation: L("자동 전환 켜기", "Turn on auto-switch"),
                   success: L("자동 전환 켜짐", "Auto-switch on"))
        }
    }

    @objc func autoSwitchOff() {
        runCLI(["config", "auto-switch", "off"], operation: L("자동 전환 끄기", "Turn off auto-switch"),
               success: L("알림만", "Notify only"))
    }

    @objc func setLanguage(_ sender: NSMenuItem) {
        guard let code = sender.representedObject as? String, code != lang else { return }
        runCLI(["config", "language", code], operation: L("언어 설정", "Language setting"))
    }

    @objc func addCurrent() {
        runCLI(["add"], operation: L("계정 등록", "Register account"), success: L("등록됨", "Registered"))
    }

    @objc func activateSlot(_ sender: NSMenuItem) {
        guard let info = sender.representedObject as? [String: Any], let n = info["slot"] as? Int else { return }
        item.button?.attributedTitle = NSAttributedString(string: " " + L("슬롯 \(n) 전환 중…", "Switching to slot \(n)…"))
        // --force 는 쓰지 않는다 — 미등록 로그인을 백업 없이 덮어쓰지 않도록 CLI 검증에 맡긴다
        runCLI(["switch", String(n)], operation: L("계정 전환", "Account switch"),
               success: L("슬롯 \(n) 활성", "Slot \(n) active"))
    }

    @objc func toggleSlot(_ sender: NSMenuItem) {
        guard let info = sender.representedObject as? [String: Any],
              let n = info["slot"] as? Int, let act = info["action"] as? String else { return }
        runCLI([act, String(n)], operation: L("계정 설정", "Account setting"),
               success: act == "enable" ? L("켜짐", "On") : L("꺼짐", "Off"))
    }

    @objc func deleteSlot(_ sender: NSMenuItem) {
        guard let info = sender.representedObject as? [String: Any], let n = info["slot"] as? Int else { return }
        let email = info["email"] as? String ?? "?"
        if confirm(L("계정 삭제", "Delete account"),
                   L("슬롯 \(n) (\(email)) 계정을 삭제할까요?\n키체인 백업도 함께 지워집니다.",
                     "Delete slot \(n) (\(email))?\nIts Keychain backup is removed too."),
                   danger: L("삭제", "Delete")) {
            runCLI(["remove", String(n)], operation: L("계정 삭제", "Account deletion"), success: L("삭제됨", "Deleted"))
        }
    }

    @objc func openDashboard() {
        runCLI(["dashboard"], operation: L("대시보드 열기", "Open dashboard"), wait: false)
    }

    @objc func openAddPanel() {
        runCLI(["dashboard", "--add"], operation: L("대시보드 열기", "Open dashboard"), wait: false)
    }

    @objc func quit() { NSApp.terminate(nil) }

    @objc func quitAndStopWatching() {
        if confirm(L("감시도 끄고 종료", "Quit and turn off watching"),
                   L("사용량 감시와 자동 전환이 모두 멈춥니다. 다시 켜려면 메뉴의 '감시 켜기' 를 누르세요.",
                     "Usage watching and automatic switching both stop. Use 'Turn on watching' to start again."),
                   danger: L("끄고 종료", "Turn off and quit")) {
            runCLI(["uninstall-daemon"], operation: L("감시 끄기", "Turn off watching"),
                   then: { NSApp.terminate(nil) })
        }
    }
}

extension NSFont {
    func withBold() -> NSFont {
        return NSFontManager.shared.convert(self, toHaveTrait: .boldFontMask)
    }
}

// 중복 실행 방지 — 같은 번들 ID 인스턴스가 이미 떠 있으면 조용히 끝낸다
let myPID = ProcessInfo.processInfo.processIdentifier
if let bid = Bundle.main.bundleIdentifier,
   NSRunningApplication.runningApplications(withBundleIdentifier: bid)
       .contains(where: { $0.processIdentifier != myPID }) {
    exit(0)
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
