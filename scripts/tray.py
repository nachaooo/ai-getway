import subprocess, os, sys, threading, webbrowser, time, signal, socket, json, winreg
import pystray
from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app_proc = None
ICON_PNG = os.path.join(BASE, "static", "icon.png")
ICON_ICO = os.path.join(BASE, "static", "icon.ico")
LNK_NAME = "AI Gateway.lnk"
STARTUP_DIR = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
LNK_PATH = os.path.join(STARTUP_DIR, LNK_NAME)
OLD_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
OLD_REG_NAME = "AI-Gateway-AutoStart"

_PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(_PYTHONW):
    _PYTHONW = sys.executable
_TRAY_PY = os.path.join(BASE, "scripts", "tray.py")

def _load_config():
    cfg_path = os.path.join(BASE, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _get_port():
    return _load_config().get("port", 5000)

def _cleanup_old_registry():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, OLD_REG_PATH, 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, OLD_REG_NAME)
    except FileNotFoundError:
        pass

def _ensure_ico():
    if os.path.exists(ICON_ICO):
        return
    try:
        img = Image.open(ICON_PNG)
        img.save(ICON_ICO, format="ICO", sizes=[(32, 32), (64, 64), (128, 128)])
    except Exception:
        pass

def _create_lnk():
    _ensure_ico()
    ps = f'''
    $WshShell = New-Object -comObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut({json.dumps(LNK_PATH)})
    $Shortcut.TargetPath = {json.dumps(_PYTHONW)}
    $Shortcut.Arguments = {json.dumps(_TRAY_PY)}
    $Shortcut.WorkingDirectory = {json.dumps(BASE)}
    $Shortcut.IconLocation = {json.dumps(ICON_ICO)}
    $Shortcut.Save()
    '''
    subprocess.run(
        ["powershell", "-Command", ps],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

def _remove_lnk():
    if os.path.exists(LNK_PATH):
        os.remove(LNK_PATH)

def _is_autostart_enabled():
    return os.path.exists(LNK_PATH)

def _set_autostart(enabled):
    _cleanup_old_registry()
    if enabled:
        _create_lnk()
    else:
        _remove_lnk()

def toggle_autostart(icon, item):
    current = _is_autostart_enabled()
    _set_autostart(not current)
    icon.update_menu()

def is_autostart_checked(item):
    return _is_autostart_enabled()

def start_gateway():
    global app_proc
    app_proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "app.py")],
        cwd=BASE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

def stop_gateway():
    global app_proc
    if app_proc:
        app_proc.terminate()
        try:
            app_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app_proc.kill()
            app_proc.wait()
        app_proc = None

def _wait_for_port_free(port, timeout=10):
    for _ in range(int(timeout / 0.3)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            s.connect(("127.0.0.1", port))
            s.close()
            time.sleep(0.3)
        except (socket.error, socket.timeout):
            return True
    return False

def open_dashboard(icon, item):
    webbrowser.open(f"http://localhost:{_get_port()}")

def restart_gateway(icon, item):
    try:
        icon.icon = _add_badge(create_icon())
    except Exception:
        pass
    stop_gateway()
    _wait_for_port_free(_get_port())
    start_gateway()
    try:
        icon.icon = create_icon()
    except Exception:
        pass

def quit_app(icon, item):
    stop_gateway()
    icon.stop()
    os._exit(0)

def create_icon():
    return Image.open(os.path.join(BASE, "static", "icon.png"))

def _add_badge(img):
    """在原图右上角叠加一个红色圆点（重启中提示）"""
    try:
        img = img.copy().convert("RGBA")
        w, h = img.size
        r = max(4, min(w, h) // 5)
        cx, cy = w - r - 1, r + 1
        draw = ImageDraw.Draw(img)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#ef4444")
        return img
    except Exception:
        return img

class _TrayIcon(pystray.Icon):
    """左键点击直接打开看板，右键弹出菜单"""
    def __call__(self):
        action = getattr(self, '_left_click_action', None)
        if action:
            action()
        elif self._menu is not None:
            self._menu(self)
            self.update_menu()

def main():
    start_gateway()
    time.sleep(2)
    icon = _TrayIcon(
        "ai_gateway", create_icon(), "AI Gateway",
        menu=pystray.Menu(
            pystray.MenuItem("打开看板", open_dashboard),
            pystray.MenuItem("重启服务", restart_gateway),
            pystray.MenuItem("开机自启动", toggle_autostart, checked=is_autostart_checked),
            pystray.MenuItem("退出", quit_app),
        )
    )
    icon._left_click_action = lambda: open_dashboard(icon, None)
    icon.run()

if __name__ == "__main__":
    main()
