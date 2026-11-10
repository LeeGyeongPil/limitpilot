"""Windows pseudo console (ConPTY) via ctypes, used by the browser-login session."""

import os
import subprocess
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
else:
    pass

from limitpilot.errors import (LimitPilotError)
from limitpilot import i18n
from limitpilot import paths


def _win_cmdline(argv: list) -> str:
    """CreateProcessW용 명령줄 문자열.

    .exe가 아니면(npm 전역 claude.cmd 등) COMSPEC으로 감싼다. 이때 /s + 전체
    외곽 따옴표 형식을 쓴다 — 평평한 인자 나열로는 'C:\\Program Files (x86)\\…'
    처럼 공백과 &()^ 특수문자가 함께 있는 경로에서 cmd의 따옴표 제거 규칙이
    발동해 실행이 깨진다(cmd /? 참조)."""
    inner = subprocess.list2cmdline(argv)
    if os.path.splitext(argv[0])[1].lower() == ".exe":
        return inner
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return '"{}" /d /s /c "{}"'.format(comspec, inner)


def _win_env_block(env: dict) -> str:
    """CreateProcessW(CREATE_UNICODE_ENVIRONMENT)용 환경 블록.

    각 항목 NUL 종단 — create_unicode_buffer가 마지막 NUL을 하나 더 붙여
    이중 NUL 종단이 완성된다. Windows 관례대로 이름 대문자 기준 정렬."""
    items = sorted(
        ((str(k), str(v)) for k, v in env.items() if v is not None),
        key=lambda kv: kv[0].upper())
    if not items:  # 빈 블록도 이중 NUL 종단이어야 한다
        return "\0"
    return "".join(k + "=" + v + "\0" for k, v in items)


