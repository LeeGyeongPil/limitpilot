// LimitPilot — Windows tray app (no dependencies: the .NET Framework built into Windows).
// Same design as the macOS menu bar app (native/macos/main.swift): it only reads the state
// file; every change (switch, ON/OFF, delete) runs the limitpilot CLI (limitpilot.pyz next
// to this exe), so the CLI's safety checks apply. Closing the tray does not stop watching.
// Build: `limitpilot widget install` (or tray-start.cmd) compiles this file with csc.exe.
// Note: the built-in csc (.NET Framework 4.x) supports C# 5 only — no $"" or ?.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("LimitPilot")]
[assembly: AssemblyProduct("LimitPilot")]
[assembly: AssemblyDescription("LimitPilot tray app")]

namespace LimitPilot
{
    static class Program
    {
        [STAThread]
        static void Main()
        {
            // 중복 실행 방지 — tray.ps1과 같은 뮤텍스 이름이라 exe/ps1 혼용 실행도 차단
            bool acquired = false;
            var mutex = new Mutex(false, "Local\\LimitPilotTray");
            try
            {
                try { acquired = mutex.WaitOne(0, false); }
                catch (AbandonedMutexException) { acquired = true; }
                if (!acquired) return;

                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                // UI 스레드 예외는 조용히 삼키고 계속 — tray.ps1의 스크립트블록 예외 처리와 동일
                Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
                Application.ThreadException += delegate { };
                using (var context = new TrayContext())
                {
                    if (context.Ready) Application.Run(context);
                }
            }
            finally
            {
                if (acquired) { try { mutex.ReleaseMutex(); } catch (Exception) { } }
                mutex.Dispose();
            }
        }
    }

    sealed class TrayContext : ApplicationContext
    {
        [DllImport("user32.dll", SetLastError = true)]
        static extern bool DestroyIcon(IntPtr handle);

        readonly string python;
        readonly string cliScript;
        readonly string statePath;
        readonly NotifyIcon tray;
        readonly ContextMenuStrip menu;
        readonly System.Windows.Forms.Timer timer;
        readonly Font boldFont;
        int lastIconKey = int.MinValue;
        IntPtr iconHandle = IntPtr.Zero;
        public readonly bool Ready;

        public TrayContext()
        {
            // limitpilot.pyz 는 tray.exe 옆에 있다(widget install 이 함께 복사)
            string baseDir = AppDomain.CurrentDomain.BaseDirectory
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            cliScript = Path.Combine(baseDir, "limitpilot.pyz");
            string home = Environment.GetEnvironmentVariable("LIMITPILOT_HOME");
            if (string.IsNullOrEmpty(home))
                home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".limitpilot");
            statePath = Path.Combine(home, "state.json");

            python = FindPython();
            if (python == null)
            {
                MessageBox.Show(
                    "Python 3을 찾을 수 없습니다. python.org 또는 Microsoft Store에서 설치하세요.",
                    "LimitPilot", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Ready = false;
                return;
            }
            Ready = true;

            tray = new NotifyIcon();
            menu = new ContextMenuStrip();
            boldFont = new Font(menu.Font, FontStyle.Bold);
            menu.Opening += delegate { RebuildMenu(); };
            tray.ContextMenuStrip = menu;

            timer = new System.Windows.Forms.Timer();
            // state.json 재독 5초 고정 — 로컬 파일 읽기(API 호출 0), main.swift와 동일
            timer.Interval = 5000;
            timer.Tick += delegate { UpdateTray(); };

            UpdateTray();
            tray.Visible = true;
            timer.Start();
            SyncDaemon();
        }

        // 트레이(새 빌드)를 실행하면 데몬 사본을 이 저장소의 CLI 코드로 맞춘다 — 파일만
        // 교체하고 CLI를 따로 쓰지 않아도 작업 스케줄러가 예전 코드로 남지 않게.
        // 데몬 미설치면 no-op. 결과를 기다리지 않고 조용히 실행한다.
        void SyncDaemon()
        {
            try
            {
                using (Process.Start(PythonStartInfo(cliScript, new[] { "sync-daemon" }, false))) { }
            }
            catch (Exception) { }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                try { if (timer != null) timer.Stop(); } catch (Exception) { }
                try { if (tray != null) tray.Visible = false; } catch (Exception) { }
                // 트레이를 닫아도 감시 데몬은 그대로 둔다 — 끄려면 "감시도 끄고 종료"
                try { if (tray != null) tray.Dispose(); } catch (Exception) { }
                try { if (menu != null) menu.Dispose(); } catch (Exception) { }
                try { if (boldFont != null) boldFont.Dispose(); } catch (Exception) { }
                if (iconHandle != IntPtr.Zero)
                {
                    DestroyIcon(iconHandle);
                    iconHandle = IntPtr.Zero;
                }
            }
            base.Dispose(disposing);
        }

