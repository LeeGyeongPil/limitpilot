# LimitPilot — Windows tray (fallback when csc.exe is missing; no dependencies)
# Same design as tray.cs / the macOS menu bar app: read-only view of state.json; every
# change runs the limitpilot CLI (limitpilot.pyz next to this script). Closing the tray
# does not stop watching.
# 실행: powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File tray.ps1

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# 중복 실행 방지 — 이미 트레이가 떠 있으면 조용히 종료 (main.swift 번들 ID 검사 대응)
$script:SingleInstance = New-Object System.Threading.Mutex($false, "Local\LimitPilotTray")
if (-not $script:SingleInstance.WaitOne(0)) { exit }

# ── 경로/도구 해석 ─────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CliScript = Join-Path $ScriptDir "limitpilot.pyz"   # widget install 이 함께 복사
$StateHome = if ($env:LIMITPILOT_HOME) { $env:LIMITPILOT_HOME } else { Join-Path $env:USERPROFILE ".limitpilot" }
$StatePath = Join-Path $StateHome "state.json"

function Find-Python {
    foreach ($c in @("py", "python")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    [System.Windows.Forms.MessageBox]::Show(
        "Python 3을 찾을 수 없습니다. python.org 또는 Microsoft Store에서 설치하세요.",
        "LimitPilot", "OK", "Error") | Out-Null
    exit 1
}

# ── 상태 읽기/해석 (main.swift 규칙과 동일) ────────────────────
function Load-State {
    try { Get-Content -Raw -Encoding UTF8 $StatePath | ConvertFrom-Json }
    catch { $null }
}

function Get-SlotKeys($slots) {
    if (-not $slots) { return @() }
    $slots.PSObject.Properties.Name |
        Where-Object { $_ -match '^\d+$' } |
        Sort-Object { [int]$_ }
}

function Get-Pct($slot, $window) {
    # [double]$null 은 예외가 아니라 0으로 강제변환됨 — '데이터 없음'이 0%로
    # 둔갑해 초록 아이콘/유령 0% 행을 만들므로 null을 먼저 걸러낸다
    try {
        $v = $slot.usage.$window.pct
        if ($null -eq $v) { return $null }
        [double]$v
    } catch { $null }
}

function Get-ResetLocal($slot, $window) {
    try {
        $iso = [string]$slot.usage.$window.resets_at
        if ($iso.Length -lt 19) { return "?" }
        $d = [DateTime]::ParseExact($iso.Substring(0, 19), "yyyy-MM-ddTHH:mm:ss",
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal)
        # InvariantCulture 고정 — ko-KR 등에서 '/'가 문화권 날짜 구분자('-')로
        # 치환되는 것을 막는다 (main.swift·tray.cs와 동일 표기)
        $d.ToLocalTime().ToString("M/d HH:mm", [Globalization.CultureInfo]::InvariantCulture)
    } catch { "?" }
}

function Sev-Color($pct) {
    if ($null -eq $pct) { return [System.Drawing.Color]::FromArgb(46, 196, 182) }
    if ($pct -ge 100) { return [System.Drawing.Color]::FromArgb(232, 72, 72) }   # 소진
    if ($pct -ge 85)  { return [System.Drawing.Color]::FromArgb(255, 176, 32) }  # 임박(앰버)
    [System.Drawing.Color]::FromArgb(46, 196, 182)                                # 여유(청록)
}

function Sev-Mark($pct) {
    if ($null -eq $pct) { return "" }
    if ($pct -ge 100) { return "■" }
    if ($pct -ge 90)  { return "▲" }
    if ($pct -ge 70)  { return "◆" }
    "●"
}

# 브랜드 배지 아이콘 — 테라코타 스쿼클 + 활성 세션 사용률 링(심각도 색).
# macOS 앱 아이콘(widget/icon.swift)과 동일 디자인 언어. 트레이는 텍스트를 못
# 다므로 링 채움량+색+툴팁으로 전달한다.
# GetHicon() 핸들은 GDI 누수 방지를 위해 DestroyIcon으로 직접 해제한다.
Add-Type -Namespace Win32 -Name NativeIcon -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true)]
public static extern bool DestroyIcon(IntPtr handle);
'@

