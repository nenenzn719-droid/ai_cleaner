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


def analyze(text: str) -> dict:
    return {
        "invisible": len(INVISIBLE.findall(text)),
        "typography": sum(1 for c in text if c in TYPOGRAPHY),
        "length": len(text),
    }


def clean_text(text: str, aggressive: bool = False) -> dict:
    before = analyze(text)
    t = INVISIBLE.sub("", text)
    t = unicodedata.normalize("NFKC", t)
    t = t.translate(TYPO_MAP)
    if aggressive:
        t = t.translate(LOOKALIKE_MAP)
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

STAMP_RULES = [
    # «не просто X, а Y» → «X, и даже Y»
    (r"\bне\s+просто\s+([^,.!?]{2,60}?),\s*а\s+", r"\1, и даже "),
    (r"\bне\s+только\s+([^,.!?]{2,60}?),\s*но\s+и\s+", r"\1 и даже "),
    (r"\bне\s+столько\s+([^,.!?]{2,60}?),\s*сколько\s+", r"не только \1, но и "),
    (r"\bэто\s+не\s+([а-яё]+),\s*а\s+", r"это \1, и даже "),

    # Клише-вводные
    (r"\bоднако\b", "но"),
    (r"\bболее\s+того\b", "к тому же"),
    (r"\bтаким\s+образом\b", "значит"),
    (r"\bв\s+свою\s+очередь\b", ""),
    (r"\bв\s+конечном\s+счёте\b", "в итоге"),
    (r"\bв\s+современном\s+мире\b", "сегодня"),
    (r"\bна\s+сегодняшний\s+день\b", "сейчас"),
    (r"\bважно\s+отметить,?\s*что\b", "замечу:"),
    (r"\bстоит\s+отметить,?\s*что\b", "замечу:"),
    (r"\bследует\s+отметить,?\s*что\b", "замечу:"),
    (r"\bважно\s+подчеркнуть,?\s*что\b", "подчеркну:"),
    (r"\bиграет\s+важную\s+роль\b", "сильно влияет"),
    (r"\bиграет\s+ключевую\s+роль\b", "определяет"),
    (r"\bиграет\s+значимую\s+роль\b", "заметно влияет"),
    (r"\bпредставляет\s+собой\b", "это"),
    (r"\bявляется\s+ключевым\b", "ключевой"),
    (r"\bявляется\s+важным\b", "важный"),
    (r"\bявляется\s+одним\s+из\b", "один из"),
    (r"\bперестал\s+быть\s+просто\b", "перестал быть только"),
    (r"\bпревратил(ся|ась|ось)\s+в\b", r"стал \1 в"),
    (r"\bи\s+в\s+этом\s+его\s+сила\b", "и вот где он силён"),
    (r"\bи\s+в\s+этом\s+его\s+слабость\b", "и вот где он слаб"),
    (r"\bи\s+в\s+этом\s+его\s+предел\b", "и здесь его предел"),
    (r"\bвыбор\s+остаётся\s+за\b", "решает"),
    (r"\bименно\s+поэтому\b", "поэтому"),
    (r"\bответ,?\s+вероятно,?\s+в\s+том,?\s*что\b", "скорее всего,"),
    (r"\bответ,?\s+возможно,?\s+в\s+том,?\s*что\b", "возможно,"),
    (r"\bбудущее\s+([а-яё]+)\s+зависит\s+не\s+столько\s+от\s+([^,.!?]{2,40}?),?\s*сколько\s+от\s+", r"\1 зависит не от \2, а от "),
    (r"\bглавный\s+урок\b", "урок"),
    (r"\bглавный\s+вывод\b", "вывод"),
    (r"\bглавная\s+мысль\b", "мысль"),
    (r"\bсегодня\s+же\b", "уже сегодня"),
    (r"\bи\s+главное,?\b", "и вот что важно:"),

    # === НОВЫЕ ПРАВИЛА — канцелярит ===
    (r"\bданный\b", "этот"),
    (r"\bданная\b", "эта"),
    (r"\bданное\b", "это"),
    (r"\bданные\b", "эти"),
    (r"\bосуществля(ет|ют|ем|ешь)\b", r"дела\1"),
    (r"\bосуществля(ть|ться|ется)\b", r"дела\1"),
    (r"\bфункциониру(ет|ют|ешь)\b", r"работа\1"),
    (r"\bфункционирова(ть|ние)\b", r"работа\1"),
    (r"\bв\s+целях\b", "чтобы"),
    (r"\bв\s+связи\s+с\s+тем,?\s*что\b", "потому что"),
    (r"\bввиду\s+того,?\s*что\b", "так как"),
    (r"\bв\s+области\b", "в"),
    (r"\bпутём\b", "через"),
    (r"\bс\s+помощью\b", "через"),
    (r"\bв\s+результате\b", "из-за"),
    (r"\bна\s+основании\b", "по"),
    (r"\bв\s+соответствии\s+с\b", "по"),
    (r"\bдля\s+того\s+чтобы\b", "чтобы"),
    (r"\bв\s+качестве\b", "как"),
    (r"\bв\s+течение\b", "за"),
    (r"\bна\s+протяжении\b", "за"),
    (r"\bв\s+процессе\b", "во время"),
    (r"\bпо\s+отношению\s+к\b", "к"),
    (r"\bпо\s+сравнению\s+с\b", "чем"),
    (r"\bв\s+отличие\s+от\b", "не как"),
    (r"\bпо\s+мнению\b", "считают"),
    (r"\bс\s+точки\s+зрения\b", "по"),
    (r"\bв\s+рамках\b", "в"),
    (r"\bна\s+данный\s+момент\b", "сейчас"),
    (r"\bв\s+настоящее\s+время\b", "сейчас"),
    (r"\bимеет\s+место\s+быть\b", "происходит"),
    (r"\bносит\s+характер\b", "похоже на"),

    # Тире → запятая в правильном контексте
    (r"\s+—\s+([а-яё])", r", \1"),
    (r"(\S)\s+—\s+(\S)", r"\1 — \2"),
]


