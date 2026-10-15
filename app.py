#!/usr/bin/env python3
"""
AI Cleaner — локальное веб-приложение для очистки текста от ИИ-маркеров.
Поддержка: txt, md, json, csv, html, pdf.
Запуск:  python app.py
"""
import base64
import io
import json
import math
import re
import zipfile
import socket
import threading
import traceback
import unicodedata
import webbrowser
import platform
import shutil
import subprocess
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import difflib
import urllib.error
import html as _html

# --- опциональные библиотеки ---
from pypdf import PdfReader
HAS_PYPDF = True

from fpdf import FPDF
HAS_FPDF = True

import fitz  # PyMuPDF
HAS_FITZ = True

from docx import Document
HAS_DOCX = True

import ollama
HAS_OLLAMA = True

OLLAMA_MODEL = "qwen2.5:3b"

# =============================================================
#  ЛОГИКА ОЧИСТКИ ТЕКСТА
# =============================================================

INVISIBLE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad\u034f\u061c"
    "\u180e\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
    "\U000e0000-\U000e007f]"
)

TYPOGRAPHY = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u2026": "...",
}
TYPO_MAP = str.maketrans(TYPOGRAPHY)

LOOKALIKES = {
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
}
LOOKALIKE_MAP = str.maketrans(LOOKALIKES)

# Вариационные селекторы и эмодзи-модификаторы
VARIATION_SELECTORS = re.compile("[\ufe00-\ufe0f]")
EMOJI_MODIFIERS = re.compile("[\U0001F3FB-\U0001F3FF]")
# Управляющие символы C0/C1 (кроме \n, \t)
CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean_safe_aggressive(text: str) -> str:
    """Безопасная агрессивная очистка.
    Чистит контрольные символы, схлопывает пробелы и знаки,
    НЕ трогает русские буквы."""
    if not text:
        return text
    t = text

    # 1. Вариационные селекторы и эмодзи-модификаторы
    t = VARIATION_SELECTORS.sub("", t)
    t = EMOJI_MODIFIERS.sub("", t)

    # 2. Управляющие символы (кроме \n, \t)
    t = CONTROL_CHARS.sub("", t)

    # 3. Множественные пробелы и табы → один
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)

    # 4. Многоточия больше 3 → три
    t = re.sub(r"\.{4,}", "...", t)

    # 5. Множественные знаки → один
    t = re.sub(r"!{2,}", "!", t)
    t = re.sub(r"\?{2,}", "?", t)
    t = re.sub(r"([!?])\s*[!?]+", r"\1", t)

    # 6. Множественные переводы строк → максимум 2
    t = re.sub(r"\n{3,}", "\n\n", t)

    # 7. Обрезка пробелов в строках
    t = "\n".join(line.strip() for line in t.split("\n"))
    t = t.strip()

    return t

def analyze(text: str) -> dict:
    return {
        "invisible": len(INVISIBLE.findall(text)),
        "typography": sum(1 for c in text if c in TYPOGRAPHY),
        "length": len(text),
    }


def clean_text(text: str, aggressive: bool = False, safe_aggressive: bool = False) -> dict:
    before = analyze(text)
    t = INVISIBLE.sub("", text)
    t = unicodedata.normalize("NFKC", t)
    t = t.translate(TYPO_MAP)
    if aggressive:
        t = t.translate(LOOKALIKE_MAP)
    if safe_aggressive:
        t = clean_safe_aggressive(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = t.strip()
    after = analyze(t)
    return {
        "cleaned": t,
        "report": {
            "invisible_removed": before["invisible"] - after["invisible"],
            "typography_removed": before["typography"] - after["typography"],
            "chars_before": before["length"],
            "chars_after": after["length"],
            "chars_saved": before["length"] - after["length"],
            "aggressive": aggressive,
        },
    }

# =============================================================
#  ПЕРЕФРАЗ ШТАМПОВ (замена клише на живые обороты)
# =============================================================

# Профили стиля для перефраза
STYLE_PRESETS = {
    "neutral": {
        "name": "Нейтральный",
        "hint": "Перепиши текст другими словами, сохранив смысл.",
        "temperature": 0.7,
        "extra_rules": [],
    },
    "academic": {
        "name": "Научный",
        "hint": "Перепиши в научном стиле. Сохрани все термины и факты. "
                "Убери канцелярит, но оставь точность формулировок.",
        "temperature": 0.4,
        "extra_rules": [
            # в научном тексте не упрощаем термины
        ],
    },
    "business": {
        "name": "Деловой",
        "hint": "Перепиши в деловом стиле. Формально, но без канцелярита. "
                "Коротко и по делу.",
        "temperature": 0.5,
        "extra_rules": [],
    },
    "casual": {
        "name": "Живой",
        "hint": "Перепиши живым, естественным языком, как будто рассказываешь "
                "другу. Убери все штампы. Можно использовать разговорные обороты.",
        "temperature": 0.9,
        "extra_rules": [],
    },
    "concise": {
        "name": "Сжатый",
        "hint": "Сократи текст на 30–40%, сохранив все факты и смысл. "
                "Убери воду, повторы, вводные слова.",
        "temperature": 0.5,
        "extra_rules": [],
    },
}

STAMP_RULES = [
    # === РАСШИРЕННЫЕ ЗАМЕНЫ ===
    # канцелярит → живое
    (r"\bв\s+целях\s+обеспечения\b", "чтобы"),
    (r"\bс\s+целью\s+обеспечения\b", "чтобы"),
    (r"\bв\s+качестве\s+примера\b", "например"),
    (r"\bв\s+частности,?\s+", "например, "),
    (r"\bпри\s+этом\b", "причём"),
    (r"\bв\s+связи\s+с\s+этим\b", "поэтому"),
    (r"\bпо\s+этой\s+причине\b", "поэтому"),
    (r"\bвследствие\s+этого\b", "из-за этого"),
    (r"\bв\s+силу\s+этого\b", "поэтому"),
    (r"\bкак\s+следствие\b", "в итоге"),
    (r"\bв\s+первую\s+очередь\b", "прежде всего"),
    (r"\bв\s+то\s+же\s+время\b", "одновременно"),
    (r"\bна\s+протяжении\s+длительного\s+времени\b", "долго"),
    (r"\bв\s+кратчайшие\s+сроки\b", "быстро"),
    (r"\bв\s+короткие\s+сроки\b", "быстро"),
    (r"\bв\s+полной\s+мере\b", "полностью"),
    (r"\bв\s+значительной\s+степени\b", "во многом"),
    (r"\bв\s+определённой\s+степени\b", "частично"),
    (r"\bв\s+достаточной\s+степени\b", "достаточно"),
    (r"\bдостаточно\s+часто\b", "часто"),
    (r"\bзачастую\b", "часто"),
    (r"\bнередко\b", "часто"),
    (r"\bв\s+большинстве\s+случаев\b", "обычно"),
    (r"\bв\s+ряде\s+случаев\b", "иногда"),
    (r"\bв\s+некоторых\s+случаях\b", "иногда"),
    (r"\bв\s+данный\s+момент\b", "сейчас"),
    (r"\bдо\s+настоящего\s+времени\b", "до сих пор"),
    (r"\bпо\s+настоящее\s+время\b", "до сих пор"),
    (r"\bна\s+данном\s+этапе\b", "сейчас"),
    (r"\bв\s+перспективе\b", "в будущем"),
    (r"\bв\s+дальнейшем\b", "потом"),
    (r"\bв\s+свою\s+очередь,?\s+", ""),
    (r"\bкроме\s+того,?\s+", "и "),

    # «пустые» усилители
    (r"\bпоистине\b", ""),
    (r"\bпоистине\s+уникальн", "уникальн"),
    (r"\bисключительно\s+важн", "важн"),
    (r"\bкрайне\s+важн", "важн"),
    (r"\bчрезвычайно\s+важн", "важн"),
    (r"\bвесьма\s+существенн", "существенн"),
    (r"\bабсолютно\s+очевидн", "очевидн"),
    (r"\bв\s+высшей\s+степени\b", "очень"),
    (r"\bсамым\s+тесным\s+образом\b", "тесно"),
    (r"\bсамым\s+непосредственным\s+образом\b", "напрямую"),
    (r"\bдолжным\s+образом\b", "как надо"),
    (r"\bнадлежащим\s+образом\b", "как надо"),
    (r"\bсоответствующим\s+образом\b", "как надо"),

    # деепричастия и причастия → проще
    (r"\bявляясь\b", "будучи"),
    (r"\bпредставляя\s+собой\b", "как"),
    (r"\bосуществляя\b", "делая"),
    (r"\bпроизводя\b", "делая"),
    (r"\bсовершая\b", "делая"),
    (r"\bиспользуя\b", "через"),
    (r"\bприменяя\b", "через"),
    (r"\bучитывая\b", "с учётом"),

    # длинные конструкции → короче
    (r"\bв\s+том\s+случае,?\s+если\b", "если"),
    (r"\bв\s+том\s+случае,?\s+когда\b", "когда"),
    (r"\bпо\s+той\s+причине,?\s+что\b", "потому что"),
    (r"\bпо\s+причине\s+того,?\s+что\b", "потому что"),
    (r"\bнесмотря\s+на\s+то,?\s+что\b", "хотя"),
    (r"\bнезависимо\s+от\s+того,?\s+что\b", "хотя"),
    (r"\bвне\s+зависимости\s+от\s+того,?\s+что\b", "хотя"),
    (r"\bвместо\s+того,?\s+чтобы\b", "вместо"),

    # фразовые «пустышки»
    (r"\bследует\s+иметь\s+в\s+виду\b", "учтите"),
    (r"\bнеобходимо\s+иметь\s+в\s+виду\b", "учтите"),
    (r"\bстоит\s+иметь\s+в\s+виду\b", "учтите"),
    (r"\bважно\s+иметь\s+в\s+виду\b", "учтите"),
    (r"\bнужно\s+иметь\s+в\s+виду\b", "учтите"),

    # «рамочные» обороты
    (r"\bв\s+рамках\s+данной\s+работы\b", "здесь"),
    (r"\bв\s+рамках\s+данного\s+исследования\b", "здесь"),
    (r"\bв\s+рамках\s+статьи\b", "здесь"),
    (r"\bв\s+данной\s+статье\b", "здесь"),
    (r"\bв\s+данной\s+работе\b", "здесь"),
    (r"\bв\s+заключение\s+хотелось\s+бы\s+отметить\b", "в итоге"),
    (r"\bв\s+заключение\s+стоит\s+отметить\b", "в итоге"),

    # двойные отрицания
    (r"\bне\s+может\s+не\s+отметить\b", "отмечу"),
    (r"\bнельзя\s+не\s+отметить\b", "стоит отметить"),
    (r"\bнельзя\s+не\s+сказать\b", "скажу"),
]

import random

# Вариативные синонимы: ключ → список вариантов.
# Заменяем одну и ту же фразу разными способами — текст становится живее.
VARIATIVE_REPLACEMENTS = {
    r"\bоднако\b": ["но", "впрочем", "и всё же", "хотя"],
    r"\bтаким\s+образом\b": ["значит", "выходит", "получается", "в итоге"],
    r"\bболее\s+того\b": ["к тому же", "и ещё", "вдобавок"],
    r"\bкроме\s+того\b": ["и ещё", "вдобавок", "плюс к этому"],
    r"\bнапример\b": ["скажем", "к примеру", "для примера"],
    r"\bв\s+современном\s+мире\b": ["сегодня", "в наши дни", "сейчас"],
    r"\bна\s+сегодняшний\s+день\b": ["сейчас", "сегодня", "в наши дни"],
    r"\bв\s+настоящее\s+время\b": ["сейчас", "теперь"],
    r"\bданный\b": ["этот", "такой", "тот"],
    r"\bданная\b": ["эта", "такая"],
    r"\bданное\b": ["это", "такое"],
    r"\bважно\s+отметить,?\s*что\b": ["замечу:", "стоит сказать:", "нужно понять:"],
    r"\bпредставляет\s+собой\b": ["это", "является", "по сути —"],
    r"\bв\s+связи\s+с\s+этим\b": ["поэтому", "из-за этого", "в итоге"],
    r"\bв\s+заключение\b": ["в конце", "под конец", "напоследок"],
    r"\bв\s+целом\b": ["в общем", "по большому счёту", "в основном"],
    r"\bкак\s+правило\b": ["обычно", "чаще всего", "в большинстве случаев"],
    r"\bв\s+частности\b": ["например", "в том числе"],
    r"\bименно\s+поэтому\b": ["поэтому", "вот почему"],
    r"\bглавный\s+урок\b": ["урок", "вывод"],
    r"\bглавный\s+вывод\b": ["вывод", "главное"],
    r"\bв\s+конечном\s+счёте\b": ["в итоге", "в конце концов", "в финале"],
}


def _variative_paraphrase(text: str, intensity: int = 5) -> tuple:
    """Замена синонимов случайным образом. Возвращает (текст, число_замен)."""
    total = 0
    for pattern, options in VARIATIVE_REPLACEMENTS.items():
        def repl(m):
            nonlocal total
            total += 1
            choice = random.choice(options)
            # сохраняем регистр
            if m.group(0) and m.group(0)[0].isupper():
                choice = choice[0].upper() + choice[1:]
            return choice

        try:
            text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
        except re.error:
            continue
    return text, total


def _apply_case(source: str, replacement: str) -> str:
    """Сохраняет регистр первого символа и заглавные внутри."""
    if not replacement:
        return replacement
    if source and source[0].isupper() and replacement[0].islower():
        return replacement[0].upper() + replacement[1:]
    # если источник был ВСЕМИ ЗАГЛАВНЫМИ
    if source and len(source) > 2 and source.isupper():
        return replacement.upper()
    return replacement


def _smart_replace(text: str, pattern: str, replacement: str) -> tuple:
    """Умная замена с сохранением регистра, обработкой падежей.
    Возвращает (новый_текст, количество_замен)."""
    applied = 0

    def repl(m):
        nonlocal applied
        applied += 1
        try:
            result = m.expand(replacement)
        except Exception:
            result = replacement
        # сохраняем регистр первой буквы
        return _apply_case(m.group(0), result)

    try:
        new_text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    except re.error:
        return text, 0
    return new_text, applied

def _split_long_sentences(text: str, max_words: int = 28) -> str:
    """Разбивает слишком длинные предложения на два.
    Ищем логичные точки разрыва: перед союзами 'а', 'но', 'и', 'однако'."""
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    out = []

    for s in sentences:
        words = s.split()
        if len(words) <= max_words:
            out.append(s)
            continue

        # ищем места для разрыва
        # приоритет: длинное тире, точка с запятой, союзы "а", "но", "однако"
        markers = [r"\s+—\s+", r";\s+", r",\s+(?=но\s)", r",\s+(?=а\s)",
                   r",\s+(?=однако\s)", r",\s+(?=и\s+[а-яё]{4,})"]

        best_split = None
        for marker in markers:
            match = re.search(marker, s)
            if match:
                # ищем ближайший к середине
                mid = len(s) // 2
                pos = match.start()
                if best_split is None or abs(pos - mid) < abs(best_split - mid):
                    best_split = pos

        if best_split and best_split > 20:
            first = s[:best_split].rstrip(" ,;—-")
            second = s[best_split:].lstrip(" ,;—-")
            # первое завершаем точкой
            first = first.rstrip(".") + "."
            # второе — с заглавной
            if second:
                second = second[0].upper() + second[1:]
            out.append(first + " " + second)
        else:
            out.append(s)

    return " ".join(out)


def _merge_short_sentences(text: str, min_words: int = 4) -> str:
    """Объединяет слишком короткие предложения с соседними через 'и' или ','."""
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    if len(sentences) < 2:
        return text

    out = [sentences[0]]
    for s in sentences[1:]:
        prev = out[-1]
        if len(s.split()) < min_words and len(prev.split()) < 15:
            # склеиваем
            connector = ", и " if not prev.endswith((",", "—")) else " "
            out[-1] = prev.rstrip(".!?") + connector + s[0].lower() + s[1:]
        else:
            out.append(s)

    return " ".join(out)

# Вводные слова в начале предложения, от которых можно избавиться
LEADING_FILLERS = [
    r"^Однако,?\s+",
    r"^Таким\s+образом,?\s+",
    r"^Более\s+того,?\s+",
    r"^Кроме\s+того,?\s+",
    r"^Итак,?\s+",
    r"^Вместе\s+с\s+тем,?\s+",
    r"^При\s+этом,?\s+",
    r"^Также\s+",
    r"^Помимо\s+этого,?\s+",
    r"^В\s+то\s+же\s+время,?\s+",
]


def _strip_leading_fillers(text: str, max_percent: float = 0.3) -> str:
    """Убирает вводные в начале предложений, но не все — максимум 30%."""
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    if not sentences:
        return text

    out = []
    removed = 0
    max_remove = max(1, int(len(sentences) * max_percent))

    for s in sentences:
        cleaned = s
        for pat in LEADING_FILLERS:
            if removed < max_remove and re.match(pat, cleaned, re.IGNORECASE):
                # убираем вводное и делаем заглавную у следующего слова
                cleaned = re.sub(pat, "", cleaned, count=1, flags=re.IGNORECASE)
                if cleaned:
                    cleaned = cleaned[0].upper() + cleaned[1:]
                removed += 1
                break
        out.append(cleaned)

    return " ".join(out)

def paraphrase_stamps(text: str, intensity: int = 5, multi_pass: bool = False) -> dict:
    """Полный пайплайн перефраза без LLM:
    1. Замена штампов по правилам (STAMP_RULES)
    2. Вариативная замена синонимов (случайные варианты)
    3. Удаление вводных в начале
    4. Разбивка длинных предложений
    5. Объединение коротких
    6. Косметика
    """
    original = text
    applied = []

    # === 1. Замена штампов ===
    def _apply_pass(txt):
        cnt_total = 0
        for pattern, replacement in STAMP_RULES:
            new_text, count = _smart_replace(txt, pattern, replacement)
            if count > 0:
                applied.append({"pattern": pattern, "count": count})
                cnt_total += count
                txt = new_text
        return txt, cnt_total

    text, n1 = _apply_pass(text)

    # === 2. Вариативная замена синонимов ===
    # чем выше интенсивность, тем сильнее вмешиваемся
    text, n2 = _variative_paraphrase(text, intensity)

    # === 3. Удаление вводных ===
    if intensity >= 4:
        text = _strip_leading_fillers(text)

    # === 4. Разбивка длинных предложений ===
    if intensity >= 5:
        text = _split_long_sentences(text)

    # === 5. Объединение коротких ===
    if intensity >= 5:
        text = _merge_short_sentences(text)

    # === 6. Второй проход при высокой интенсивности ===
    if multi_pass or intensity >= 8:
        text, _ = _apply_pass(text)
        text, _ = _variative_paraphrase(text, intensity)

    # === 7. Косметика ===
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"^\s*,\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\.\s*,", ".", text)
    # заглавная после точки
    text = re.sub(r"([.!?])\s*([а-яё])",
                  lambda m: m.group(1) + " " + m.group(2).upper(), text)
    # убираем двойные точки
    text = re.sub(r"\.\.(?!\.)", ".", text)
    text = re.sub(r"\s+\.", ".", text)
    text = text.strip()

    return {
        "paraphrased": text,
        "replacements": len(applied),
        "total_swaps": sum(a["count"] for a in applied) + n2,
        "details": applied[:15],
        "chars_before": len(original),
        "chars_after": len(text),
        "intensity": intensity,
        "multi_pass": multi_pass or intensity >= 8,
    }