function New-TrayIcon($pct) {
    $bmp = New-Object System.Drawing.Bitmap 16, 16
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    # 네이비(#0F1E33) 둥근 사각 + 270° 게이지(사용률만큼, 심각도 색) — tray.cs 와 같은 모양
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc(0, 0, 8, 8, 180, 90)
    $path.AddArc(8, 0, 8, 8, 270, 90)
    $path.AddArc(8, 8, 8, 8, 0, 90)
    $path.AddArc(0, 8, 8, 8, 90, 90)
    $path.CloseFigure()
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(15, 30, 51))
    $g.FillPath($bg, $path)
    $track = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(90, 255, 255, 255)), 2.2
    $g.DrawArc($track, 3, 3, 10, 10, 135, 270)
    if ($null -ne $pct) {
        $sweep = [math]::Max(8, [math]::Min(270, 270.0 * [double]$pct / 100.0))
        $arc = New-Object System.Drawing.Pen (Sev-Color $pct), 2.4
        $arc.StartCap = "Round"; $arc.EndCap = "Round"
        $g.DrawArc($arc, 3, 3, 10, 10, 135, $sweep)
        $arc.Dispose()
    }
    $g.Dispose(); $bg.Dispose(); $track.Dispose(); $path.Dispose()
    $handle = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($handle)
    $bmp.Dispose()
    @{ Icon = $icon; Handle = $handle }
}

# 툴팁 규칙: 활성 세션 % + 괄호 안 전체(ON) 평균. 예: "LimitPilot 40% (80%)"
function Build-Summary($st) {
    $activeText = $null; $activePct = $null; $pcts = @()
    if ($st -and $st.slots) {
        $active = $st.activeSlot
        foreach ($key in (Get-SlotKeys $st.slots)) {
            $slot = $st.slots.$key
            $isActive = ("$active" -eq $key)
            $enabled = ($null -eq $slot.enabled) -or $slot.enabled
            if (-not $isActive -and -not $enabled) { continue }
            if ($slot.kind -eq "setup-token") {
                if ($isActive) { $activeText = "⚠ 제한" }
                continue
            }
            if ($slot.kind -eq "gateway") {
                if ($isActive) {
                    $activeText = if ($slot.alias) { "↗ $($slot.alias)" } else { "↗ Gateway" }
                }
                continue
            }
            $pct = Get-Pct $slot "five_hour"
            if ($null -ne $pct) { $pcts += [double]$pct }
            if ($isActive) {
                $activePct = $pct
                $activeText = if ($null -ne $pct) { "$([math]::Round($pct))%" } else { "—" }
            }
        }
    }
    $summary = if ($null -ne $activeText) { "LimitPilot $activeText" } else { "LimitPilot — 데이터 없음" }
    if ($null -ne $activeText -and $pcts.Count -gt 1) {
        $avg = ($pcts | Measure-Object -Average).Average
        $summary += " ($([math]::Round($avg))%)"
    }
    if ($summary.Length -gt 63) { $summary = $summary.Substring(0, 60) + "…" }  # NotifyIcon.Text 63자 제한
    @{ Text = $summary; ActivePct = $activePct }
}

# ── CLI 위임 (runCLI 대응) ─────────────────────────────────────
function Run-Cli([string[]]$CliArgs, [string]$Operation, [string]$Success) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $argList = @()
    if ((Split-Path -Leaf $Python) -ieq "py.exe") { $argList += "-3" }
    $argList += @("`"$CliScript`"") + $CliArgs
    $psi.Arguments = $argList -join " "
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        $out = $p.StandardOutput.ReadToEnd() + $p.StandardError.ReadToEnd()
        $p.WaitForExit()
        if ($p.ExitCode -eq 0) {
            if ($Success) { $script:Tray.ShowBalloonTip(2500, "LimitPilot", $Success, "Info") }
        } else {
            $detail = if ($out.Trim()) { $out.Trim() } else { "요청을 완료하지 못했습니다. 잠시 후 다시 시도하세요." }
            [System.Windows.Forms.MessageBox]::Show($detail, "$Operation 실패", "OK", "Warning") | Out-Null
        }
    } catch {
        [System.Windows.Forms.MessageBox]::Show("명령을 시작할 수 없습니다: $($_.Exception.Message)",
            "$Operation 실패", "OK", "Warning") | Out-Null
    }
    Update-Tray
}

function Open-Dashboard([bool]$AddPanel = $false) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $argList = @()
    if ((Split-Path -Leaf $Python) -ieq "py.exe") { $argList += "-3" }
    $argList += @("`"$CliScript`"", "dashboard")
    if ($AddPanel) { $argList += "--add" }
    $psi.Arguments = $argList -join " "
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    [System.Diagnostics.Process]::Start($psi) | Out-Null
}

