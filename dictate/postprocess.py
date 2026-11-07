"""Локальная постобработка текста диктовки (версия 0.2.0).

Без сети и без тяжёлых зависимостей: только stdlib. Функция ``improve``:

1. применяет безопасные авто-правки (пробелы, тире, капитализация, терминальная
   точка, запятые по простым правилам, частые грамматические замены);
2. возвращает список ``Edit`` — что и почему изменено (UI пишет «N правок»);
3. находит «спорные» слова (``Suggestion``) с вариантами и причиной по-русски —
   они НЕ применяются автоматически.

``apply_suggestions`` — хелпер для окна «Спорные слова»: по выбору пользователя
заменяет слова в тексте.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- данные


@dataclass
class Edit:
    """Одна авто-правка: диапазон в тексте на момент применения."""

    start: int
    end: int
    original: str
    replacement: str
    rule: str


@dataclass
class Suggestion:
    """«Спорное» слово в координатах уже обработанного текста."""

    word: str
    start: int
    end: int
    variants: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class PostprocessResult:
    text: str
    edits: list[Edit] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)


# --------------------------------------------------------------------------- константы

TERMINAL = ".!?…"
_SPACE_OR_TAB = " \t"

# Служебные слова, которые не считаем «очень короткими» подозрительными.
SERVICE_WORDS = {
    "и", "в", "во", "не", "на", "я", "с", "со", "а", "но", "что", "по", "из", "к",
    "ко", "у", "о", "об", "же", "ли", "то", "он", "мы", "вы", "их", "мне", "ты",
    "за", "до", "при", "для", "без", "над", "под", "от", "так", "как", "она",
    "оно", "они", "это", "все", "всё", "да", "нет", "ну", "вот", "где", "кто",
    "чем", "или", "либо", "если", "когда", "там", "тут", "уже", "ещё", "еще",
    "бы", "был", "была", "было", "были", "есть", "им", "ей", "его", "её", "ее",
    "нас", "вас", "вам", "нам", "мой", "моя", "моё", "мое", "твой", "свой",
    "эта", "этот", "эти", "тот", "та", "те", "один", "одна", "два", "две", "три",
    "их", "ею", "им", "нею", "мною", "тебя", "меня", "себя", "всё", "уж", "ведь",
}

_INFINITIVE_CONTEXT = {
    "не", "что", "надо", "нужно", "можно", "хочу", "хочешь", "хочет", "хотим",
    "хотите", "хотят", "должен", "должна", "должны", "будет", "будут", "может",
    "могут", "пора", "умеет", "умеют", "любит", "люблю", "стал", "стала",
    "начал", "начала", "продолжал", "продолжала", "решил", "решила", "пытался",
    "пыталась", "хотел", "хотела", "следует", "стоит", "нельзя", "легко",
    "трудно", "интересно", "важно", "намерен", "собирается", "собираюсь",
}

# Частые опечатки: слово -> безопасные варианты.
COMMON_FIXES: dict[str, list[str]] = {
    "корекция": ["коррекция"],
    "програма": ["программа"],
    "программе": ["программе"],
    "камент": ["коммент", "комментарий"],
    "вообщем": ["в общем"],
    "извени": ["извини"],
    "извените": ["извините"],
    "пожалуста": ["пожалуйста"],
    "спосибо": ["спасибо"],
    "агенство": ["агентство"],
    "зделать": ["сделать"],
    "зделал": ["сделал"],
    "здарово": ["здорово"],
    "щас": ["сейчас"],
    "тута": ["тут"],
}

# Слитно написанное «не» с глаголом: слово -> варианты.
MERGED_NE: dict[str, list[str]] = {
    "немогу": ["не могу"],
    "нехочу": ["не хочу"],
    "несмог": ["не смог"],
    "незнаю": ["не знаю"],
    "небуду": ["не буду"],
    "небыло": ["не было"],
    "небыл": ["не был"],
    "нетак": ["не так"],
    "ненадо": ["не надо"],
    "неочень": ["не очень"],
}

# Латинские символы -> кириллические двойники (для слов вида «cлово»).
_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф", "g": "г",
        "h": "н", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "п",
        "o": "о", "p": "р", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
        "v": "в", "w": "в", "x": "х", "y": "у", "z": "з",
        "A": "А", "B": "Б", "C": "С", "D": "Д", "E": "Е", "F": "Ф", "G": "Г",
        "H": "Н", "I": "И", "J": "Й", "K": "К", "L": "Л", "M": "М", "N": "П",
        "O": "О", "P": "Р", "Q": "К", "R": "Р", "S": "С", "T": "Т", "U": "У",
        "V": "В", "W": "В", "X": "Х", "Y": "У", "Z": "З",
    }
)

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]+")

# Авто-грамматика: (pattern, replacement, rule, conservative).
GRAMMAR_RULES: list[tuple[re.Pattern[str], str, str, bool]] = [
    (
        re.compile(
            r"\bв течении(?=\s+(?:\d|дв|тр|чет|пят|шест|сем|вос|дев|десят|одн|пол|"
            r"недол|месяц|год|лет|дн|час|минут|врем|срок))",
            re.IGNORECASE,
        ),
        "в течение",
        "grammar:в течение",
        True,
    ),
    (re.compile(r"\bне смотря на\b", re.IGNORECASE), "несмотря на", "grammar:несмотря на", True),
    (re.compile(r"\bто-же\b", re.IGNORECASE), "то же", "grammar:то же", True),
    (re.compile(r"\bтак-же\b", re.IGNORECASE), "так же", "grammar:так же", True),
    (
        re.compile(r"\bввиду того что\b", re.IGNORECASE),
        "ввиду того, что",
        "grammar:ввиду того, что",
        True,
    ),
    (re.compile(r"\bи\s+т\.\s*д\.?", re.IGNORECASE), "и т. д.", "grammar:и т. д.", True),
    (re.compile(r"\bт\.\s*е\.?", re.IGNORECASE), "т. е.", "grammar:т. е.", True),
]

_COMMA_BEFORE = (
    "потому что",
    "так как",
    "а",
    "но",
    "однако",
    "зато",
    "чтобы",
    "что",
    "если",
    "когда",
    "хотя",
    "поэтому",
    "значит",
    "который",
    "которая",
    "которое",
    "которые",
    "которую",
    "которой",
    "которым",
    "которых",
)
_AFTER_WORD_GUARD = {"потому", "так", "ввиду", "несмотря", "пока", "потом"}
_INTRO_WORDS = (
    "к сожалению",
    "по-моему",
    "во-первых",
    "во-вторых",
    "конечно",
    "наверное",
    "возможно",
    "кстати",
    "итак",
    "например",
)


# --------------------------------------------------------------------------- утилиты


def _case_like(original: str, replacement: str) -> str:
    """Сохранить заглавную первую букву при замене."""
    if original and replacement and original[0].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


class _Editor:
    """Последовательное применение правок с журналом ``Edit``."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[Edit] = []

    def replace(self, start: int, end: int, replacement: str, rule: str) -> bool:
        original = self.text[start:end]
        if original == replacement:
            return False
        self.text = self.text[:start] + replacement + self.text[end:]
        self.edits.append(Edit(start, end, original, replacement, rule))
        return True

    def replace_matches(self, pattern: re.Pattern[str], repl, rule: str) -> None:
        """Заменить все совпадения (справа налево, чтобы индексы не съезжали)."""
        for match in reversed(list(pattern.finditer(self.text))):
            replacement = repl(match)
            if replacement is not None and replacement != match.group(0):
                self.replace(match.start(), match.end(), replacement, rule)