def diff_words_html(before: str, after: str) -> dict:
    """Построчное сравнение: удалённое — красное зачёркнуто, добавленное — зелёное."""
    if not before and not after:
        return {"html": "", "removed": 0, "added": 0}

    before_lines = before.splitlines()
    after_lines = after.splitlines()

    removed_total = 0
    added_total = 0
    out_lines = []

    matcher = difflib.SequenceMatcher(None, before_lines, after_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in before_lines[i1:i2]:
                out_lines.append(f'<div class="diff-line">{_html.escape(line) or "&nbsp;"}</div>')
        elif tag == "delete":
            for line in before_lines[i1:i2]:
                removed_total += len(line)
                out_lines.append(f'<div class="diff-line diff-del">{_html.escape(line) or "&nbsp;"}</div>')
        elif tag == "insert":
            for line in after_lines[j1:j2]:
                added_total += len(line)
                out_lines.append(f'<div class="diff-line diff-add">{_html.escape(line) or "&nbsp;"}</div>')
        elif tag == "replace":
            # строим word-level diff для пары строк
            b_lines = before_lines[i1:i2]
            a_lines = after_lines[j1:j2]
            max_n = max(len(b_lines), len(a_lines))
            for k in range(max_n):
                b = b_lines[k] if k < len(b_lines) else ""
                a = a_lines[k] if k < len(a_lines) else ""
                if b == a:
                    out_lines.append(f'<div class="diff-line">{_html.escape(b) or "&nbsp;"}</div>')
                    continue
                # word-level
                sm = difflib.SequenceMatcher(None, b.split(), a.split())
                buf = []
                for wt, wi1, wi2, wj1, wj2 in sm.get_opcodes():
                    if wt == "equal":
                        buf.append(_html.escape(" ".join(b.split()[wi1:wi2])))
                    elif wt == "delete":
                        removed_total += sum(len(w) for w in b.split()[wi1:wi2])
                        buf.append(f'<span class="diff-w-del">{" ".join(_html.escape(w) for w in b.split()[wi1:wi2])}</span>')
                    elif wt == "insert":
                        added_total += sum(len(w) for w in a.split()[wj1:wj2])
                        buf.append(f'<span class="diff-w-add">{" ".join(_html.escape(w) for w in a.split()[wj1:wj2])}</span>')
                    elif wt == "replace":
                        removed_total += sum(len(w) for w in b.split()[wi1:wi2])
                        added_total += sum(len(w) for w in a.split()[wj1:wj2])
                        if wi2 > wi1:
                            buf.append(f'<span class="diff-w-del">{" ".join(_html.escape(w) for w in b.split()[wi1:wi2])}</span>')
                        if wj2 > wj1:
                            buf.append(f'<span class="diff-w-add">{" ".join(_html.escape(w) for w in a.split()[wj1:wj2])}</span>')
                out_lines.append(f'<div class="diff-line">{" ".join(buf) or "&nbsp;"}</div>')

    return {
        "html": "\n".join(out_lines),
        "removed": removed_total,
        "added": added_total,
    }

# =============================================================
#  ПРОВЕРКА «ИИ-МАНЕРЫ» (эвристика, чистый Python)
# =============================================================

# Слова и обороты, часто встречающиеся в русскоязычном ИИ-тексте
AI_PATTERNS = [
    # === Основные конструкции ИИ ===
    (r"чем\s+[^,.!?]{2,40},\s*тем\s", 4, "«чем…, тем…»"),
    (r"\bне\s+прост[оа][йа]?\s+[^,.!?]{2,30},?\s+а\s+", 5, "«не просто X, а Y»"),
    (r"\bне\s+столько\s+[^,.!?]{2,30},?\s+сколько\b", 5, "«не столько X, сколько Y»"),
    (r"\bне\s+только\s+[^,.!?]{2,30},?\s+но\s+и\b", 4, "«не только X, но и Y»"),
    (r"\bэто\s+не\s+[а-яё]+,\s*а\s+[а-яё]+", 5, "«это не X, а Y»"),
    (r"\bперестал\s+быть\s+просто\b", 5, "«перестал быть просто…»"),
    (r"\bпревратил(ся|ась|ось)\s+в\b", 3, "«превратился в»"),
    (r"\bот\s+[а-яё]{4,}\s+до\s+[а-яё]{4,}", 3, "«от X до Y»"),
    (r"\bи\s+в\s+этом\s+(его|её|их|наш|ваш|весь|вся|всё)\b", 6, "«и в этом его…»"),
    (r"\bвыбор\s+остаётся\s+за\b", 5, "«выбор остаётся за…»"),
    (r"\bответ,?\s+(вероятно|возможно|очевидно|прост)\b", 5, "«ответ, вероятно…»"),
    (r"\bбудущее\s+\S+\s+зависит\b", 5, "«будущее X зависит…»"),
    (r"\bглавн[а-яё]+\s+(урок|вывод|идея|мысль|принцип|вопрос)\b", 5, "«главный урок…»"),
    (r"\bсегодня\s+же\b", 4, "«сегодня же»"),
    (r"\bи\s+главное\b", 4, "«и главное»"),
    (r"\bничего\s+не\s+надо\s+менять\b", 3, "«ничего не надо менять»"),

    # === Вводные клише ===
    (r"\bоднако\b", 2, "«однако»"),
    (r"\bболее\s+того\b", 3, "«более того»"),
    (r"\bкроме\s+того\b", 2, "«кроме того»"),
    (r"\bтаким\s+образом\b", 3, "«таким образом»"),
    (r"\bв\s+свою\s+очередь\b", 3, "«в свою очередь»"),
    (r"\bв\s+конечном\s+счёте\b", 3, "«в конечном счёте»"),
    (r"\bименно\s+поэтому\b", 4, "«именно поэтому»"),
    (r"\bв\s+связи\s+с\s+этим\b", 3, "«в связи с этим»"),
    (r"\bв\s+заключение\b", 3, "«в заключение»"),
    (r"\bподводя\s+итог\b", 4, "«подводя итог»"),
    (r"\bв\s+целом\b", 2, "«в целом»"),
    (r"\bкак\s+правило\b", 3, "«как правило»"),
    (r"\bв\s+частности\b", 2, "«в частности»"),
    (r"\bнаряду\s+с\s+этим\b", 3, "«наряду с этим»"),
    (r"\bвместе\s+с\s+тем\b", 3, "«вместе с тем»"),
    (r"\bпо\s+сути\b", 2, "«по сути»"),
    (r"\bв\s+свою\s+очередь,?\s", 3, "«в свою очередь»"),

    # === Псевдо-важность ===
    (r"\bважно\s+(отметить|понимать|помнить|подчеркнуть|учитывать)\b", 5, "«важно отметить»"),
    (r"\bстоит\s+(отметить|подчеркнуть|заметить|учитывать)\b", 4, "«стоит отметить»"),
    (r"\bследует\s+(отметить|подчеркнуть|заметить|учитывать)\b", 4, "«следует отметить»"),
    (r"\bнеобходимо\s+(отметить|подчеркнуть|учитывать|понимать)\b", 4, "«необходимо отметить»"),
    (r"\bиграет\s+(важную|ключевую|значимую|немаловажную)\s+роль\b", 5, "«играет важную роль»"),
    (r"\bимеет\s+(важное|ключевое|особое|принципиальное)\s+значение\b", 5, "«имеет важное значение»"),
    (r"\bзанимает\s+(особое|важное|центральное)\s+место\b", 4, "«занимает особое место»"),
    (r"\bобладает\s+рядом\s+преимуществ\b", 4, "«обладает рядом преимуществ»"),
    (r"\bоткрывает\s+новые\s+(возможности|горизонты|перспективы)\b", 5, "«открывает новые возможности»"),
    (r"\bдаёт\s+возможность\b", 3, "«даёт возможность»"),
    (r"\bпозволяет\s+(нам|нам\s+лучше|более)\b", 3, "«позволяет лучше»"),

    # === Абстрактные обороты ===
    (r"\bв\s+современном\s+мире\b", 5, "«в современном мире»"),
    (r"\bв\s+современных\s+условиях\b", 5, "«в современных условиях»"),
    (r"\bв\s+условиях\s+(современности|неопределённости|глобализации)\b", 5, "«в условиях…»"),
    (r"\bна\s+сегодняшний\s+день\b", 3, "«на сегодняшний день»"),
    (r"\bв\s+настоящее\s+время\b", 3, "«в настоящее время»"),
    (r"\bна\s+данный\s+момент\b", 3, "«на данный момент»"),
    (r"\bпредставляет\s+собой\b", 3, "«представляет собой»"),
    (r"\bявляется\s+(ключевым|важным|основным|одним|центральным)\b", 3, "«является ключевым»"),
    (r"\bвыступает\s+в\s+качестве\b", 3, "«выступает в качестве»"),
    (r"\bносит\s+(системный|комплексный|целостный)\s+характер\b", 4, "«носит комплексный характер»"),
    (r"\bнезаметно\s+стал\b", 4, "«незаметно стал»"),
    (r"\b(формируется|складывается)\s+(современн|нов|целостн)", 3, "«формируется современная…»"),
    (r"\bв\s+рамках\s+данного\b", 4, "«в рамках данного»"),
    (r"\bв\s+контексте\b", 3, "«в контексте»"),
    (r"\bв\s+свете\s+вышесказанного\b", 5, "«в свете вышесказанного»"),

    # === Триады и перечисления ===
    (r"[а-яё]{4,},\s+[а-яё]{4,}\s+и\s+[а-яё]{4,}", 3, "триада X, Y, Z"),
    (r"[а-яё]{4,},\s+[а-яё]{4,},\s+[а-яё]{4,}", 3, "перечисление из трёх"),

    # === Тире и пунктуация ===
    (r"—", 1, "длинное тире"),
    (r"\s+;\s+", 2, "точка с запятой внутри предложения"),

    # === Модальность ===
    (r"\b(важно|необходимо|следует|нужно|стоит|должен|может|позволяет)\b", 1, "модальный глагол"),

    # === Абстрактные существительные ===
    (r"\b(развитие|совершенствование|оптимизация|трансформация|модернизация)\b", 3, "абстрактное «развитие»"),
    (r"\b(технология|технологии|инновация|инновации)\b", 2, "«технологии»"),
    (r"\b(возможность|возможности|потенциал|перспектива|перспективы)\b", 2, "«возможности»"),
    (r"\b(эффективность|результативность|продуктивность)\b", 3, "«эффективность»"),
    (r"\b(подход|подходы|методология|парадигма)\b", 3, "«подход»"),

    # === Процессуальность ===
    (r"\bосуществля(ется|ются|ет|ют)\b", 3, "«осуществляется»"),
    (r"\bпроводи(тся|тся|ть)\b", 2, "«проводится»"),
    (r"\bреализу(ется|ются|ет)\b", 3, "«реализуется»"),
    (r"\bобеспечива(ется|ют|ет)\b", 3, "«обеспечивается»"),
    (r"\bформиру(ется|ют|ет)\b", 2, "«формируется»"),
    (r"\bсозда(ётся|ются|ет)\b", 2, "«создаётся»"),

    # === Псевдо-конкретика ===
    (r"\bразличн[а-яё]+\s+аспект", 3, "«различные аспекты»"),
    (r"\bширокий\s+спектр\b", 3, "«широкий спектр»"),
    (r"\bключев[а-яё]+\s+(фактор|аспект|элемент)\b", 3, "«ключевой фактор»"),
    (r"\bосновн[а-яё]+\s+(цель|задача|принцип|подход)\b", 2, "«основная цель»"),
    (r"\bряд\s+(особенностей|факторов|преимуществ)\b", 3, "«ряд особенностей»"),

    # === «Туманные» начала ===
    (r"^\s*итак", 3, "«итак» в начале"),
    (r"^\s*в\s+данной\s+статье", 5, "«в данной статье»"),
    (r"^\s*данн[а-яё]+\s+(статья|работа|исследование)\b", 4, "«данная статья»"),
]

ABSTRACT_STEMS = [
    "технолог", "процесс", "подход", "возможност", "ценност", "развити",
    "будущ", "представлени", "сред", "культур", "проблем", "решени",
    "творчеств", "знани", "опыт", "эмоци", "манипуляц", "преимуществ",
    "недостат", "тенденц", "концепц", "перспектив", "потенциал",
    "инновац", "влияни", "фактор", "аспект", "принцип", "метод",
    "систем", "структур", "парадигм", "трансформац", "коммуникац",
    "сущност", "явлени", "взаимодейств",
]
# Анти-паттерны: то, что ИИ почти никогда не делает, а человек — постоянно
HUMAN_MARKERS = [
    # личные вставки
    r"\bя\s+(думаю|считаю|помню|знаю|видел|читал|пробовал)\b",
    r"\bмне\s+(кажется|нравится|кажется|представляется)\b",
    r"\bпо[-\s]моему\b", r"\bпо[-\s]моему\s+мнению\b",
    r"\bна\s+мой\s+взгляд\b",
    r"\bчестно\s+говоря\b", r"\bесли\s+честно\b",
    r"\bпризнаться\b", r"\bскажу\s+честно\b",
    # сомнения и хезитации
    r"\bнаверное\b", r"\bнаверно\b", r"\bпожалуй\b",
    r"\bвроде\s+бы\b", r"\bкак\s+бы\b", r"\bвроде\b",
    r"\bможет\s+быть\b", r"\bвозможно\b",
    # разговорные
    r"\bкороче\b", r"\bвообще\b", r"\bвообще[-\s]то\b",
    r"\bприкинь\b", r"\bкстати\b", r"\bк\s+слову\b",
    r"\bтак\s+вот\b", r"\bвот\s+именно\b",
    r"\bзнаешь\b", r"\bпонимаешь\b", r"\bслышь\b",
    # личный опыт
    r"\bоднажды\b", r"\bкак[-\s]то\s+раз\b", r"\bпомню\b",
    r"\bв\s+детстве\b", r"\bу\s+меня\s+был\b",
    r"\bя\s+сам\b", r"\bсвоими\s+глазами\b",
    # эмоции
    r"\bужас\b", r"\bкошмар\b", r"\bкапец\b", r"\bжесть\b",
    r"\bкласс\b", r"\bкруто\b", r"\bсупер\b", r"\bогонь\b",
    r"\bобидно\b", r"\bпечально\b", r"\bгрустно\b",
    r"\bненавижу\b", r"\bобожаю\b", r"\bлюблю\b",
]

# Риторические маркеры
RHETORICAL = {
    "metaphor": [
        r"\bкак\s+будто\b", r"\bсловно\b", r"\bточно\b", r"\bпохож[а-яё]*\s+на\b",
    ],
    "comparison": [
        r"\bлучше\s+чем\b", r"\bхуже\s+чем\b", r"\bбыстрее\s+чем\b",
        r"\bсильнее\s+чем\b", r"\b[а-яё]+\s+как\s+[а-яё]+",
    ],
    "rhetorical_q": [
        r"\?[^?]{0,40}\?",  # несколько вопросов подряд
    ],
    "exclamation": [
        r"!", r"!!", r"!!",
    ],
}

# Функциональные слова (у ИИ обычно более равномерно распределены)
FUNCTION_WORDS = {
    "prepositions": ["в", "на", "с", "по", "из", "к", "от", "для", "при", "о", "об", "за", "под", "над", "про", "без", "до", "у"],
    "conjunctions": ["и", "а", "но", "или", "что", "чтобы", "если", "когда", "потому", "хотя", "пока", "зато"],
    "particles": ["же", "ли", "бы", "не", "ни", "вот", "ведь", "даже", "только", "именно", "просто", "лишь"],
    "pronouns": ["я", "ты", "он", "она", "мы", "вы", "они", "это", "то", "этот", "тот"],
}
# Эмоционально-оценочная лексика (у людей её больше)
EMOTION_WORDS = [
    "нравится", "люблю", "ненавижу", "обожаю", "бесит", "раздражает",
    "рад", "радостно", "грустно", "печально", "здорово", "классно",
    "круто", "ужасно", "отвратительно", "прекрасно", "замечательно",
    "интересно", "скучно", "страшно", "весело", "прикольно", "кайф",
    "фигня", "жесть", "кошмар", "восторг", "уныние", "тоска",
    "надеюсь", "боюсь", "переживаю", "волнуюсь", "радуюсь",
    "сомневаюсь", "верю", "не верю", "думаю", "считаю", "кажется",
]

# Дискурсивные маркеры (структурные вводные — у ИИ плотнее)
DISCOURSE_MARKERS = [
    "во-первых", "во-вторых", "в-третьих", "в-четвёртых",
    "с одной стороны", "с другой стороны",
    "сначала", "затем", "потом", "далее", "итак",
    "наконец", "в заключение", "подводя итог", "в итоге",
    "прежде всего", "в первую очередь", "в конечном счёте",
    "таким образом", "следовательно", "значит", "итак",
]

# Характерные для «живого» текста обороты
CASUAL_MARKERS = [
    "ну ", "вот ", "короче", "блин", "хз", "щас", "чё",
    "вообще-то", "как-то", "типа", "вроде", "прикольно",
    "классно", "жесть", "фигня", "капец", "блин",
]


def _sentences(text: str) -> list:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [s for s in parts if len(s.strip()) > 3]


def _words(text: str) -> list:
    return re.findall(r"[А-Яа-яЁёA-Za-z\-]+", text.lower())


def _stdev(xs: list) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)