# 확인창 — 기본 버튼(Enter)은 '아니요'(Button2): 실수로 위험한 동작을 실행하지 않게
function Confirm-Danger([string]$Text, [string]$Title) {
    $r = [System.Windows.Forms.MessageBox]::Show($Text, $Title, "YesNo", "Warning", "Button2")
    return ($r -eq "Yes")
}

# ── 메뉴 구성 (rebuildMenu 대응) — 열릴 때마다 재구성 ──────────
function Add-Info($menu, [string]$text, [bool]$dim = $false) {
    $mi = New-Object System.Windows.Forms.ToolStripMenuItem $text
    $mi.Enabled = $false
    $menu.Items.Add($mi) | Out-Null
}

function Rebuild-Menu($menu) {
    $menu.Items.Clear()
    $st = Load-State
    $keys = if ($st) { Get-SlotKeys $st.slots } else { @() }
    if ($keys.Count -lt 2) {
        Add-Info $menu "시작하기: ① 지금 로그인된 계정 등록 ② 다른 계정 추가 ③ 감시 켜기"
        if (-not $keys.Count) {
            $addCur = New-Object System.Windows.Forms.ToolStripMenuItem "  현재 로그인 계정 등록"
            $addCur.Add_Click({ Run-Cli @("add") "계정 등록" "등록됨" })
            $menu.Items.Add($addCur) | Out-Null
        }
        $addWeb = New-Object System.Windows.Forms.ToolStripMenuItem "  브라우저로 계정 추가…"
        $addWeb.Add_Click({ Open-Dashboard $true })
        $menu.Items.Add($addWeb) | Out-Null
        $menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
    }
    $active = if ($st) { $st.activeSlot } else { $null }
    foreach ($key in $keys) {
        $slot = $st.slots.$key
        $n = [int]$key
        $email = if ($slot.email) { [string]$slot.email } else { "?" }
        $isActive = ("$active" -eq $key)
        $enabled = ($null -eq $slot.enabled) -or $slot.enabled
        $limited = ($slot.kind -eq "setup-token")
        $gateway = ($slot.kind -eq "gateway")
        $quarantined = ($null -ne $slot.quarantine)

        $display = if ($slot.alias) { [string]$slot.alias } else { $email }
        $head = New-Object System.Windows.Forms.ToolStripMenuItem $display
        $head.Enabled = $false
        $head.Checked = $isActive
        $head.Font = New-Object System.Drawing.Font($head.Font, [System.Drawing.FontStyle]::Bold)
        $menu.Items.Add($head) | Out-Null

        if ($gateway) {
            Add-Info $menu "  서드파티 게이트웨이 · 수동 전용"
            Add-Info $menu "  $($slot.modelId)"
        }
        elseif ($limited) { Add-Info $menu "  ⚠ 제한 토큰 — 삭제 후 다시 추가" }
        elseif ($quarantined) { Add-Info $menu "  ■ 다시 로그인 필요 — 브라우저로 다시 추가" }
        if (-not $limited -and -not $gateway) {
            $p5 = Get-Pct $slot "five_hour"
            if ($null -ne $p5) {
                Add-Info $menu ("  $(Sev-Mark $p5) 세션 $([math]::Round($p5))% · 리셋 $(Get-ResetLocal $slot 'five_hour')")
            }
            $p7 = Get-Pct $slot "seven_day"
            if ($null -ne $p7) {
                Add-Info $menu ("  $(Sev-Mark $p7) 주간 $([math]::Round($p7))% · 리셋 $(Get-ResetLocal $slot 'seven_day')")
            }
            if ($slot.usageFetchedAt) {
                # PS 5.1의 Get-Date -UFormat %s는 로컬타임 기준(버그) — UTC epoch 직접 계산
                $age = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$slot.usageFetchedAt)
                # [int] 캐스트는 반올림 — 버림([math]::Floor)이 main.swift·tray.cs와
                # 동일한 표기(90초 → "1분 전")
                $ageText = if ($age -ge 86400) { "$([int][math]::Floor($age/86400))일" }
                           elseif ($age -ge 3600) { "$([int][math]::Floor($age/3600))시간" }
                           elseif ($age -ge 60) { "$([int][math]::Floor($age/60))분" } else { "${age}초" }
                Add-Info $menu "  데이터 $ageText 전"
            }
        }
        # 핸들러는 $this/Tag 대신 GetNewClosure로 루프 지역변수($n, $email 등)를
        # 캡처한다 — PS 이벤트 핸들러의 $this 바인딩 모호성을 회피하는 확정적 방식
        if (-not $isActive -and $enabled -and -not $limited -and -not $quarantined) {
            $sw = New-Object System.Windows.Forms.ToolStripMenuItem "  ⇄ 이 계정으로 전환"
            $sw.Add_Click({ Run-Cli @("switch", "$n") "계정 전환" "슬롯 $n 활성" }.GetNewClosure())
            $menu.Items.Add($sw) | Out-Null
        }
        $manage = New-Object System.Windows.Forms.ToolStripMenuItem "  관리"
        if (-not $gateway) {
            $toggleAction = if ($enabled) { "disable" } else { "enable" }
            $toggle = New-Object System.Windows.Forms.ToolStripMenuItem $(
                if ($enabled) { "끄기 (자동전환 제외)" } else { "켜기 (자동전환 포함)" })
            $toggle.Enabled = (-not $isActive) -and (-not $limited)
            $toggle.Add_Click({ Run-Cli @($toggleAction, "$n") "계정 설정" $null }.GetNewClosure())
            $manage.DropDownItems.Add($toggle) | Out-Null
        }
        $del = New-Object System.Windows.Forms.ToolStripMenuItem "삭제…"
        $del.Enabled = -not $isActive
        $del.Add_Click({
            if (Confirm-Danger "슬롯 $n ($email) 계정을 삭제할까요?`n백업 자격증명도 함께 제거됩니다." "계정 삭제") {
                Run-Cli @("remove", "$n") "계정 삭제" $null
            }
        }.GetNewClosure())
        $manage.DropDownItems.Add($del) | Out-Null
        $menu.Items.Add($manage) | Out-Null
        $menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
    }

    $autoOn = $false
    if ($st -and $st.settings) {
        $thr = if ($st.settings.threshold) { [math]::Round([double]$st.settings.threshold) } else { 90 }
        Add-Info $menu "전환 임계 $thr%"
        $autoOn = ($st.settings.autoSwitch -eq $true)
    }

    # 감시 데몬 상태 — 등록: 작업 스케줄러 작업 존재, 작동: state.json 하트비트.
    # 자동 전환은 따로 켠다(옵트인, 확인창 기본 버튼 = 아니요)
    $taskOk = $false
    try {
        schtasks /Query /TN "LimitPilot-Daemon" 2>$null | Out-Null
        $taskOk = ($LASTEXITCODE -eq 0)
    } catch { }
    $hb = $false
    try {
        $mtime = (Get-Item $StatePath -ErrorAction Stop).LastWriteTime
        $hb = ((Get-Date) - $mtime).TotalSeconds -le 180
    } catch { }
    if ($taskOk -and $hb) {
        Add-Info $menu "● 감시 중" $true
    } elseif ($taskOk) {
        Add-Info $menu "⚠ 감시가 멈춰 있습니다"
        $don = New-Object System.Windows.Forms.ToolStripMenuItem "감시 다시 켜기"
        $don.Add_Click({ Run-Cli @("install-daemon") "감시 켜기" "감시 켜짐" })
        $menu.Items.Add($don) | Out-Null
    } else {
        Add-Info $menu "감시 꺼짐"
        $don = New-Object System.Windows.Forms.ToolStripMenuItem "감시 켜기 (알림만)"
        $don.Add_Click({ Run-Cli @("install-daemon") "감시 켜기" "감시 켜짐" })
        $menu.Items.Add($don) | Out-Null
    }
    if ($autoOn) {
        Add-Info $menu "자동 전환 켜짐"
        $aoff = New-Object System.Windows.Forms.ToolStripMenuItem "자동 전환 끄기 (알림만)"
        $aoff.Add_Click({ Run-Cli @("config", "auto-switch", "off") "자동 전환 끄기" "알림만" })
        $menu.Items.Add($aoff) | Out-Null
    } else {
        Add-Info $menu "자동 전환 꺼짐 — 알림만"
        $aon = New-Object System.Windows.Forms.ToolStripMenuItem "자동 전환 켜기…"
        $aon.Add_Click({
            if (Confirm-Danger ("활성 계정이 임계에 닿으면 감시 데몬이 등록된 다른 계정으로 Claude Code 로그인을 바꿉니다.`n" +
                                "각 계정의 이용 약관·요금제 조건을 지켜 본인 계정에만 쓰세요. 켤까요?") "자동 전환 켜기") {
                Run-Cli @("config", "auto-switch", "on") "자동 전환 켜기" "자동 전환 켜짐"
            }
        })
        $menu.Items.Add($aon) | Out-Null
    }
    $menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
    $dash = New-Object System.Windows.Forms.ToolStripMenuItem "대시보드 열기"
    $dash.Add_Click({ Open-Dashboard $false })
    $menu.Items.Add($dash) | Out-Null
    $re = New-Object System.Windows.Forms.ToolStripMenuItem "새로고침"
    $re.Add_Click({ Update-Tray })
    $menu.Items.Add($re) | Out-Null
    $menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
    # "종료" 는 트레이만 닫는다 — 감시 데몬은 계속 돈다
    $quit = New-Object System.Windows.Forms.ToolStripMenuItem "LimitPilot 종료 (감시는 계속)"
    $quit.Add_Click({ $script:Tray.Visible = $false; [System.Windows.Forms.Application]::Exit() })
    $menu.Items.Add($quit) | Out-Null
    $quitAll = New-Object System.Windows.Forms.ToolStripMenuItem "감시도 끄고 종료…"
    $quitAll.Add_Click({
        if (Confirm-Danger "사용량 감시와 자동 전환이 모두 멈춥니다. 끄고 종료할까요?" "감시도 끄고 종료") {
            Run-Cli @("uninstall-daemon") "감시 끄기" $null
            $script:Tray.Visible = $false
            [System.Windows.Forms.Application]::Exit()
        }
    })
    $menu.Items.Add($quitAll) | Out-Null
}