def _inside_quotes(text: str, position: int) -> bool:
    """Грубая проверка: точка вставки внутри «кавычек» или \"кавычек\"."""
    depth = 0
    straight = False
    for char in text[:position]:
        if char == "«":
            depth += 1
        elif char == "»":
            depth = max(0, depth - 1)
        elif char == '"':
            straight = not straight
    return depth % 2 == 1 or straight


# --------------------------------------------------------------------------- авто-правки


def _normalize_spaces(editor: _Editor) -> None:
    # «...» -> «…»; повторные пробелы; пробел перед знаками; пробел после запятой.
    editor.replace_matches(re.compile(r"\.{3,}"), lambda match: "…", "spaces:ellipsis")
    editor.replace_matches(re.compile(r"[ \t]{2,}"), lambda match: " ", "spaces:double")
    editor.replace_matches(
        re.compile(r"[ \t]+([,.;:!?…])"), lambda match: match.group(1), "spaces:before-punct"
    )
    editor.replace_matches(
        re.compile(r"(?<!\d),(?![\d\s,.;:!?…)])"), lambda match: ", ", "spaces:after-comma"
    )
    # Пробел после точки между словами (кириллица; URL/инициалы/сокращения не трогаем).
    editor.replace_matches(
        re.compile(r"(?<=[а-яё]{2})\.(?=[А-ЯЁа-яё])"), lambda match: ". ", "spaces:after-dot"
    )
    stripped = editor.text.strip(_SPACE_OR_TAB)
    if stripped != editor.text:
        editor.replace(0, len(editor.text), stripped, "spaces:trim")