def _entropy(text: str) -> float:
    """Информационная энтропия по буквам (Шеннон)."""
    if not text:
        return 0.0
    low = text.lower()
    freq = Counter(c for c in low if c.isalpha())
    total = sum(freq.values())
    if total == 0:
        return 0.0
    ent = 0.0
    for count in freq.values():
        p = count / total
        if p > 0:
            ent -= p * math.log2(p)
    return ent


def _hapax_ratio(words: list) -> float:
    """Доля слов, встречающихся ровно 1 раз. У людей выше."""
    if not words:
        return 0.0
    counts = Counter(words)
    hapax = sum(1 for c in counts.values() if c == 1)
    return hapax / len(counts) if counts else 0.0


def _trigram_repeat_ratio(words: list) -> float:
    """Доля повторяющихся триграмм. У ИИ выше."""
    if len(words) < 6:
        return 0.0
    trigrams = [tuple(words[i:i+3]) for i in range(len(words) - 2)]
    counts = Counter(trigrams)
    repeated = sum(c for c in counts.values() if c > 1)
    return repeated / len(trigrams) if trigrams else 0.0


def _punctuation_variety(text: str) -> float:
    """Разнообразие используемых знаков препинания (0-1)."""
    allowed = set(",.!?;:—-()«»\"'…")
    used = set(c for c in text if c in allowed)
    return len(used) / len(allowed)


