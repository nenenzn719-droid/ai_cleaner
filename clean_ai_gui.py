#!/usr/bin/env python3
"""
clean_ai_gui.py — Красивое окно для очистки текста от ИИ-маркеров.
Запуск:  python clean_ai_gui.py
"""

import re
import unicodedata
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

try:
    import dewatermark
    HAS_DEWATERMARK = True
except ImportError:
    HAS_DEWATERMARK = False


# ---------- Логика очистки ----------

INVISIBLE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad\u034f\u061c"
    "\u180e\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
    "\U000e0000-\U000e007f]"
)

LOOKALIKES = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u2026": "...",
}
LOOKUP = str.maketrans(LOOKALIKES)


def analyze(text: str) -> dict:
    """Возвращает отчёт о найденных маркерах."""
    invisible = INVISIBLE.findall(text)
    smart = [c for c in text if c in LOOKALIKES]
    return {
        "invisible": len(invisible),
        "typography": len(smart),
    }


def clean(text: str) -> tuple[str, dict]:
    """Чистит текст и возвращает (результат, отчёт)."""
    report = analyze(text)

    # 1. dewatermark, если есть
    if HAS_DEWATERMARK:
        try:
            text = dewatermark.sanitize(text)
        except Exception:
            pass

    # 2. Убираем невидимку
    text = INVISIBLE.sub("", text)

    # 3. NFKC + двойники
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(LOOKUP)

    # 4. Нормализуем пробелы
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)

    report["removed_chars"] = len(text)
    return text, report


# ---------- GUI ----------