        // ── 경로/도구 해석 ─────────────────────────────────────
        static string FindPython()
        {
            string[] names = { "py.exe", "python.exe" };
            string path = Environment.GetEnvironmentVariable("PATH") ?? "";
            foreach (string name in names)
                foreach (string dir in path.Split(Path.PathSeparator))
                {
                    if (dir == null || dir.Trim().Length == 0) continue;
                    try
                    {
                        string full = Path.Combine(dir.Trim(), name);
                        if (File.Exists(full)) return full;
                    }
                    catch (ArgumentException) { }  // PATH의 잘못된 항목은 건너뜀
                }
            return null;
        }

        // ── 상태 읽기/해석 (main.swift 규칙과 동일) ─────────────
        Dictionary<string, object> LoadState()
        {
            try
            {
                var serializer = new JavaScriptSerializer();
                serializer.MaxJsonLength = int.MaxValue;
                return serializer.DeserializeObject(File.ReadAllText(statePath, Encoding.UTF8))
                    as Dictionary<string, object>;
            }
            catch (Exception) { return null; }
        }

        static object Field(object node, params string[] path)
        {
            foreach (string key in path)
            {
                var dict = node as Dictionary<string, object>;
                if (dict == null || !dict.TryGetValue(key, out node)) return null;
            }
            return node;
        }

        static double? AsNumber(object value)
        {
            if (value == null || value is bool) return null;
            try { return Convert.ToDouble(value, CultureInfo.InvariantCulture); }
            catch (Exception) { return null; }
        }

        static string KeyString(object value)
        {
            return value == null ? null : Convert.ToString(value, CultureInfo.InvariantCulture);
        }

        static List<string> SlotKeys(object slots)
        {
            var numbers = new List<int>();
            var dict = slots as Dictionary<string, object>;
            if (dict != null)
                foreach (string key in dict.Keys)
                {
                    int n;
                    if (int.TryParse(key, NumberStyles.None, CultureInfo.InvariantCulture, out n))
                        numbers.Add(n);
                }
            numbers.Sort();
            return numbers.ConvertAll(delegate(int n) { return n.ToString(CultureInfo.InvariantCulture); });
        }

        static double? GetPct(object slot, string window)
        {
            // '데이터 없음'과 0%를 구분한다 — null이 0%로 둔갑하면 초록 아이콘/유령 0% 행이 생긴다
            return AsNumber(Field(slot, "usage", window, "pct"));
        }

        static string GetResetLocal(object slot, string window)
        {
            try
            {
                string iso = Field(slot, "usage", window, "resets_at") as string;
                if (iso == null || iso.Length < 19) return "?";
                DateTime parsed = DateTime.ParseExact(iso.Substring(0, 19), "yyyy-MM-ddTHH:mm:ss",
                    CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal);
                return parsed.ToLocalTime().ToString("M/d HH:mm", CultureInfo.InvariantCulture);
            }
            catch (Exception) { return "?"; }
        }

        static bool IsEnabled(object slot)
        {
            object value = Field(slot, "enabled");
            return !(value is bool) || (bool)value;
        }

        static Color SevColor(double? pct)
        {
            if (!pct.HasValue) return Color.FromArgb(46, 196, 182);
            if (pct.Value >= 100) return Color.FromArgb(232, 72, 72);   // 소진
            if (pct.Value >= 85) return Color.FromArgb(255, 176, 32);   // 임박(앰버 레드라인)
            return Color.FromArgb(46, 196, 182);                         // 여유(청록)
        }

        static string SevMark(double? pct)
        {
            if (!pct.HasValue) return "";
            if (pct.Value >= 100) return "■";
            if (pct.Value >= 90) return "▲";
            if (pct.Value >= 70) return "◆";
            return "●";
        }

        static string Round(double value)
        {
            return Math.Round(value).ToString(CultureInfo.InvariantCulture);
        }

