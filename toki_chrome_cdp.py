"""toki 사이트용 일반 Chrome(CDP) 헬퍼.

Playwright launch/persistent 창은 Cloudflare Turnstile 체크박스가 안 뜨는 경우가 많아,
실제 chrome.exe 를 --remote-debugging-port 로 띄운 뒤 connect_over_cdp 로 붙습니다.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BASE_PATH = Path("D:/booktoon")
DEFAULT_USER_DATA_DIR = DEFAULT_BASE_PATH / ".toki_browser"
CDP_CONNECT_TIMEOUT_SEC = 45
CDP_PORT_FILE_NAME = "cdp_port.txt"
BROWSER_ZOOM_PERCENT = 50  # .toki_browser 페이지 기본 확대/축소 비율(%)
WINDOW_SIDE = "left"  # .toki_browser 창 위치: 화면 왼쪽 절반(작업 영역 높이 전체)


def window_rect(side: str = WINDOW_SIDE) -> tuple[int, int, int, int]:
    """화면 작업 영역의 좌/우 절반 (x, y, w, h)."""
    try:
        from link_open_helper import half_rect

        return half_rect(side)
    except Exception:  # noqa: BLE001
        return (960 if side == "right" else 0), 0, 960, 1040


def prepare_profile_prefs(user_data_dir: Path, rect: tuple[int, int, int, int]) -> None:
    """프로필이 아직 없을 때만 기본 배율과 창 위치를 넣습니다.

    이미 있는 Preferences 를 json 으로 통째로 다시 쓰면 큰 정수 정밀도가 바뀌거나
    보호된 설정 서명이 어긋나, Chrome 이
    '프로필을 여는 동안 문제가 발생했습니다. 일부 기능을 사용할 수 없습니다.'
    대화상자를 띄웁니다. 그 상태에서 페이지 타깃이 닫히면 수집이 중단됩니다.
    창 위치는 실행 인자(--window-position, --window-size)로도 지정합니다.
    """
    prefs_path = user_data_dir / "Default" / "Preferences"
    if prefs_path.exists():
        return
    x, y, w, h = rect
    zoom_level = math.log(BROWSER_ZOOM_PERCENT / 100) / math.log(1.2)  # Chrome 배율 단위
    prefs = {
        "partition": {
            "default_zoom_level": {"x": zoom_level},
            "per_host_zoom_levels": {"x": {}},
        },
        "browser": {
            "window_placement": {
                "left": x,
                "top": y,
                "right": x + w,
                "bottom": y + h,
                "maximized": False,
            }
        },
    }
    try:
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        prefs_path.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def find_chrome_executable() -> str | None:
    env_path = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    env_path_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        str(Path(env_path) / "Google/Chrome/Application/chrome.exe"),
        str(Path(env_path_x86) / "Google/Chrome/Application/chrome.exe"),
        str(Path(local_app) / "Google/Chrome/Application/chrome.exe") if local_app else "",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


def pick_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_cdp_ready(port: int, timeout_sec: float = CDP_CONNECT_TIMEOUT_SEC) -> bool:
    from urllib.error import URLError
    from urllib.request import urlopen

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as res:
                if res.status == 200:
                    return True
        except (URLError, OSError, TimeoutError):
            time.sleep(0.25)
    return False


def _pid_running(pid: int) -> bool:
    if sys.platform == "win32":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return str(pid) in (completed.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _taskkill(pid: int, force: bool) -> None:
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _cdp_browser_close(port: int) -> bool:
    """디버깅 포트로 Browser.close 를 보냅니다. 강제 종료보다 프로필을 정상 저장합니다."""
    from urllib.request import urlopen

    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as res:
            info = json.loads(res.read().decode("utf-8", "replace"))
    except (OSError, ValueError, TimeoutError):
        return False
    ws_url = str(info.get("webSocketDebuggerUrl") or "")
    if not ws_url.startswith("ws://"):
        return False
    host_port, _, path = ws_url[len("ws://"):].partition("/")
    host, _, port_text = host_port.partition(":")
    try:
        import socket

        sock = socket.create_connection((host, int(port_text)), timeout=3)
    except OSError:
        return False
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET /{path} HTTP/1.1\r\n"
        f"Host: {host_port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    try:
        sock.sendall(request.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        if b" 101 " not in data.split(b"\r\n", 1)[0]:
            return False
        payload = json.dumps({"id": 1, "method": "Browser.close"}).encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        else:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        sock.sendall(bytes(header) + masked)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def stop_chrome_proc(proc: subprocess.Popen | None, port: int | None = None) -> None:
    """CDP용으로 띄운 Chrome 프로세스 트리를 종료합니다.

    바로 /F 로 죽이면 설정 저장이 끊겨 다음 실행마다
    '프로필을 여는 동안 문제가 발생했습니다' 대화상자가 다시 뜹니다.
    """
    if proc is not None and proc.poll() is not None and port is None:
        return
    if port:
        _cdp_browser_close(port)
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        _taskkill(proc.pid, force=False)
        try:
            proc.wait(timeout=12)
        except Exception:
            _taskkill(proc.pid, force=True)
        return
    proc.terminate()
    try:
        proc.wait(timeout=12)
    except Exception:
        proc.kill()


def _process_rows(image_name: str) -> list[dict] | None:
    """image_name 프로세스의 ProcessId, CommandLine 목록. 조회 실패 시 None."""
    if sys.platform != "win32":
        return []
    query = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{image_name}'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        rows = json.loads(completed.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def find_processes(image_name: str, markers: list[str]) -> list[int] | None:
    """image_name(예: chrome.exe) 중 명령줄에 markers 중 하나가 들어간 PID 목록.

    경로 구분자(\\, /)와 대소문자는 무시합니다. 조회에 실패하면 None 을 반환합니다(Windows 전용).
    """
    rows = _process_rows(image_name)
    if rows is None:
        return None

    def norm(text: str) -> str:
        return text.replace("\\", "/").lower()

    keys = [norm(marker).rstrip("/") for marker in markers if marker]
    return [
        int(row["ProcessId"])
        for row in rows
        if any(key in norm(row.get("CommandLine") or "") for key in keys)
    ]


def kill_process_trees(pids: list[int]) -> None:
    """PID 와 하위 프로세스를 종료합니다. 정상 종료 후 남은 프로세스만 강제 종료합니다."""
    for pid in pids:
        _taskkill(pid, force=False)
    deadline = time.monotonic() + 12
    alive = [pid for pid in pids if _pid_running(pid)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.3)
        alive = [pid for pid in alive if _pid_running(pid)]
    for pid in alive:
        _taskkill(pid, force=True)


def close_profile_chrome(profile_dir: Path) -> int | None:
    """이 프로필 Chrome 을 CDP 로 먼저 닫고, 남은 프로세스만 종료합니다.

    조회에 실패하면 None 을 반환합니다.
    """
    rows = _process_rows("chrome.exe")
    if rows is None:
        return None
    marker = str(profile_dir).replace("\\", "/").lower().rstrip("/")
    matched = []
    for row in rows:
        command = row.get("CommandLine") or ""
        text = command.replace("\\", "/").lower()
        start = 0
        found_dir = False
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            after = text[index + len(marker):index + len(marker) + 1]
            if after not in set("abcdefghijklmnopqrstuvwxyz0123456789_."):
                found_dir = True
                break
            start = index + 1
        if found_dir:
            matched.append((int(row["ProcessId"]), command))
    ports = []
    for _pid, command in matched:
        found = re.search(r"--remote-debugging-port=(\d+)", command)
        if found:
            ports.append(int(found.group(1)))
    for port in dict.fromkeys(ports):
        _cdp_browser_close(port)
    if ports:
        time.sleep(1)
    pids = [pid for pid, _command in matched if _pid_running(pid)]
    if pids:
        kill_process_trees(pids)
    return len(matched)


def start_chrome_cdp(url: str, user_data_dir: Path) -> tuple[subprocess.Popen, int]:
    """일반 Chrome을 remote-debugging으로 띄웁니다(Playwright 자동화 플래그 없음)."""
    chrome = find_chrome_executable()
    if not chrome:
        raise RuntimeError("Chrome 실행 파일을 찾지 못했습니다.")

    user_data_dir.mkdir(parents=True, exist_ok=True)
    x, y, w, h = rect = window_rect()
    prepare_profile_prefs(user_data_dir, rect)
    port = pick_free_port()
    # --disable-blink-features=AutomationControlled 는 넣지 않습니다.
    # Playwright 가 띄운 창이 아닌 일반 chrome.exe 라 navigator.webdriver 가 원래 false 이고,
    # 넣으면 "지원되지 않는 명령줄 플래그" 경고줄만 뜹니다.
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={str(user_data_dir)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        f"--window-position={x},{y}",
        f"--window-size={w},{h}",
        url or "about:blank",
    ]
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, **kwargs)
    if not wait_cdp_ready(port):
        stop_chrome_proc(proc, port)
        raise RuntimeError(
            f"Chrome CDP({port}) 연결에 실패했습니다. "
            f"프로필을 쓰는 다른 크롬이 있으면 모두 종료 후 다시 시도하세요: {user_data_dir}"
        )
    write_cdp_port(port, user_data_dir)
    return proc, port


def connect_cdp_page(playwright, port: int):
    """CDP로 붙어서 열려 있는 첫 페이지(또는 새 탭)를 반환합니다. (browser, context, page)"""
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = None
    for candidate in list(context.pages):
        try:
            if not candidate.is_closed():
                page = candidate
                break
        except Exception:
            continue
    if page is None:
        page = context.new_page()
    return browser, context, page


def cdp_port_file(user_data_dir: Path | None = None) -> Path:
    root = Path(user_data_dir) if user_data_dir else DEFAULT_USER_DATA_DIR
    return root / CDP_PORT_FILE_NAME


def write_cdp_port(port: int, user_data_dir: Path | None = None) -> Path:
    path = cdp_port_file(user_data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(port), encoding="utf-8")
    return path


def read_cdp_port(user_data_dir: Path | None = None) -> int | None:
    path = cdp_port_file(user_data_dir)
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def clear_cdp_port(user_data_dir: Path | None = None) -> None:
    path = cdp_port_file(user_data_dir)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