# ── 트레이 갱신 루프 ───────────────────────────────────────────
function Update-Tray {
    $st = Load-State
    $sum = Build-Summary $st
    # 아이콘은 심각도 색이 바뀔 때만 재생성 — GDI 핸들 낭비 방지
    # 사용률(반올림)이 바뀔 때만 다시 그린다 — 링 채움량이 곧 상태
    $iconKey = if ($null -eq $sum.ActivePct) { -1 } else { [math]::Round([double]$sum.ActivePct) }
    if ($script:LastColor -ne $iconKey) {
        $made = New-TrayIcon $sum.ActivePct
        $oldIcon = $script:Tray.Icon
        $oldHandle = $script:IconHandle
        $script:Tray.Icon = $made.Icon
        $script:IconHandle = $made.Handle
        $script:LastColor = $iconKey
        if ($oldIcon) { $oldIcon.Dispose() }
        if ($oldHandle) { [Win32.NativeIcon]::DestroyIcon($oldHandle) | Out-Null }
    }
    $script:Tray.Text = $sum.Text
    # state.json 재독 5초 고정 — 로컬 파일 읽기(API 호출 0), main.swift와 동일
    if ($script:Timer.Interval -ne 5000) { $script:Timer.Interval = 5000 }
}

$script:LastColor = $null
$script:IconHandle = $null
$script:Tray = New-Object System.Windows.Forms.NotifyIcon
$script:Tray.Visible = $true
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$menu.Add_Opening({ Rebuild-Menu $this })
$script:Tray.ContextMenuStrip = $menu