def _fix_dashes(editor: _Editor) -> None:
    # «слово - слово» -> «слово — слово»; дефисы внутри слов и диапазоны цифр целы.
    editor.replace_matches(
        re.compile(r"(?<!\d)(?<=\S)[ \t]+-[ \t]+(?=\S)(?!\d)"),
        lambda match: " — ",
        "dash",
    )


def _fix_grammar(editor: _Editor) -> None:
    for pattern, replacement, rule, _conservative in GRAMMAR_RULES:
        editor.replace_matches(
            pattern,
            lambda match, target=replacement: _case_like(match.group(0), target),
            rule,
        )
    # «Сейчас» не в начале предложения -> строчная (частая ошибка распознавания).
    pattern = re.compile(r"\bСейчас\b")
    for match in reversed(list(pattern.finditer(editor.text))):
        before = editor.text[: match.start()].rstrip(_SPACE_OR_TAB)
        if not before or before[-1] in TERMINAL or before[-1] in "\n":
            continue
        editor.replace(match.start(), match.start() + 1, "с", "grammar:сейчас")


def _capitalize(editor: _Editor) -> None:
    # Первая буква текста: ищем первый алфавитный символ в любом регистре, но
    # поднимаем его только если он действительно начинает текст (не «10 - 20 штук»).
    match = re.search(r"[А-Яа-яЁёA-Za-z]", editor.text)
    lead = len(editor.text) - len(editor.text.lstrip())
    starts_text = match is not None and (
        match.start() == lead or (match.start() == lead + 1 and editor.text[lead] in "«\"'(")
    )
    if starts_text and match.group(0).islower():
        editor.replace(match.start(), match.start() + 1, match.group(0).upper(), "capitalize:start")
    # Начало предложения: точка/!/?/… + пробелы + слово минимум из двух букв
    # (однобуквенные слова и сокращения вроде «т. е.» не трогаем).
    editor.replace_matches(
        re.compile(r"([.!?…])([ \t]+)([а-яёa-z])(?=[а-яёa-z])"),
        lambda match: match.group(1) + match.group(2) + match.group(3).upper(),
        "capitalize:sentence",
    )
    # Местоимение «я» с заглавной в начале предложения (в середине — строчная,
    # внутри кавычек чужую цитату не правим).
    pattern = re.compile(r"(?<![А-Яа-яЁёA-Za-z])я(?![А-Яа-яЁёA-Za-z])")
    for match in reversed(list(pattern.finditer(editor.text))):
        if _inside_quotes(editor.text, match.start()):
            continue
        before = editor.text[: match.start()].rstrip(_SPACE_OR_TAB)
        if before and before[-1] not in TERMINAL and before[-1] not in "\n":
            continue
        editor.replace(match.start(), match.end(), "Я", "capitalize:я")


def _terminal_punctuation(editor: _Editor) -> None:
    text = editor.text
    if not text:
        return
    last = text[-1]
    if last in TERMINAL:
        return
    if last in ",;:—–-":
        editor.replace(len(text) - 1, len(text), ".", "terminal")
        return
    words = re.findall(r"[А-Яа-яЁёA-Za-z0-9]+", text)
    if len(words) > 2:
        editor.replace(len(text), len(text), ".", "terminal")