def _emotion_density(text: str) -> float:
    """Плотность эмоциональной лексики на 100 слов."""
    words = _words(text)
    if not words:
        return 0.0
    low = text.lower()
    cnt = sum(low.count(w) for w in EMOTION_WORDS)
    return cnt / len(words) * 100


def _discourse_density(text: str) -> float:
    """Плотность дискурсивных маркеров на 100 слов."""
    words = _words(text)
    if not words:
        return 0.0
    low = text.lower()
    cnt = sum(low.count(m) for m in DISCOURSE_MARKERS)
    return cnt / len(words) * 100


def _word_length_stdev(words: list) -> float:
    """Стандартное отклонение длины слов."""
    if len(words) < 3:
        return 0.0
    return _stdev([len(w) for w in words])


def _sentence_opener_diversity(sentences: list) -> float:
    """Разнообразие первых слов предложений."""
    if len(sentences) < 3:
        return 1.0
    firsts = []
    for s in sentences:
        w = _words(s)
        if w:
            firsts.append(w[0])
    if not firsts:
        return 1.0
    return len(set(firsts)) / len(firsts)


def _casual_density(text: str) -> float:
    """Плотность разговорных оборотов на 100 слов."""
    words = _words(text)
    if not words:
        return 0.0
    low = text.lower()
    cnt = sum(low.count(c) for c in CASUAL_MARKERS)
    return cnt / len(words) * 100

def _burstiness(sent_lens: list) -> float:
    """B-коэффициент: чередование коротких и длинных предложений.
    У людей больше, у ИИ почти нет."""
    if len(sent_lens) < 3:
        return 0.0
    # доля смен между «длинными» и «короткими»
    avg = sum(sent_lens) / len(sent_lens)
    long_s = sum(1 for x in sent_lens if x > avg * 1.3)
    short_s = sum(1 for x in sent_lens if x < avg * 0.7)
    transitions = 0
    for i in range(1, len(sent_lens)):
        if (sent_lens[i-1] > avg) != (sent_lens[i] > avg):
            transitions += 1
    return transitions / (len(sent_lens) - 1)


def _entropy(text: str) -> float:
    """Энтропия Шеннона по буквам. У людей выше."""
    if not text:
        return 0.0
    letters = [c.lower() for c in text if c.isalpha()]
    if not letters:
        return 0.0
    freq = Counter(letters)
    total = len(letters)
    ent = 0.0
    for cnt in freq.values():
        p = cnt / total
        if p > 0:
            ent -= p * math.log2(p)
    return ent


def _hapax_ratio(words: list) -> float:
    """Доля слов, встречающихся 1 раз. У людей выше."""
    if not words:
        return 0.0
    counts = Counter(words)
    hapax = sum(1 for c in counts.values() if c == 1)
    return hapax / len(counts) if counts else 0.0


def _zipf_deviation(words: list) -> float:
    """Отклонение от закона Ципфа. У людей меньше."""
    if len(words) < 20:
        return 0.0
    freq = Counter(words)
    counts = sorted(freq.values(), reverse=True)
    # ожидаемая частота: f(n) ~ 1/n
    n = len(counts)
    deviations = 0.0
    for i, c in enumerate(counts):
        rank = i + 1
        expected = counts[0] / rank
        if expected > 0:
            deviations += abs(c - expected) / expected
    return deviations / n if n else 0.0


def _function_word_density(text: str) -> dict:
    """Плотность функциональных слов разных типов."""
    words = _words(text)
    if not words:
        return {}
    total = len(words)
    result = {}
    for cat, lst in FUNCTION_WORDS.items():
        cnt = sum(words.count(w) for w in lst)
        result[cat] = round(cnt / total * 100, 2)
    return result


def _human_markers(text: str) -> int:
    """Считает «человеческие» маркеры — то, чего у ИИ почти нет."""
    low = text.lower()
    return sum(len(re.findall(p, low, re.IGNORECASE)) for p in HUMAN_MARKERS)


def _rhetorical_score(text: str) -> float:
    """Разнообразие риторических приёмов."""
    score = 0.0
    for cat, patterns in RHETORICAL.items():
        cnt = sum(len(re.findall(p, text, re.IGNORECASE)) for p in patterns)
        if cnt > 0:
            score += 1.0
    return score / len(RHETORICAL)


def _clause_complexity(text: str) -> float:
    """Среднее число придаточных на предложение."""
    sentences = _sentences(text)
    if not sentences:
        return 0.0
    sub_markers = r"\b(что|чтобы|если|когда|пока|хотя|поскольку|так\s+как|потому\s+что|котор[а-яё]+|где|куда|откуда)\b"
    total_clauses = sum(len(re.findall(sub_markers, s, re.IGNORECASE)) for s in sentences)
    return total_clauses / len(sentences)


def _positional_drift(text: str) -> float:
    """Как меняется длина предложений от начала к концу.
    У людей обычно есть развитие, у ИИ — плато."""
    sentences = _sentences(text)
    if len(sentences) < 6:
        return 0.0
    half = len(sentences) // 2
    first = [len(_words(s)) for s in sentences[:half]]
    second = [len(_words(s)) for s in sentences[half:]]
    avg1 = sum(first) / len(first) if first else 0
    avg2 = sum(second) / len(second) if second else 0
    if avg1 == 0:
        return 0.0
    return abs(avg2 - avg1) / avg1