class CleanerApp:
    BG = "#f5f6fa"
    CARD = "#ffffff"
    ACCENT = "#4f46e5"
    ACCENT_HOVER = "#4338ca"
    TEXT = "#111827"
    MUTED = "#6b7280"
    BORDER = "#e5e7eb"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AI Cleaner — очистка ИИ-маркеров")
        self.root.geometry("1100x700")
        self.root.minsize(900, 600)
        self.root.configure(bg=self.BG)

        self.original_text = ""
        self.cleaned_text = ""
        self.current_path: Path | None = None

        self._setup_style()
        self._build_ui()

    # ----- стили -----
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Card.TFrame", background=self.CARD)
        style.configure("Bg.TFrame", background=self.BG)

        style.configure("Title.TLabel",
                        background=self.BG, foreground=self.TEXT,
                        font=("SF Pro Display", 20, "bold"))
        style.configure("Subtitle.TLabel",
                        background=self.BG, foreground=self.MUTED,
                        font=("SF Pro Text", 11))
        style.configure("CardTitle.TLabel",
                        background=self.CARD, foreground=self.MUTED,
                        font=("SF Pro Text", 10, "bold"))
        style.configure("Status.TLabel",
                        background=self.BG, foreground=self.MUTED,
                        font=("SF Pro Text", 11))

        # кнопки
        style.configure("Accent.TButton",
                        background=self.ACCENT, foreground="white",
                        borderwidth=0, focusthickness=0,
                        font=("SF Pro Text", 12, "bold"), padding=(18, 10))
        style.map("Accent.TButton",
                  background=[("active", self.ACCENT_HOVER),
                              ("disabled", "#c7c9d1")])

        style.configure("Ghost.TButton",
                        background=self.CARD, foreground=self.TEXT,
                        borderwidth=1, relief="solid",
                        font=("SF Pro Text", 11), padding=(14, 8))
        style.map("Ghost.TButton",
                  background=[("active", "#f3f4f6")])

    # ----- разметка -----
    def _build_ui(self):
        outer = ttk.Frame(self.root, style="Bg.TFrame", padding=(24, 20, 24, 20))
        outer.pack(fill="both", expand=True)

        # Заголовок
        header = ttk.Frame(outer, style="Bg.TFrame")
        header.pack(fill="x", pady=(0, 16))

        ttk.Label(header, text="AI Cleaner", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Убирает невидимые Unicode-маркеры, «умную» типографику и гомоглифы",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # Панель инструментов
        toolbar = ttk.Frame(outer, style="Bg.TFrame")
        toolbar.pack(fill="x", pady=(0, 14))

        self.btn_open = ttk.Button(toolbar, text="📂  Открыть файл",
                                   style="Ghost.TButton", command=self.open_file)
        self.btn_open.pack(side="left")

        self.btn_clean = ttk.Button(toolbar, text="✨  Очистить",
                                    style="Accent.TButton", command=self.run_clean)
        self.btn_clean.pack(side="left", padx=(10, 0))

        self.btn_save = ttk.Button(toolbar, text="💾  Сохранить как…",
                                   style="Ghost.TButton",
                                   command=self.save_file, state="disabled")
        self.btn_save.pack(side="left", padx=(10, 0))

        self.btn_copy = ttk.Button(toolbar, text="📋  Копировать результат",
                                   style="Ghost.TButton",
                                   command=self.copy_result, state="disabled")
        self.btn_copy.pack(side="left", padx=(10, 0))

        # Две колонки
        cols = ttk.Frame(outer, style="Bg.TFrame")
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1, uniform="col")
        cols.columnconfigure(1, weight=1, uniform="col")
        cols.rowconfigure(0, weight=1)

        self.left_card, self.txt_before = self._make_card(
            cols, "ИСХОДНЫЙ ТЕКСТ", col=0)
        self.right_card, self.txt_after = self._make_card(
            cols, "РЕЗУЛЬТАТ", col=1, padx=(12, 0))

        # Статус
        self.status = tk.StringVar(value="Открой текстовый файл, чтобы начать.")
        ttk.Label(outer, textvariable=self.status,
                  style="Status.TLabel").pack(anchor="w", pady=(14, 0))

    def _make_card(self, parent, title, col, padx=(0, 0)):
        card = tk.Frame(parent, bg=self.CARD,
                        highlightbackground=self.BORDER,
                        highlightthickness=1)
        card.grid(row=0, column=col, sticky="nsew", padx=padx)

        head = tk.Frame(card, bg=self.CARD)
        head.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(anchor="w")

        txt = tk.Text(
            card, wrap="word", undo=True, borderwidth=0,
            bg=self.CARD, fg=self.TEXT, insertbackground=self.TEXT,
            font=("SF Mono", 12), padx=14, pady=6, relief="flat",
        )
        txt.pack(fill="both", expand=True, padx=(0, 0), pady=(0, 12))

        scroll = ttk.Scrollbar(card, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        scroll.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        return card, txt

    # ----- действия -----
    def open_file(self):
        path = filedialog.askopenfilename(
            title="Выбери текстовый файл",
            filetypes=[
                ("Текстовые файлы",
                 "*.txt *.md *.csv *.json *.html *.xml *.yaml *.yml *.log *.srt"),
                ("Все файлы", "*.*"),
            ],
        )
        if not path:
            return
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            messagebox.showerror("Ошибка чтения", str(e))
            return

        self.current_path = p
        self.original_text = text

        self.txt_before.delete("1.0", "end")
        self.txt_before.insert("1.0", text)

        self.txt_after.delete("1.0", "end")
        self.cleaned_text = ""
        self.btn_save.config(state="disabled")
        self.btn_copy.config(state="disabled")

        report = analyze(text)
        self.status.set(
            f"Загружено: {p.name}  •  {len(text)} символов  •  "
            f"невидимых: {report['invisible']}  •  типографика: {report['typography']}"
        )

    def run_clean(self):
        if not self.original_text:
            messagebox.showinfo("Нет текста", "Сначала открой файл.")
            return

        cleaned, report = clean(self.original_text)
        self.cleaned_text = cleaned

        self.txt_after.delete("1.0", "end")
        self.txt_after.insert("1.0", cleaned)

        self.btn_save.config(state="normal")
        self.btn_copy.config(state="normal")

        delta = len(self.original_text) - len(cleaned)
        self.status.set(
            f"✅ Готово  •  удалено символов: {delta}  •  "
            f"невидимых: {report['invisible']}  •  типографика: {report['typography']}  •  "
            f"итог: {len(cleaned)} символов"
        )

    def save_file(self):
        if not self.cleaned_text:
            return
        default = "clean.txt"
        if self.current_path:
            default = f"{self.current_path.stem}.clean{self.current_path.suffix}"
        path = filedialog.asksaveasfilename(
            title="Сохранить результат",
            initialfile=default,
            defaultextension=".txt",
        )
        if not path:
            return
        try:
            Path(path).write_text(self.cleaned_text, encoding="utf-8")
            self.status.set(f"💾 Сохранено: {path}")
        except Exception as e:
            messagebox.showerror("Ошибка сохранения", str(e))

    def copy_result(self):
        if not self.cleaned_text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.cleaned_text)
        self.status.set("📋 Результат скопирован в буфер обмена")


def main():
    root = tk.Tk()
    app = CleanerApp(root)
    if not HAS_DEWATERMARK:
        app.status.set("⚠️  dewatermark не установлен — работает встроенная очистка.")
    root.mainloop()


if __name__ == "__main__":
    main()
