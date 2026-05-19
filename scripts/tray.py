import subprocess, os, sys, threading, webbrowser, time, signal
import pystray
from PIL import Image

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app_proc = None

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
        app_proc.wait(5)
        app_proc = None

def open_dashboard(icon, item):
    webbrowser.open("http://localhost:5000")

def quit_app(icon, item):
    stop_gateway()
    icon.stop()
    os._exit(0)

def create_icon():
    return Image.open(os.path.join(BASE, "static", "icon.png"))

def main():
    start_gateway()
    time.sleep(2)
    icon = pystray.Icon(
        "ai_gateway", create_icon(), "AI Gateway",
        menu=pystray.Menu(
            pystray.MenuItem("打开看板", open_dashboard),
            pystray.MenuItem("退出", quit_app),
        )
    )
    icon.run()

if __name__ == "__main__":
    main()