def check_ai_score(text: str) -> dict:
    """Расширенная оценка «ИИ-манеры». 30+ метрик.
    Учитывает burstiness, энтропию, hapax, Zipf, human-маркеры,
    риторику, синтаксис. Цель — имитировать работу детекторов."""
    if not text or len(text) < 80:
        return {"available": True, "score": 0, "label": "слишком короткий текст",
                "features": {}, "notes": []}

    clean = re.sub(r"[*_`#>]+", "", text)
    sentences = _sentences(clean)
    words = _words(clean)
    if not words or not sentences:
        return {"available": True, "score": 0, "label": "нет текста",
                "features": {}, "notes": []}

    notes = []
    score = 0.0
    low = clean.lower()

    # === БАЗОВЫЕ ===
    sent_lens = [len(_words(s)) for s in sentences]
    avg_sent = sum(sent_lens) / len(sent_lens)
    sd = _stdev(sent_lens)
    cv = sd / avg_sent if avg_sent else 0
    avg_wl = sum(len(w) for w in words) / len(words)
    uniq = len(set(words))
    guiraud = uniq / math.sqrt(len(words)) if words else 0

    # === НОВЫЕ ===
    burst = _burstiness(sent_lens)
    ent = _entropy(clean)
    hapax = _hapax_ratio(words)
    zipf_dev = _zipf_deviation(words)
    human_cnt = _human_markers(clean)
    rhet = _rhetorical_score(clean)
    clauses = _clause_complexity(clean)
    drift = _positional_drift(clean)
    func_dens = _function_word_density(clean)

    # ---- 1. Burstiness (у ИИ ниже 0.3) ----
    if burst < 0.2:
        score += 12; notes.append(f"Очень ровный ритм (burstiness={burst:.2f})")
    elif burst < 0.35:
        score += 6; notes.append(f"Ровный ритм (burstiness={burst:.2f})")
    elif burst > 0.55:
        score -= 3; notes.append(f"Живой ритм (burstiness={burst:.2f}): −3")

    # ---- 2. Разброс длины предложений ----
    if cv < 0.3:
        score += 10; notes.append(f"Очень однородные предложения (cv={cv:.2f})")
    elif cv < 0.45:
        score += 6
    elif cv > 0.7:
        score -= 2

    # ---- 3. Hapax ratio ----
    if hapax < 0.35:
        score += 10; notes.append(f"Мало уникальных слов (hapax={hapax:.2f})")
    elif hapax < 0.45:
        score += 5
    elif hapax > 0.6:
        score -= 4; notes.append(f"Богатая лексика (hapax={hapax:.2f}): −4")

    # ---- 4. Энтропия ----
    if ent < 4.2:
        score += 8; notes.append(f"Низкая энтропия ({ent:.2f})")
    elif ent < 4.4:
        score += 4
    elif ent > 4.7:
        score -= 3

    # ---- 5. Zipf-отклонение ----
    if zipf_dev > 1.5:
        score += 8; notes.append(f"Патологическое распределение слов (Zipf-dev={zipf_dev:.2f})")
    elif zipf_dev > 0.8:
        score += 4

    # ---- 6. Абстрактная лексика ----
    abstract_count = sum(1 for w in words
                         if any(w.startswith(s) for s in ABSTRACT_STEMS))
    abs_density = abstract_count / len(words) * 100
    if abs_density > 14:
        score += 10; notes.append(f"Много абстрактных слов ({abs_density:.0f}%)")
    elif abs_density > 9:
        score += 5

    # ---- 7. ПАТТЕРНЫ ИИ ----
    pattern_hits = []
    pattern_score = 0
    for rx, weight, name in AI_PATTERNS:
        matches = re.findall(rx, low)
        if matches:
            pattern_score += weight * min(len(matches), 3)
            pattern_hits.append(f"{name}×{len(matches)}")
    pattern_score = min(pattern_score, 45)
    score += pattern_score
    if pattern_hits:
        notes.append(f"Паттерны ИИ (+{pattern_score}): " + "; ".join(pattern_hits[:10]))
    else:
        notes.append("Характерных паттернов ИИ не найдено")

    # ---- 8. Тире, запятые ----
    em_dashes = clean.count("—") + clean.count("–")
    if em_dashes / max(1, len(sentences)) > 0.3:
        score += 8; notes.append(f"Много длинных тире ({em_dashes})")

    comma_density = clean.count(",") / max(1, len(sentences))
    if comma_density > 4:
        score += 6; notes.append(f"Плотные запятые ({comma_density:.1f}/предл.)")

    # ---- 9. Модальность и пассив ----
    passive = len(re.findall(r"\b(является|представляет|осуществляется|проводится|выполняется)\b", low))
    modal = len(re.findall(r"\b(важно|необходимо|следует|нужно|стоит)\b", low))
    modal_ratio = modal / max(1, len(sentences))
    if passive > 3:
        score += 6; notes.append(f"Пассивные конструкции ({passive})")
    if modal_ratio > 0.7:
        score += 5; notes.append(f"Много модальных ({modal_ratio:.1f}/предл.)")

    # ---- 10. Триады ----
    triads = len(re.findall(r"[а-яё]{3,},\s+[а-яё]{3,}\s+(и|или)\s+[а-яё]{3,}", low))
    if triads >= 3:
        score += 6; notes.append(f"Триады ({triads})")
    elif triads >= 1:
        score += 2

    # ---- 11. Синтаксическая сложность ----
    if clauses < 0.4 and len(sentences) > 4:
        score += 5; notes.append(f"Мало придаточных ({clauses:.2f}/предл.)")

    # ---- 12. Риторика ----
    if rhet < 0.25 and len(sentences) > 4:
        score += 6; notes.append("Нет риторических приёмов")
    elif rhet > 0.5:
        score -= 3

    # ---- 13. Позиционный дрейф ----
    if drift < 0.05 and len(sentences) > 8:
        score += 6; notes.append("Плоский ритм (нет развития)")

    # ---- 14. Функциональные слова ----
    if func_dens:
        if func_dens.get("particles", 0) < 1.5:
            score += 5; notes.append("Мало частиц (же, ведь, вот)")
        if func_dens.get("conjunctions", 0) < 3:
            score += 3

    # ---- 15. ШТРАФЫ ЗА ЧЕЛОВЕЧНОСТЬ ----
    if human_cnt > 0:
        penalty = min(20, human_cnt * 3)
        score -= penalty
        notes.append(f"Человеческие маркеры ({human_cnt}): −{penalty}")
    else:
        score += 3; notes.append("Нет человеческих маркеров: +3")

    emoji_count = sum(1 for c in text if ord(c) > 0x1F000)
    if emoji_count > 0:
        score -= min(10, emoji_count)

    score = max(0, min(100, round(score)))

    if score <= 25:      label = "скорее человек"
    elif score <= 50:    label = "смешанный"
    elif score <= 75:    label = "есть следы ИИ"
    else:                label = "сильные следы ИИ"

    return {
        "available": True,
        "score": score,
        "label": label,
        "features": {
            "предложений": len(sentences),
            "слов": len(words),
            "burstiness": round(burst, 2),
            "энтропия": round(ent, 2),
            "hapax": round(hapax, 2),
            "Zipf-dev": round(zipf_dev, 2),
            "разброс предл.": round(cv, 2),
            "ср. длина слова": round(avg_wl, 2),
            "Guiraud": round(guiraud, 1),
            "придаточных": round(clauses, 2),
            "риторика": f"{rhet:.0%}",
            "человеческих": human_cnt,
            "паттернов ИИ": len(pattern_hits),
            "абстрактных": f"{abs_density:.0f}%",
        },
        "notes": notes,
    }

def check_ai_combined(text: str) -> dict:
    """Комбинированная оценка: эвристики + LLM (если доступна)."""
    heuristic = check_ai_score(text)

    result = {
        "available": True,
        "score": heuristic.get("score", 0),
        "label": heuristic.get("label", "—"),
        "features": heuristic.get("features", {}),
        "notes": heuristic.get("notes", []),
        "heuristic_score": heuristic.get("score", 0),
        "llm_score": None,
        "combined": False,
    }

    # Пытаемся получить оценку от LLM
    if HAS_OLLAMA:
        try:
            llm = check_ai_with_llm(text)
            if llm.get("available") and not llm.get("error"):
                llm_score = llm.get("score", 0)
                result["llm_score"] = llm_score
                # вес: LLM 0.55, эвристики 0.45
                combined = round(0.45 * heuristic.get("score", 0) + 0.55 * llm_score)
                result["score"] = combined
                result["combined"] = True

                if combined <= 25:      result["label"] = "скорее человек"
                elif combined <= 50:    result["label"] = "смешанный"
                elif combined <= 75:    result["label"] = "есть следы ИИ"
                else:                   result["label"] = "сильные следы ИИ"

                result["notes"].insert(0,
                    f"Комбинированный балл: эвристики {heuristic.get('score',0)} + "
                    f"LLM {llm_score} → {combined}")
        except Exception as e:
            result["notes"].append(f"LLM-оценка недоступна: {e}")

    return result

def check_ai_with_llm(text: str) -> dict:
    """Проверка ИИ через локальную модель Ollama. Точнее эвристик, но медленнее."""
    if not HAS_OLLAMA:
        return {"error": "Ollama не установлена. Выполни: pip install ollama", "available": False}

    if not text or len(text) < 100:
        return {"score": 0, "label": "слишком короткий текст", "available": True}

    # обрезаем до разумного размера, чтобы модель не захлебнулась
    sample = text[:6000]

    prompt = (
        "Ты — эксперт по анализу текстов. Оцени от 0 до 100, насколько этот текст "
        "похож на сгенерированный искусственным интеллектом (нейросетью).\n\n"
        "Критерии:\n"
        "- 0-25: написан человеком, живой язык, личный опыт, ошибки\n"
        "- 26-50: смешанный, есть и человеческие, и машинные черты\n"
        "- 51-75: заметны следы ИИ, шаблонные конструкции, гладкий стиль\n"
        "- 76-100: почти наверняка ИИ, идеально гладкий, шаблонный, безличный\n\n"
        f"Текст:\n{sample}\n\n"
        "Ответь ТОЛЬКО числом от 0 до 100, без объяснений и знаков."
    )

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.1},
        )
        raw = response["message"]["content"].strip()
        # вытаскиваем число из ответа
        m = re.search(r"\d+", raw)
        score = int(m.group()) if m else 50
        score = max(0, min(100, score))

        if score <= 25:      label = "скорее человек"
        elif score <= 50:    label = "смешанный"
        elif score <= 75:    label = "есть следы ИИ"
        else:                label = "сильные следы ИИ"

        return {
            "available": True,
            "score": score,
            "label": label,
            "features": {"модель": OLLAMA_MODEL, "метод": "LLM"},
            "notes": [f"Оценка через {OLLAMA_MODEL}. Точнее эвристик, но медленнее."],
        }
    except Exception as e:
        return {"error": f"Ollama недоступна: {e}", "available": False}

def paraphrase_with_llm(text: str, intensity: int = 5, style: str = "neutral") -> dict:
    if not HAS_OLLAMA:
        raise RuntimeError("Ollama не установлена")

    paragraphs = [p for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return {"paraphrased": text, "replacements": 0, "total_swaps": 0,
                "chars_before": len(text), "chars_after": len(text)}

    # эти строки — служебный мусор, пропускаем
    SKIP_MARKERS = [
        "правила", "исходный текст", "переписанный текст",
        "сохраняю весь смысл", "отвечай только", "избегай штампов",
        "язык — русский", "не сокращай",
    ]

    def is_garbage(p: str) -> bool:
        low = p.lower().strip()
        if len(low) < 5:
            return True
        for m in SKIP_MARKERS:
            if m in low:
                return True
        # строки, начинающиеся с "1. ", "2. " и т.п. — это пункты правил
        if re.match(r"^\d+\.\s", low):
            return True
        return False

    out_parts = []
    for para in paragraphs:
        if is_garbage(para) or len(para) < 30:
            out_parts.append(para)
            continue

        # максимально короткий промпт
        prompt = (
            "Перепиши этот текст другими словами на русском. "
            "Сохрани смысл. Ответь только текстом.\n\n"
            f"{para}"
        )

        try:
            response = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content":
                     "Ты — редактор. Отвечай только готовым текстом, без правил и пояснений."},
                    {"role": "user", "content": prompt},
                ],
                options={
                    "temperature": 0.6,
                    "top_p": 0.9,
                    "repeat_penalty": 1.15,
                    "num_predict": max(150, len(para) * 2),
                },
            )
            new_para = response["message"]["content"].strip()

            # отсекаем префиксы
            new_para = re.sub(
                r"^(переписанный текст|ответ|вот|текст)\s*:?\s*",
                "", new_para, flags=re.IGNORECASE
            ).strip()

            # если модель вернула тот же текст или мусор — оставляем оригинал
            if not new_para or new_para == para or len(new_para) < len(para) * 0.4:
                new_para = para

            out_parts.append(new_para)
        except Exception:
            out_parts.append(para)

    result = "\n\n".join(out_parts)
    return {
        "paraphrased": result,
        "replacements": 1,
        "total_swaps": len(paragraphs),
        "chars_before": len(text),
        "chars_after": len(result),
        "method": "llm",
    }

