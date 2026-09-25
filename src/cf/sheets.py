import logging
import time

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import (
    ValueInputOption,
    ValueRenderOption,
    numericise,
    rowcol_to_a1,
)

from cf.columns import apply_column_aliases
from cf.config import load_config, resolve_path
from cf.retry import NON_RETRIABLE, with_retry

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Тяжёлые колонки: n8n пишет raw_json до ~45 КБ/ячейку (~90% payload вкладки).
# read_rows по умолчанию их НЕ выкачивает (проекция), отдавая только по явному
# include_heavy=True. Имена — фактические заголовки листа (raw_json не алиасится).
# Переопределяемо через config["heavy_columns"].
HEAVY_COLUMNS = ("raw_json",)

# Колонки, которых код ЖДЁТ в живых листах, но пишет мягко (optional_fields в
# update_row_fields / append_row): их отсутствие не роняет запись, а ТИХО меняет
# поведение. Без briefs.reviewed_at кап auto-approve считается по generated_at
# (фикс M31 мёртв), без briefs.cta cmd_add_brief теряет CTA, без
# reels.prompt_version eval теряет привязку к версии промпта, без reels.account
# публикация теряет привязку к аккаунту — деньги UTM-контура не сходятся. См.
# docs/RUNBOOK.md → «Google Sheets (колонки в живых листах)».
EXPECTED_COLUMNS = {
    # creator_slot/assigned_at — назначение исполнителя (2026-07-28). Попадают
    # сюда, а не только в fail-loud записи, чтобы отсутствие колонок было видно
    # ДО того, как продюсер нажмёт «Отдал в работу»: здоровье систем и cf status
    # скажут о дрейфе схемы заранее.
    "briefs": ("reviewed_at", "cta", "creator_slot", "assigned_at"),
    "reels": ("prompt_version", "account"),
    # media_* — медиа-контур сборщика (тикеты 03–04 визуального контура): без
    # колонок боевой upsert молча выбросит манифест и статус с одним warning.
    # Колонки в живые листы добавляет оператор (тикет 13, RUNBOOK «Google
    # Sheets»); до этого дрейф здесь — честное напоминание, а не ошибка.
    # visual_* пишет только cf vision (тикет 05): отсутствие колонок — громкий
    # останов записи разбора (UnknownFieldsError), дрейф виден заранее.
    # visual_facts НАМЕРЕННО не в HEAVY_COLUMNS: анализ читает строки
    # проекцией, и «тяжёлый» visual_facts молча спрятал бы факты (тикет 07);
    # компактный JSON фактов на порядок меньше raw_json.
    "raw_tiktok": ("media_status", "media_manifest",
                   "visual_status", "visual_facts"),
    "raw_instagram": ("media_status", "media_manifest",
                      "visual_status", "visual_facts"),
}

logger = logging.getLogger(__name__)


def schema_drift(sheets, expected=None):
    """{tab_key: [пропавшие колонки]} по EXPECTED_COLUMNS — один источник правды
    для health-чека дашборда и cf status. Дрейф схемы боевой таблицы обязан быть
    громким: молчаливый optional-no-op незаметно меняет поведение конвейера.

    Исключения НЕ глушим — вызывающий сам решает (health показывает fail,
    cf status печатает «проверить не удалось»)."""
    drift = {}
    for tab_key, columns in (expected or EXPECTED_COLUMNS).items():
        missing = sheets.missing_columns(tab_key, columns)
        if missing:
            drift[tab_key] = missing
    return drift


class UnknownFieldsError(ValueError):
    """Запрошенные поля отсутствуют в заголовках вкладки — запись невозможна.

    Наследник ValueError: with_retry считает его неретраибельным и всплывает сразу
    (расхождение схемы ретраем не лечится). Так решение оператора не теряется молча.
    """

    def __init__(self, tab_key, fields):
        self.tab_key = tab_key
        self.fields = list(fields)
        super().__init__(
            f"{tab_key}: поля вне заголовков вкладки, запись невозможна: {self.fields}")


def _col_letter(n):
    """1 -> A, 26 -> Z, 27 -> AA … номер колонки в A1-нотацию."""
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


