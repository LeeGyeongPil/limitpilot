"""``limitpilot widget build|install|uninstall`` — compile the menu bar / tray app locally.

macOS: the built-in ``swiftc`` compiles ``native/macos/main.swift`` into
``LimitPilot.app`` with the daemon's ``limitpilot.pyz`` inside, so the app works without
knowing where the package is installed. Windows: the .NET Framework ``csc.exe`` compiles
``native/windows/tray.cs`` (``tray.ps1`` is the fallback). Nothing is downloaded.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from importlib import resources

from limitpilot import __version__
from limitpilot.daemon import deploy
from limitpilot import i18n
from limitpilot import paths

APP_NAME = "LimitPilot.app"


def _native(*parts) -> bytes:
    return resources.files("limitpilot.native").joinpath("/".join(parts)).read_bytes()


def render_info_plist(python=None) -> bytes:
    """Info.plist from the template with the version (single source) and interpreter."""
    info = plistlib.loads(_native("macos", "Info.plist.in"))
    info["CFBundleShortVersionString"] = __version__
    info["CFBundleVersion"] = __version__
    info["CFBundleIdentifier"] = paths.BUNDLE_ID
    info["LimitPilotPython"] = python or deploy.find_python()
    return plistlib.dumps(info)


def _say(msg: str) -> None:
    print(msg, flush=True)


def build_macos(out_dir: str, dry_run: bool = False) -> str:
    """Compile LimitPilot.app into ``out_dir`` and return its path."""
    out_dir = os.path.abspath(out_dir)
    app = os.path.join(out_dir, APP_NAME)
    swiftc = shutil.which("swiftc")
    steps = [
        f"swiftc -O main.swift -framework Cocoa -o {app}/Contents/MacOS/LimitPilot",
        f"Info.plist (version {__version__}, id {paths.BUNDLE_ID})",
        f"{app}/Contents/Resources/LimitPilot.icns",
        f"{app}/Contents/Resources/limitpilot.pyz",
    ]
    if dry_run:
        for s in steps:
            _say(i18n.t("wid_would_build", step=s))
        return app
    if sys.platform != "darwin" or not swiftc:
        raise SystemExit("widget build needs macOS with the Xcode command line tools (swiftc)")
    if os.path.lexists(app):
        if not app.endswith(APP_NAME):
            raise SystemExit(f"refusing to replace {app}")
        shutil.rmtree(app)
    macos_dir = os.path.join(app, "Contents", "MacOS")
    res_dir = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos_dir)
    os.makedirs(res_dir)
    with tempfile.TemporaryDirectory(prefix="limitpilot-swift-") as work:
        src = os.path.join(work, "main.swift")
        with open(src, "wb") as handle:
            handle.write(_native("macos", "main.swift"))
        # 모듈 캐시도 임시 폴더에 — 레포·출력 폴더에 부산물을 남기지 않는다
        cmd = [swiftc, "-O", "-module-cache-path", os.path.join(work, "cache"),
               src, "-framework", "Cocoa", "-o", os.path.join(macos_dir, "LimitPilot")]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            raise SystemExit("swiftc failed")
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as handle:
        handle.write(render_info_plist())
    with open(os.path.join(res_dir, "LimitPilot.icns"), "wb") as handle:
        handle.write(_native("icons", "LimitPilot.icns"))
    deploy.write_pyz(os.path.join(res_dir, "limitpilot.pyz"))
    return app


def _windows_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(paths.HOME, "AppData", "Local")
    return os.path.join(base, "LimitPilot")


def build_windows(out_dir: str, dry_run: bool = False) -> str:
    """Copy the tray sources + pyz to ``out_dir`` and compile tray.exe with csc when present."""
    out_dir = os.path.abspath(out_dir)
    if dry_run:
        _say(i18n.t("wid_would_build_win", out=out_dir))
        return out_dir
    os.makedirs(out_dir, exist_ok=True)
    for name in ("tray.cs", "tray.ps1", "tray-start.cmd"):
        with open(os.path.join(out_dir, name), "wb") as handle:
            handle.write(_native("windows", name))
    with open(os.path.join(out_dir, "LimitPilot.ico"), "wb") as handle:
        handle.write(_native("icons", "LimitPilot.ico"))
    deploy.write_pyz(os.path.join(out_dir, "limitpilot.pyz"))
    windir = os.environ.get("WINDIR", r"C:\Windows")
    csc = os.path.join(windir, "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe")
    if os.path.isfile(csc):
        subprocess.run([csc, "/nologo", "/target:winexe", f"/out:{os.path.join(out_dir, 'tray.exe')}",
                        f"/win32icon:{os.path.join(out_dir, 'LimitPilot.ico')}",
                        "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
                        os.path.join(out_dir, "tray.cs")], check=False, capture_output=True)
    return out_dir


def run(args) -> int:
    action = args.action
    if action == "build":
        out = args.out or (os.path.join(os.getcwd(), "dist") if not paths.IS_WIN else _windows_dir())
        if paths.IS_WIN:
            target = build_windows(out, dry_run=args.dry_run)
        else:
            target = build_macos(out, dry_run=args.dry_run)
        if not args.dry_run:
            _say(f"built {target} ({__version__})")
        return 0
    if action == "install":
        if paths.IS_WIN:
            target = build_windows(args.out or _windows_dir(), dry_run=args.dry_run)
            if not args.dry_run:
                subprocess.Popen(["cmd", "/c", os.path.join(target, "tray-start.cmd")])
        else:
            target = build_macos(args.out or os.path.join(paths.HOME, "Applications"), dry_run=args.dry_run)
            if not args.dry_run:
                subprocess.run(["open", target], check=False)
        if not args.dry_run:
            _say(f"installed {target} ({__version__})")
        return 0
    # uninstall: 앱만 지운다 — 감시 데몬은 `limitpilot uninstall-daemon` 으로 따로
    target = (args.out or _windows_dir()) if paths.IS_WIN else os.path.join(
        args.out or os.path.join(paths.HOME, "Applications"), APP_NAME)
    if args.dry_run:
        _say(i18n.t("wid_would_remove", target=target))
        return 0
    if os.path.isdir(target) and os.path.basename(target) in (APP_NAME, "LimitPilot"):
        shutil.rmtree(target)
        _say(f"removed {target}")
    else:
        _say(f"not installed: {target}")
    return 0