def paraphrase_stream_generator(text: str, intensity: int = 5, style: str = "neutral"):
    """Генератор для SSE-стриминга перефраза."""
    if not HAS_OLLAMA:
        yield {"type": "error", "message": "Ollama не установлена"}
        return

    paragraphs = [p for p in text.split("\n") if p.strip()]
    total = len(paragraphs)

    if intensity <= 3:
        hint = "Замени только штампы и канцелярит. Остальное не трогай."
    elif intensity <= 7:
        hint = "Перепиши текст другими словами, сохранив смысл."
    else:
        hint = "Полностью перепиши текст, измени структуру, но сохрани факты."

    preset = STYLE_PRESETS.get(style, STYLE_PRESETS["neutral"])
    system_prompt = (
        f"Ты — редактор. Отвечаешь только готовым текстом, без пояснений. "
        f"{preset['hint']} {hint}"
    )
    temperature = preset["temperature"]

    for i, para in enumerate(paragraphs):
        yield {"type": "progress", "current": i + 1, "total": total}

        if len(para) < 30:
            yield {"type": "token", "text": para}
            yield {"type": "end_chunk"}
            continue

        prompt = f"Перепиши:\n{para}"
        try:
            for chunk in ollama.chat(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                stream=True,
                options={
                    "temperature": temperature,
                    "num_predict": max(500, len(para) * 3),
                },
            ):
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    yield {"type": "token", "text": piece}
        except Exception as e:
            yield {"type": "error", "message": str(e)}
            yield {"type": "token", "text": para}

        yield {"type": "end_chunk"}

    yield {"type": "done"}

def find_ai_sentences(text: str) -> dict:
    """Возвращает текст с подсветкой предложений, наиболее похожих на ИИ.
    Помечает их как [AI]...[/AI] для отображения в UI."""
    sentences = _sentences(text)
    if not sentences:
        return {"text": text, "highlights": []}

    # Паттерны, которые прямо указывают на ИИ
    ai_patterns = [
        r"не\s+просто\s+[^,.!?]{2,30},?\s+а\s+",
        r"не\s+только\s+[^,.!?]{2,30},?\s+но\s+и\b",
        r"не\s+столько\s+[^,.!?]{2,30},?\s+сколько\b",
        r"\bважно\s+(отметить|подчеркнуть)\b",
        r"\bиграет\s+(важную|ключевую|значимую)\s+роль\b",
        r"\bв\s+современном\s+мире\b",
        r"\bпредставляет\s+собой\b",
        r"\bи\s+в\s+этом\s+(его|её|их)\b",
        r"\bглавн[а-яё]+\s+(урок|вывод|мысль|идея)\b",
        r"\bответ,?\s+(вероятно|возможно)\b",
        r"\bбудущее\s+\S+\s+зависит\b",
    ]

    highlights = []
    marked = []
    for sent in sentences:
        low = sent.lower()
        hits = sum(1 for p in ai_patterns if re.search(p, low))
        # длинные предложения с абстрактной лексикой — тоже маркер
        words = _words(sent)
        abstract = sum(1 for w in words if any(w.startswith(s) for s in ABSTRACT_STEMS))
        abstract_ratio = abstract / max(1, len(words))

        is_ai = hits > 0 or (len(words) > 15 and abstract_ratio > 0.15)
        if is_ai:
            marked.append(sent)
            highlights.append({
                "text": sent,
                "patterns": hits,
                "abstract": round(abstract_ratio, 2),
            })

    return {
        "highlights": highlights,
        "count": len(highlights),
        "total_sentences": len(sentences),
    }

def find_ai_phrases(text: str) -> dict:
    """Находит конкретные ИИ-штампы в тексте и возвращает их позиции.
    Для подсветки прямо в textarea."""
    if not text:
        return {"phrases": [], "count": 0}

    # Список шаблонов для подсветки (regex + подпись)
    PATTERNS = [
        (r"\bоднако\b", "однако"),
        (r"\bболее\s+того\b", "более того"),
        (r"\bтаким\s+образом\b", "таким образом"),
        (r"\bв\s+свою\s+очередь\b", "в свою очередь"),
        (r"\bв\s+конечном\s+счёте\b", "в конечном счёте"),
        (r"\bв\s+современном\s+мире\b", "в современном мире"),
        (r"\bна\s+сегодняшний\s+день\b", "на сегодняшний день"),
        (r"\bважно\s+отметить\b", "важно отметить"),
        (r"\bстоит\s+отметить\b", "стоит отметить"),
        (r"\bследует\s+отметить\b", "следует отметить"),
        (r"\bважно\s+подчеркнуть\b", "важно подчеркнуть"),
        (r"\bиграет\s+важную\s+роль\b", "играет важную роль"),
        (r"\bиграет\s+ключевую\s+роль\b", "играет ключевую роль"),
        (r"\bпредставляет\s+собой\b", "представляет собой"),
        (r"\bявляется\s+(ключевым|важным|основным)\b", "является ключевым"),
        (r"\bперестал\s+быть\s+просто\b", "перестал быть просто"),
        (r"\bпревратил(ся|ась|ось)\s+в\b", "превратился в"),
        (r"\bи\s+в\s+этом\s+(его|её|их)\b", "и в этом его"),
        (r"\bвыбор\s+остаётся\s+за\b", "выбор остаётся за"),
        (r"\bименно\s+поэтому\b", "именно поэтому"),
        (r"\bне\s+просто\s+[^,.!?]{2,40}?,?\s+а\s+", "не просто X, а Y"),
        (r"\bне\s+только\s+[^,.!?]{2,40}?,?\s+но\s+и\b", "не только X, но и Y"),
        (r"\bне\s+столько\s+[^,.!?]{2,40}?,?\s+сколько\b", "не столько X, сколько Y"),
        (r"\bэто\s+не\s+[а-яё]+,\s*а\s+[а-яё]+", "это не X, а Y"),
        (r"\bглавн[а-яё]+\s+(урок|вывод|мысль|идея)\b", "главный урок"),
        (r"\bот\s+[а-яё]{4,}\s+до\s+[а-яё]{4,}", "от X до Y"),
        (r"—", "длинное тире"),
    ]

    found = []
    for regex, label in PATTERNS:
        for m in re.finditer(regex, text, flags=re.IGNORECASE):
            found.append({
                "start": m.start(),
                "end": m.end(),
                "text": m.group(0),
                "label": label,
            })

    # сортируем по позиции и убираем пересечения
    found.sort(key=lambda x: x["start"])
    cleaned = []
    last_end = -1
    for f in found:
        if f["start"] >= last_end:
            cleaned.append(f)
            last_end = f["end"]

    return {"phrases": cleaned, "count": len(cleaned)}

def paragraph_heatmap(text: str) -> dict:
    """Heatmap: оценка каждого абзаца по эвристикам."""
    if not text:
        return {"paragraphs": []}

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out = []
    for i, p in enumerate(paragraphs):
        if len(p) < 30:
            out.append({
                "index": i, "text": p[:200], "score": 0,
                "level": "short", "words": len(p.split()),
            })
            continue
        sub = check_ai_score(p)
        score = sub.get("score", 0)
        if score <= 25:      level = "low"
        elif score <= 50:    level = "mid"
        elif score <= 75:    level = "high"
        else:                level = "critical"
        out.append({
            "index": i,
            "text": p[:300] + ("…" if len(p) > 300 else ""),
            "score": score,
            "level": level,
            "words": len(p.split()),
        })

    return {"paragraphs": out, "total": len(out)}

def generate_report(text: str) -> dict:
    """Генерирует детальный отчёт: баллы, топ-причины, примеры, рекомендации."""
    combined = check_ai_combined(text)
    heuristic = check_ai_score(text)
    phrases = find_ai_phrases(text)
    heatmap = paragraph_heatmap(text)

    # Топ-причины (из notes эвристик)
    top_reasons = []
    for n in heuristic.get("notes", [])[:8]:
        if n.startswith("Паттерны ИИ"):
            # Разбираем паттерны отдельно
            continue
        top_reasons.append(n)

    # Группируем паттерны
    pattern_groups = {}
    low = text.lower()
    for rx, weight, name in AI_PATTERNS:
        matches = len(re.findall(rx, low))
        if matches > 0:
            pattern_groups[name] = pattern_groups.get(name, 0) + matches

    top_patterns = sorted(pattern_groups.items(), key=lambda x: -x[1])[:10]

    # Примеры из текста
    examples = []
    for p in phrases.get("phrases", [])[:10]:
        start = max(0, p["start"] - 30)
        end = min(len(text), p["end"] + 30)
        context = text[start:end].replace("\n", " ")
        examples.append({
            "phrase": p["text"],
            "label": p["label"],
            "context": "…" + context.strip() + "…",
        })

    # Рекомендации
    recommendations = []
    if pattern_groups.get("«однако»", 0) > 2:
        recommendations.append("Заменить часть «однако» на «но» или убрать совсем")
    if pattern_groups.get("«важно отметить»", 0) > 0:
        recommendations.append("Убрать «важно отметить» — это штамп")
    if pattern_groups.get("«таким образом»", 0) > 1:
        recommendations.append("Заменить «таким образом» на «значит»")
    if pattern_groups.get("длинное тире", 0) > 5:
        recommendations.append("Заменить часть длинных тире на запятые/точки")
    if combined.get("score", 0) > 50:
        recommendations.append("Прогнать через 🔄 Перефразировать или 🤖 LLM-перефраз")
    if heatmap.get("total", 0) > 5:
        high = sum(1 for p in heatmap["paragraphs"] if p["level"] in ("high", "critical"))
        if high > 0:
            recommendations.append(f"Переписать {high} самых «ИИ-шных» абзацев вручную")

    return {
        "combined": combined,
        "heuristic": heuristic,
        "phrases_count": phrases.get("count", 0),
        "heatmap": heatmap,
        "top_patterns": [{"name": k, "count": v} for k, v in top_patterns],
        "examples": examples,
        "recommendations": recommendations,
        "timestamp": None,  # заполним в endpoint
    }


def report_to_pdf(report: dict, original_text: str) -> bytes:
    """Генерирует PDF с отчётом."""
    if not HAS_FPDF:
        raise RuntimeError("fpdf2 не установлен")

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    font_path = find_cyrillic_font()
    if font_path:
        try:
            pdf.add_font("main", "", font_path)
        except TypeError:
            pdf.add_font("main", "", font_path, uni=True)
        pdf.set_font("main", size=11)
    else:
        pdf.set_font("Helvetica", size=11)

    def _line(text, size=11, style=""):
        pdf.set_x(pdf.l_margin)
        pdf.set_font_size(size)
        pdf.multi_cell(0, 6, text if text else " ")

    combined = report.get("combined", {})
    score = combined.get("score", 0)

    _line("AI Cleaner — Отчёт анализа", size=18)
    pdf.ln(3)
    _line(f"Итоговый балл: {score} / 100", size=14)
    _line(f"Вердикт: {combined.get('label', '—')}", size=12)
    pdf.ln(2)

    if combined.get("combined"):
        _line(f"Эвристики: {combined.get('heuristic_score', 0)} | "
              f"LLM: {combined.get('llm_score', 0)}", size=11)
        pdf.ln(2)

    _line("Топ причин:", size=13)
    for n in report.get("heuristic", {}).get("notes", [])[:10]:
        _line("• " + n, size=10)
    pdf.ln(3)

    patterns = report.get("top_patterns", [])
    if patterns:
        _line("Найденные ИИ-штампы:", size=13)
        for p in patterns[:15]:
            _line(f"• {p['name']} — {p['count']} раз", size=10)
        pdf.ln(3)

    examples = report.get("examples", [])
    if examples:
        _line("Примеры из текста:", size=13)
        for ex in examples[:8]:
            _line(f"→ «{ex['phrase']}» ({ex['label']})", size=10)
            _line(f"   Контекст: {ex['context']}", size=9)
            pdf.ln(1)
        pdf.ln(2)

    recs = report.get("recommendations", [])
    if recs:
        _line("Рекомендации:", size=13)
        for r in recs:
            _line("• " + r, size=10)
        pdf.ln(3)

    heatmap = report.get("heatmap", {})
    if heatmap.get("paragraphs"):
        _line("Карта по абзацам:", size=13)
        for p in heatmap["paragraphs"][:25]:
            _line(f"Абзац {p['index']+1}: {p['score']}/100 "
                  f"({p['level']}, {p['words']} слов)", size=10)

    out = pdf.output(dest="S")
    if isinstance(out, str):
        out = out.encode("latin-1")
    return bytes(out)

