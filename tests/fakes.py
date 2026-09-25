import json
from pathlib import Path
import logging

from gspread.exceptions import APIError, WorksheetNotFound

from cf.columns import apply_column_aliases
from cf.sheets import HEAVY_COLUMNS, UnknownFieldsError

logger = logging.getLogger(__name__)


class _FakeResponse:
    """Минимальный requests.Response для конструктора gspread APIError."""

    def __init__(self, status_code, message="transient api error"):
        self.status_code = status_code
        self.text = message

    def json(self):
        return {"error": {"code": self.status_code, "message": self.text,
                          "status": "UNAVAILABLE"}}


def _api_error(status_code=503):
    """Настоящий gspread.exceptions.APIError — как транзиентный/протухший хэндл."""
    return APIError(_FakeResponse(status_code))


def _col_num(letters):
    """'A' -> 1, 'C' -> 3, 'AA' -> 27."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n


def _parse_col_range(rng):
    """A1-диапазон -> (start_col, end_col), 1-based включительно.

    Понимает целоколоночные ('A:C') и обычные ('A2:C5') формы, срезает префикс
    имени листа ('Title'!A:C), как это делает реальный batch_get."""
    body = rng.split("!")[-1]
    left, _, right = body.partition(":")

    def col_of(part):
        return _col_num("".join(ch for ch in part if ch.isalpha()))

    start = col_of(left)
    end = col_of(right) if right else start
    return start, end


def _numericise(value):
    """Приводит числоподобные строки к int/float, как gspread.get_all_records.

    Трогаем только строки: локально-форматированное «1 000» (пробел-разделитель
    тысяч) не парсится и остаётся строкой; уже типизированные числа не портим
    (int(19.833) обрезал бы дробную часть)."""
    if not isinstance(value, str) or value == "":
        return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


class FakeWorksheet:
    def __init__(self, headers, rows=None, fail_reads=0, fail_appends=0, fail_after_appends=0,
                 fail_updates=0, unformatted_rows=None, title=None, interleave_row=None,
                 fail_reads_api=0, fail_after_append_rows=0):
        self.headers = list(headers)
        self.rows = [list(r) for r in (rows or [])]
        # Значения без локальной форматки (как UNFORMATTED_VALUE у реального Sheets).
        # Если не заданы, отдаём rows — для тестов, где форматирование не важно.
        self.unformatted_rows = ([list(r) for r in unformatted_rows]
                                 if unformatted_rows is not None else None)
        self.fail_reads = fail_reads
        # Транзиентная APIError на чтении (протухший/битый хэндл): проверяем, что
        # слой Sheets инвалидирует кэш worksheet-хэндла и пересоздаёт его (P3.1).
        self.fail_reads_api = fail_reads_api
        self.fail_appends = fail_appends  # сбой ДО записи (запись не произошла)
        self.fail_after_appends = fail_after_appends  # сбой ПОСЛЕ записи (сервер применил, ответ потерян)
        # То же для батч-аппенда: строки приземлились, ответ потерян — ретрай upsert
        # должен перечитать лист и увидеть их существующими (без дублей).
        self.fail_after_append_rows = fail_after_append_rows
        # Тест-хук P1.5: чужая строка, которую параллельный аппендер вписывает МЕЖДУ
        # приземлением нашей записи и ретраем (когда ответ сервера потерян после записи).
        # Ломает наивную проверку «наша строка — последняя»: после интерливинга последней
        # оказывается чужая. Вставляется один раз, вместе со срабатыванием fail_after_appends.
        self.interleave_row = list(interleave_row) if interleave_row is not None else None
        self.fail_updates = fail_updates  # сбой update_cell/update (для ретрай-веток записи)
        self.update_calls = 0
        self.update_cell_calls = 0    # P3.2: убеждаемся, что запись ушла в batch, не в цикл update_cell
        self.batch_update_calls = 0   # P3.2: число вызовов values-batch_update
        self.append_rows_calls = 0    # P3.3: батч-аппенд одним вызовом
        self.batch_get_ranges = None  # P3.7: диапазоны последней проекции чтения
        self.fetched_columns = None   # P3.7: имена колонок, реально прочитанных проекцией
        self.get_all_records_calls = 0  # P3.4: спай на полное чтение вкладки
        self.col_values_reads = 0     # P3.4: спай на дешёвое чтение одной колонки
        self.col_count = len(self.headers)  # как grid limit в реальном Sheets
        self.title = title

    def add_cols(self, n):
        self.col_count += n

    def _src_rows(self, value_render_option):
        wants_unformatted = getattr(value_render_option, "value",
                                    value_render_option) == "UNFORMATTED_VALUE"
        if wants_unformatted and self.unformatted_rows is not None:
            return self.unformatted_rows
        return self.rows

    def get_all_records(self, value_render_option=None, **kwargs):
        self.get_all_records_calls += 1
        if self.fail_reads_api > 0:
            self.fail_reads_api -= 1
            raise _api_error()
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise ConnectionError("transient")
        rows = self._src_rows(value_render_option)
        # Реальный gspread дополняет короткие строки до длины заголовков "" и
        # нумеризует ячейки — воспроизводим оба поведения.
        records = []
        for r in rows:
            padded = list(r) + [""] * (len(self.headers) - len(r))
            records.append({h: _numericise(v)
                            for h, v in zip(self.headers, padded[:len(self.headers)])})
        # Реальный Sheets-API обрезает пустые хвостовые строки (values доходят лишь до
        # последней непустой) — воспроизводим, иначе replace_rows/restore «видели» бы
        # затёртые пустые строки как данные.
        while records and all(str(v) == "" for v in records[-1].values()):
            records.pop()
        return records

    def batch_get(self, ranges, value_render_option=None, **kwargs):
        """Проекция чтения по диапазонам колонок — как gspread batch_get.

        Возвращает список блоков (по блоку на диапазон); каждый блок — строки
        только запрошенных колонок (строка заголовков + данные), с обрезкой пустого
        хвоста, как реальный Sheets-API. Фиксирует, какие колонки реально прочитаны,
        чтобы тест мог убедиться: тяжёлые (raw_json) НЕ выкачаны (P3.7)."""
        if self.fail_reads_api > 0:
            self.fail_reads_api -= 1
            raise _api_error()
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise ConnectionError("transient")
        ranges = list(ranges)
        self.batch_get_ranges = list(ranges)
        src = self._src_rows(value_render_option)
        full = [list(self.headers)]
        for r in src:
            full.append(list(r) + [""] * (len(self.headers) - len(r)))
        fetched = []
        blocks = []
        for rng in ranges:
            start, end = _parse_col_range(rng)
            for h in self.headers[start - 1:end]:
                if h not in fetched:
                    fetched.append(h)
            block = [row[start - 1:end] for row in full]
            while len(block) > 1 and all(str(c) == "" for c in block[-1]):
                block.pop()
            blocks.append(block)
        self.fetched_columns = fetched
        return blocks

    def get_all_values(self):
        # Как и get_all_records, реальный Sheets-API обрезает пустые хвостовые строки —
        # выравниваем, чтобы n_existing в replace_rows совпадал с реальным Sheets.
        rows = [list(r) for r in self.rows]
        while rows and all(str(c) == "" for c in rows[-1]):
            rows.pop()
        return [list(self.headers)] + rows

    def row_values(self, n):
        if n == 1:
            return list(self.headers)
        return self.rows[n - 2]

    def col_values(self, n):
        """Значения одной колонки (заголовок + данные), пустой хвост обрезан —
        как gspread col_values. Дешёвый путь count_rows (P3.4): raw_json не читается."""
        self.col_values_reads += 1
        if self.fail_reads_api > 0:
            self.fail_reads_api -= 1
            raise _api_error()
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise ConnectionError("transient")
        col = [(row[n - 1] if n - 1 < len(row) else "")
               for row in [self.headers] + self.rows]
        while col and str(col[-1]) == "":
            col.pop()
        return col

    def append_row(self, values, value_input_option="RAW"):
        if self.fail_appends > 0:
            self.fail_appends -= 1
            raise ConnectionError("transient before write")
        # Пустая вкладка: первая запись становится строкой заголовков (как в реальном
        # Sheets append_row на только что созданную вкладку).
        if not self.headers:
            self.headers = list(values)
            self.col_count = len(self.headers)
        else:
            self.rows.append(list(values))
        if self.fail_after_appends > 0:
            self.fail_after_appends -= 1
            # Интерливинг: параллельный аппендер вписывает свою строку между
            # приземлением нашей и ретраем -> наша перестаёт быть последней.
            if self.interleave_row is not None:
                self.rows.append(list(self.interleave_row))
                self.interleave_row = None
            raise ConnectionError("network dropped after write")

    def append_rows(self, values, value_input_option="RAW"):
        """Батч-аппенд нескольких строк одним вызовом — как gspread append_rows."""
        if self.fail_appends > 0:
            self.fail_appends -= 1
            raise ConnectionError("transient before write")
        self.append_rows_calls += 1
        for v in values:
            if not self.headers:
                self.headers = list(v)
                self.col_count = len(self.headers)
            else:
                self.rows.append(list(v))
        if self.fail_after_append_rows > 0:
            self.fail_after_append_rows -= 1
            raise ConnectionError("network dropped after batch write")

    def update_cell(self, row, col, value):
        self.update_cell_calls += 1
        if self.fail_updates > 0:
            self.fail_updates -= 1
            raise ConnectionError("transient update")
        self.rows[row - 2][col - 1] = value

    def _set_cell(self, row, col, value):
        while len(self.rows) < row - 1:
            self.rows.append([])
        r = self.rows[row - 2]
        while len(r) < col:
            r.append("")
        r[col - 1] = value

    def batch_update(self, data, value_input_option=None, **kwargs):
        """Батч-запись ячеек одним values-batch_update — как gspread batch_update.

        data: список {'range': A1, 'values': [[...]]}. Пишет каждую ячейку; ретрай-хук
        fail_updates роняет весь батч один раз (как транзиентный сбой сети записи)."""
        if self.fail_updates > 0:
            self.fail_updates -= 1
            raise ConnectionError("transient update")
        self.batch_update_calls += 1
        self.last_batch_update_option = value_input_option  # H17: RAW-инвариант
        for entry in data:
            start, _ = _parse_col_range(entry["range"])
            body = entry["range"].split("!")[-1].split(":")[0]
            start_row = int("".join(ch for ch in body if ch.isdigit()))
            for i, rowvals in enumerate(entry["values"]):
                for j, value in enumerate(rowvals):
                    self._set_cell(start_row + i, start + j, value)

    def update(self, range_name=None, values=None, **kwargs):
        """Запись диапазона 'A2:C42' матрицей — как gspread Worksheet.update.

        Поддерживает и одну колонку (set_column_by_key), и полную матрицу строк
        (replace_rows): каждый элемент values — список ячеек строки слева направо
        от стартовой колонки диапазона."""
        if self.fail_updates > 0:
            self.fail_updates -= 1
            raise ConnectionError("transient update")
        self.update_calls += 1
        start = range_name.split(":")[0]
        letters = "".join(ch for ch in start if ch.isalpha())
        start_col = 0
        for ch in letters:
            start_col = start_col * 26 + (ord(ch.upper()) - ord("A") + 1)
        width = max((len(r) for r in values), default=1)
        if start_col + width - 1 > self.col_count:
            raise ValueError(f"Range {range_name} exceeds grid limits. Max columns: {self.col_count}")
        start_row = int("".join(ch for ch in start if ch.isdigit()))
        for i, rowvals in enumerate(values):
            row_num = start_row + i
            for j, value in enumerate(rowvals):
                col = start_col + j
                if row_num == 1:
                    while len(self.headers) < col:
                        self.headers.append("")
                    self.headers[col - 1] = value
                else:
                    while len(self.rows) < row_num - 1:
                        self.rows.append([])
                    row = self.rows[row_num - 2]
                    while len(row) < col:
                        row.append("")
                    row[col - 1] = value


class FakeClient:
    def __init__(self, worksheets, new_ws_fail_appends=0):
        self._worksheets = dict(worksheets)
        for title, ws in self._worksheets.items():
            ws.title = title
        # Сбой append заголовков у только что созданной add_worksheet вкладки —
        # для теста идемпотентности ensure_tab под ретраем.
        self.new_ws_fail_appends = new_ws_fail_appends
        self.added = []
        # P3.1: реальный gspread на каждый worksheet() делает fetch_sheet_metadata.
        # Счётчики доказывают, что слой Sheets кэширует хэндлы (не дёргает их на
        # каждую операцию) и пересоздаёт их после инвалидации.
        self.open_by_key_calls = 0
        self.worksheet_calls = 0

    def open_by_key(self, key):
        self.open_by_key_calls += 1
        return self

    def worksheet(self, title):
        self.worksheet_calls += 1
        try:
            return self._worksheets[title]
        except KeyError:
            # Как настоящий gspread: отсутствующая вкладка — WorksheetNotFound,
            # не KeyError. На KeyError фейк маскировал баг ретраев (приёмка 31.07).
            raise WorksheetNotFound(title) from None

    def worksheets(self):
        return list(self._worksheets.values())

    def add_worksheet(self, title, rows=0, cols=0):
        ws = FakeWorksheet([], fail_appends=self.new_ws_fail_appends, title=title)
        self._worksheets[title] = ws
        self.added.append(title)
        return ws


class FakeSheets:
    """Мимикрирует публичный интерфейс cf.sheets.Sheets для тестов CLI и логики.

    Моделирует заголовки вкладок: append_row пишет только колонки из заголовков,
    update_row_fields/update_rows_where падают UnknownFieldsError на поле вне
    заголовков — как реальный слой Sheets, чтобы тесты ловили молчаливую потерю
    поля. Заголовки берутся из явного headers={tab_key: [...]} либо выводятся из
    ключей начальных строк; пустая вкладка задаёт заголовки первой записью.

    Заголовки задавать ЯВНО там, где важна схема живого листа: без headers фейк
    выводит их из ключей самой записываемой строки, поэтому любое поле «сохраняется»
    всегда и дрейф схемы (production_notes против фактической колонки notes) в тестах
    невидим. Имена колонок канонические <-> фактические переводятся теми же
    config['column_aliases'], что и в боевом слое (запись — _to_actual, чтение —
    apply_column_aliases), иначе алиасная колонка выглядела бы потерянной."""

    def __init__(self, tables=None, headers=None, config=None, on_append=None,
                 fail_read_tabs=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        # Тест-хук: read_rows(tab) бросает заданное исключение — для best-effort веток
        # (например performance/reels недоступны в formula-guard). {tab_key: Exception}.
        self.fail_read_tabs = dict(fail_read_tabs or {})
        self.appended = []
        # P3.3: раздельные счётчики доказывают, что export-seeds шлёт ОДИН batch-append,
        # а не цикл одиночных append_row (30 URL -> 90+ HTTP -> квота Sheets -> 429).
        self.append_row_calls = 0
        self.append_rows_calls = 0
        self._config = config
        # Тест-хук P1.5: побочный эффект после каждого append_row — сигнатура
        # on_append(sheets, tab_key, stored). Позволяет тесту воспроизвести
        # конкурентную запись (параллельный процесс вписывает свой ряд), пока
        # cmd_add_brief между своей pre-check и записью.
        self.on_append = on_append
        self._headers = {}
        explicit = headers or {}
        for tab_key in set(self.tables) | set(explicit):
            if tab_key in explicit:
                self._headers[tab_key] = list(explicit[tab_key])
            else:
                cols = []
                for r in self.tables.get(tab_key, []):
                    for k in r:
                        if k not in cols:
                            cols.append(k)
                # Пустая вкладка без явных заголовков — схема ещё не известна (None):
                # первая запись её и задаст.
                self._headers[tab_key] = cols or None

    @property
    def config(self):
        # Как у настоящего Sheets.config; по умолчанию tabs выводим из имеющихся вкладок
        # (канон == факт), чего хватает cf backup для обхода всех вкладок.
        if self._config is not None:
            return self._config
        return {"tabs": {k: k for k in self.tables}}

    def _aliases(self, tab_key):
        """canonical -> фактическое имя колонки (как column_aliases боевого слоя)."""
        return (self.config.get("column_aliases", {}) or {}).get(tab_key, {})

    def _to_actual(self, tab_key, row):
        aliases = self._aliases(tab_key)
        return {aliases.get(k, k): v for k, v in row.items()}

    def _actual_name(self, tab_key, column):
        return self._aliases(tab_key).get(column, column)

    def _missing_fields(self, tab_key, fields):
        headers = self._headers.get(tab_key)
        if headers is None:
            return []
        # Сверяем ФАКТИЧЕСКИЕ имена: канонический review_status при алиасе на
        # human_status пропажей не является (как Sheets.missing_columns).
        return [c for c in fields if self._actual_name(tab_key, c) not in headers]

    def read_rows(self, tab_key, include_heavy=False):
        # Как реальный Sheets.read_rows: по умолчанию тяжёлые колонки (raw_json) НЕ
        # отдаются (проекция экономит трафик); include_heavy=True возвращает всё.
        if tab_key in self.fail_read_tabs:
            raise self.fail_read_tabs[tab_key]
        rows = [dict(r) for r in self.tables.get(tab_key, [])]
        if not include_heavy:
            rows = [{k: v for k, v in r.items() if k not in HEAVY_COLUMNS} for r in rows]
        # Фактические колонки листа -> канонические имена (боевой read_rows делает
        # ровно это, оставляя и исходный ключ): без перевода вызывающий не нашёл бы
        # reel_id в строке, где живая колонка называется published_id.
        return apply_column_aliases(rows, self._aliases(tab_key))

    def count_rows(self, tab_key):
        # P3.4: дешёвый счётчик строк вкладки (как чтение одной колонки в боевом слое).
        return len(self.tables.get(tab_key, []))

    def header(self, tab_key):
        # P3.4: строка заголовков одним лёгким запросом (для health-проверки связности).
        headers = self._headers.get(tab_key)
        if headers is not None:
            return list(headers)
        rows = self.tables.get(tab_key, [])
        return list(rows[0].keys()) if rows else []

    def missing_columns(self, tab_key, columns):
        # как боевой слой: пустые заголовки = схема неизвестна, тревогу не поднимаем
        headers = self.header(tab_key)
        if not headers:
            return []
        return [c for c in columns if self._actual_name(tab_key, c) not in headers]

    def append_rows(self, tab_key, rows):
        # Батч-аппенд: как реальный Sheets.append_rows, каждая строка пишется строго
        # по заголовкам (лишние ключи выброшены). Пустой список — no-op без HTTP,
        # поэтому счётчик не растёт (как ранний return у боевого слоя).
        if not rows:
            return
        self.append_rows_calls += 1
        for row in rows:
            self._store_appended(tab_key, row)

    def append_row(self, tab_key, row):
        self.append_row_calls += 1
        self._store_appended(tab_key, row)

    def _store_appended(self, tab_key, row):
        # Канонические имена -> фактические колонки листа ДО сверки с заголовками:
        # иначе production_notes при алиасе на notes считался бы выброшенным.
        actual_row = self._to_actual(tab_key, row)
        headers = self._headers.get(tab_key)
        if headers is None:
            headers = list(actual_row.keys())
            self._headers[tab_key] = headers
        # Реальный Sheets пишет только колонки из заголовков: лишние ключи теряются,
        # отсутствующие становятся "". Как и боевой append_row, громко предупреждаем
        # о выброшенных колонках (дрейф схемы вроде отсутствующей cta), но НЕ падаем.
        stored = {h: actual_row.get(h, "") for h in headers}
        dropped = [k for k in actual_row if k not in headers]
        if dropped:
            logger.warning(
                "append %s: колонки вне заголовков вкладки, значения выброшены: %s",
                tab_key, dropped)
        self.tables.setdefault(tab_key, []).append(stored)
        self.appended.append((tab_key, stored))
        if self.on_append is not None:
            self.on_append(self, tab_key, stored)

    def upsert_rows(self, tab_key, key_column, rows, merge=None):
        # Как реальный Sheets.upsert_rows: существующий ключ -> update строки
        # (последняя на ключ побеждает), новый -> append; merge — хук coalesce.
        table = self.tables.setdefault(tab_key, [])
        # Ключ и хук merge работают с КАНОНИЧЕСКИМИ именами (как боевой upsert_rows),
        # а строки листа лежат под фактическими — переводим на границе.
        actual_key = self._actual_name(tab_key, key_column)
        aliases = self._aliases(tab_key)
        index = {}
        for r in table:
            key = str(r.get(actual_key, ""))
            if key:
                index[key] = r
        updated = appended = 0
        for row in rows:
            key = str(row.get(key_column, ""))
            existing = index.get(key) if key else None
            canon_existing = (apply_column_aliases([existing], aliases)[0]
                              if existing is not None else None)
            out = merge(row, canon_existing) if merge else dict(row)
            if existing is not None:
                headers = self._headers.get(tab_key)
                out = self._to_actual(tab_key, out)
                if headers:
                    out = {k: v for k, v in out.items() if k in headers}
                existing.update(out)
                updated += 1
            else:
                self._store_appended(tab_key, out)
                stored = self.tables[tab_key][-1]
                if key:
                    index[key] = stored
                appended += 1
        return {"updated": updated, "appended": appended}

    def update_row_fields(self, tab_key, key_column, key_value, fields,
                          optional_fields=None):
        missing = self._missing_fields(tab_key, fields)
        if missing:
            logger.warning("update %s: поля вне заголовков, пропущены: %s", tab_key, missing)
            raise UnknownFieldsError(tab_key, missing)
        payload = self._to_actual(tab_key, fields)
        if optional_fields:
            # как боевой слой: optional-колонки без заголовка молча пропускаются
            opt_missing = set(self._missing_fields(tab_key, optional_fields))
            payload.update(self._to_actual(
                tab_key, {k: v for k, v in optional_fields.items()
                          if k not in opt_missing}))
        key_column = self._actual_name(tab_key, key_column)
        for r in self.tables.get(tab_key, []):
            if str(r.get(key_column)) == str(key_value):
                r.update(payload)
                return True
        return False

    def update_rows_where(self, tab_key, match, fields, exclude=None):
        missing = self._missing_fields(tab_key, fields)
        if missing:
            logger.warning("update %s: поля вне заголовков, пропущены: %s", tab_key, missing)
            raise UnknownFieldsError(tab_key, missing)
        match = self._to_actual(tab_key, match)
        fields = self._to_actual(tab_key, fields)
        exclude = self._to_actual(tab_key, exclude) if exclude else exclude

        def norm(v):
            return str(v).strip().upper()

        updated = 0
        for r in self.tables.get(tab_key, []):
            if not all(norm(r.get(k)) == norm(v) for k, v in match.items()):
                continue
            if exclude and all(norm(r.get(k)) == norm(v) for k, v in exclude.items()):
                continue  # как боевой слой: строка-исключение не гасится (M36)
            r.update(fields)
            updated += 1
        return updated

    def ensure_tab(self, tab_key, headers):
        if tab_key in self.tables:
            return False
        self.tables[tab_key] = []
        self._headers[tab_key] = list(headers)
        return True

    def replace_rows(self, tab_key, rows):
        # Полная замена строк данных: как реальный Sheets.replace_rows, пишем строго
        # по заголовкам (лишние ключи отбрасываются, отсутствующие -> "") и
        # каноническими именами по фактическим колонкам.
        rows = [self._to_actual(tab_key, r) for r in rows]
        headers = self._headers.get(tab_key)
        if headers is None:
            headers = list(rows[0].keys()) if rows else []
            self._headers[tab_key] = headers
        stored = [{h: r.get(h, "") for h in headers} for r in rows]
        self.tables[tab_key] = stored
        return len(stored)

    def set_column_by_key(self, tab_key, key_column, field, mapping):
        # set_column создаёт колонку при отсутствии — регистрируем её в заголовках.
        hdr = self._headers.get(tab_key)
        if hdr is not None and field not in hdr:
            hdr.append(field)
        updated = 0
        for r in self.tables.get(tab_key, []):
            key = str(r.get(key_column))
            if key in mapping:
                r[field] = str(mapping[key])
                updated += 1
            else:
                r.setdefault(field, "")
        return updated


def make_raw_row(i, **overrides):
    row = {
        "source_url": f"https://tiktok.com/@acc/video/{i}",
        "account": "acc",
        "views": 1000 + i * 100,
        "likes": 100, "comments": 10, "shares": 5, "saves": 8,
        "posted_at": "2026-07-01",
        "niche": "pets",
        "hook_text": "А вы знали?",
        "duration": 20,
    }
    row.update(overrides)
    return row


def make_ready_theme(root, niche="тест-тема"):
    """Сделать корень раннера «работающим заводом»: тема реально производит.

    С появлением очередей воркеров (queues.py) шаг «Сценарии» не запускается,
    если ни у одной темы нет ВКЛЮЧЁННОГО промпта — платный вызов, который заведомо
    вернёт «работы нет», больше не делается. Тестам, которые проверяют сам прогон
    генератора, нужна хотя бы одна готовая тема: утверждённый рецепт + файл
    промпта. Активную строку prompt_versions отдаёт FakeSheets (см. ready_versions).
    """
    root = Path(root)
    index = root / "formulas" / "_approved" / "index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({"approved": [
        {"name": "recipe", "niche": niche, "version": 1,
         "path": f"formulas/_approved/{niche}/recipe-v1.json"}]},
        ensure_ascii=False), encoding="utf-8")
    prompt = root / "prompts" / "briefs" / niche / "reel.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("# промпт темы\n", encoding="utf-8")
    return niche


def ready_versions(niche="тест-тема"):
    """Строки CF Prompt Versions с активным промптом темы."""
    return [{"prompt_id": f"brief-{niche}-reel", "active": "TRUE",
             "version": "v1", "github_path": f"prompts/briefs/{niche}/reel.md"}]
