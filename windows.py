from __future__ import annotations

import ctypes
import ipaddress
import json
import logging
import logging.handlers
import os
import winreg
import psutil
import sys
import threading
import time
import webbrowser
import asyncio as _asyncio
from pathlib import Path
from typing import Dict, Optional

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    import pystray
except ImportError:
    pystray = None

try:
    import customtkinter as ctk
except ImportError:
    ctk = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

import proxy.tg_ws_proxy as tg_ws_proxy
from proxy.tg_ws_proxy import proxy_config
from proxy import __version__

from utils.default_config import default_tray_config
from ui.ctk_tray_ui import (
    install_tray_config_buttons,
    install_tray_config_form,
    populate_first_run_window,
    tray_settings_scroll_and_footer,
    validate_config_form,
)
from ui.ctk_theme import (
    CONFIG_DIALOG_FRAME_PAD,
    CONFIG_DIALOG_SIZE,
    FIRST_RUN_SIZE,
    create_ctk_toplevel,
    ctk_theme_for_platform,
    main_content_frame,
)


IS_FROZEN = bool(getattr(sys, "frozen", False))

APP_NAME = "TgWsProxy"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "proxy.log"
FIRST_RUN_MARKER = APP_DIR / ".first_run_done"
IPV6_WARN_MARKER = APP_DIR / ".ipv6_warned"


DEFAULT_CONFIG = default_tray_config()


_proxy_thread: Optional[threading.Thread] = None
_async_stop: Optional[object] = None
_tray_icon: Optional[object] = None
_config: dict = {}
_exiting: bool = False
_lock_file_path: Optional[Path] = None

_ctk_root = None
_ctk_root_ready = threading.Event()

log = logging.getLogger("tg-ws-tray")

_user32 = ctypes.windll.user32
_user32.MessageBoxW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_uint,
]
_user32.MessageBoxW.restype = ctypes.c_int


def _same_process(lock_meta: dict, proc: psutil.Process) -> bool:
    try:
        lock_ct = float(lock_meta.get("create_time", 0.0))
        proc_ct = float(proc.create_time())
        if lock_ct > 0 and abs(lock_ct - proc_ct) > 1.0:
            return False
    except Exception:
        return False

    try:
        for arg in proc.cmdline():
            if "windows.py" in arg:
                return True
    except Exception:
        pass

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return (
            os.path.basename(sys.executable).lower() == proc.name().lower()
        )

    return False


def _release_lock():
    global _lock_file_path
    if not _lock_file_path:
        return
    try:
        _lock_file_path.unlink(missing_ok=True)
    except Exception:
        pass
    _lock_file_path = None


def _acquire_lock() -> bool:
    global _lock_file_path
    _ensure_dirs()
    lock_files = list(APP_DIR.glob("*.lock"))

    for f in lock_files:
        pid = None
        meta: dict = {}

        try:
            pid = int(f.stem)
        except Exception:
            f.unlink(missing_ok=True)
            continue

        try:
            raw = f.read_text(encoding="utf-8").strip()
            if raw:
                meta = json.loads(raw)
        except Exception:
            meta = {}

        try:
            proc = psutil.Process(pid)
            if _same_process(meta, proc):
                return False
        except Exception:
            pass

        f.unlink(missing_ok=True)

    lock_file = APP_DIR / f"{os.getpid()}.lock"
    try:
        proc = psutil.Process(os.getpid())
        payload = {
            "create_time": proc.create_time(),
        }
        lock_file.write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")
    except Exception:
        lock_file.touch()

    _lock_file_path = lock_file
    return True


def _ensure_dirs():
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    _ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception as exc:
            log.warning("Failed to load config: %s", exc)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    _ensure_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def setup_logging(verbose: bool = False, log_max_mb: float = 5):
    _ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fh = logging.handlers.RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=max(32 * 1024, log_max_mb * 1024 * 1024),
        backupCount=0,
        encoding='utf-8',
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)

    if not getattr(sys, "frozen", False):
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG if verbose else logging.INFO)
        ch.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-5s  %(message)s",
            datefmt="%H:%M:%S"))
        root.addHandler(ch)