# =============================================================
#  РАБОТА С PDF
# =============================================================

def extract_pdf_text(data: bytes) -> str:
    if not HAS_PYPDF:
        raise RuntimeError("pypdf не установлен. Выполни: pip install pypdf")
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    return "\n\n".join(parts).strip()


# Готовые пресеты шрифтов (5 + Auto). Auto — подбираем по имени из оригинала.
PRESET_FONTS = {
    "Auto": None,
    "Arial Unicode": "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "Arial":         "/System/Library/Fonts/Supplemental/Arial.ttf",
    "Times New Roman": "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "Verdana":       "/System/Library/Fonts/Supplemental/Verdana.ttf",
    "Georgia":       "/System/Library/Fonts/Supplemental/Georgia.ttf",
}

# Готовые размеры (5 + Auto)
PRESET_SIZES = [None, 10, 11, 12, 13, 14]  # None == Auto

# Запасные пути для "Auto"-режима по совпадению имени шрифта
_AUTO_FONT_HINTS = [
    ("times",   "/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
    ("georgia", "/System/Library/Fonts/Supplemental/Georgia.ttf"),
    ("verdana", "/System/Library/Fonts/Supplemental/Verdana.ttf"),
    ("arial",   "/System/Library/Fonts/Supplemental/Arial.ttf"),
    ("helvetica", "/System/Library/Fonts/Supplemental/Arial.ttf"),
]

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def pick_font_for_span(span_font_name: str, preset: str):
    """Возвращает путь к .ttf для перерисовки спана.
    preset == 'Auto' → пытаемся подобрать шрифт под оригинал.
    Иначе — используем заданный пресет."""
    if preset and preset != "Auto":
        path = PRESET_FONTS.get(preset)
        if path and Path(path).exists():
            return path
        # пресет выбран, но файл не найден — падаем в Auto
    # Auto-режим: ищем по имени из оригинала
    low = (span_font_name or "").lower()
    for needle, path in _AUTO_FONT_HINTS:
        if needle in low and Path(path).exists():
            return path
    return find_cyrillic_font()


def find_cyrillic_font():
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def build_pdf(text: str) -> bytes:
    if not HAS_FPDF:
        raise RuntimeError("fpdf2 не установлен. Выполни: pip install fpdf2")

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    font_path = find_cyrillic_font()
    if font_path:
        try:
            pdf.add_font("main", "", font_path)
        except TypeError:
            pdf.add_font("main", "", font_path, uni=True)
        pdf.set_font("main", size=11)
        for line in text.split("\n"):
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 6, line if line.strip() else " ")
    else:
        pdf.set_font("Helvetica", size=11)
        for line in text.split("\n"):
            pdf.set_x(pdf.l_margin)
            safe = line.encode("latin-1", errors="replace").decode("latin-1")
            pdf.multi_cell(0, 6, safe or " ")

    out = pdf.output(dest="S")
    if isinstance(out, str):
        out = out.encode("latin-1")
    return bytes(out)

def clean_pdf_preserve(pdf_bytes: bytes,
                       aggressive: bool = False,
                       safe_aggressive: bool = False,
                       font_preset: str = "Auto",
                       size_override=None) -> bytes:
    """Чистит PDF, максимально сохраняя структуру."""
    if not HAS_FITZ:
        raise RuntimeError("PyMuPDF не установлен. Выполни: pip install pymupdf")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_cleaned = 0

    for page in doc:
        text_dict = page.get_text("dict")
        to_clean = []
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    original = span["text"]
                    cleaned = INVISIBLE.sub("", original)
                    cleaned = unicodedata.normalize("NFKC", cleaned)
                    cleaned = cleaned.translate(TYPO_MAP)
                    if aggressive:
                        cleaned = cleaned.translate(LOOKALIKE_MAP)
                    if safe_aggressive:
                        cleaned = clean_safe_aggressive(cleaned)
                    if cleaned != original:
                        to_clean.append((span, cleaned, original))

        if not to_clean:
            continue
        total_cleaned += len(to_clean)

        # 1. Стираем старые спаны
        for span, _, _ in to_clean:
            try:
                rect = fitz.Rect(span["bbox"])
                page.add_redact_annot(rect)
            except Exception:
                pass
        try:
            page.apply_redactions(images=0)
        except TypeError:
            page.apply_redactions()

        # 2. Перерисовываем с сохранением ВСЕХ свойств
        for span, cleaned, original in to_clean:
            try:
                origin = span["origin"]
                size = size_override if size_override else span.get("size", 11)

                # цвет: RGB из int
                color_int = span.get("color", 0)
                r = ((color_int >> 16) & 255) / 255
                g = ((color_int >> 8) & 255) / 255
                b = (color_int & 255) / 255

                # шрифт: авто или пресет
                font_path = pick_font_for_span(span.get("font", ""), font_preset)
                font_name = span.get("font", "").lower()

                # флаги шрифта: bold, italic
                flags = span.get("flags", 0)
                is_bold = bool(flags & 16)   # 2^4
                is_italic = bool(flags & 2)  # 2^1

                kwargs = dict(fontsize=size, color=(r, g, b))

                # PyMuPDF встроенные шрифты с bold/italic вариантами
                if font_path:
                    kwargs["fontname"] = "cleanfont"
                    kwargs["fontfile"] = font_path
                else:
                    base = "helv"
                    if "times" in font_name:
                        base = "tiro"
                    elif "courier" in font_name or "mono" in font_name:
                        base = "cour"
                    if is_bold and is_italic:
                        base += "bi"
                    elif is_bold:
                        base += "b"
                    elif is_italic:
                        base += "i"
                    kwargs["fontname"] = base

                page.insert_text(fitz.Point(*origin), cleaned, **kwargs)
            except Exception:
                pass

    # Чистим метаданные
    try:
        doc.set_metadata({
            "title": "", "author": "", "subject": "",
            "keywords": "", "creator": "AI Cleaner", "producer": "AI Cleaner",
        })
    except Exception:
        pass
    try:
        doc.del_xml_metadata()
    except Exception:
        pass

    return doc.tobytes(garbage=4, deflate=True)

# HTML читается из внешнего файла index.html рядом со скриптом
HTML_PATH = Path(__file__).parent / "index.html"

def load_html() -> bytes:
    return HTML_PATH.read_text(encoding="utf-8").encode("utf-8")

def extract_docx_text(data: bytes) -> str:
    """Извлекает текст из .docx, сохраняя порядок абзацев."""
    if not HAS_DOCX:
        raise RuntimeError("python-docx не установлен. Выполни: pip install python-docx")
    doc = Document(io.BytesIO(data))
    parts = []
    for para in doc.paragraphs:
        parts.append(para.text)
    return "\n".join(parts)

def clean_docx_preserve(data: bytes, aggressive: bool = False, safe_aggressive: bool = False) -> bytes:
    """Чистит .docx, сохраняя структуру максимально: абзацы, таблицы,
    колонтитулы, сноски, комментарии, стили."""
    if not HAS_DOCX:
        raise RuntimeError("python-docx не установлен. Выполни: pip install python-docx")

    doc = Document(io.BytesIO(data))
    touched = 0

    def _clean(s):
        if not s:
            return s
        t = INVISIBLE.sub("", s)
        t = unicodedata.normalize("NFKC", t)
        t = t.translate(TYPO_MAP)
        if aggressive:
            t = t.translate(LOOKALIKE_MAP)
        if safe_aggressive:
            t = clean_safe_aggressive(t)
        return t

    def _clean_runs_in_paragraph(para):
        nonlocal touched
        for run in para.runs:
            original = run.text
            if not original:
                continue
            cleaned = _clean(original)
            if cleaned != original:
                run.text = cleaned
                touched += 1

    # 1. Обычные абзацы
    for para in doc.paragraphs:
        _clean_runs_in_paragraph(para)

    # 2. Таблицы (включая вложенные)
    def _walk_table(table):
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _clean_runs_in_paragraph(para)
                for nested in cell.tables:
                    _walk_table(nested)

    for table in doc.tables:
        _walk_table(table)

    # 3. Колонтитулы (секции)
    for section in doc.sections:
        for header_footer in [section.header, section.footer,
                              section.first_page_header, section.first_page_footer,
                              section.even_page_header, section.even_page_footer]:
            try:
                for para in header_footer.paragraphs:
                    _clean_runs_in_paragraph(para)
                for table in header_footer.tables:
                    _walk_table(table)
            except Exception:
                pass

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

def build_docx(text: str) -> bytes:
    """Создаёт новый .docx из текста."""
    if not HAS_DOCX:
        raise RuntimeError("python-docx не установлен. Выполни: pip install python-docx")
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".html", ".htm",
    ".xml", ".yaml", ".yml", ".log", ".srt", ".py", ".js", ".ts",
}


def batch_clean(files: list, aggressive: bool = False, safe_aggressive: bool = False) -> tuple:
    """files: [{name, data (base64)}].
    Возвращает (zip_bytes, report)."""
    out = io.BytesIO()
    report = []

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in files:
            name = item.get("name", "file")
            try:
                raw = base64.b64decode(item.get("data", ""))
            except Exception as e:
                report.append({"name": name, "status": "skip", "reason": f"base64: {e}"})
                continue

            ext = Path(name).suffix.lower()
            try:
                if ext == ".pdf":
                    result = clean_pdf_preserve(raw, aggressive=aggressive, safe_aggressive=safe_aggressive)
                    zf.writestr(f"clean/{name}", result)
                    report.append({"name": name, "status": "ok", "type": "pdf"})

                elif ext == ".docx":
                    result = clean_docx_preserve(raw, aggressive=aggressive, safe_aggressive=safe_aggressive)
                    zf.writestr(f"clean/{name}", result)
                    report.append({"name": name, "status": "ok", "type": "docx"})

                elif ext in TEXT_EXTS:
                    text = raw.decode("utf-8", errors="replace")
                    cleaned = clean_text(text, aggressive=aggressive, safe_aggressive=safe_aggressive)["cleaned"]
                    zf.writestr(f"clean/{name}", cleaned.encode("utf-8"))
                    report.append({"name": name, "status": "ok", "type": "text"})

                else:
                    report.append({"name": name, "status": "skip",
                                   "reason": f"неподдерживаемый формат: {ext}"})
            except Exception as e:
                report.append({"name": name, "status": "error", "reason": str(e)})

        zf.writestr("clean/_report.json",
                    json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))

    return out.getvalue(), report