        // 트레이 아이콘 — 앱 아이콘과 같은 모양: 네이비 사각 + 270° 게이지(사용률만큼
        // 채움, 색은 심각도) + 흰 바늘. 트레이는 글자를 못 담으므로 게이지+툴팁으로 알린다.
        static void MakeTrayIcon(double? pct, out Icon icon, out IntPtr handle)
        {
            using (var bmp = new Bitmap(16, 16))
            {
                using (var g = Graphics.FromImage(bmp))
                {
                    g.SmoothingMode = SmoothingMode.AntiAlias;
                    // 네이비(#0F1E33) 둥근 사각 배경
                    using (var path = new GraphicsPath())
                    {
                        path.AddArc(0, 0, 8, 8, 180, 90);
                        path.AddArc(8, 0, 8, 8, 270, 90);
                        path.AddArc(8, 8, 8, 8, 0, 90);
                        path.AddArc(0, 8, 8, 8, 90, 90);
                        path.CloseFigure();
                        using (var bg = new SolidBrush(Color.FromArgb(15, 30, 51)))
                            g.FillPath(bg, path);
                    }
                    // 게이지 트랙(반투명) — 7시 방향(135°)에서 시계 방향 270°
                    using (var track = new Pen(Color.FromArgb(90, 255, 255, 255), 2.2f))
                        g.DrawArc(track, 3, 3, 10, 10, 135f, 270f);
                    float frac = pct.HasValue ? (float)Math.Max(0.0, Math.Min(1.0, pct.Value / 100.0)) : 0f;
                    if (frac > 0)
                    {
                        using (var arc = new Pen(SevColor(pct), 2.4f))
                        {
                            arc.StartCap = LineCap.Round;
                            arc.EndCap = LineCap.Round;
                            g.DrawArc(arc, 3, 3, 10, 10, 135f, Math.Max(8f, 270f * frac));
                        }
                    }
                    double rad = (135.0 + 270.0 * frac) * Math.PI / 180.0;
                    using (var needle = new Pen(Color.FromArgb(245, 247, 250), 1.5f))
                        g.DrawLine(needle, 8f, 8f, 8f + (float)(Math.Cos(rad) * 3.6), 8f + (float)(Math.Sin(rad) * 3.6));
                }
                // GetHicon() 핸들은 GDI 누수 방지를 위해 DestroyIcon으로 직접 해제한다
                handle = bmp.GetHicon();
                icon = Icon.FromHandle(handle);
            }
        }

        // 툴팁 규칙: 활성 세션 % + 괄호 안 전체(ON) 평균. 예: "LimitPilot 40% (80%)"
        sealed class Summary
        {
            public string Text;
            public double? ActivePct;
        }

        Summary BuildSummary(Dictionary<string, object> st)
        {
            string activeText = null;
            double? activePct = null;
            var pcts = new List<double>();
            object slots = Field(st, "slots");
            if (slots != null)
            {
                string active = KeyString(Field(st, "activeSlot"));
                foreach (string key in SlotKeys(slots))
                {
                    object slot = Field(slots, key);
                    bool isActive = active == key;
                    if (!isActive && !IsEnabled(slot)) continue;
                    string kind = Field(slot, "kind") as string;
                    if (kind == "setup-token")
                    {
                        if (isActive) activeText = "⚠ 제한";
                        continue;
                    }
                    if (kind == "gateway")
                    {
                        if (isActive)
                        {
                            string alias = Field(slot, "alias") as string;
                            activeText = string.IsNullOrEmpty(alias) ? "↗ Gateway" : "↗ " + alias;
                        }
                        continue;
                    }
                    double? pct = GetPct(slot, "five_hour");
                    if (pct.HasValue) pcts.Add(pct.Value);
                    if (isActive)
                    {
                        activePct = pct;
                        activeText = pct.HasValue ? Round(pct.Value) + "%" : "—";
                    }
                }
            }
            string summary = activeText != null ? "LimitPilot " + activeText : "LimitPilot — 데이터 없음";
            if (activeText != null && pcts.Count > 1)
            {
                double total = 0;
                foreach (double p in pcts) total += p;
                summary += " (" + Round(total / pcts.Count) + "%)";
            }
            if (summary.Length > 63) summary = summary.Substring(0, 60) + "…";  // NotifyIcon.Text 63자 제한
            return new Summary { Text = summary, ActivePct = activePct };
        }