def _autostart_reg_name() -> str:
    return APP_NAME


def _supports_autostart() -> bool:
    return IS_FROZEN


def _autostart_command() -> str:
    return f'"{sys.executable}"'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as k:
            val, _ = winreg.QueryValueEx(k, _autostart_reg_name())
        stored = str(val).strip()
        expected = _autostart_command().strip()
        return stored == expected
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart_enabled(enabled: bool) -> None:
    try:
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as k:
            if enabled:
                winreg.SetValueEx(
                    k,
                    _autostart_reg_name(),
                    0,
                    winreg.REG_SZ,
                    _autostart_command(),
                )
            else:
                try:
                    winreg.DeleteValue(k, _autostart_reg_name())
                except FileNotFoundError:
                    pass
    except OSError as exc:
        log.error("Failed to update autostart: %s", exc)
        _show_error(
            "Не удалось изменить автозапуск.\n\n"
            "Попробуйте запустить приложение от имени пользователя с правами на реестр.\n\n"
            f"Ошибка: {exc}"
        )


def _make_icon_image(size: int = 64):
    if Image is None:
        raise RuntimeError("Pillow is required for tray icon")
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    margin = 2
    draw.ellipse([margin, margin, size - margin, size - margin],
                 fill=(0, 136, 204, 255))
                 
    try:
        font = ImageFont.truetype("arial.ttf", size=int(size * 0.55))
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "T", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1]
    draw.text((tx, ty), "T", fill=(255, 255, 255, 255), font=font)

    return img


def _load_icon():
    icon_path = Path(__file__).parent / "icon.ico"
    if icon_path.exists() and Image:
        try:
            return Image.open(str(icon_path))
        except Exception:
            pass
    return _make_icon_image()



def _run_proxy_thread():
    global _async_stop

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)

    stop_ev = _asyncio.Event()
    _async_stop = (loop, stop_ev)

    try:
        loop.run_until_complete(
            tg_ws_proxy._run(stop_event=stop_ev))
    except Exception as exc:
        log.error("Proxy thread crashed: %s", exc)
        if "10048" in str(exc) or "Address already in use" in str(exc):
            _show_error("Не удалось запустить прокси:\nПорт уже используется другим приложением.\n\nЗакройте приложение, использующее этот порт, или измените порт в настройках прокси и перезапустите.")
    finally:
        loop.close()
        _async_stop = None


def start_proxy():
    global _proxy_thread, _config
    if _proxy_thread and _proxy_thread.is_alive():
        log.info("Proxy already running")
        return

    cfg = _config
    port = cfg.get("port", DEFAULT_CONFIG["port"])
    host = cfg.get("host", DEFAULT_CONFIG["host"])
    secret = cfg.get("secret", DEFAULT_CONFIG["secret"])
    dc_ip_list = cfg.get("dc_ip", DEFAULT_CONFIG["dc_ip"])
    buf_kb = cfg.get("buf_kb", DEFAULT_CONFIG["buf_kb"])
    pool_size = cfg.get("pool_size", DEFAULT_CONFIG["pool_size"])

    try:
        dc_redirects = tg_ws_proxy.parse_dc_ip_list(dc_ip_list)
    except ValueError as e:
        log.error("Bad config dc_ip: %s", e)
        _show_error(f"Ошибка конфигурации:\n{e}")
        return

    proxy_config.port = port
    proxy_config.host = host
    proxy_config.secret = secret
    proxy_config.dc_redirects = dc_redirects
    proxy_config.buffer_size = max(4, buf_kb) * 1024
    proxy_config.pool_size = max(0, pool_size)

    log.info("Starting proxy on %s:%d ...", host, port)

    _proxy_thread = threading.Thread(
        target=_run_proxy_thread,
        daemon=True, name="proxy")
    _proxy_thread.start()


def stop_proxy():
    global _proxy_thread, _async_stop
    if _async_stop:
        loop, stop_ev = _async_stop
        loop.call_soon_threadsafe(stop_ev.set)
        if _proxy_thread:
            _proxy_thread.join(timeout=5)
            if _proxy_thread.is_alive():
                log.warning(
                    "Proxy thread did not finish within timeout; "
                    "the process may still exit shortly")
    _proxy_thread = None
    log.info("Proxy stopped")


