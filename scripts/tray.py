import subprocess, os, sys, threading, webbrowser, time, signal, socket, json
import pystray
from PIL import Image

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app_proc = None
TASK_NAME = "AI-Gateway-AutoStart"
START_VBS = os.path.join(BASE, "start.vbs")

def _load_config():
    cfg_path = os.path.join(BASE, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_config(cfg):
    cfg_path = os.path.join(BASE, "config.json")
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _get_port():
    return _load_config().get("port", 5000)

def _task_exists():
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        return False

def _set_task(enabled):
    if enabled:
        subprocess.run(
            [
                "schtasks", "/create", "/tn", TASK_NAME,
                "/tr", f'wscript.exe "{START_VBS}"',
                "/sc", "onlogon", "/rl", "limited", "/f",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

def sync_autostart():
    cfg = _load_config()
    desired = cfg.get("autostart", False)
    exists = _task_exists()
    if desired and not exists:
        _set_task(True)
    elif not desired and exists:
        _set_task(False)

def toggle_autostart(icon, item):
    cfg = _load_config()
    current = cfg.get("autostart", False)
    cfg["autostart"] = not current
    _save_config(cfg)
    _set_task(cfg["autostart"])
    icon.update_menu()

def is_autostart_checked(item):
    return _load_config().get("autostart", False)

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
    stop_gateway()
    _wait_for_port_free(_get_port())
    start_gateway()

def quit_app(icon, item):
    stop_gateway()
    icon.stop()
    os._exit(0)

def create_icon():
    return Image.open(os.path.join(BASE, "static", "icon.png"))

def main():
    sync_autostart()
    start_gateway()
    time.sleep(2)
    icon = pystray.Icon(
        "ai_gateway", create_icon(), "AI Gateway",
        menu=pystray.Menu(
            pystray.MenuItem("打开看板", open_dashboard),
            pystray.MenuItem("重启服务", restart_gateway),
            pystray.MenuItem("开机自启动", toggle_autostart, checked=is_autostart_checked),
            pystray.MenuItem("退出", quit_app),
        )
    )
    icon.run()

if __name__ == "__main__":
    main()