        // ── CLI 위임 (runCLI 대응) ─────────────────────────────
        ProcessStartInfo PythonStartInfo(string script, string[] args, bool capture)
        {
            var psi = new ProcessStartInfo();
            psi.FileName = python;
            var parts = new List<string>();
            if (string.Equals(Path.GetFileName(python), "py.exe", StringComparison.OrdinalIgnoreCase))
                parts.Add("-3");
            parts.Add("\"" + script + "\"");
            if (args != null) parts.AddRange(args);
            psi.Arguments = string.Join(" ", parts.ToArray());
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            if (capture)
            {
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                psi.StandardOutputEncoding = Encoding.UTF8;
                psi.StandardErrorEncoding = Encoding.UTF8;
            }
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            return psi;
        }

        void RunCli(string[] cliArgs, string operation, string success)
        {
            try
            {
                using (var process = Process.Start(PythonStartInfo(cliScript, cliArgs, true)))
                {
                    // stderr는 비동기 소비 — 두 스트림 순차 동기 읽기의 파이프 교착 방지
                    var errTask = process.StandardError.ReadToEndAsync();
                    string output = process.StandardOutput.ReadToEnd();
                    process.WaitForExit();
                    string detail = (output + errTask.Result).Trim();
                    if (process.ExitCode == 0)
                    {
                        if (success != null)
                            tray.ShowBalloonTip(2500, "LimitPilot", success, ToolTipIcon.Info);
                    }
                    else
                    {
                        if (detail.Length == 0) detail = "요청을 완료하지 못했습니다. 잠시 후 다시 시도하세요.";
                        MessageBox.Show(detail, operation + " 실패",
                            MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    }
                }
            }
            catch (Exception ex)
            {
                MessageBox.Show("명령을 시작할 수 없습니다: " + ex.Message, operation + " 실패",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            UpdateTray();
        }

        void OpenDashboard(bool addPanel)
        {
            try
            {
                string[] args = addPanel ? new[] { "dashboard", "--add" } : new[] { "dashboard" };
                using (Process.Start(PythonStartInfo(cliScript, args, false))) { }
            }
            catch (Exception ex)
            {
                MessageBox.Show("대시보드를 열 수 없습니다: " + ex.Message,
                    "LimitPilot", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }

        // 확인창 — 기본 버튼(Enter)은 '아니요'(Button2): 실수로 위험한 동작을 실행하지 않게
        static bool ConfirmDanger(string text, string title)
        {
            return MessageBox.Show(text, title, MessageBoxButtons.YesNo, MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2) == DialogResult.Yes;
        }

        // ── 메뉴 구성 (rebuildMenu 대응) — 열릴 때마다 재구성 ──
        void AddInfo(string text)
        {
            var item = new ToolStripMenuItem(text);
            item.Enabled = false;
            menu.Items.Add(item);
        }

        void RebuildMenu()
        {
            // 장기 실행 exe이므로 이전 항목을 Dispose해 핸들 누적을 막는다
            var stale = new List<ToolStripItem>();
            foreach (ToolStripItem item in menu.Items) stale.Add(item);
            menu.Items.Clear();
            foreach (ToolStripItem item in stale) item.Dispose();

            var st = LoadState();
            object slots = Field(st, "slots");
            List<string> keys = st != null ? SlotKeys(slots) : new List<string>();
            if (keys.Count < 2)
            {
                // 처음 쓰는 사람용 안내 — 명령을 외우지 않아도 메뉴에서 바로
                AddInfo("시작하기: ① 지금 로그인된 계정 등록 ② 다른 계정 추가 ③ 감시 켜기");
                if (keys.Count == 0)
                {
                    var addCurrent = new ToolStripMenuItem("  현재 로그인 계정 등록");
                    addCurrent.Click += delegate { RunCli(new[] { "add" }, "계정 등록", "등록됨"); };
                    menu.Items.Add(addCurrent);
                }
                var addBrowser = new ToolStripMenuItem("  브라우저로 계정 추가…");
                addBrowser.Click += delegate { OpenDashboard(true); };
                menu.Items.Add(addBrowser);
                menu.Items.Add(new ToolStripSeparator());
            }
            string active = KeyString(Field(st, "activeSlot"));
            foreach (string key in keys)
            {
                object slot = Field(slots, key);
                string slotN = key;
                string email = Field(slot, "email") as string;
                if (string.IsNullOrEmpty(email)) email = "?";
                string capturedEmail = email;
                bool isActive = active == key;
                bool enabled = IsEnabled(slot);
                string kind = Field(slot, "kind") as string;
                bool limited = kind == "setup-token";
                bool gateway = kind == "gateway";
                bool quarantined = Field(slot, "quarantine") != null;

                string alias = Field(slot, "alias") as string;
                var head = new ToolStripMenuItem(string.IsNullOrEmpty(alias) ? email : alias);
                head.Enabled = false;
                head.Checked = isActive;
                head.Font = boldFont;
                menu.Items.Add(head);

                if (gateway)
                {
                    AddInfo("  서드파티 게이트웨이 · 수동 전용");
                    AddInfo("  " + (Field(slot, "modelId") as string));
                }
                else if (limited) AddInfo("  ⚠ 제한 토큰 — 삭제 후 다시 추가");
                else if (quarantined) AddInfo("  ■ 다시 로그인 필요 — 브라우저로 다시 추가");
                if (!limited && !gateway)
                {
                    double? p5 = GetPct(slot, "five_hour");
                    if (p5.HasValue)
                        AddInfo("  " + SevMark(p5) + " 세션 " + Round(p5.Value)
                            + "% · 리셋 " + GetResetLocal(slot, "five_hour"));
                    double? p7 = GetPct(slot, "seven_day");
                    if (p7.HasValue)
                        AddInfo("  " + SevMark(p7) + " 주간 " + Round(p7.Value)
                            + "% · 리셋 " + GetResetLocal(slot, "seven_day"));
                    double? fetched = AsNumber(Field(slot, "usageFetchedAt"));
                    if (fetched.HasValue && fetched.Value > 0)
                    {
                        long age = DateTimeOffset.UtcNow.ToUnixTimeSeconds() - (long)fetched.Value;
                        if (age < 0) age = 0;
                        string ageText = age >= 86400 ? (age / 86400) + "일"
                            : age >= 3600 ? (age / 3600) + "시간"
                            : age >= 60 ? (age / 60) + "분" : age + "초";
                        AddInfo("  데이터 " + ageText + " 전");
                    }
                }
                if (!isActive && enabled && !limited && !quarantined)
                {
                    var sw = new ToolStripMenuItem("  ⇄ 이 계정으로 전환");
                    sw.Click += delegate
                    {
                        RunCli(new[] { "switch", slotN }, "계정 전환", "슬롯 " + slotN + " 활성");
                    };
                    menu.Items.Add(sw);
                }
                var manage = new ToolStripMenuItem("  관리");
                if (!gateway)
                {
                    string toggleAction = enabled ? "disable" : "enable";
                    var toggle = new ToolStripMenuItem(
                        enabled ? "끄기 (자동전환 제외)" : "켜기 (자동전환 포함)");
                    toggle.Enabled = !isActive && !limited;
                    toggle.Click += delegate { RunCli(new[] { toggleAction, slotN }, "계정 설정", null); };
                    manage.DropDownItems.Add(toggle);
                }
                var del = new ToolStripMenuItem("삭제…");
                del.Enabled = !isActive;
                del.Click += delegate
                {
                    if (ConfirmDanger(
                        "슬롯 " + slotN + " (" + capturedEmail + ") 계정을 삭제할까요?\n백업 자격증명도 함께 제거됩니다.",
                        "계정 삭제"))
                        RunCli(new[] { "remove", slotN }, "계정 삭제", null);
                };
                manage.DropDownItems.Add(del);
                menu.Items.Add(manage);
                menu.Items.Add(new ToolStripSeparator());
            }

            bool autoOn = false;
            if (Field(st, "settings") != null)
            {
                double? threshold = AsNumber(Field(st, "settings", "threshold"));
                double shown = threshold.HasValue && threshold.Value != 0 ? threshold.Value : 90;
                AddInfo("전환 임계 " + Round(shown) + "%");
                object auto = Field(st, "settings", "autoSwitch");
                autoOn = auto is bool && (bool)auto;
            }

            // 감시 데몬 상태 — 등록: 작업 스케줄러 작업 존재, 작동: state.json 하트비트
            // (데몬이 매 틱 저장, 3분 이상 조용하면 멈춘 것). 자동 전환은 따로 켠다(옵트인).
            bool taskOk = false;
            try
            {
                var psi = new ProcessStartInfo("schtasks", "/Query /TN \"LimitPilot-Daemon\"");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                using (var process = Process.Start(psi))
                {
                    process.StandardOutput.ReadToEndAsync();
                    process.StandardError.ReadToEndAsync();
                    if (process.WaitForExit(5000)) taskOk = process.ExitCode == 0;
                    else { try { process.Kill(); } catch (Exception) { } }
                }
            }
            catch (Exception) { }
            bool heartbeat = false;
            try
            {
                if (File.Exists(statePath))
                    heartbeat = (DateTime.Now - File.GetLastWriteTime(statePath)).TotalSeconds <= 180;
            }
            catch (Exception) { }
            if (taskOk && heartbeat)
            {
                AddInfo("● 감시 중");
            }
            else if (taskOk)
            {
                AddInfo("⚠ 감시가 멈춰 있습니다");
                var on = new ToolStripMenuItem("감시 다시 켜기");
                on.Click += delegate { RunCli(new[] { "install-daemon" }, "감시 켜기", "감시 켜짐"); };
                menu.Items.Add(on);
            }
            else
            {
                AddInfo("감시 꺼짐");
                var on = new ToolStripMenuItem("감시 켜기 (알림만)");
                on.Click += delegate { RunCli(new[] { "install-daemon" }, "감시 켜기", "감시 켜짐"); };
                menu.Items.Add(on);
            }
            if (autoOn)
            {
                AddInfo("자동 전환 켜짐");
                var autoOff = new ToolStripMenuItem("자동 전환 끄기 (알림만)");
                autoOff.Click += delegate { RunCli(new[] { "config", "auto-switch", "off" }, "자동 전환 끄기", "알림만"); };
                menu.Items.Add(autoOff);
            }
            else
            {
                AddInfo("자동 전환 꺼짐 — 알림만");
                var autoOnItem = new ToolStripMenuItem("자동 전환 켜기…");
                autoOnItem.Click += delegate
                {
                    if (ConfirmDanger(
                        "활성 계정이 임계에 닿으면 감시 데몬이 등록된 다른 계정으로 Claude Code 로그인을 바꿉니다.\n"
                        + "각 계정의 이용 약관·요금제 조건을 지켜 본인 계정에만 쓰세요. 켤까요?",
                        "자동 전환 켜기"))
                        RunCli(new[] { "config", "auto-switch", "on" }, "자동 전환 켜기", "자동 전환 켜짐");
                };
                menu.Items.Add(autoOnItem);
            }
            menu.Items.Add(new ToolStripSeparator());
            var dashboard = new ToolStripMenuItem("대시보드 열기");
            dashboard.Click += delegate { OpenDashboard(false); };
            menu.Items.Add(dashboard);
            var refresh = new ToolStripMenuItem("새로고침");
            refresh.Click += delegate { UpdateTray(); };
            menu.Items.Add(refresh);
            menu.Items.Add(new ToolStripSeparator());
            // "종료" 는 트레이만 닫는다 — 감시 데몬은 계속 돈다
            var quit = new ToolStripMenuItem("LimitPilot 종료 (감시는 계속)");
            quit.Click += delegate
            {
                tray.Visible = false;
                ExitThread();
            };
            menu.Items.Add(quit);
            // 감시까지 끄려면 별도 항목 + 확인(기본 버튼 = 아니요)
            var quitAll = new ToolStripMenuItem("감시도 끄고 종료…");
            quitAll.Click += delegate
            {
                if (!ConfirmDanger("사용량 감시와 자동 전환이 모두 멈춥니다. 끄고 종료할까요?", "감시도 끄고 종료"))
                    return;
                RunCli(new[] { "uninstall-daemon" }, "감시 끄기", null);
                tray.Visible = false;
                ExitThread();
            };
            menu.Items.Add(quitAll);
        }

        // ── 트레이 갱신 루프 ───────────────────────────────────
        void UpdateTray()
        {
            Summary summary = BuildSummary(LoadState());
            // 아이콘은 사용률(반올림)이 바뀔 때만 재생성 — 링 채움량이 곧 상태, GDI 핸들 낭비 방지
            int iconKey = summary.ActivePct.HasValue ? (int)Math.Round(summary.ActivePct.Value) : -1;
            if (lastIconKey != iconKey)
            {
                Icon icon;
                IntPtr handle;
                MakeTrayIcon(summary.ActivePct, out icon, out handle);
                Icon oldIcon = tray.Icon;
                IntPtr oldHandle = iconHandle;
                tray.Icon = icon;
                iconHandle = handle;
                lastIconKey = iconKey;
                if (oldIcon != null) oldIcon.Dispose();
                if (oldHandle != IntPtr.Zero) DestroyIcon(oldHandle);
            }
            tray.Text = summary.Text;
        }
    }
}