def _comma_before(editor: _Editor) -> None:
    for phrase in _COMMA_BEFORE:
        pattern = re.compile(
            r"(?<![А-Яа-яЁёA-Za-z0-9])" + re.escape(phrase) + r"(?=[\s,.;:!?…]|$)",
            re.IGNORECASE,
        )
        position = 0
        while True:
            match = pattern.search(editor.text, position)
            if match is None:
                break
            start = match.start()
            spaces_start = start
            while spaces_start > 0 and editor.text[spaces_start - 1] in _SPACE_OR_TAB:
                spaces_start -= 1
            if spaces_start == 0:
                position = match.end()
                continue
            previous = editor.text[spaces_start - 1]
            if previous in ",;:—–-–(!?…\n«\"":
                position = match.end()
                continue
            previous_word_match = re.search(r"([А-Яа-яЁёA-Za-z0-9]+)$", editor.text[:spaces_start])
            previous_word = previous_word_match.group(1).lower() if previous_word_match else ""
            if phrase.lower() == "что" and previous_word in _AFTER_WORD_GUARD:
                position = match.end()
                continue
            if _inside_quotes(editor.text, start):
                position = match.end()
                continue
            editor.replace(spaces_start, start, ", ", "comma:before")
            position = spaces_start + 2 + len(match.group(0))
        # После вставки текст изменился — начинаем следующую фразу заново.


def _comma_after_intro(editor: _Editor) -> None:
    for word in _INTRO_WORDS:
        pattern = re.compile(
            r"(?<![А-Яа-яЁёA-Za-z0-9])" + re.escape(word) + r"(?![А-Яа-яЁёA-Za-z0-9])",
            re.IGNORECASE,
        )
        position = 0
        while True:
            match = pattern.search(editor.text, position)
            if match is None:
                break
            end = match.end()
            next_position = end
            while next_position < len(editor.text) and editor.text[next_position] in _SPACE_OR_TAB:
                next_position += 1
            if next_position >= len(editor.text) or editor.text[next_position] in ",.;:!?…":
                position = end
                continue
            if _inside_quotes(editor.text, match.start()):
                position = end
                continue
            previous_word_match = re.search(
                r"([А-Яа-яЁёA-Za-z0-9]+)$",
                editor.text[: match.start()].rstrip(_SPACE_OR_TAB),
            )
            previous_word = previous_word_match.group(1).lower() if previous_word_match else ""
            if previous_word == "не":  # «не возможно», «не конечно» — не вводные
                position = end
                continue
            editor.replace(end, next_position, ", ", "comma:intro")
            position = end + 2


# --------------------------------------------------------------------------- «спорные» слова


def _add_suggestion(
    suggestions: list[Suggestion], text: str, start: int, end: int, variants: list[str], reason: str
) -> None:
    word = text[start:end]
    variants = [variant for variant in variants if variant and variant != word]
    if not variants:
        return
    suggestions.append(
        Suggestion(word=word, start=start, end=end, variants=variants, reason=reason)
    )


def _check_word(
    word: str,
    previous: str,
    text: str,
    start: int,
    end: int,
    suggestions: list[Suggestion],
) -> None:
    lowered = word.lower()
    has_cyrillic = bool(_CYRILLIC_RE.search(word))
    has_latin = bool(_LATIN_RE.search(word))

    # Латиница внутри кириллического слова: «cлово» -> «слово».
    if has_cyrillic and has_latin:
        variant = word.translate(_LATIN_TO_CYRILLIC)
        if _CYRILLIC_RE.search(variant) and not _LATIN_RE.search(variant) and variant != word:
            _add_suggestion(
                suggestions, text, start, end, [variant], "латиница в слове"
            )
        return

    # Частые опечатки из словаря.
    if lowered in COMMON_FIXES:
        variants = [_case_like(word, replacement) for replacement in COMMON_FIXES[lowered]]
        _add_suggestion(suggestions, text, start, end, variants, "частая опечатка")
        return

    # Слитно написанное «не».
    if lowered in MERGED_NE:
        variants = [_case_like(word, replacement) for replacement in MERGED_NE[lowered]]
        _add_suggestion(suggestions, text, start, end, variants, "«не» слитно с глаголом")
        return

    # -ться / -тся: эвристика по предыдущему слову.
    if has_cyrillic and word.endswith("ться") and len(word) > 4:
        if not _infinitve_context(previous) and previous and not previous.endswith(("тся", "ться")):
            _add_suggestion(
                suggestions,
                text,
                start,
                end,
                [word[:-4] + "тся"],
                "похоже на -ться/-тся (перед словом существительное/местоимение)",
            )
        return
    if has_cyrillic and word.endswith("тся") and len(word) > 3:
        if _infinitve_context(previous):
            _add_suggestion(
                suggestions,
                text,
                start,
                end,
                [word[:-3] + "ться"],
                "похоже на -ться/-тся (после «не/что/надо/хочет» нужен инфинитив)",
            )
        return

    # Очень короткие неизвестные слова (1-2 буквы) — проверить вручную.
    # Аббревиатуры вида «т.», «д.», «е.» не беспокоим.
    is_abbreviation = len(word) == 1 and end < len(text) and text[end] == "."
    if has_cyrillic and len(word) <= 2 and lowered not in SERVICE_WORDS and not is_abbreviation:
        suggestions.append(
            Suggestion(
                word=word,
                start=start,
                end=end,
                variants=[],
                reason="очень короткое слово — проверьте",
            )
        )