# =============================================================
#  HTTP-СЕРВЕР
# =============================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # CORS — разрешаем запросы из нативного окна (origin = null)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # клиент отключился до ответа — это не ошибка
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, load_html())
        elif self.path == "/ping":
            self._json(200, {
                "pdf_read": HAS_PYPDF,
                "pdf_write": HAS_FPDF,
                "pdf_clean": HAS_FITZ,
                "docx": HAS_DOCX,
                "ollama": HAS_OLLAMA,
                "ollama_model": OLLAMA_MODEL if HAS_OLLAMA else None,
            })
        elif self.path == "/ollama-status":
            model = OLLAMA_MODEL
            installed = check_ollama_installed()
            running = check_ollama_running()
            model_ready = check_model_installed(model) if installed and running else False
            self._json(200, {
                "installed": installed,
                "running": running,
                "model_ready": model_ready,
                "model_name": model,
                "platform": platform.system(),
                "binary": _which_ollama() or None,
            })    
        elif self.path == "/version":
            self._json(200, {"version": APP_VERSION})

        elif self.path == "/check-update":
            self._json(200, check_for_update())
        else:
            self._send(404, b"not found", "text/plain")

    def do_OPTIONS(self):
        """Preflight-запрос от браузера/WebView. Отвечаем «можно»."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if self.path == "/clean":
            try:
                data = json.loads(raw)
                result = clean_text(
                    data.get("text", ""),
                    aggressive=bool(data.get("aggressive", False)),
                    safe_aggressive=bool(data.get("safe_aggressive", False)),
                )
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/check-ai":
            try:
                data = json.loads(raw)
                result = check_ai_score(data.get("text", ""))
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/check-ai-llm":
            try:
                data = json.loads(raw)
                result = check_ai_with_llm(data.get("text", ""))
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/paraphrase-llm-stream":
            try:
                data = json.loads(raw)
                text = data.get("text", "")
                intensity = int(data.get("intensity", 5))
                style = data.get("style", "neutral")

                # SSE-ответ
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                for event in paraphrase_stream_generator(text, intensity, style):
                    line = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    try:
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        # пользователь нажал «Стоп» — просто выходим
                        print("[stream] клиент отключился")
                        return
            except Exception as e:
                traceback.print_exc()
                try:
                    err = f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"
                    self.wfile.write(err.encode("utf-8"))
                except Exception:
                    pass

        elif self.path == "/highlight":
            try:
                data = json.loads(raw)
                result = find_ai_sentences(data.get("text", ""))
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/extract-pdf":
            try:
                data = json.loads(raw)
                pdf_bytes = base64.b64decode(data["data"])
                text = extract_pdf_text(pdf_bytes)
                pages = 0
                if HAS_PYPDF:
                    pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
                self._json(200, {"text": text, "pages": pages})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/build-pdf":
            try:
                data = json.loads(raw)
                pdf_bytes = build_pdf(data.get("text", ""))
                self._send(200, pdf_bytes, "application/pdf")
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/clean-pdf":
            try:
                data = json.loads(raw)
                pdf_bytes = base64.b64decode(data["data"])
                result = clean_pdf_preserve(
                    pdf_bytes,
                    aggressive=bool(data.get("aggressive", False)),
                    font_preset=data.get("font", "Auto"),
                    size_override=data.get("size") or None,
                )
                self._send(200, result, "application/pdf")
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/extract-docx":
            try:
                data = json.loads(raw)
                docx_bytes = base64.b64decode(data["data"])
                text = extract_docx_text(docx_bytes)
                self._json(200, {"text": text})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/clean-docx":
            try:
                data = json.loads(raw)
                docx_bytes = base64.b64decode(data["data"])
                result = clean_docx_preserve(
                    docx_bytes,
                    aggressive=bool(data.get("aggressive", False)),
                )
                self._send(200, result,
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/paraphrase":
            try:
                data = json.loads(raw)
                result = paraphrase_stamps(
                    data.get("text", ""),
                    intensity=int(data.get("intensity", 5)),
                    multi_pass=bool(data.get("multi_pass", False)),
                )
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/diff":
            try:
                data = json.loads(raw)
                result = diff_words_html(
                    data.get("before", ""),
                    data.get("after", ""),
                )
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/batch-clean":
            try:
                data = json.loads(raw)
                files = data.get("files", [])
                aggressive = bool(data.get("aggressive", False))
                safe_aggressive = bool(data.get("safe_aggressive", False))
                print(f"[batch] получено файлов: {len(files)}")
                zip_bytes, report = batch_clean(files, aggressive, safe_aggressive)
                print(f"[batch] zip: {len(zip_bytes)} байт, "
                      f"ok={sum(1 for r in report if r['status']=='ok')}, "
                      f"err={sum(1 for r in report if r['status']=='error')}, "
                      f"skip={sum(1 for r in report if r['status']=='skip')}")
                self._json(200, {
                    "zip": base64.b64encode(zip_bytes).decode("ascii"),
                    "report": report,
                    "total": len(files),
                    "ok": sum(1 for r in report if r["status"] == "ok"),
                    "errors": sum(1 for r in report if r["status"] == "error"),
                    "skipped": sum(1 for r in report if r["status"] == "skip"),
                })
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/install-ollama":
            try:
                result = download_ollama_installer()
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/launch-ollama":
            ok = launch_ollama_app()
            self._json(200, {"launched": ok})

        elif self.path == "/pull-model":
            try:
                data = json.loads(raw)
                model = data.get("model", OLLAMA_MODEL)
                result = pull_model(model)
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/highlight-phrases":
            try:
                data = json.loads(raw)
                result = find_ai_phrases(data.get("text", ""))
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})        

        elif self.path == "/check-ai-combined":
            try:
                data = json.loads(raw)
                result = check_ai_combined(data.get("text", ""))
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/paragraph-heatmap":
            try:
                data = json.loads(raw)
                result = paragraph_heatmap(data.get("text", ""))
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/report":
            try:
                data = json.loads(raw)
                text = data.get("text", "")
                result = generate_report(text)
                import datetime
                result["timestamp"] = datetime.datetime.now().isoformat()
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        elif self.path == "/report-pdf":
            try:
                data = json.loads(raw)
                text = data.get("text", "")
                report = generate_report(text)
                pdf_bytes = report_to_pdf(report, text)
                self._send(200, pdf_bytes, "application/pdf")
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})        

        else:
            self._send(404, b"not found", "text/plain")

# =============================================================
#  УПРАВЛЕНИЕ OLLAMA (установка, запуск, скачивание модели)
# =============================================================

def _which_ollama() -> str | None:
    """Возвращает путь к бинарнику ollama, если он установлен."""
    # 1. Стандартный путь через PATH
    p = shutil.which("ollama")
    if p:
        return p
    # 2. macOS: встроен в Ollama.app
    if platform.system() == "Darwin":
        candidates = [
            "/Applications/Ollama.app/Contents/Resources/ollama",
            str(Path.home() / "Applications/Ollama.app/Contents/Resources/ollama"),
        ]
        for c in candidates:
            if Path(c).exists():
                return c
    # 3. Windows: обычно уже в PATH, но проверим AppData
    if platform.system() == "Windows":
        candidate = Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"
        if candidate.exists():
            return str(candidate)
    return None


def check_ollama_installed() -> bool:
    return _which_ollama() is not None


def check_ollama_running() -> bool:
    """Ollama слушает на 127.0.0.1:11434."""
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.5):
            return True
    except OSError:
        return False


_model_cache = {"time": 0, "result": False, "model": None}

def check_model_installed(model: str) -> bool:
    """Проверяет, что модель скачана, через `ollama list`.
    Кэширует результат на 30 секунд, чтобы не дёргать subprocess постоянно."""
    import time as _time
    now = _time.time()
    if (now - _model_cache["time"] < 30
            and _model_cache["model"] == model):
        return _model_cache["result"]

    if not check_ollama_installed():
        _model_cache.update(time=now, result=False, model=model)
        return False

    binary = _which_ollama()
    try:
        result = subprocess.run(
            [binary, "list"],
            capture_output=True, text=True, timeout=5,
        )
        found = model in result.stdout
        _model_cache.update(time=now, result=found, model=model)
        return found
    except Exception:
        _model_cache.update(time=now, result=False, model=model)
        return False


def launch_ollama_app() -> bool:
    """Запускает Ollama на macOS или Windows."""
    system = platform.system()
    try:
        if system == "Darwin":
            # Ollama.app запускается через open
            for path in [
                "/Applications/Ollama.app",
                str(Path.home() / "Applications/Ollama.app"),
            ]:
                if Path(path).exists():
                    subprocess.Popen(["open", "-a", path])
                    return True
            return False
        elif system == "Windows":
            binary = _which_ollama()
            if binary:
                subprocess.Popen([binary, "serve"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                return True
        return False
    except Exception as e:
        print(f"[ollama] launch failed: {e}")
        return False


def download_ollama_installer() -> dict:
    """Скачивает установщик Ollama для текущей ОС и запускает его."""
    system = platform.system()

    if system == "Darwin":
        url = "https://ollama.com/download/Ollama-darwin.zip"
        filename = "Ollama-darwin.zip"
    elif system == "Windows":
        url = "https://ollama.com/download/OllamaSetup.exe"
        filename = "OllamaSetup.exe"
    else:
        return {"error": f"ОС {system} не поддерживается автоматической установкой",
                "manual_url": "https://ollama.com/download"}

    downloads = Path.home() / "Downloads"
    downloads.mkdir(exist_ok=True)
    target = downloads / filename

    print(f"[ollama] скачиваю {url} → {target}")
    try:
        urllib.request.urlretrieve(url, target)
    except Exception as e:
        return {"error": f"не удалось скачать: {e}", "manual_url": url}

    size_mb = target.stat().st_size // 1024 // 1024
    print(f"[ollama] скачано {size_mb} МБ")

    # Сразу запускаем установку
    try:
        if system == "Darwin":
            # Распаковываем через ditto — сохраняет всё правильно
            subprocess.Popen(["open", str(target)])
            message = ("Архив скачан в Загрузки и открыт. "
                       "Распакуйте его и перетащите Ollama.app в Программы.")
        elif system == "Windows":
            subprocess.Popen([str(target)], shell=True)
            message = "Запущен установщик. Следуйте инструкциям на экране."
    except Exception as e:
        return {"error": f"скачано, но не удалось запустить: {e}",
                "path": str(target)}

    return {
        "status": "downloaded",
        "path": str(target),
        "size_mb": size_mb,
        "message": message,
    }


def pull_model(model: str) -> dict:
    """Скачивает модель через ollama pull. Блокирующий, 2–10 минут."""
    binary = _which_ollama()
    if not binary:
        return {"error": "Ollama не установлена"}

    print(f"[ollama] ollama pull {model}")
    try:
        result = subprocess.run(
            [binary, "pull", model],
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            return {"error": result.stderr or "не удалось скачать модель"}
        return {"status": "ok", "model": model}
    except subprocess.TimeoutExpired:
        return {"error": "таймаут (больше 30 минут)"}
    except Exception as e:
        return {"error": str(e)}

APP_VERSION = "2.5.1"

def check_for_update() -> dict:
    """Проверяет GitHub Releases на новую версию."""
    try:
        url = "https://api.github.com/repos/nenenzn719-droid/ai_cleaner/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "AI-Cleaner"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))

        latest = data.get("tag_name", "").lstrip("v")
        html_url = data.get("html_url", "")

        def _ver_tuple(v):
            try:
                return tuple(int(x) for x in v.split("."))
            except Exception:
                return (0,)

        if _ver_tuple(latest) > _ver_tuple(APP_VERSION):
            return {
                "update_available": True,
                "current": APP_VERSION,
                "latest": latest,
                "url": html_url,
            }
        return {
            "update_available": False,
            "current": APP_VERSION,
            "latest": latest,
        }
    except Exception as e:
        return {"error": str(e), "current": APP_VERSION}

def find_port(start=8765):
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("не нашёл свободный порт")


def main():
    port = find_port()
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    print("─" * 52)
    print("  ✨  AI Cleaner запущен")
    print(f"  🌐  Открой:  {url}")
    print(f"  🛑  Стоп:    Ctrl+C")
    print(f"  📄  PDF:     читать {'✅' if HAS_PYPDF else '❌'}"
          f"  писать {'✅' if HAS_FPDF else '❌'}"
          f"  чистить {'✅' if HAS_FITZ else '❌ (pip install pymupdf)'}")
    print("─" * 52)

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        server.shutdown()


if __name__ == "__main__":
    main()