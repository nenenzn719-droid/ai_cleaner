#!/usr/bin/env python3
"""
AI Cleaner — лаунчер. Запускает сервер и открывает браузер.
Используется как точка входа для .app.
"""
import socket
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer

from app import Handler, find_port


def wait_for_server(port, timeout=5.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    port = find_port()
    url = f"http://127.0.0.1:{port}/"
    print(f"✨ AI Cleaner запущен на {url}")
    print("Закрой это окно, чтобы остановить сервер.")

    server = HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    if not wait_for_server(port):
        print("❌ Сервер не поднялся.")
        sys.exit(1)

    # небольшая пауза и открытие браузера
    time.sleep(0.3)
    webbrowser.open(url)

    try:
        # держим процесс живым, пока пользователь не закроет
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nОстанавливаюсь.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