$script:Timer = New-Object System.Windows.Forms.Timer
$script:Timer.Interval = 5000
$script:Timer.Add_Tick({ Update-Tray })
$script:Timer.Start()

Update-Tray
# 트레이(새 빌드)를 실행하면 데몬 사본을 이 저장소의 CLI 코드로 맞춘다(데몬 미설치면 no-op,
# 결과를 기다리지 않음) — tray.cs SyncDaemon과 동일
try {
    $syncPsi = New-Object System.Diagnostics.ProcessStartInfo
    $syncPsi.FileName = $Python
    $syncArgs = @()
    if ((Split-Path -Leaf $Python) -ieq "py.exe") { $syncArgs += "-3" }
    $syncPsi.Arguments = ($syncArgs + @("`"$CliScript`"", "sync-daemon")) -join " "
    $syncPsi.UseShellExecute = $false
    $syncPsi.CreateNoWindow = $true
    $syncProc = [System.Diagnostics.Process]::Start($syncPsi)
    if ($syncProc) { $syncProc.Dispose() }
} catch { }
try {
    [System.Windows.Forms.Application]::Run((New-Object System.Windows.Forms.ApplicationContext))
} finally {
    try { $script:Timer.Stop() } catch { }
    try { $script:Tray.Visible = $false } catch { }
    try { $script:Tray.Dispose() } catch { }
}
