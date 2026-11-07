"""Desktop launcher; the analysis worker stays in the existing audio environment."""
import os
import importlib.util
# Keep the standard-library dependencies of external UI/core adapters in the EXE.
import copy, datetime, functools, hashlib, html, json, math, re, secrets
import shutil, sqlite3, subprocess, tempfile, time, uuid, webbrowser
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import sys
import threading
from pathlib import Path

def load_gui(root):
    # Load the patch next to the EXE explicitly, bypassing frozen import precedence.
    root = Path(root)
    sys.path.insert(0, str(root))
    path = root/'gui.py'
    if not path.is_file(): raise RuntimeError('Не найден gui.py рядом с TagCleaner.exe. Распакуйте патч в папку приложения.')
    spec = importlib.util.spec_from_file_location('_tagcleaner_gui', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if getattr(module, 'UI_API_VERSION', 0) < 2:
        raise RuntimeError('Устаревший gui.py. Распакуйте последний патч интерфейса с заменой файлов.')
    return module


def main():
    root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    os.chdir(root)
    if not (root/'app.py').is_file() or not (root/'ui/index.html').is_file():
        raise RuntimeError('Поместите TagCleaner.exe в папку TagCleaner с файлами ui/index.html и app.py.')
    if getattr(sys, 'frozen', False) and not (root/'.venv-audio/Scripts/python.exe').is_file():
        raise RuntimeError('Не найдено окружение .venv-audio. Запустите install_free_audio.cmd.')
    gui = load_gui(root)
    gui.restore_library(automatic=True)
    import webview
    server = gui.ThreadingHTTPServer(('127.0.0.1', 0), gui.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    window = webview.create_window('TagCleaner', 'http://127.0.0.1:'+str(server.server_port),
                                  width=1360, height=900, min_size=(860, 620), background_color='#10151f')
    def closing():
        # This callback must never wait for a Python lock or for JavaScript:
        # WebView2's UI thread is waiting for the callback to return.
        phase=gui.STATE.get('phase')
        if phase in ('scanning','finishing','stopping'):
            threading.Thread(target=request_stop,daemon=True).start()
            return False
        if phase in ('writing','review','prompt','maintenance'):
            gui.STATE['close_notice']='Сначала завершите запись или отмените ручную проверку.'
            return False
        return True
    def request_stop():
        try:gui.stop_search()
        except ValueError:pass
    window.events.closing += closing
    try:
        webview.start(gui='edgechromium')
    finally:
        server.shutdown();server.server_close()
        if gui.PROCESS and gui.PROCESS.poll() is None:
            try:gui.PROCESS.stdin.close()
            except OSError:pass

if __name__ == '__main__':
    try:main()
    except Exception as error:
        import traceback
        root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
        try:(root/'desktop_error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        except OSError:pass
        import tkinter as tk
        from tkinter import messagebox
        t=tk.Tk();t.withdraw()
        messagebox.showerror('TagCleaner — ошибка запуска',str(error)+'\n\nЕсли отсутствует WebView2 Runtime:\nhttps://developer.microsoft.com/microsoft-edge/webview2/')
        t.destroy()
