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
import socket
import threading
import traceback
import unicodedata
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# --- опциональные библиотеки ---
from pypdf import PdfReader
HAS_PYPDF = True

from fpdf import FPDF
HAS_FPDF = True

import fitz  # PyMuPDF
HAS_FITZ = True


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
    """Эвристическая оценка «ИИ-манеры». 0-100, где 100 — сильные признаки ИИ."""
    if not text or len(text) < 80:
        return {"available": True, "score": 0, "label": "слишком короткий текст",
                "features": {}, "notes": []}

    # убираем markdown-разметку, чтобы не ломать анализ предложений
    clean = re.sub(r"[*_`#>]+", "", text)

    sentences = _sentences(clean)
    words = _words(clean)
    if not words or not sentences:
        return {"available": True, "score": 0, "label": "нет текста",
                "features": {}, "notes": []}

    notes = []
    score = 0.0
    low = clean.lower()

    # ---- 1. Однородность длины предложений ----
    sent_lens = [len(_words(s)) for s in sentences]
    avg_sent = sum(sent_lens) / len(sent_lens)
    sd = _stdev(sent_lens)
    cv = sd / avg_sent if avg_sent else 0
    if cv < 0.3:
        score += 15; notes.append(f"Однородные предложения (cv={cv:.2f})")
    elif cv < 0.5:
        score += 10; notes.append(f"Умеренно однородные предложения (cv={cv:.2f})")
    elif cv < 0.65:
        score += 4; notes.append(f"Средний разброс предложений (cv={cv:.2f})")
    else:
        notes.append(f"Живой разброс предложений (cv={cv:.2f})")

    # ---- 2. Средняя длина слова ----
    avg_wl = sum(len(w) for w in words) / len(words)
    if avg_wl > 7.0:
        score += 10; notes.append(f"Длинные слова ({avg_wl:.1f} букв)")
    elif avg_wl > 6.2:
        score += 5; notes.append(f"Средние слова ({avg_wl:.1f} букв)")

    # ---- 3. ПАТТЕРНЫ ИИ ----
    pattern_hits = []
    pattern_score = 0
    for rx, weight, name in AI_PATTERNS:
        matches = re.findall(rx, low)
        if matches:
            pattern_score += weight * min(len(matches), 2)
            pattern_hits.append(f"{name}×{len(matches)}")
    pattern_score = min(pattern_score, 45)   # потолок, чтобы не перекосить
    score += pattern_score
    if pattern_hits:
        notes.append(f"Паттерны ИИ (+{pattern_score}): " + "; ".join(pattern_hits[:8]))
    else:
        notes.append("Характерных паттернов ИИ не найдено")

    # ---- 4. Длинные тире ----
    em_dashes = clean.count("—") + clean.count("–")
    dash_ratio = em_dashes / len(sentences)
    if dash_ratio > 0.3:
        score += 12; notes.append(f"Много длинных тире ({em_dashes})")
    elif dash_ratio > 0.15:
        score += 5; notes.append(f"Есть длинные тире ({em_dashes})")

    # ---- 5. Плотность абстрактных слов ----
    abstract_count = sum(1 for w in words
                         if any(w.startswith(s) for s in ABSTRACT_STEMS))
    abs_density = abstract_count / len(words) * 100
    if abs_density > 12:
        score += 12; notes.append(f"Много абстрактных слов ({abs_density:.0f}%)")
    elif abs_density > 8:
        score += 6; notes.append(f"Умеренно абстрактная лексика ({abs_density:.0f}%)")

    # ---- 6. Триады (X, Y и Z) ----
    triads = len(re.findall(r"[а-яё]{3,},\s+[а-яё]{3,}\s+(и|или)\s+[а-яё]{3,}", low))
    if triads >= 3:
        score += 10; notes.append(f"Перечисления-триады ({triads})")
    elif triads >= 1:
        score += 5; notes.append(f"Перечисления-триады ({triads})")

    # ---- 7. Риторические вопросы ----
    q_count = sum(1 for s in sentences if s.strip().endswith("?"))
    if q_count > 0:
        score += 4; notes.append(f"Риторические вопросы ({q_count})")

    # ---- 8. Повторяющиеся начала предложений ----
    firsts = []
    for s in sentences:
        w = _words(s)
        if w: firsts.append(w[0])
    if firsts:
        repeats = len(firsts) - len(set(firsts))
        if repeats / len(firsts) > 0.2:
            score += 8; notes.append(f"Повторяющиеся начала ({repeats}/{len(firsts)})")

    # ---- 9. Однородные абзацы ----
    paragraphs = [p for p in re.split(r"\n\s*\n", clean) if p.strip()]
    if len(paragraphs) >= 3:
        plens = [len(_words(p)) for p in paragraphs]
        pavg = sum(plens) / len(plens)
        psd = _stdev(plens)
        pcv = psd / pavg if pavg else 0
        if pcv < 0.35:
            score += 10; notes.append(f"Абзацы близкой длины (cv={pcv:.2f})")

    # ---- 10. Штрафы (только явные маркеры живого стиля) ----
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
            "паттернов ИИ": len(pattern_hits),
            "длинных тире": em_dashes,
            "абстрактных": f"{abs_density:.0f}%",
        },
        "notes": notes,
    }

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
    """
    Чистит PDF, сохраняя структуру.
    font_preset: 'Auto' или ключ из PRESET_FONTS
    size_override: None (авто) или число пт
    """
    if not HAS_FITZ:
        raise RuntimeError("PyMuPDF не установлен. Выполни: pip install pymupdf")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    touched_total = 0

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
                        to_clean.append((span, cleaned))

        if not to_clean:
            continue
        touched_total += len(to_clean)

        for span, _ in to_clean:
            try:
                page.add_redact_annot(fitz.Rect(span["bbox"]))
            except Exception:
                pass
        try:
            page.apply_redactions(images=0)
        except TypeError:
            page.apply_redactions()

        for span, cleaned in to_clean:
            try:
                origin = span["origin"]
                # размер: override или из оригинала
                size = size_override if size_override else span.get("size", 11)
                color_int = span.get("color", 0)
                r = ((color_int >> 16) & 255) / 255
                g = ((color_int >> 8) & 255) / 255
                b = (color_int & 255) / 255

                font_path = pick_font_for_span(span.get("font", ""), font_preset)
                kwargs = dict(fontsize=size, color=(r, g, b))
                if font_path:
                    kwargs["fontname"] = "cleanfont"
                    kwargs["fontfile"] = font_path
                else:
                    kwargs["fontname"] = "helv"

                page.insert_text(fitz.Point(*origin), cleaned, **kwargs)
            except Exception:
                pass

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
            })
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

        elif self.path == "/paraphrase":
            try:
                data = json.loads(raw)
                result = paraphrase_stamps(data.get("text", ""))
                self._json(200, result)
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": str(e)})

        else:
            self._send(404, b"not found", "text/plain")


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
    server = HTTPServer(("127.0.0.1", port), Handler)

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