def restart_proxy():
    log.info("Restarting proxy...")
    stop_proxy()
    time.sleep(0.3)
    start_proxy()


def _show_error(text: str, title: str = "TG WS Proxy — Ошибка"):
    _user32.MessageBoxW(None, text, title, 0x10)


def _show_info(text: str, title: str = "TG WS Proxy"):
    _user32.MessageBoxW(None, text, title, 0x40)


def _ask_open_release_page(latest_version: str, url: str) -> bool:
    """Win32 Yes/No: открыть страницу релиза."""
    MB_YESNO = 0x4
    MB_ICONQUESTION = 0x20
    IDYES = 6
    text = (
        f"Доступна новая версия: {latest_version}\n\n"
        f"Открыть страницу релиза в браузере?"
    )
    r = _user32.MessageBoxW(
        None,
        text,
        "TG WS Proxy — обновление",
        MB_YESNO | MB_ICONQUESTION,
    )
    return r == IDYES


def _maybe_notify_update_async():
    """
    Фоновая проверка GitHub Releases и уведомление (не блокирует трей).
    """
    def _work():
        time.sleep(1.5)
        if _exiting:
            return
        if not _config.get("check_updates", True):
            return
        try:
            from utils.update_check import RELEASES_PAGE_URL, get_status, run_check
            run_check(__version__)
            st = get_status()
            if not st.get("has_update"):
                return
            url = (st.get("html_url") or "").strip() or RELEASES_PAGE_URL
            ver = st.get("latest") or "?"
            if _ask_open_release_page(str(ver), url):
                webbrowser.open(url)
        except Exception as exc:
            log.debug("Update check failed: %s", exc)

    threading.Thread(target=_work, daemon=True, name="update-check").start()


def _ensure_ctk_thread() -> bool:
    """Start the persistent hidden CTk root in its own thread (once)."""
    global _ctk_root
    if ctk is None:
        return False
    if _ctk_root_ready.is_set():
        return True

    def _run():
        global _ctk_root
        from ui.ctk_theme import (
            apply_ctk_appearance,
            _install_tkinter_variable_del_guard,
        )
        _install_tkinter_variable_del_guard()
        apply_ctk_appearance(ctk)
        _ctk_root = ctk.CTk()
        _ctk_root.withdraw()
        _ctk_root_ready.set()
        _ctk_root.mainloop()

    threading.Thread(target=_run, daemon=True, name="ctk-root").start()
    _ctk_root_ready.wait(timeout=5.0)
    return _ctk_root is not None


def _ctk_run_dialog(build_fn) -> None:
    """Schedule build_fn(done_event) on the CTk thread and block until done_event is set."""
    if _ctk_root is None:
        return
    done = threading.Event()

    def _invoke():
        try:
            build_fn(done)
        except Exception:
            log.exception("CTk dialog failed")
            done.set()

    _ctk_root.after(0, _invoke)
    done.wait()


def _on_open_in_telegram(icon=None, item=None):
    host = _config.get("host", DEFAULT_CONFIG["host"])
    port = _config.get("port", DEFAULT_CONFIG["port"])
    secret = _config.get("secret", DEFAULT_CONFIG["secret"])
    
    url = f"tg://proxy?server={host}&port={port}&secret=dd{secret}"
    log.info("Opening %s", url)
    try:
        result = webbrowser.open(url)
        if not result:
            raise RuntimeError("webbrowser.open returned False")
    except Exception:
        log.info("Browser open failed, copying to clipboard")
        if pyperclip is None:
            _show_error(
                "Не удалось открыть Telegram автоматически.\n\n"
                f"Установите пакет pyperclip для копирования в буфер или откройте вручную:\n{url}")
            return
        try:
            pyperclip.copy(url)
            _show_info(
                f"Не удалось открыть Telegram автоматически.\n\n"
                f"Ссылка скопирована в буфер обмена, отправьте её в Telegram и нажмите по ней ЛКМ:\n{url}",
                "TG WS Proxy")
        except Exception as exc:
            log.error("Clipboard copy failed: %s", exc)
            _show_error(f"Не удалось скопировать ссылку:\n{exc}")


