#!/usr/bin/env python3
"""
AI Cleaner — нативное окно (Qt WebEngine, HTML из строки).
Запуск: python app_desktop.py
"""
import socket
import sys
import threading
import time
import traceback
from http.server import HTTPServer
from pathlib import Path

import webview

from app import Handler, find_port


def start_server(port):
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except Exception as e:
        print(f"❌ Не удалось создать сервер на порту {port}: {e}")
        traceback.print_exc()
        sys.exit(1)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"✅ Сервер запущен: http://127.0.0.1:{port}/")
    return server


def wait_for_server(port, timeout=5.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                print(f"✅ Сервер отвечает на порту {port}")
                return True
        except OSError:
            time.sleep(0.1)
    print(f"⚠️  Сервер не ответил за {timeout} сек")
    return False


def load_html_with_server(port: int) -> str:
    """Читаем index.html и подставляем адрес сервера, чтобы fetch работал."""
    path = Path(__file__).parent / "index.html"
    html = path.read_text(encoding="utf-8")
    # внедряем глобальную переменную до всех скриптов
    injection = f"<script>window.__SERVER_URL__ = 'http://127.0.0.1:{port}';</script>"
    if "<head>" in html:
        html = html.replace("<head>", "<head>" + injection, 1)
    else:
        html = injection + html
    return html


def main():
    port = find_port()
    print(f"🔍 Выбран порт: {port}")

    server = start_server(port)
    if not wait_for_server(port):
        print("❌ Сервер не запустился, выходим.")
        server.shutdown()
        return

    html = load_html_with_server(port)
    print(f"📄 HTML загружен в память ({len(html)} символов)")

    try:
        webview.create_window(
            title="AI Cleaner",
            html=html,
            width=1280,
            height=820,
            min_size=(900, 600),
            background_color="#0f1117",
            text_select=True,
        )
        webview.start()
    except Exception:
        print("❌ Ошибка при запуске окна:")
        traceback.print_exc()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()