def _infinitve_context(previous: str) -> bool:
    if not previous:
        return False
    if previous in _INFINITIVE_CONTEXT:
        return True
    return previous.endswith(("ть", "ться", "ла", "ли", "ло", "л"))


def _find_suggestions(text: str) -> list[Suggestion]:
    suggestions: list[Suggestion] = []
    words = [(match.start(), match.end(), match.group(0)) for match in _WORD_RE.finditer(text)]
    lowered = [word.lower() for _start, _end, word in words]
    for index, (start, end, word) in enumerate(words):
        previous = lowered[index - 1] if index > 0 else ""
        _check_word(word, previous, text, start, end, suggestions)

    # Фразовые опечатки: «не возможно» -> «невозможно».
    for match in re.finditer(r"(?<![А-Яа-яЁёA-Za-z])не возможно(?![А-Яа-яЁёA-Za-z])", text, re.IGNORECASE):
        suggestions.append(
            Suggestion(
                word=match.group(0),
                start=match.start(),
                end=match.end(),
                variants=["невозможно"],
                reason="частая опечатка: «не возможно» → «невозможно»",
            )
        )

    suggestions.sort(key=lambda suggestion: suggestion.start)
    # Убрать пересечения и ограничить разумным числом.
    unique: list[Suggestion] = []
    last_end = -1
    for suggestion in suggestions:
        if suggestion.start < last_end:
            continue
        unique.append(suggestion)
        last_end = suggestion.end
        if len(unique) >= 100:
            break
    return unique


# --------------------------------------------------------------------------- публичный API


def improve(text: str, language: str = "ru") -> PostprocessResult:
    """Локальная постобработка: текст с авто-правками, список правок и «спорные» слова."""
    raw = str(text or "")
    if not raw.strip():
        return PostprocessResult(text="", edits=[], suggestions=[])

    language_code = str(language or "ru").strip().lower()
    russian = language_code in ("", "ru", "russian", "auto") or language_code.startswith("ru")
    editor = _Editor(raw)
    _normalize_spaces(editor)
    if russian:
        _fix_dashes(editor)
        _fix_grammar(editor)
    _capitalize(editor)
    _terminal_punctuation(editor)
    if russian:
        _comma_before(editor)
        _comma_after_intro(editor)
    result_text = editor.text
    suggestions = _find_suggestions(result_text) if russian else []
    return PostprocessResult(text=result_text, edits=editor.edits, suggestions=suggestions)


def apply_suggestions(
    text: str,
    suggestions: list[Suggestion],
    choices: dict[int, str | None],
) -> str:
    """Применить выбранные варианты «спорных» слов.

    ``choices``: индекс suggestion'а в списке -> выбранный вариант (или ``None``,
    чтобы оставить слово как есть). Координаты берутся из ``suggestions``.
    """
    if not text or not suggestions or not choices:
        return text
    picks: list[tuple[int, int, str]] = []
    for index, suggestion in enumerate(suggestions):
        if index not in choices:
            continue
        choice = choices[index]
        if choice is None:
            continue
        if suggestion.variants and choice not in suggestion.variants:
            continue
        start, end = int(suggestion.start), int(suggestion.end)
        if not 0 <= start < end <= len(text):
            continue
        picks.append((start, end, str(choice)))
    picks.sort()
    filtered: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, choice in picks:
        if start < last_end:
            continue
        filtered.append((start, end, choice))
        last_end = end
    result = text
    for start, end, choice in reversed(filtered):
        result = result[:start] + choice + result[end:]
    return result
