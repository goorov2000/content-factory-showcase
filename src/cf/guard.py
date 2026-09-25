import re

BLOCKED_PREFIXES = ("agent-runtime/", "secrets/")
# Конкатенация — чтобы сам guard.py не содержал маркеры и проходил собственную проверку.
# Метки идут в сообщение о нарушении (сам секрет — никогда).
SECRET_MARKERS = (
    ("pem", "PRIVATE" + " KEY"),
    ("sa-json", '"private' + '_key"'),
)
# Регекспы API-токенов (находка C2: Apify-токен прошёл мимо строковых маркеров).
# Символьные классы не матчат собственный исходник — guard.py остаётся коммитабельным.
SECRET_PATTERNS = tuple((label, re.compile(p)) for label, p in (
    ("apify", r"apify_api_[A-Za-z0-9]{16,}"),   # реальные ~37 символов; порог 16 не задевает
                                                # короткие плейсхолдеры вроде xxx/REDACTED в доках
    # OpenAI-style, включая project-ключи; lookbehind отсекает 'sk-' внутри
    # дефисной прозы (task-..., asterisk-...).
    ("openai", r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9]{20,}"),
    ("ghp", r"ghp_[A-Za-z0-9]{20,}"),           # GitHub PAT (classic)
    ("github_pat", r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PAT
    ("aws", r"AKIA[0-9A-Z]{16}"),               # AWS access key id
    # JWT (ключи n8n API — это JWT). Lookbehind — защита от квадратичного скана:
    # символы префикса eyJ сами входят в класс, без lookbehind длинный
    # base64url-ран без точек пересканировался бы с каждого вхождения eyJ.
    ("jwt", r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\."),
    # Telegram bot-токен: цифры bot id, ':', секрет. Границы отсекают время
    # (07:30) и длинные числовые раны.
    #
    # Диапазоны, а не точные длины (правка 2026-08-10): прежний шаблон требовал
    # РОВНО 35 знаков секрета и 8-10 цифр id, из-за чего мимо guard-а проходил
    # даже пример токена из официальной документации Telegram (секрет 34 знака).
    # Боевой токен завода спасала случайность — у него оказалось ровно 35.
    # Длина секрета Telegram нигде не зафиксирована контрактом, а bot id растут:
    # верхняя граница в 10 цифр была миной замедленного действия.
    ("telegram", r"(?<!\d)\d{7,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"),
))


def _find_secret(text):
    for label, marker in SECRET_MARKERS:
        if marker in text:
            return label
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def check_staged(paths, read_text):
    violations = []
    for path in paths:
        norm = path.replace("\\", "/")
        if norm.startswith(BLOCKED_PREFIXES):
            violations.append(f"{path}: runtime/секретный путь не коммитится")
            continue
        try:
            text = read_text(path)
        except OSError as exc:
            # Fail-closed: непрочитанный staged blob нельзя объявить чистым.
            violations.append(f"{path}: не удалось прочитать staged blob ({exc})")
            continue
        label = _find_secret(text)
        if label:
            violations.append(f"{path}: содержит маркер секрета ({label})")
    return violations