if paths.IS_WIN:
    class _COORD(ctypes.Structure):
        _fields_ = (("X", ctypes.c_short), ("Y", ctypes.c_short))

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        )

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = (("StartupInfo", _STARTUPINFOW),
                    ("lpAttributeList", ctypes.c_void_p))

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = (("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD))

    class _WinConPty:
        """Windows ConPTY(가상 콘솔)로 대화형 CLI를 구동한다 — 표준 라이브러리만 사용.

        유닉스 pty.openpty()+select의 대응물: 출력은 PeekNamedPipe 폴링(비차단),
        입력은 파이프 WriteFile, Enter는 '\\r'. Windows 10 1809+ kernel32 전용 API.
        """

        _PSEUDOCONSOLE_ATTR = 0x00020016   # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
        _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
        _CREATE_UNICODE_ENVIRONMENT = 0x00000400
        _STILL_ACTIVE = 259
        _WAIT_TIMEOUT = 0x102

        def __init__(self, argv: list, env: dict, cols: int = 400, rows: int = 60):
            self._hpc = None
            self._hproc = None
            self._in_write = None
            self._out_read = None
            self._exit_code = None
            self.pid = None
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._k32 = k32
            try:
                create_pty = k32.CreatePseudoConsole
            except AttributeError:
                raise LimitPilotError(i18n.t("cp_missing")) from None
            self._declare_prototypes(k32, create_pty)

            # 파이프 2쌍: (우리 쓰기→클라이언트 stdin), (클라이언트 stdout→우리 읽기)
            in_read, in_write = wintypes.HANDLE(), wintypes.HANDLE()
            out_read, out_write = wintypes.HANDLE(), wintypes.HANDLE()
            if not k32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0):
                raise LimitPilotError(i18n.t("cp_in_pipe"))
            if not k32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0):
                k32.CloseHandle(in_read); k32.CloseHandle(in_write)
                raise LimitPilotError(i18n.t("cp_out_pipe"))
            hpc = ctypes.c_void_p()
            hresult = create_pty(_COORD(cols, rows), in_read, out_write, 0,
                                 ctypes.byref(hpc))
            # ConPTY가 자기 몫의 파이프 끝을 복제하므로 원본 두 끝은 즉시 닫는다
            k32.CloseHandle(in_read)
            k32.CloseHandle(out_write)
            if hresult != 0:
                k32.CloseHandle(in_write); k32.CloseHandle(out_read)
                raise LimitPilotError(
                    i18n.t("cp_create_failed", hr=f"{hresult & 0xFFFFFFFF:08x}"))
            self._hpc = hpc
            self._in_write = in_write
            self._out_read = out_read

            try:
                self._spawn(argv, env)
            except Exception:
                self.close()
                raise

        @staticmethod
        def _declare_prototypes(k32, create_pty) -> None:
            # 64비트에서 HANDLE(포인터 크기)이 기본 int로 잘리는 것을 막는 필수 선언
            HANDLE_P = ctypes.POINTER(wintypes.HANDLE)
            DWORD_P = ctypes.POINTER(wintypes.DWORD)
            k32.CreatePipe.argtypes = (HANDLE_P, HANDLE_P, ctypes.c_void_p, wintypes.DWORD)
            k32.CreatePipe.restype = wintypes.BOOL
            create_pty.argtypes = (_COORD, wintypes.HANDLE, wintypes.HANDLE,
                                   wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
            create_pty.restype = ctypes.c_long
            k32.ClosePseudoConsole.argtypes = (ctypes.c_void_p,)
            k32.ClosePseudoConsole.restype = None
            k32.InitializeProcThreadAttributeList.argtypes = (
                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                ctypes.POINTER(ctypes.c_size_t))
            k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
            k32.UpdateProcThreadAttribute.argtypes = (
                ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
            k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
            k32.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
            k32.DeleteProcThreadAttributeList.restype = None
            k32.CreateProcessW.argtypes = (
                wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                ctypes.c_void_p, ctypes.POINTER(_PROCESS_INFORMATION))
            k32.CreateProcessW.restype = wintypes.BOOL
            k32.PeekNamedPipe.argtypes = (wintypes.HANDLE, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.c_void_p, DWORD_P,
                                          ctypes.c_void_p)
            k32.PeekNamedPipe.restype = wintypes.BOOL
            k32.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                     DWORD_P, ctypes.c_void_p)
            k32.ReadFile.restype = wintypes.BOOL
            k32.WriteFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                      DWORD_P, ctypes.c_void_p)
            k32.WriteFile.restype = wintypes.BOOL
            k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, DWORD_P)
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            k32.TerminateProcess.restype = wintypes.BOOL
            k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            k32.CloseHandle.restype = wintypes.BOOL

        def _spawn(self, argv: list, env: dict) -> None:
            k32 = self._k32
            size = ctypes.c_size_t(0)
            k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attrs = ctypes.create_string_buffer(size.value)
            if not k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
                raise LimitPilotError(i18n.t("cp_attr_init"))
            try:
                # 공식 샘플과 동일: lpValue 자리에 HPCON 핸들 값을 그대로 전달
                if not k32.UpdateProcThreadAttribute(
                        attrs, 0, self._PSEUDOCONSOLE_ATTR, self._hpc,
                        ctypes.sizeof(ctypes.c_void_p), None, None):
                    raise LimitPilotError(i18n.t("cp_attr_set"))
                startup = _STARTUPINFOEXW()
                startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
                startup.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
                proc_info = _PROCESS_INFORMATION()
                cmdline = ctypes.create_unicode_buffer(_win_cmdline(argv))
                env_block = ctypes.create_unicode_buffer(_win_env_block(env))
                ok = k32.CreateProcessW(
                    None, cmdline, None, None, False,
                    self._EXTENDED_STARTUPINFO_PRESENT | self._CREATE_UNICODE_ENVIRONMENT,
                    env_block, None, ctypes.byref(startup), ctypes.byref(proc_info))
                if not ok:
                    raise LimitPilotError(
                        i18n.t("cp_spawn_failed", err=ctypes.get_last_error()))
            finally:
                k32.DeleteProcThreadAttributeList(attrs)
            k32.CloseHandle(proc_info.hThread)
            self._hproc = proc_info.hProcess
            self.pid = int(proc_info.dwProcessId)

        def read_available(self, limit: int = 4096) -> bytes:
            """비차단 읽기 — 대기 데이터가 없거나 파이프가 닫혔으면 b''."""
            if self._out_read is None:
                return b""
            avail = wintypes.DWORD(0)
            if not self._k32.PeekNamedPipe(self._out_read, None, 0, None,
                                           ctypes.byref(avail), None):
                return b""  # 파이프 닫힘(클라이언트/conhost 종료)
            if not avail.value:
                return b""
            want = min(int(avail.value), limit)
            buf = ctypes.create_string_buffer(want)
            got = wintypes.DWORD(0)
            if not self._k32.ReadFile(self._out_read, buf, want, ctypes.byref(got), None):
                return b""
            return buf.raw[:got.value]

        def write(self, data: bytes) -> None:
            if self._in_write is None:
                raise OSError(i18n.t("cp_input_closed"))
            written = wintypes.DWORD(0)
            if not self._k32.WriteFile(self._in_write, data, len(data),
                                       ctypes.byref(written), None):
                raise OSError(i18n.t("cp_write_failed", err=ctypes.get_last_error()))

        def poll(self):
            """subprocess.Popen.poll() 대응 — 실행 중 None, 종료 시 종료코드."""
            if self._exit_code is not None or self._hproc is None:
                return self._exit_code
            code = wintypes.DWORD(0)
            if not self._k32.GetExitCodeProcess(self._hproc, ctypes.byref(code)):
                return None
            if code.value == self._STILL_ACTIVE:
                # 실제 종료코드가 259(STILL_ACTIVE)일 극단 케이스 — 시그널 상태로 재확인
                if self._k32.WaitForSingleObject(self._hproc, 0) == self._WAIT_TIMEOUT:
                    return None
            self._exit_code = int(code.value)
            return self._exit_code

        def close(self) -> None:
            k32 = self._k32
            # 파이프를 먼저 닫아 conhost 쓰기 블록으로 인한 대기를 방지
            for name in ("_in_write", "_out_read"):
                handle = getattr(self, name)
                if handle is not None:
                    setattr(self, name, None)
                    k32.CloseHandle(handle)
            # 클라이언트를 ClosePseudoConsole보다 먼저 정리한다 — 클라이언트가 붙어
            # 있는 동안 ClosePseudoConsole은 반환하지 않을 수 있다(취소 경로 행).
            # TerminateProcess는 리더(cmd.exe 심)만 죽이므로 taskkill /T로 트리
            # 전체(심 아래 node claude와 콜백 리스너)까지 제거한다 — 고아가 남으면
            # 평문 credentials 파일을 잡아 profile 삭제가 실패하고 콜백 포트를 점유한다.
            if self._hproc is not None:
                if self.poll() is None and self.pid:
                    try:
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(self.pid)],
                            capture_output=True, timeout=5,
                            creationflags=0x08000000)  # CREATE_NO_WINDOW
                    except (OSError, subprocess.SubprocessError):
                        pass
                    k32.WaitForSingleObject(self._hproc, 750)
                if self.poll() is None:
                    k32.TerminateProcess(self._hproc, 1)
                    k32.WaitForSingleObject(self._hproc, 750)
                    self.poll()
                k32.CloseHandle(self._hproc)
                self._hproc = None
            hpc, self._hpc = self._hpc, None
            if hpc is not None:
                k32.ClosePseudoConsole(hpc)
