import platform
import subprocess
import time

WIFI_ADAPTER = "Wi-Fi"


def check_internet() -> bool:
    param = "-n" if platform.system().lower() == "windows" else "-c"
    try:
        result = subprocess.run(
            ["ping", param, "1", "8.8.8.8"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False


def reset_wifi(adapter: str = WIFI_ADAPTER):
    subprocess.run(
        ["netsh", "interface", "set", "interface", f"name={adapter}", "admin=disabled"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(7)
    subprocess.run(
        ["netsh", "interface", "set", "interface", f"name={adapter}", "admin=enabled"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def ensure_connected():
    if check_internet():
        return
    print("   ⚠ No internet — resetting wifi...")
    reset_wifi()
    print("   ⏳ Waiting 20s for connection to re-establish...")
    time.sleep(20)
    if check_internet():
        print("   ✅ Reconnected successfully.")
    else:
        print("   ⚠ Still no internet after reconnect attempt. Proceeding anyway.")