def _on_restart(icon=None, item=None):
    threading.Thread(target=restart_proxy, daemon=True).start()


def _on_edit_config(icon=None, item=None):
    threading.Thread(target=_edit_config_dialog, daemon=True).start()


def _edit_config_dialog():
    if not _ensure_ctk_thread():
        _show_error("customtkinter не установлен.")
        return

    cfg = dict(_config)
    cfg["autostart"] = is_autostart_enabled()
    if _supports_autostart() and not cfg["autostart"]:
        set_autostart_enabled(False)

    def _build(done: threading.Event):
        theme = ctk_theme_for_platform()
        w, h = CONFIG_DIALOG_SIZE
        if _supports_autostart():
            h += 100

        icon_path = str(Path(__file__).parent / "icon.ico")
        root = create_ctk_toplevel(
            ctk,
            title="TG WS Proxy — Настройки",
            width=w,
            height=h,
            theme=theme,
            after_create=lambda r: r.iconbitmap(icon_path),
        )

        fpx, fpy = CONFIG_DIALOG_FRAME_PAD
        frame = main_content_frame(ctk, root, theme, padx=fpx, pady=fpy)
        scroll, footer = tray_settings_scroll_and_footer(ctk, frame, theme)
        widgets = install_tray_config_form(
            ctk,
            scroll,
            theme,
            cfg,
            DEFAULT_CONFIG,
            show_autostart=_supports_autostart(),
            autostart_value=cfg.get("autostart", False),
        )

        def _finish():
            root.destroy()
            done.set()

        def on_save():
            merged = validate_config_form(
                widgets,
                DEFAULT_CONFIG,
                include_autostart=_supports_autostart(),
            )
            if isinstance(merged, str):
                _show_error(merged)
                return

            new_cfg = merged
            save_config(new_cfg)
            _config.update(new_cfg)
            log.info("Config saved: %s", new_cfg)
            if _supports_autostart():
                set_autostart_enabled(bool(new_cfg.get("autostart", False)))
            _tray_icon.menu = _build_menu()

            from tkinter import messagebox
            do_restart = messagebox.askyesno(
                "Перезапустить?",
                "Настройки сохранены.\n\nПерезапустить прокси сейчас?",
                parent=root)
            _finish()
            if do_restart:
                threading.Thread(
                    target=restart_proxy, daemon=True).start()

        def on_cancel():
            _finish()

        root.protocol("WM_DELETE_WINDOW", on_cancel)
        install_tray_config_buttons(
            ctk, footer, theme, on_save=on_save, on_cancel=on_cancel)

    _ctk_run_dialog(_build)


def _on_open_logs(icon=None, item=None):
    log.info("Opening log file: %s", LOG_FILE)
    if LOG_FILE.exists():
        os.startfile(str(LOG_FILE))
    else:
        _show_info("Файл логов ещё не создан.", "TG WS Proxy")


def _on_exit(icon=None, item=None):
    global _exiting
    if _exiting:
        os._exit(0)
        return
    _exiting = True
    log.info("User requested exit")

    if _ctk_root is not None:
        try:
            _ctk_root.after(0, _ctk_root.quit)
        except Exception:
            pass

    def _force_exit():
        time.sleep(3)
        os._exit(0)
    threading.Thread(target=_force_exit, daemon=True, name="force-exit").start()

    if icon:
        icon.stop()


