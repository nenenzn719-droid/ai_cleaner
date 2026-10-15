import os
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView

html = """
<html><body style='background:#222;color:#0f0;font-size:60px;
font-family:-apple-system;display:flex;align-items:center;justify-content:center;height:100vh'>
HELLO FROM HTML
</body></html>
"""

app = QApplication(sys.argv)
view = QWebEngineView()
view.setHtml(html)
view.resize(700, 400)
view.show()
sys.exit(app.exec())