# Ширина окна скана хвоста при ретрае append_row. Ответ сервера мог потеряться уже
# ПОСЛЕ применения записи; при этом параллельный аппендер (боевой фан-аут — 2 воркера,
# плюс health/раннер/агенты пишут run_log) успевает вписать свою строку между нашей
# попыткой и ретраем, сдвинув нашу с последней позиции. Поэтому на ретрае ищем свою
# строку не только в последней позиции, а в последних _TAIL_SCAN_ROWS строках.
# 10 — запас на редкий всплеск параллельных записей (реально между попыткой и ретраем
# приземляется 0–1 чужая строка); при этом окно узкое, чтобы не плодить НОВЫХ ложных
# дедупов для легитимно одинаковых строк, лежащих дальше по листу.
_TAIL_SCAN_ROWS = 10


def _tail_contains_row(ws, values):
    """Есть ли наша строка среди последних _TAIL_SCAN_ROWS строк данных листа.

    Замена проверки «только последняя строка»: под конкурентным аппендером наша
    строка может оказаться не последней. Равенство — то же, что раньше: сравнение
    с обрезкой хвостовых пустых ячеек.

    Ограничение: как и прежняя проверка последней строки, при легитимно ОДИНАКОВЫХ
    строках в пределах окна возможен ложный дедуп (второй такой аппенд сочтётся уже
    приземлившимся). Окно узкое (=10) и скан идёт только на ретрае, поэтому риск не
    выше практического минимума; точный дедуп одинаковых строк — вне задачи (нужен
    идемпотентный ключ, здесь не решаем и НЕ регрессируем)."""
    def strip_tail(cells):
        cells = [str(c) for c in cells]
        while cells and cells[-1] == "":
            cells.pop()
        return cells

    data = ws.get_all_values()
    target = strip_tail(values)
    # data[0] — строка заголовков; данные — data[1:]. Скан последнего окна.
    for row in data[1:][-_TAIL_SCAN_ROWS:]:
        if strip_tail(row) == target:
            return True
    return False


def _light_column_ranges(headers, heavy):
    """Смежные группы «лёгких» колонок (все, кроме heavy) -> целоколоночные A1-диапазоны.

    Тяжёлая колонка в середине разрывает диапазон надвое ('A:B', 'D:F'), поэтому
    возвращаем список диапазонов и параллельно — имена лёгких колонок по порядку.
    """
    light_idx = [i for i, h in enumerate(headers) if h not in heavy]
    runs = []
    for i in light_idx:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    ranges = [f"{_col_letter(r[0] + 1)}:{_col_letter(r[-1] + 1)}" for r in runs]
    widths = [len(r) for r in runs]
    light_headers = [headers[i] for i in light_idx]
    return ranges, widths, light_headers


def _project_records(ws, headers, heavy):
    """Прочитать вкладку БЕЗ тяжёлых колонок: values_get только по лёгким диапазонам.

    Экономит трафик (raw_json — до ~90% payload не выкачивается). Значения читаются
    UNFORMATTED и нумеризуются как get_all_records, чтобы результат совпадал с обычным
    чтением по набору лёгких колонок."""
    ranges, widths, light_headers = _light_column_ranges(headers, heavy)
    if not ranges:
        return []
    blocks = ws.batch_get(ranges, value_render_option=ValueRenderOption.unformatted)
    # blocks[k][0] — строка заголовков диапазона; данные — с индекса 1. Блоки могут
    # быть разной длины (Sheets обрезает пустой хвост каждого по-своему) — берём max
    # и добиваем недостающие ячейки пустыми, как делает get_all_records с короткими строками.
    n = max((len(b) - 1 for b in blocks), default=0)
    records = []
    for row_i in range(n):
        cells = []
        for k, block in enumerate(blocks):
            data_row = block[row_i + 1] if (row_i + 1) < len(block) else []
            data_row = list(data_row) + [""] * (widths[k] - len(data_row))
            cells.extend(data_row[:widths[k]])
        records.append({h: numericise(v) for h, v in zip(light_headers, cells)})
    while records and all(str(v) == "" for v in records[-1].values()):
        records.pop()
    return records