def _show_first_run():
    _ensure_dirs()
    if FIRST_RUN_MARKER.exists():
        return
    if not _ensure_ctk_thread():
        FIRST_RUN_MARKER.touch()
        return

    host = _config.get("host", DEFAULT_CONFIG["host"])
    port = _config.get("port", DEFAULT_CONFIG["port"])
    secret = _config.get("secret", DEFAULT_CONFIG["secret"])

    def _build(done: threading.Event):
        theme = ctk_theme_for_platform()
        icon_path = str(Path(__file__).parent / "icon.ico")
        w, h = FIRST_RUN_SIZE
        root = create_ctk_toplevel(
            ctk,
            title="TG WS Proxy",
            width=w,
            height=h,
            theme=theme,
            after_create=lambda r: r.iconbitmap(icon_path),
        )

        def on_done(open_tg: bool):
            FIRST_RUN_MARKER.touch()
            root.destroy()
            done.set()
            if open_tg:
                _on_open_in_telegram()

        populate_first_run_window(
            ctk, root, theme, host=host, port=port, secret=secret,
            on_done=on_done)

    _ctk_run_dialog(_build)


def _has_ipv6_enabled() -> bool:
    import socket as _sock
    try:
        addrs = _sock.getaddrinfo(_sock.gethostname(), None, _sock.AF_INET6)
        for addr in addrs:
            ip = addr[4][0]
            if not ip or ip.startswith("::1"):
                continue
            try:
                if ipaddress.IPv6Address(ip).is_link_local:
                    continue
            except ValueError:
                if ip.startswith("fe80:"):
                    continue
            return True
    except Exception:
        pass
    try:
        s = _sock.socket(_sock.AF_INET6, _sock.SOCK_STREAM)
        s.bind(('::1', 0))
        s.close()
        return True
    except Exception:
        return False


def _check_ipv6_warning():
    _ensure_dirs()
    if IPV6_WARN_MARKER.exists():
        return
    if not _has_ipv6_enabled():
        return

    IPV6_WARN_MARKER.touch()

    threading.Thread(target=_show_ipv6_dialog, daemon=True).start()


def _show_ipv6_dialog():
    _show_info(
        "На вашем компьютере включена поддержка подключения по IPv6.\n\n"
        "Telegram может пытаться подключаться через IPv6, "
        "что не поддерживается и может привести к ошибкам.\n\n"
        "Если прокси не работает или в логах присутствуют ошибки, "
        "связанные с попытками подключения по IPv6 - "
        "попробуйте отключить в настройках прокси Telegram попытку соединения "
        "по IPv6. Если данная мера не помогает, попробуйте отключить IPv6 "
        "в системе.\n\n"
        "Это предупреждение будет показано только один раз.",
        "TG WS Proxy")


def _build_menu():
    if pystray is None:
        return None
    host = _config.get("host", DEFAULT_CONFIG["host"])
    port = _config.get("port", DEFAULT_CONFIG["port"])
    link_host = tg_ws_proxy.get_link_host(host)

    return pystray.Menu(
        pystray.MenuItem(
            f"Открыть в Telegram ({link_host}:{port})",
            _on_open_in_telegram,
            default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Перезапустить прокси", _on_restart),
        pystray.MenuItem("Настройки...", _on_edit_config),
        pystray.MenuItem("Открыть логи", _on_open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", _on_exit),
    )


def run_tray():
    global _tray_icon, _config

    _config = load_config()
    save_config(_config)

    if LOG_FILE.exists():
        try:
            LOG_FILE.unlink()
        except Exception:
            pass

    setup_logging(_config.get("verbose", False),
                  log_max_mb=_config.get("log_max_mb", DEFAULT_CONFIG["log_max_mb"]))
    log.info("TG WS Proxy версия %s, tray app starting", __version__)
    log.info("Config: %s", _config)
    log.info("Log file: %s", LOG_FILE)

    if pystray is None or Image is None or ctk is None:
        log.error(
            "pystray, Pillow or customtkinter not installed; "
            "running in console mode")
        start_proxy()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_proxy()
        return

    start_proxy()

    _maybe_notify_update_async()

    _show_first_run()
    _check_ipv6_warning()

    icon_image = _load_icon()
    _tray_icon = pystray.Icon(
        APP_NAME,
        icon_image,
        "TG WS Proxy",
        menu=_build_menu())

    log.info("Tray icon running")
    _tray_icon.run()

    stop_proxy()
    log.info("Tray app exited")


def main():
    if not _acquire_lock():
        _show_info("Приложение уже запущено.", os.path.basename(sys.argv[0]))
        return

    try:
        run_tray()
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