def _apply_case(source: str, replacement: str) -> str:
    """Сохраняет регистр первого символа."""
    if not replacement:
        return replacement
    if source and source[0].isupper() and replacement[0].islower():
        return replacement[0].upper() + replacement[1:]
    return replacement


def paraphrase_stamps(text: str) -> dict:
    """Заменяет штампы и клише на живые обороты. Возвращает текст и отчёт."""
    original = text
    applied = []

    for pattern, replacement in STAMP_RULES:
        # замена с сохранением регистра для первой буквы
        def repl(m, _r=replacement):
            result = m.expand(_r)
            return _apply_case(m.group(0), result)

        new_text, count = re.subn(pattern, repl, text, flags=re.IGNORECASE)
        if count > 0:
            applied.append({
                "pattern": pattern,
                "count": count,
            })
            text = new_text

    # косметика: лишние пробелы, пробелы перед запятыми/точками, двойные заглавные
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"^\s*,\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"([.!?])\s*([а-яё])", lambda m: m.group(1) + " " + m.group(2).upper(), text)

    # предлоги/союзы с большой буквы внутри предложения — оставим как есть
    # (это редкий случай, лучше не трогать)

    return {
        "paraphrased": text,
        "replacements": len(applied),
        "total_swaps": sum(a["count"] for a in applied),
        "details": applied[:10],  # первые 10 для отчёта
        "chars_before": len(original),
        "chars_after": len(text),
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
    (r"чем\s+[^,.!?]{2,40},\s*тем\s", 5, "«чем…, тем…»"),
    (r"\bне\s+прост[оа][йа]?\s+[^,.!?]{2,30},?\s+а\s+", 5, "«не просто X, а Y»"),
    (r"\bне\s+столько\s+[^,.!?]{2,30},?\s+сколько\b", 5, "«не столько X, сколько Y»"),
    (r"\bне\s+только\s+[^,.!?]{2,30},?\s+но\s+и\b", 4, "«не только X, но и Y»"),
    (r"\bэто\s+не\s+[а-яё]+,\s*а\s+[а-яё]+", 5, "«это не X, а Y»"),
    (r"\bперестал\s+быть\s+просто\b", 5, "«перестал быть просто…»"),
    (r"\bпревратил(ся|ась|ось)\s+в\b", 3, "«превратился в»"),
    (r"\bот\s+[а-яё]{4,}\s+до\s+[а-яё]{4,}", 4, "«от X до Y»"),
    (r"\bи\s+в\s+этом\s+(его|её|их|наш|ваш|весь|вся|всё)\b", 6, "«и в этом его…»"),
    (r"\bвыбор\s+остаётся\s+за\b", 5, "«выбор остаётся за…»"),
    (r"\bответ,?\s+(вероятно|возможно|очевидно|прост)\b", 5, "«ответ, вероятно…»"),
    (r"\bбудущее\s+\S+\s+зависит\b", 5, "«будущее X зависит…»"),
    (r"\bглавн[а-яё]+\s+(урок|вывод|идея|мысль|принцип|вопрос)\b", 5, "«главный урок…»"),
    (r"\bсегодня\s+же\b", 5, "«сегодня же»"),
    (r"\bи\s+главное\b", 5, "«и главное»"),
    (r"\bоднако\b", 2, "«однако»"),
    (r"\bболее\s+того\b", 3, "«более того»"),
    (r"\bтаким\s+образом\b", 3, "«таким образом»"),
    (r"\bв\s+свою\s+очередь\b", 3, "«в свою очередь»"),
    (r"\bв\s+конечном\s+счёте\b", 3, "«в конечном счёте»"),
    (r"\bименно\s+поэтому\b", 4, "«именно поэтому»"),
    (r"\bследует\s+(отметить|подчеркнуть|заметить)\b", 4, "«следует отметить»"),
    (r"\bважно\s+(отметить|понимать|помнить|подчеркнуть)\b", 5, "«важно отметить»"),
    (r"\bстоит\s+(отметить|подчеркнуть|заметить)\b", 4, "«стоит отметить»"),
    (r"\bиграет\s+(важную|ключевую|значимую)\s+роль\b", 5, "«играет важную роль»"),
    (r"\bв\s+современном\s+мире\b", 5, "«в современном мире»"),
    (r"\bна\s+сегодняшний\s+день\b", 3, "«на сегодняшний день»"),
    (r"\bпредставляет\s+собой\b", 3, "«представляет собой»"),
    (r"\bявляется\s+(ключевым|важным|основным|одним)\b", 3, "«является ключевым»"),
    (r"\bнезаметно\s+стал\b", 4, "«незаметно стал»"),
    (r"\b(формируется|складывается)\s+(современн|нов|целостн)", 3, "«формируется современная…»"),
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

def check_ai_score(text: str) -> dict:
    """Расширенная эвристическая оценка «ИИ-манеры». 0-100."""
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

    # ---- 1. Разброс длины предложений ----
    sent_lens = [len(_words(s)) for s in sentences]
    avg_sent = sum(sent_lens) / len(sent_lens)
    sd = _stdev(sent_lens)
    cv = sd / avg_sent if avg_sent else 0
    if cv < 0.3:
        score += 12; notes.append(f"Очень однородные предложения (cv={cv:.2f})")
    elif cv < 0.45:
        score += 8; notes.append(f"Умеренно однородные предложения (cv={cv:.2f})")
    elif cv < 0.6:
        score += 3
    else:
        notes.append(f"Живой разброс предложений (cv={cv:.2f})")

    # ---- 2. Средняя длина слова ----
    avg_wl = sum(len(w) for w in words) / len(words)
    if avg_wl > 7.5:
        score += 10; notes.append(f"Очень длинные слова ({avg_wl:.1f} букв)")
    elif avg_wl > 6.5:
        score += 5; notes.append(f"Длинные слова ({avg_wl:.1f} букв)")

    # ---- 3. Лексическое разнообразие (Guiraud index) ----
    uniq = len(set(words))
    guiraud = uniq / math.sqrt(len(words)) if words else 0
    if guiraud < 8:
        score += 8; notes.append(f"Низкое разнообразие лексики (Guiraud={guiraud:.1f})")
    elif guiraud < 12:
        score += 4

    # ---- 4. ПАТТЕРНЫ ИИ (расширенный список) ----
    pattern_hits = []
    pattern_score = 0
    for rx, weight, name in AI_PATTERNS:
        matches = re.findall(rx, low)
        if matches:
            pattern_score += weight * min(len(matches), 3)
            pattern_hits.append(f"{name}×{len(matches)}")
    pattern_score = min(pattern_score, 50)
    score += pattern_score
    if pattern_hits:
        notes.append(f"Паттерны ИИ (+{pattern_score}): " + "; ".join(pattern_hits[:10]))
    else:
        notes.append("Характерных паттернов ИИ не найдено")

    # ---- 5. Длинные тире ----
    em_dashes = clean.count("—") + clean.count("–")
    dash_ratio = em_dashes / len(sentences)
    if dash_ratio > 0.35:
        score += 10; notes.append(f"Много длинных тире ({em_dashes})")
    elif dash_ratio > 0.15:
        score += 4

    # ---- 6. Плотность запятых и союзов ----
    comma_density = clean.count(",") / len(sentences)
    if comma_density > 4:
        score += 8; notes.append(f"Плотные запятые ({comma_density:.1f}/предл.)")
    elif comma_density > 3:
        score += 4

    # ---- 7. Абстрактная лексика ----
    abstract_count = sum(1 for w in words
                         if any(w.startswith(s) for s in ABSTRACT_STEMS))
    abs_density = abstract_count / len(words) * 100
    if abs_density > 14:
        score += 12; notes.append(f"Очень много абстрактных слов ({abs_density:.0f}%)")
    elif abs_density > 9:
        score += 6; notes.append(f"Много абстрактных слов ({abs_density:.0f}%)")

    # ---- 8. Пассивные конструкции и модальность ----
    passive = len(re.findall(r"\b(является|представляет|осуществляется|проводится|выполняется|был[аио]?\s+\w+н[ыоа])\b", low))
    modal = len(re.findall(r"\b(важно|необходимо|следует|нужно|стоит|должен|может|позволяет)\b", low))
    modal_ratio = modal / max(1, len(sentences))
    if passive > 3:
        score += 8; notes.append(f"Пассивные конструкции ({passive})")
    if modal_ratio > 0.7:
        score += 6; notes.append(f"Модальные глаголы ({modal_ratio:.1f}/предл.)")

    # ---- 9. Триады ----
    triads = len(re.findall(r"[а-яё]{3,},\s+[а-яё]{3,}\s+(и|или)\s+[а-яё]{3,}", low))
    if triads >= 3:
        score += 8; notes.append(f"Перечисления-триады ({triads})")
    elif triads >= 1:
        score += 3

    # ---- 10. Повторы начал ----
    firsts = []
    for s in sentences:
        w = _words(s)
        if w: firsts.append(w[0])
    if firsts:
        repeats = len(firsts) - len(set(firsts))
        ratio = repeats / len(firsts)
        if ratio > 0.35:
            score += 8; notes.append(f"Сильные повторы начал ({repeats}/{len(firsts)})")
        elif ratio > 0.2:
            score += 4

    # ---- 11. Однородные абзацы ----
    paragraphs = [p for p in re.split(r"\n\s*\n", clean) if p.strip()]
    if len(paragraphs) >= 3:
        plens = [len(_words(p)) for p in paragraphs]
        pavg = sum(plens) / len(plens)
        psd = _stdev(plens)
        pcv = psd / pavg if pavg else 0
        if pcv < 0.3:
            score += 8; notes.append(f"Абзацы почти одинаковые (cv={pcv:.2f})")
        elif pcv < 0.45:
            score += 4

    # ---- 12. Личные местоимения (у ИИ их меньше) ----
    personal = len(re.findall(r"\b(я|мы|мне|нам|меня|нас|мой|наш|моя|наша)\b", low))
    personal_ratio = personal / max(1, len(sentences))
    if personal_ratio < 0.05 and len(sentences) > 5:
        score += 6; notes.append("Мало личных местоимений (безликий стиль)")

    # ---- 13. N-граммы (частотные ИИ-обороты) ----
    ai_bigrams = [
        "важно отметить", "следует отметить", "таким образом", "более того",
        "кроме того", "в свою очередь", "в конечном счёте", "на сегодняшний",
        "в современном мире", "данный подход", "играет важную", "необходимо учитывать",
        "стоит подчеркнуть", "в данной статье", "в заключение", "в рамках данного",
    ]
    bigram_hits = sum(1 for b in ai_bigrams if b in low)
    if bigram_hits >= 3:
        score += 10; notes.append(f"Много ИИ-штампов ({bigram_hits})")
    elif bigram_hits >= 1:
        score += 4

    # ---- ШТРАФЫ ----
    casual_count = 0
    for c in ["блин", "короче", "прикольно", "классно", "хз",
              "щас", "вообще-то", "как-то", "ну,"]:
        casual_count += len(re.findall(r"\b" + re.escape(c.strip().rstrip(",")) + r"\b", low))
    if casual_count > 0:
        penalty = min(15, casual_count * 3)
        score -= penalty
        notes.append(f"Разговорные обороты: −{penalty}")

    emoji_count = sum(1 for c in text if ord(c) > 0x1F000)
    if emoji_count > 0:
        score -= min(10, emoji_count)
        notes.append(f"Эмодзи: −{min(10, emoji_count)}")

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
            "ср. длина предл.": round(avg_sent, 1),
            "разброс предл.": round(cv, 2),
            "ср. длина слова": round(avg_wl, 2),
            "разнообразие (Guiraud)": round(guiraud, 1),
            "паттернов ИИ": len(pattern_hits),
            "длинных тире": em_dashes,
            "абстрактных": f"{abs_density:.0f}%",
            "пассивных": passive,
            "модальных": modal,
        },
        "notes": notes,
    }

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