class Sheets:
    def __init__(self, config=None, client=None, retry_delay=1.0, header_ttl=300.0):
        self._config = config
        self._client = client
        self.retry_delay = retry_delay
        # P3.1: кэш хэндлов НА ИНСТАНС (не модульный) — иначе разные фейки/аккаунты
        # пересекались бы, а health-инстанс (P2.11) делил бы состояние с дашбордом.
        # Реальный gspread на каждый .worksheet() шлёт fetch_sheet_metadata, поэтому
        # хэндл кэшируем и переиспользуем; инвалидация — на APIError (self-heal).
        self._book = None
        self._ws_cache = {}   # tab_key -> Worksheet
        # Кэш строки заголовков ТОЛЬКО для лёгкого пути чтения (решение о проекции),
        # чтобы не платить лишним запросом заголовков на каждое чтение. Пути записи
        # читают заголовки заново (fail-fast на дрейфе схемы — не кэшируется).
        # TTL (M35): дашборд живёт неделями — колонку, добавленную другим
        # процессом (classify-niche -> set_column_by_key), вечный кэш не увидел бы
        # никогда, и проекция read_rows молча теряла бы её значения.
        self.header_ttl = header_ttl
        self._hdr_cache = {}  # tab_key -> (monotonic, list[str])

    @property
    def config(self):
        if self._config is None:
            self._config = load_config()
        return self._config

    @property
    def client(self):
        if self._client is None:
            creds = Credentials.from_service_account_file(
                resolve_path(self.config["service_account_file"]), scopes=SCOPES
            )
            self._client = gspread.authorize(creds)
        return self._client

    def _spreadsheet(self):
        if self._book is None:
            self._book = self.client.open_by_key(self.config["spreadsheet_id"])
        return self._book

    def _ws(self, tab_key):
        ws = self._ws_cache.get(tab_key)
        if ws is None:
            # Проверка-и-запись без блокировки: dict-операции атомарны под GIL;
            # гонка воркеров в худшем случае создаёт лишний ВАЛИДНЫЙ хэндл (не баг).
            ws = self._spreadsheet().worksheet(self.config["tabs"][tab_key])
            self._ws_cache[tab_key] = ws
        return ws

    def _read_headers(self, ws, tab_key):
        cached = self._hdr_cache.get(tab_key)
        if cached is not None:
            stamp, hdr = cached
            if time.monotonic() - stamp < self.header_ttl:
                return hdr
        hdr = ws.row_values(1)
        self._hdr_cache[tab_key] = (time.monotonic(), hdr)
        return hdr

    def _invalidate(self, tab_key):
        """Сбросить закэшированный хэндл (и заголовки) вкладки — пересоздадутся на след. вызове."""
        self._ws_cache.pop(tab_key, None)
        self._hdr_cache.pop(tab_key, None)

    def _retry(self, tab_key, op, label):
        """with_retry + инвалидация хэндла вкладки на APIError.

        Протухший/битый Worksheet-хэндл (лист переименован/удалён, gid сменился)
        всплывает APIError — сбрасываем кэш, чтобы следующая попытка (в ретрае или
        следующий вызов) пересоздала хэндл. Классификацию 4xx/429/5xx оставляем
        with_retry: перманентные ошибки всплывают сразу, транзиентные — ретраятся.

        WorksheetNotFound — перманентное состояние, а не сбой: ретрай его не
        лечит, а переупаковка в RetryError прячет тип от вызывающих, которые
        показывают отсутствие вкладки как пустое состояние (страница «Деньги»
        до первого прогона сборщика)."""
        def wrapped():
            try:
                return op()
            except gspread.exceptions.APIError:
                self._invalidate(tab_key)
                raise
        return with_retry(
            wrapped, base_delay=self.retry_delay, label=label,
            non_retriable=NON_RETRIABLE + (gspread.exceptions.WorksheetNotFound,))

    def _to_actual(self, tab_key, d):
        """Канонические имена колонок -> фактические имена вкладки (те же column_aliases)."""
        aliases = self.config.get("column_aliases", {}).get(tab_key, {})
        return {aliases.get(k, k): v for k, v in d.items()}

    def _actual_name(self, tab_key, column):
        return self.config.get("column_aliases", {}).get(tab_key, {}).get(column, column)

    def _require_headers(self, tab_key, headers, fields):
        """Все запрошенные поля обязаны быть в заголовках, иначе запись невозможна.

        Fail-fast до записи: частичная запись хуже честного отказа. Пропущенное поле —
        потерянное решение оператора, поэтому логируем и падаем, а не молча пишем часть."""
        missing = [c for c in fields if c not in headers]
        if missing:
            logger.warning("update %s: поля вне заголовков, запись отменена: %s",
                           tab_key, missing)
            raise UnknownFieldsError(tab_key, missing)

    def read_rows(self, tab_key, include_heavy=False):
        """Прочитать вкладку. По умолчанию тяжёлые колонки (raw_json) НЕ выкачиваются:
        читаем проекцией только по лёгким колонкам — экономия трафика ~90% на raw-вкладках.
        include_heavy=True возвращает ВСЕ колонки (полное get_all_records, как раньше);
        передавай его лишь там, где raw_json реально нужен (backup, backfill-attribution)."""
        heavy = set(self.config.get("heavy_columns", HEAVY_COLUMNS))

        def op():
            ws = self._ws(tab_key)
            # UNFORMATTED_VALUE: ru_RU-локаль рендерит 19.833 как «19,833»,
            # а gspread-numericise съедает запятую как разделитель тысяч -> 19833.
            if include_heavy:
                return ws.get_all_records(
                    value_render_option=ValueRenderOption.unformatted)
            headers = self._read_headers(ws, tab_key)
            if not any(h in heavy for h in headers):
                # Нет тяжёлых колонок — обычное дешёвое чтение одним запросом.
                return ws.get_all_records(
                    value_render_option=ValueRenderOption.unformatted)
            return _project_records(ws, headers, heavy)

        rows = self._retry(tab_key, op, f"read {tab_key}")
        aliases = self.config.get("column_aliases", {}).get(tab_key, {})
        return apply_column_aliases(rows, aliases)

    def count_rows(self, tab_key):
        """Дешёвый счётчик строк данных вкладки: читает ОДНУ колонку (col_values),
        а не всю вкладку. raw_json (~90% payload) не выкачивается — для поллинга
        роста raw-вкладок хватает числа строк. Возвращает число строк без заголовка."""
        def op():
            ws = self._ws(tab_key)
            values = ws.col_values(1)   # первая колонка: заголовок + строки данных
            return max(len(values) - 1, 0)

        return self._retry(tab_key, op, f"count {tab_key}")

    def header(self, tab_key):
        """Строка заголовков вкладки одним лёгким запросом (row_values(1)).

        Для health-проверки связности: реальный round-trip к Sheets БЕЗ выкачивания
        данных вкладки. Заголовки не кэшируются (в отличие от _read_headers) —
        проверка должна честно ходить в сеть на каждом цикле."""
        def op():
            return self._ws(tab_key).row_values(1)

        return self._retry(tab_key, op, f"header {tab_key}")

    def missing_columns(self, tab_key, columns):
        """Какие из канонических columns отсутствуют в заголовках живого листа.

        Канонические имена переводятся в фактические теми же column_aliases, что и
        запись (иначе review_status->human_status считался бы пропажей). Пустые
        заголовки (вкладки нет в config["tabs"] / лист пуст) — схема неизвестна,
        возвращаем []: ложная тревога хуже молчания."""
        try:
            headers = self.header(tab_key)
        except KeyError:            # вкладки нет в config["tabs"]
            return []
        if not headers:
            return []
        return [c for c in columns
                if self._actual_name(tab_key, c) not in headers]

    def append_row(self, tab_key, row):
        attempted = False
        warned = False
        actual_row = self._to_actual(tab_key, row)

        def op():
            nonlocal attempted, warned
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            # Колонки вне заголовков живого листа МОЛЧА теряются (values строится
            # только по headers). В отличие от update_* здесь НЕ падаем — часть
            # вызывающих легитимно передаёт лишние ключи, — но громко предупреждаем,
            # чтобы дрейф схемы (напр. отсутствующая колонка cta) всплывал сразу при
            # записи, а не позже на чтении. Логируем один раз, не на каждом ретрае.
            if not warned:
                dropped = [k for k in actual_row if k not in headers]
                if dropped:
                    logger.warning(
                        "append %s: колонки вне заголовков вкладки, значения выброшены: %s",
                        tab_key, dropped)
                warned = True
            values = [str(actual_row.get(h, "")) for h in headers]
            # Сбой мог случиться после того, как сервер применил запись:
            # на повторной попытке сперва смотрим, нет ли нашей строки в хвосте листа
            # (не только в последней позиции — параллельный аппендер мог сдвинуть её).
            if attempted and _tail_contains_row(ws, values):
                return
            attempted = True
            ws.append_row(values, value_input_option="RAW")

        self._retry(tab_key, op, f"append {tab_key}")

    def append_rows(self, tab_key, rows):
        """Батч-аппенд списка строк одним ws.append_rows (+ одно чтение заголовков).

        Для экспорта пачками (напр. cf export-seeds). Как append_row: пишем строго по
        заголовкам вкладки (лишние ключи выбрасываются с предупреждением, отсутствующие
        -> ""), ключи канонические -> фактические колонки. Идемпотентность хвоста здесь
        НЕ реализуется (в отличие от одиночного append_row): при редком ретрае после
        сбоя-post-write возможен дубль, поэтому вызывающий обязан обеспечить дедуп
        входных строк (export-seeds фильтрует уже существующие seed_url до вызова)."""
        if not rows:
            return
        actual_rows = [self._to_actual(tab_key, r) for r in rows]

        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            dropped = sorted({k for r in actual_rows for k in r if k not in headers})
            if dropped:
                logger.warning(
                    "append %s: колонки вне заголовков вкладки, значения выброшены: %s",
                    tab_key, dropped)
            values = [[str(r.get(h, "")) for h in headers] for r in actual_rows]
            ws.append_rows(values, value_input_option="RAW")

        self._retry(tab_key, op, f"append_rows {tab_key}")

    def upsert_rows(self, tab_key, key_column, rows, merge=None):
        """Upsert по ключу (спека §2): существующие строки -> ОДИН батч-update,
        новые -> ОДИН append_rows; нативного upsert в gspread нет. merge(incoming,
        existing) — хук coalesce P1.16, работает с каноническими именами колонок.

        Лист читается без тяжёлых колонок: мержу нужны только лёгкие
        (transcript_text/source_query/collected_at), raw_json всегда берёт свежее.
        Ретрай перечитывает лист целиком, поэтому append, приземлившийся до потери
        ответа сервера, на повторе оказывается в update-ветке — дубль не возникает.
        Дубль ключа в листе: обновляется последняя строка (парность appendOrUpdate)."""
        if not rows:
            return {"updated": 0, "appended": 0}
        heavy = set(self.config.get("heavy_columns", HEAVY_COLUMNS))
        aliases = self.config.get("column_aliases", {}).get(tab_key, {})
        actual_key = self._actual_name(tab_key, key_column)

        def cell(value):
            return "" if value is None else str(value)

        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            if actual_key not in headers:
                raise UnknownFieldsError(tab_key, [actual_key])
            dropped = sorted({k for r in rows
                              for k in self._to_actual(tab_key, r) if k not in headers})
            if dropped:
                logger.warning(
                    "upsert %s: колонки вне заголовков вкладки, значения выброшены: %s",
                    tab_key, dropped)
            if any(h in heavy for h in headers):
                records = _project_records(ws, headers, heavy)
            else:
                records = ws.get_all_records(
                    value_render_option=ValueRenderOption.unformatted)
            canon = apply_column_aliases(records, aliases)
            index = {}
            for i, rec in enumerate(canon):
                key = str(rec.get(key_column, ""))
                if key:
                    index[key] = (i, rec)
            data, appends, updated = [], [], 0
            pending = {}   # H12/M33: новый ключ, уже накопленный в appends этого батча
            for row in rows:
                key = str(row.get(key_column, ""))
                hit = index.get(key) if key else None
                if hit is None and key in pending:
                    # второй ряд с тем же НОВЫМ ключом в одном вызове: мержим в
                    # уже накопленный append (одно видео из двух батчей выдачи),
                    # иначе в лист легли бы две строки одного raw_id.
                    pos, prev = pending[key]
                    out = merge(row, prev) if merge else {**prev, **row}
                    appends[pos] = [cell(self._to_actual(tab_key, out).get(h, ""))
                                    for h in headers]
                    pending[key] = (pos, out)
                    continue
                out = merge(row, hit[1] if hit else None) if merge else dict(row)
                actual = self._to_actual(tab_key, out)
                if hit is None:
                    appends.append([cell(actual.get(h, "")) for h in headers])
                    if key:
                        pending[key] = (len(appends) - 1, out)
                    continue
                for col, value in actual.items():
                    if col in headers:
                        data.append({"range": rowcol_to_a1(hit[0] + 2,
                                                           headers.index(col) + 1),
                                     "values": [[cell(value)]]})
                updated += 1
                # повторное вхождение того же ключа мержится с УЖЕ обновлёнными
                # значениями, а не с устаревшим снимком листа
                index[key] = (hit[0], {**hit[1], **out})
            if data:
                # RAW (H17): caption/transcript_text контролирует автор ролика —
                # user_entered исполнял бы '='-строки как формулы (инъекция) и
                # парсил числоподобный текст в даты (порча типов, ru_RU-локаль).
                ws.batch_update(data, value_input_option=ValueInputOption.raw)
            if appends:
                ws.append_rows(appends, value_input_option="RAW")
            return {"updated": updated, "appended": len(appends)}

        return self._retry(tab_key, op, f"upsert {tab_key}")

    def update_row_fields(self, tab_key, key_column, key_value, fields,
                          optional_fields=None):
        key_column = self._actual_name(tab_key, key_column)
        fields = self._to_actual(tab_key, fields)
        # optional_fields пишутся только при наличии колонки в живых заголовках —
        # мягкая миграция схемы (reviewed_at добавляет оператор; до этого — no-op),
        # в отличие от fields, где отсутствие колонки — ошибка.
        optional = self._to_actual(tab_key, optional_fields or {})

        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            self._require_headers(tab_key, headers, fields)
            fields.update({c: v for c, v in optional.items() if c in headers})
            # UNFORMATTED_VALUE: как в read_rows — иначе числовые ключи не совпадут
            # с ключами вызывающего (ru_RU-локаль рендерит числа с пробелами/запятыми).
            for i, rec in enumerate(ws.get_all_records(
                    value_render_option=ValueRenderOption.unformatted)):
                if str(rec.get(key_column)) == str(key_value):
                    # Все изменённые поля строки — ОДНИМ batch_update (раньше был цикл
                    # update_cell: N HTTP на решение -> 429 на ревью пачки). Диапазоны
                    # поячеечные, чтобы не затирать непереданные колонки между ними.
                    data = [{"range": rowcol_to_a1(i + 2, headers.index(col) + 1),
                             "values": [[str(value)]]}
                            for col, value in fields.items()]
                    if data:
                        # RAW (H17): значения пишутся как есть — текст ревью не
                        # должен парситься как формула/дата (чтение numericise'ит само).
                        ws.batch_update(data, value_input_option=ValueInputOption.raw)
                    return True
            return False

        return self._retry(tab_key, op, f"update {tab_key}")

    def update_rows_where(self, tab_key, match, fields, exclude=None):
        match = self._to_actual(tab_key, match)
        fields = self._to_actual(tab_key, fields)
        # exclude: строка пропускается, когда ВСЕ exclude-колонки совпали (той же
        # нормализацией, что match). Нужен M36: деактивация прежних версий промпта
        # не должна гасить только что дописанную новую строку.
        exclude = self._to_actual(tab_key, exclude or {})

        def norm(v):
            return str(v).strip().upper()

        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            self._require_headers(tab_key, headers, fields)
            # Все совпавшие строки копим и шлём ОДНИМ batch_update (раньше: update_cell
            # на каждую ячейку каждой строки — до сотен HTTP на массовом апдейте).
            data, updated = [], 0
            for i, rec in enumerate(ws.get_all_records(
                    value_render_option=ValueRenderOption.unformatted)):
                if not all(norm(rec.get(k)) == norm(v) for k, v in match.items()):
                    continue
                if exclude and all(norm(rec.get(k)) == norm(v)
                                   for k, v in exclude.items()):
                    continue
                for col_name, value in fields.items():
                    data.append({"range": rowcol_to_a1(i + 2, headers.index(col_name) + 1),
                                 "values": [[str(value)]]})
                updated += 1
            if data:
                ws.batch_update(data, value_input_option=ValueInputOption.raw)  # H17
            return updated

        return self._retry(tab_key, op, f"update {tab_key}")

    def ensure_tab(self, tab_key, headers):
        """Создать вкладку с заголовками, если её нет. Возвращает True, если создали."""
        title = self.config["tabs"][tab_key]

        def op():
            book = self._spreadsheet()
            existing = {ws.title: ws for ws in book.worksheets()}
            if title in existing:
                ws = existing[title]
                # Вкладка есть, но без строки заголовков (add_worksheet прошёл, а запись
                # заголовков упала прежде) — дописываем, иначе append писал бы в пустоту.
                if not any(str(v).strip() for v in ws.row_values(1)):
                    ws.append_row(headers, value_input_option="RAW")
                    return True
                return False
            ws = book.add_worksheet(title=title, rows=200, cols=len(headers))
            ws.append_row(headers, value_input_option="RAW")
            return True

        created = self._retry(tab_key, op, f"ensure {tab_key}")
        # Схема вкладки изменилась (создана/дописаны заголовки) — сбрасываем кэши,
        # чтобы последующие _ws/чтения взяли свежий хэндл и заголовки.
        self._invalidate(tab_key)
        return created

    def set_column_by_key(self, tab_key, key_column, field, mapping):
        """Записать колонку field по ключу key_column одним вызовом API.

        mapping: {key_value: field_value}. Строки без ключа в mapping сохраняют
        текущее значение. Колонки нет — создаётся. Возвращает число обновлённых строк.
        """
        key_column = self._actual_name(tab_key, key_column)
        field = self._actual_name(tab_key, field)

        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            col = headers.index(field) + 1 if field in headers else len(headers) + 1
            if col > ws.col_count:
                ws.add_cols(col - ws.col_count)
            # UNFORMATTED_VALUE: нетронутые строки переписываются str(rec[field]) —
            # FORMATTED вернул бы «1 000» и перезаписал бы число текстом (см. read_rows).
            records = ws.get_all_records(value_render_option=ValueRenderOption.unformatted)
            values, updated = [[field]], 0
            for rec in records:
                key = str(rec.get(key_column))
                if key in mapping:
                    values.append([str(mapping[key])])
                    updated += 1
                else:
                    values.append([str(rec.get(field, ""))])
            letter = _col_letter(col)
            ws.update(range_name=f"{letter}1:{letter}{len(records) + 1}", values=values)
            return updated

        updated = self._retry(tab_key, op, f"set column {tab_key}")
        # Могли создать новую колонку — сбрасываем кэш заголовков (используется лёгким
        # чтением-проекцией), чтобы новая колонка не потерялась при следующем read_rows.
        self._hdr_cache.pop(tab_key, None)
        return updated

    def replace_rows(self, tab_key, rows):
        """Полностью заменить строки данных вкладки (строка заголовков сохраняется).

        Точечный откат для cf restore: пишем rows начиная с A2 и, если их меньше
        прежнего, добиваем хвост пустыми строками — иначе от старого снимка остались
        бы «висячие» строки ниже новых данных. Ключи rows — канонические, переводятся
        в фактические колонки (те же column_aliases, что append_row). Строки пишутся
        строго по заголовкам вкладки: лишние ключи отбрасываются. Возвращает число
        записанных строк данных.
        """
        rows_actual = [self._to_actual(tab_key, r) for r in rows]

        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            n_existing = max(len(ws.get_all_values()) - 1, 0)
            n = max(n_existing, len(rows_actual))
            if n == 0:
                return 0
            matrix = []
            for i in range(n):
                if i < len(rows_actual):
                    matrix.append([str(rows_actual[i].get(h, "")) for h in headers])
                else:
                    matrix.append(["" for _ in headers])  # затираем устаревший хвост
            end_col = _col_letter(len(headers))
            ws.update(range_name=f"A2:{end_col}{n + 1}", values=matrix,
                      value_input_option="RAW")
            return len(rows_actual)

        return self._retry(tab_key, op, f"replace {tab_key}")