def paraphrase_with_llm(text: str) -> dict:
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

def paraphrase_stream_generator(text: str, intensity: int = 5):
    """Генератор для SSE-стриминга перефраза.
    yield-ит словари: {"type": "progress"|"token"|"end_chunk"|"done"|"error", ...}"""
    if not HAS_OLLAMA:
        yield {"type": "error", "message": "Ollama не установлена"}
        return

    paragraphs = [p for p in text.split("\n") if p.strip()]
    total = len(paragraphs)

    if intensity <= 3:
        style_hint = "Замени только штампы и канцелярит. Остальное не трогай."
    elif intensity <= 7:
        style_hint = "Перепиши текст другими словами, сохранив смысл."
    else:
        style_hint = "Полностью перепиши текст, измени структуру, но сохрани факты."

    system_prompt = (
        "Ты — редактор. Отвечаешь только готовым текстом, без пояснений. "
        f"{style_hint}"
    )

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
                    "temperature": 0.3 if intensity <= 3 else 0.7 if intensity <= 7 else 1.0,
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

def clean_docx_preserve(data: bytes, aggressive: bool = False) -> bytes:
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


def batch_clean(files: list, aggressive: bool = False) -> tuple:
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
                    result = clean_pdf_preserve(raw, aggressive=aggressive)
                    zf.writestr(f"clean/{name}", result)
                    report.append({"name": name, "status": "ok", "type": "pdf"})

                elif ext == ".docx":
                    result = clean_docx_preserve(raw, aggressive=aggressive)
                    zf.writestr(f"clean/{name}", result)
                    report.append({"name": name, "status": "ok", "type": "docx"})

                elif ext in TEXT_EXTS:
                    text = raw.decode("utf-8", errors="replace")
                    cleaned = clean_text(text, aggressive=aggressive)["cleaned"]
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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # CORS — разрешаем запросы из нативного окна (origin = null)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

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
                result = clean_text(data.get("text", ""), aggressive=bool(data.get("aggressive", False)))
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

                # SSE-ответ
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                for event in paraphrase_stream_generator(text, intensity):
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
                result = paraphrase_stamps(data.get("text", ""))
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
                print(f"[batch] получено файлов: {len(files)}")
                zip_bytes, report = batch_clean(files, aggressive)
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


def check_model_installed(model: str) -> bool:
    """Проверяет, что модель скачана, через `ollama list`."""
    if not check_ollama_installed():
        return False
    binary = _which_ollama()
    try:
        result = subprocess.run(
            [binary, "list"],
            capture_output=True, text=True, timeout=5,
        )
        return model in result.stdout
    except Exception:
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

APP_VERSION = "2.1.0"

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