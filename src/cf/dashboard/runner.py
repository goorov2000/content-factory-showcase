import concurrent.futures
import hashlib
import functools
import inspect
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from cf import pipeline_lock
from cf.analyze import _num, dedupe_rows
from cf.config import resolve_path
from cf.dashboard.labels import plural_ru
from cf.dashboard.progress import (DEFAULT_ETA_SEC, PIPELINE_STEPS,
                                   PRODUCING_STEPS, median_duration)
from cf.dashboard.autogate import is_auto, proposed_formula_drafts
from cf.dashboard.prompt_apply import (PromptApplyError, apply_candidate,
                                       apply_prompt)
from cf.dashboard.queues import (blocking_gates, build_gates,
                                 niches_awaiting_prompt_draft,
                                 niches_ready_for_briefs,
                                 prompt_candidate_drafts, prompt_drafts,
                                 prompt_gate)
from cf.dashboard.sections import is_valid_niche_name
from cf.profile import CRITICAL_FIELDS
from cf.lock import (LockTimeout, ProcessLock, file_lock,
                     repo_mutation_lock_path, stage_lock_path)
from cf.collect import progress as collect_progress
# ISO ('…Z' и '+00:00') -> aware UTC, мусор -> None. Импортировать
# cf.cli.parse_generated_at нельзя: cli.py уже тянет runner (цикл).
from cf.collect.util import _parse_date_string
from cf.runlog import log_run, now_iso

logger = logging.getLogger(__name__)

# C3.2: raw и stats идут subprocess'ом `python -m cf collect ...` (kind "cli"),
# без n8n. publish остаётся на вебхуке (kind "n8n") до этапа 6.
STAGES = {
    "raw": {"kind": "cli", "commands": [["collect", "tiktok"], ["collect", "instagram"]]},
    "factory": {"kind": "fanout"},
    "publish": {"kind": "n8n"},
    "stats": {"kind": "cli", "commands": [["collect", "performance"]]},
}
# Подписи единиц работы шага «Сбор» — что именно сейчас собирается (лента §7).
PLATFORM_LABELS = {"tiktok": "TikTok", "instagram": "Instagram",
                   "snowball": "Новые источники", "performance": "Статистика"}

# Корень репозитория по умолчанию. Вынесен в модуль ради герметичности тестов:
# раннер читает отсюда formulas/, prompts/briefs/ и proposals/ (очереди воркеров),
# и без подмены сюита на боевом VPS планировала бы черновики промптов по РЕАЛЬНЫМ
# темам завода. Тот же приём, что у DEFAULT_LOCKS_DIR (аудит 2026-07-24, H4/M5).
DEFAULT_ROOT = Path(".")

AUTO_CYCLE = ["raw", "factory"]  # дальше — ворота оператора (см. _cycle_end_note)

# Человеческие имена этапов для заметок оператору. Раньше cycle_note подставляла
# сырой ключ («ошибка на звене «factory»») — оператор видел служебный слаг там,
# где везде в UI написано «Контент-завод».
STAGE_LABELS = {"raw": "Собранные ролики", "factory": "Контент-завод",
                "publish": "Съёмка и публикация", "stats": "Статистика"}

# Агент строки Run Log про ЦИКЛ целиком (разбор 2026-07-27). Звенья пишут
# dashboard-raw/-factory каждое за себя, а у цикла следа не было вовсе: ночью
# 27.07 он оборвался после сбоя сбора, и единственным свидетельством осталась
# cycle_note в памяти процесса — расхождение с железным правилом №6.
CYCLE_STAGE = "cycle"

# Служебный ключ заметки цикла в снимке прогресса. cf-dashboard ходит с
# Restart=always, поэтому cycle_note обязана пережить рестарт: кладём её в тот же
# pipeline-progress.json (атомарная запись под локом уже есть — второй механизм
# персиста заводить незачем). Ключ намеренно не совпадает ни с одним шагом
# PRODUCING_STEPS: и загрузка прогресса, и build_timeline ходят по шагам и чужих
# ключей не замечают.
CYCLE_NOTE_KEY = "__cycle__"

# С чем раннер снимает бриф с очереди фиксера, когда агент отказался переписывать.
# Текст уходит человеку в reviewer_notes, поэтому он про суть («заводу это не по
# силам»), а не про механику отсутствия файла.
FIXER_REFUSED_NOTE = ("завод не переписал сценарий: агент-фиксер не оставил "
                      "исправленной версии — решение за вами")

# P5.7 «Ритуалы недели»: недельные слэш-команды, которые дашборд запускает тем же
# путём, что звенья фан-аута (_fanout_claude → отчёт в «Отчёты звеньев»). Это НЕ
# звенья конвейера: у них нет вебхука/взаимных исключений/цикла — один вызов claude.
# key совпадает с sections.RITUAL_SPECS (маршрут /rituals/<key>/run); отчёты идут в
# канал reports["ritual-<key>"].
RITUALS = {
    "eval": {"command": "/cf-eval", "label": "Недельный eval",
             "agent": "eval-agent"},
    "tune-sources": {"command": "/cf-tune-sources", "label": "Тюнинг источников",
                     "agent": "source-tuner"},
}

# «Ниши ждут brief-промпта»: точечный прогон агента, пишущего промпт ниши по её
# УТВЕРЖДЁННЫМ формулам. Не ритуал (аргумент-ниша, запуск не по расписанию) и не
# звено конвейера (нет мьютекса/вебхука) — тот же один вызов claude, свой канал
# отчётов. Заменил кнопку-скаффолд (P5.2), которая копировала промпт ниши-донора с
# чужими правилами и битой ссылкой на формулу, активировала версию и коммитила без
# ревью — то есть гасила диагностику brief-generator и обходила правило №3.
PROMPT_WRITER_CHANNEL = "prompt-writer"
PROMPT_WRITER = {"command": "/cf-write-brief-prompt", "label": "Промпт ниши",
                 "agent": "brief-prompt-writer",
                 "command_file": (".claude", "commands", "cf-write-brief-prompt.md")}

# Взаимно исключающие звенья: raw и factory не идут одновременно (общие raw-данные).
# Проверка в обе стороны под self._lock (P2.7): stage -> (блокирующее звено, заметка).
_MUTEX_BLOCK = {
    "factory": ("raw", "«Контент-завод» не запущен: raw ещё выгружается"),
    "raw": ("factory", "Сбор не запущен: «Контент-завод» ещё работает"),
}

# Единый порог строк: очередь фан-аута и профайлер (`cf profile --min-rows`) должны
# совпадать, иначе ниши 12–19 строк ставятся в очередь, но всегда валят профиль
# (insufficient_data) и жгут платный вызов claude каждый цикл. Значение — дефолт
# профайлера. cli.cmd_profile импортирует эту же константу как свой дефолт.
PROFILE_MIN_ROWS = 20

# Пороги фан-аута: очередь ниш и размер пула. Переопределяются в dashboard.fanout.
# Дедлайн подпроцесса звена (сбор/claude). Константа, а не литерал: путь
# «убили по таймауту» иначе не проверить тестом за разумное время.
#
# 1800 -> 3600 (разбор 2026-07-27, второй заход). Потолок съела не авария, а
# рост: длительности шага «Сценарии» шли 1230 -> 954 -> 1654 -> 1700 -> 1801.9,
# и на пятом прогоне генератор брифов убили SIGKILL ровно на границе. Отчёта не
# осталось вовсе (исключение летит ДО _add_report), звено легло в деградацию с
# «генерация брифов упала» — а сценариев за сутки не написано ни одного. «Ревью»
# идёт следом: 1735 в пике при том же потолке. Дедлайн нужен (повисший агент
# иначе держит звено и его ОС-лок навсегда), поэтому не снимаем, а поднимаем и
# отдаём в конфиг: dashboard.fanout.subprocess_timeout.
SUBPROCESS_TIMEOUT = 3600

# Доля дедлайна, после которой шаг попадает в деградацию звена. Смысл в том,
# чтобы следующий потолок увидели ЗАРАНЕЕ: сегодняшний обвал был виден в
# step-durations.json четыре прогона подряд, но никому ничего не говорил.
SUBPROCESS_WARN_SHARE = 0.8


FANOUT_DEFAULTS = {
    "workers": 2,                  # параллельных прогонов ниш
    "min_rows": PROFILE_MIN_ROWS,  # строк ниши для очереди == порог профайлера
    "min_views": 1000,             # порог просмотров строки
    # Автопродолжение после закрытия ворот (решение оператора 2026-07-26 —
    # «ехать сразу»). Выключатель здесь, а не в коде: если немедленные платные
    # вызовы после каждого решения начнут раздражать, откат стоит одну строку.
    "autocontinue": True,
    # Аддитивный гейт возврата ниши в очередь: сколько НОВЫХ строк должно
    # набраться после прошлого анализа. До 2026-07-26 гейт был мультипликативным
    # (growth=1.5 × total_rows прошлого анализа): приток линейный, порог — в разы,
    # поэтому интервал между анализами рос экспоненциально и очередь встала совсем
    # (26.07: 0 ниш из 13; мужские-образы 499 строк при пороге 672, бренды-магазины
    # 290/387, мужской-стиль 238/320, стритвир 144/214).
    # Калибровка 40 по фактическому притоку (~115 дедуп-строк TikTok за один
    # автоматический сбор в сутки): мужские-образы ~13 строк/сут -> переанализ раз
    # в ~3 дня, бренды-магазины ~4, мужской-стиль ~5, маркетплейс-подборки ~7.
    # На срезе 26.07 сразу разблокируются две ниши (мужские-образы,
    # маркетплейс-подборки), ещё четыре подходят за трое суток. Если оператор
    # применит ретайр мусорных источников (-27% притока TikTok), каденция
    # растянется в ~1.4 раза и останется в целевом коридоре 3-7 дней; если очередь
    # всё же будет казаться пустой — снижать до 30, а не возвращать множитель.
    # Сколько черновиков промптов тем пишется за один прогон. Без лимита первый же
    # прогон после расшивки завода написал бы черновики на все темы разом — а это
    # платные вызовы под тексты, которые оператор всё равно читает по одному.
    "prompt_drafts_per_run": 2,
    "min_new_rows": 40,
    "briefs_per_formula": 2,       # (используется генератором брифов, не здесь)
    # Дедлайн подпроцесса шага, с. В конфиге, а не только в константе: 27.07
    # генератор брифов упёрся в прежние 1800 и был убит SIGKILL — без отчёта и
    # без единого сценария за сутки. Двигать этот порог должно стоить строки в
    # конфиге, а не правки кода, потому что растёт он от объёма данных завода.
    "subprocess_timeout": SUBPROCESS_TIMEOUT,
    # (читает cf auto-approve, не раннер) кап авто-одобрений формулы за 7 дней.
    # Оставлен равным прежнему литералу argparse: поднимать потолок бессмысленно,
    # пока brief -> reel не замкнут (26.07: 10 approved-брифов, 0 опубликованных
    # роликов) — рост капа лишь ускорит накопление невостребованного склада.
    # Цифру двигает оператор, синхронно с порогом в prompts/agents/brief-generator.md.
    "approve_cap": 5,
    # Нецелевые ниши: классифицируются (чтобы не текли в целевые), но НЕ ставятся в
    # очередь производства — иначе каждый цикл жёг бы платный прогон claude на нишу,
    # по которой бренд никогда не выпускает контент (JE LA PECHE — мужская одежда,
    # см. docs/BUSINESS-CONTEXT.md). Правится в cf.config.json → dashboard.fanout.
    "exclude_niches": [],
}

# Имя ниши из Sheets попадает в аргумент слэш-команды claude: разрешаем только
# буквы (вкл. кириллицу), цифры, дефис, подчёркивание. Пробел/точка-с-запятой/
# перевод строки сломали бы разбор аргументов — такая ниша уходит в error без
# запуска claude.
_NICHE_NAME_RE = re.compile(r"[\w-]+", re.UNICODE)

# session_id из формы /stages/{stage}/reply уходит в argv claude (M22): только
# формат реальных session id — первый символ алфанумерик (токен с ведущим
# дефисом парсился бы клиентом как флаг), дальше [A-Za-z0-9_-].
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ANALYSIS_DIR = Path("agent-runtime/analysis")  # где лежат прошлые отчёты анализа ниш

REPORT_HISTORY_LIMIT = 10   # сколько последних отчётов хранить на звено
REPORT_TEXT_LIMIT = 20000   # обрезка текста отчёта, символов

# «Отчёты звеньев» переживают рестарт дашборда: снимок истории лежит в
# agent-runtime (правило 4 — runtime-артефакты не версионируются). None в
# конструкторе StageRunner — без персиста (тесты не пишут в боевой каталог).
REPORTS_STATE_PATH = Path("agent-runtime/reports/stage-reports.json")

# Прогресс ленты конвейера (Фаза 2б) переживает рестарт дашборда: idle-числа —
# это «прошлый прогон». Тот же каталог, что «Отчёты звеньев» (agent-runtime,
# правило 4 — runtime-артефакты не версионируются). None в конструкторе — без
# персиста (тесты не пишут в боевой каталог).
PROGRESS_STATE_PATH = Path("agent-runtime/reports/pipeline-progress.json")

# Сколько последних замеров длительности шага храним: медиана по ним и есть ETA
# кривой прогресса. Пять — как у median_duration из Run Log: достаточно, чтобы
# сгладить выброс, и достаточно мало, чтобы поспевать за изменившимся объёмом.
DURATION_HISTORY = 5

# Headless claude при недоверенном воркспейсе пишет в stderr «Ignoring N
# permissions.allow entries …: this workspace has not been trusted» и молча
# отклоняет все команды allowlist'а — агент выходит с кодом 0, не сделав ничего
# (так 2026-07-24 полный блок пермишенов маскировался под success звена).
_UNTRUSTED_MARKER = "has not been trusted"
UNTRUSTED_PROBLEM = ("claude игнорирует проектный allowlist — воркспейс не доверен "
                     "(hasTrustDialogAccepted в ~/.claude.json)")

# raw-вкладки для фан-аута (классификация, очередь ниш); сбор идёт через cli-звено
RAW_TABS = (("raw_tiktok", "TikTok"), ("raw_instagram", "Instagram"))

# Сколько последних строк stdout cli-команды уходит в отчёт звена: сводка гейтов
# и итог печатаются в конце, начало (прогресс батчей) оператору не нужно.
CLI_REPORT_TAIL = 20

# Куда кладём лок-файлы захвата звеньев (gitignored). Единый каталог с CLI-локами
# (cf collect/archive берут те же stage-локи через cf.pipeline_lock) — от корня
# репозитория, не от CWD; параметр locks_dir у StageRunner переопределяет для тестов.
DEFAULT_LOCKS_DIR = pipeline_lock.DEFAULT_LOCKS_DIR


_token_cache = {}       # путь → успешно прочитанный токен (кэш на процесс)
_warned_paths = set()   # пути, о которых warning уже выдан — не спамить на каждый POST
_token_lock = threading.Lock()  # звенья постят из разных потоков


def _webhook_token(config):
    """Токен X-CF-Token из файла n8n.webhook_token_file. None — слать без заголовка.

    Кэшируется только успешное чтение (ротация токена — рестарт дашборда).
    Файла нет / пуст / не читается — один warning и POST без заголовка, но
    чтение повторяется на каждом POST: токен, доложенный оператором после
    старта, подхватывается без рестарта (см. docs/n8n-integration.md).
    """
    path = (config or {}).get("n8n", {}).get("webhook_token_file")
    if not path:
        return None
    with _token_lock:
        if path in _token_cache:
            return _token_cache[path]
        reason = "файл пуст"
        try:
            token = Path(resolve_path(path)).read_text(encoding="utf-8").strip()
        except (OSError, ValueError) as exc:  # ValueError: и битая кодировка — деградация
            token, reason = "", str(exc)
        if token:
            _token_cache[path] = token
            return token
        if path not in _warned_paths:
            _warned_paths.add(path)
            logger.warning(
                "webhook-токен не прочитан (%s: %s) — POST уйдёт без X-CF-Token",
                path, reason)
        return None


def _default_post(url, config=None):
    token = _webhook_token(config)
    headers = {"X-CF-Token": token} if token else {}
    httpx.post(url, timeout=30.0, headers=headers).raise_for_status()


def _default_notify(url, payload, config=None):
    """POST со сводкой-JSON на dashboard.notify_url (тот же токен, что у вебхуков).

    Отличие от _default_post — тело запроса (json=payload): n8n превращает его в
    сообщение оператору. Вызывается только best-effort (см. StageRunner._notify),
    поэтому raise_for_status здесь безопасен: исключение ловит вызывающий."""
    token = _webhook_token(config)
    headers = {"X-CF-Token": token} if token else {}
    httpx.post(url, json=payload, timeout=30.0, headers=headers).raise_for_status()


class _StreamTimeout(Exception):
    """Подпроцесс не уложился в дедлайн при потоковом чтении stdout."""


def _stream_process(proc, on_line, timeout, kill):
    """Читает stdout подпроцесса построчно, отдавая строки в ``on_line``.

    Возвращает (stdout, stderr) целиком — контракт _default_run не меняется, но
    строки становятся доступны сразу. stderr читается фоновым потоком (иначе
    заполненный pipe stderr встал бы намертво). Дедлайн проверяется по каждой
    строке и после закрытия stdout.

    На таймауте убираем за собой ЗДЕСЬ: ``kill`` (у вызывающего это kill всей
    группы процессов) → ``wait`` → join читателей → ``_StreamTimeout``. Иначе
    вызывающий звал бы ``communicate()`` на том же pipe, который ещё читает наш
    поток: хвост stdout делился между двумя читателями произвольно, а поток
    получал ValueError на закрытом файле.
    Сбой колбэка прогресса не отменяет прогон — телеметрия не важнее данных.
    """
    deadline = time.monotonic() + timeout
    err_chunks = []
    lines = queue.Queue()

    def drain_stderr():
        try:
            err_chunks.append(proc.stderr.read() or "")
        except Exception:      # noqa: BLE001 — pipe мог закрыться при kill
            pass

    def read_stdout():
        # Читаем в потоке, а не в основном цикле: подпроцесс, который завис МОЛЧА
        # (ни строчки в stdout), иначе держал бы нас в блокирующем чтении до EOF —
        # и дедлайн никогда бы не сработал.
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:      # noqa: BLE001 — pipe закрыт при kill
            pass
        finally:
            lines.put(None)    # маркер конца потока

    # имена нужны в journal/py-spy боевого сервиса и в тестах уборки
    err_thread = threading.Thread(target=drain_stderr, daemon=True,
                                  name="cf-stderr-reader")
    out_thread = threading.Thread(target=read_stdout, daemon=True,
                                  name="cf-stdout-reader")
    err_thread.start()
    out_thread.start()

    def give_up():
        """Убить группу и дождаться читателей, прежде чем отдать управление."""
        kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        raise _StreamTimeout()

    out_lines = []
    failures = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            give_up()
        try:
            line = lines.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue           # тишина в stdout — просто проверяем дедлайн снова
        if line is None:
            break
        out_lines.append(line)
        try:
            on_line(line.rstrip("\n"))
        except Exception:      # noqa: BLE001 — прогресс не ломает звено
            failures += 1
            if failures == 1:
                logger.warning("колбэк прогресса упал на строке вывода",
                               exc_info=True)
    if failures > 1:
        # трейсбек на каждую строку топит настоящую причину в журнале юнита
        logger.warning("колбэк прогресса падал ещё %d раз — прогресс шага неполный",
                       failures - 1)
    try:
        proc.wait(timeout=max(deadline - time.monotonic(), 1))
    except subprocess.TimeoutExpired:
        give_up()
    err_thread.join(timeout=5)
    return "".join(out_lines), "".join(err_chunks)


def _default_run(argv, on_line=None, timeout=None):
    # timeout=None -> модульный дефолт: подпись остаётся совместимой с тестовыми
    # run_command, а StageRunner передаёт своё значение из конфига (partial).
    timeout = SUBPROCESS_TIMEOUT if timeout is None else float(timeout)
    exe = shutil.which(argv[0])
    if exe is None:
        raise RuntimeError(f"{argv[0]} CLI не найден в PATH")
    # Раннер уже держит ОС-лок звена — дочерний `cf collect` не должен брать
    # stage-локи повторно (cf.pipeline_lock.locks_held_by_parent).
    env = dict(os.environ, **{pipeline_lock.LOCKS_HELD_ENV: "1"})
    # M21: своя сессия процессов + kill всей группы по таймауту — иначе SIGKILL
    # получал только сам claude/cf, а его дети (Bash-инструменты, MCP) доживали
    # сиротами и продолжали писать в Sheets после пометки звена error.
    popen_kwargs = {"start_new_session": True} if os.name == "posix" else {}
    p = subprocess.Popen([exe, *argv[1:]], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", env=env, **popen_kwargs)
    def _kill_group():
        if os.name == "posix":
            import signal
            try:
                os.killpg(p.pid, signal.SIGKILL)   # pgid == pid (setsid)
            except ProcessLookupError:
                pass
        else:
            p.kill()

    try:
        if on_line is None:
            stdout, stderr = p.communicate(timeout=timeout)
        else:
            # Живой прогресс (спека §7): stdout читаем построчно по мере вывода, а
            # не целиком в конце — иначе маркеры `CF_PROGRESS` приходят, когда сбор
            # уже закончился, и полоска прыгает 0 → 50%. stderr тянем отдельным
            # потоком: без этого полный pipe stderr заблокировал бы подпроцесс.
            stdout, stderr = _stream_process(p, on_line, timeout=timeout,
                                             kill=_kill_group)
    except subprocess.TimeoutExpired:
        _kill_group()
        p.communicate()
        raise RuntimeError(f"{argv[0]} превысил таймаут {timeout:.0f}с — "
                           f"группа процессов остановлена")
    except _StreamTimeout:
        # группу уже убил и читателей дождался _stream_process — трогать pipe
        # второй раз нельзя
        raise RuntimeError(f"{argv[0]} превысил таймаут {timeout:.0f}с — "
                           f"группа процессов остановлена")
    # stderr отдельным элементом: там живут предупреждения claude (недоверенный
    # воркспейс), которые «stdout or stderr» терял при непустом stdout.
    return (p.returncode, stdout or "", stderr or "")


def _parse_claude_output(raw):
    """Разбирает вывод `claude -p ... --output-format json`.

    Падение claude (не-JSON на stdout, обрыв) — не авария парсера: в этом
    случае в отчёт идёт сырой текст как есть, без session_id.
    """
    try:
        obj = json.loads(raw)
        session_id = obj.get("session_id") or ""
        if "result" in obj:
            result = obj["result"]
            if isinstance(result, str) and result.strip():
                return (result, session_id)
            # ключ есть, но пустой — не подменять сырым JSON целиком
            return ("(пустой ответ агента)", session_id)
        return (raw, session_id)
    except Exception:
        return (raw, "")


def _parse_niche_result(text):
    """Последняя строка `NICHE_RESULT: {…}` из вывода агента ниши, или None.

    Агент обязан её печатать во всех исходах (даже при раннем стопе). Битый JSON
    или отсутствие строки — не авария: вернём None, вызывающий трактует как 0 формул.
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("NICHE_RESULT:"):
            try:
                return json.loads(line[len("NICHE_RESULT:"):].strip())
            except Exception:
                return None
    return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _unclassified_in(rows):
    """Сколько строк без темы (пустая `niche`) в уже прочитанных строках."""
    return sum(1 for r in rows if not str(r.get("niche", "")).strip())


def _accepts_on_line(fn):
    """Умеет ли ``fn`` принимать колбэк ``on_line``.

    Смотрим подпись, а НЕ «вызвать и поймать TypeError»: вызов команды сбора —
    платный побочный эффект (прогон Apify + запись в Sheets), а TypeError из тела
    уже выполненной команды неотличим от «не та арность». Прошлая версия в этом
    случае запускала сбор второй раз.

    Подпись недоступна (C-функция, Mock) → считаем, что колбэк принимается:
    лишний аргумент виден сразу и ничего не выполняет дважды.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    try:
        signature.bind_partial(["argv"], on_line=lambda line: None)
    except TypeError:
        return False
    return True


class StageRunner:
    def __init__(self, sheets, config, http_post=None, run_command=None,
                 sleep_fn=None, now_fn=None, analysis_dir=None, locks_dir=None,
                 http_notify=None, reports_path=None, progress_path=None,
                 root=None):
        self.sheets = sheets
        # Корень репозитория: раннер уже предполагал cwd=корень (_git_commit), но
        # проверка наличия слэш-команды должна быть подменяемой в тестах.
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.config = config or {}
        # дефолтный POST получает конфиг раннера (путь к webhook-токену)
        self.http_post = http_post or (lambda url: _default_post(url, self.config))
        # уведомление о завершении цикла — отдельный poster (несёт JSON-тело);
        # тоже получает конфиг раннера для webhook-токена
        self.http_notify = http_notify or (
            lambda url, payload: _default_notify(url, payload, self.config))
        # Дедлайн подпроцесса берём из конфига (dashboard.fanout.subprocess_timeout)
        # и зашиваем в дефолтный run_command. Инжектированный run_command (тесты,
        # кастомная обвязка) не трогаем: у него свой контракт.
        self.subprocess_timeout = float(
            self._fanout_params().get("subprocess_timeout", SUBPROCESS_TIMEOUT))
        self.run_command = run_command or functools.partial(
            _default_run, timeout=self.subprocess_timeout)
        self.sleep = sleep_fn or time.sleep
        self.now = now_fn or time.monotonic
        # база для глоба прошлых анализов — параметр ради тестов (cwd-независимость)
        self.analysis_dir = Path(analysis_dir) if analysis_dir is not None else ANALYSIS_DIR
        # каталог лок-файлов звеньев — параметр ради изоляции тестов от боевых локов
        self.locks_dir = Path(locks_dir) if locks_dir is not None else DEFAULT_LOCKS_DIR
        self.state = {s: {"status": "idle", "detail": ""} for s in STAGES}
        self.reports = {s: [] for s in STAGES}
        # P5.7: каналы отчётов ритуалов ("ritual-eval"/"ritual-tune-sources") и их
        # состояние — отдельно от звеньев, чтобы не примешиваться к STAGES/циклу.
        self.reports.update({f"ritual-{r}": [] for r in RITUALS})
        self.ritual_state = {r: {"status": "idle"} for r in RITUALS}
        # Канал агента brief-промптов и его состояние по нишам (niche -> status).
        # Ключ заводится ДО _load_reports: тот отбрасывает неизвестные каналы, и
        # без этой строки персист истории агента терялся бы при каждом рестарте.
        self.reports[PROMPT_WRITER_CHANNEL] = []
        self.prompt_writer_state = {}
        # Персист «Отчётов звеньев» (None — только в памяти, как раньше): история
        # раньше жила в процессе и терялась при каждом рестарте дашборда — вместе
        # с единственным следом заблокированных claude-звеньев.
        self.reports_path = Path(reports_path) if reports_path is not None else None
        self._reports_lock = threading.Lock()  # звенья пишут отчёты из своих потоков
        if self.reports_path is not None:
            self._load_reports()
        # Фаза 2б: per-run прогресс производящих шагов ленты (Сбор→Ревью). Счётчики
        # обнуляются на старте прогона, «прошлый прогон» показывается в idle.
        # Downstream-шаги (Одобрение/Съёмка/Статистика) сюда НЕ входят — они живой
        # бэклог из overview_metrics. Пишется из фоновых потоков → свой лок.
        self.run_progress = self._empty_progress()
        self._progress_lock = threading.Lock()
        self.progress_path = Path(progress_path) if progress_path is not None else None
        # Собственные замеры длительностей шагов — источник ETA для кривой прогресса
        # (Run Log для этого непригоден, см. _stage_etas). Лежат рядом со снимком.
        self.step_durations = {}
        self.durations_path = (self.progress_path.with_name("step-durations.json")
                               if self.progress_path is not None else None)
        # поток закрытия следа прерванного прогона: ссылку держим, чтобы тесты
        # (и будущий graceful shutdown) могли дождаться записи, а не угадывать
        self._startup_log_thread = None
        # Заметка цикла объявляется ДО загрузки прогресса (разбор 2026-07-27):
        # снимок несёт её с прошлого запуска, и присваивание после _load_progress
        # затирало бы восстановленный текст пустой строкой.
        self.cycle_note = ""
        if self.progress_path is not None:
            self._load_progress()
            self._load_durations()
        self._lock = threading.Lock()
        self._stage_locks = {}   # stage -> ProcessLock, удерживается на время прогона
        # Момент старта прогона звена в СТЕННЫХ часах (now_iso, НЕ self.now —
        # там monotonic-float, в Run Log его класть нельзя). Кладётся при входе
        # в run_sync/_reply_sync, снимается в _finish и уходит в started_at
        # строки dashboard-<звено>. Одно звено в один момент ведёт ровно один
        # поток (ОС-лок + self._lock), поэтому обычного dict хватает.
        self._stage_started = {}
        # Цикл активен между звеньями, даже когда ни одно звено не 'running'
        # (зазор перед захватом следующего). Держит any_running() → HTMX-поллинг
        # не обрывается посреди цикла (P2.7).
        self._cycle_active = False

    def any_running(self):
        # Признак «есть живая активность» для HTMX-поллинга «Отчётов/Конвейера».
        # Ритуал и агент brief-промптов сюда входят намеренно: их отчёт должен
        # подхватиться без ручного F5. Для ГЕЙТИНГА цикла это использовать нельзя
        # (ни то, ни другое циклу не помеха) — там _pipeline_busy (P5.7).
        return self._pipeline_busy() or any(
            r["status"] == "running" for r in self.ritual_state.values()) or any(
            s == "running" for s in self.prompt_writer_state.values())

    def _pipeline_busy(self):
        """Занят ли конвейер звеньями/циклом. Ритуалы (cf-eval/cf-tune-sources) сюда
        НЕ входят: 10-30-минутный eval не должен блокировать «Запустить цикл» (P5.7)."""
        return self._cycle_active or any(
            s["status"] == "running" for s in self.state.values())

    def _add_report(self, stage, title, text, session_id=""):
        with self._reports_lock:
            history = self.reports[stage]
            history.append({
                "title": title,
                # обрезаем с начала: хвост важнее — вопросы агента идут в конце текста
                "text": (text or "").strip()[-REPORT_TEXT_LIMIT:],
                "session_id": session_id or "",
                "at": datetime.now().strftime("%H:%M"),
                "day": datetime.now().strftime("%Y-%m-%d"),
            })
            del history[:-REPORT_HISTORY_LIMIT]  # держим только последние N
            self._save_reports_locked()

    def _save_reports_locked(self):
        """Снимок всех каналов отчётов на диск (вызывается под _reports_lock).

        Best-effort: сбой записи не влияет на звено — история остаётся в памяти.
        Атомарность против второго процесса (отладочный дашборд/slash-команда из
        того же корня): tmp-имя с pid — чужой write_text не перезапишет наш tmp,
        а os.replace под межпроцессным file_lock не наедет на чужую запись.
        Полный снапшот СВОЕГО процесса: долгая параллельная работа двух раннеров
        с одним файлом не поддерживается (боевой сервис один) — проигравший
        перетирает страницы истории другого, но файл всегда цел."""
        if self.reports_path is None:
            return
        tmp = self.reports_path.with_name(
            f"{self.reports_path.name}.{os.getpid()}.tmp")
        try:
            self.reports_path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(self._reports_file_lock_path()):
                tmp.write_text(json.dumps(self.reports, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                os.replace(tmp, self.reports_path)
        except Exception:
            logger.warning("не удалось сохранить отчёты звеньев в %s",
                           self.reports_path, exc_info=True)
            tmp.unlink(missing_ok=True)   # не плодить осиротевшие tmp

    def _reports_file_lock_path(self):
        return self.reports_path.with_name(self.reports_path.name + ".lock")

    def _load_reports(self):
        """Восстановить «Отчёты звеньев» после рестарта дашборда.

        Битый/отсутствующий файл и незнакомые каналы — не авария: история просто
        начнётся заново. Отчётам прошлых дней в «at» дописывается дата — иначе
        вчерашний «17:23» после рестарта читается как сегодняшний."""
        try:
            data = json.loads(self.reports_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("файл отчётов %s не прочитан — история начнётся заново",
                           self.reports_path, exc_info=True)
            return
        if not isinstance(data, dict):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        for channel, entries in data.items():
            if channel not in self.reports or not isinstance(entries, list):
                continue
            clean = []
            for e in entries:
                if not (isinstance(e, dict) and isinstance(e.get("title"), str)
                        and isinstance(e.get("text"), str)):
                    continue
                e.setdefault("session_id", "")
                e.setdefault("at", "")
                e.setdefault("day", "")
                # идемпотентно: «at» с уже дописанной датой (перезапись файла между
                # рестартами) не префиксуется повторно — иначе дата копилась бы
                # с каждым рестартом («2026-07-24 2026-07-24 17:23…»)
                if (e["day"] and e["day"] != today
                        and not e["at"].startswith(e["day"])):
                    e["at"] = f"{e['day']} {e['at']}".strip()
                clean.append(e)
            self.reports[channel][:] = clean[-REPORT_HISTORY_LIMIT:]

    # ── прогресс ленты конвейера (Фаза 2б) ───────────────────────────────────
    # Слой строго additive: source of truth по статусу/локам звена — self.state;
    # run_progress лишь показывает вертикальной ленте, какой шаг сейчас идёт и
    # насколько. Все апдейты best-effort (обёрнуты try) — сбой прогресса НИКОГДА
    # не роняет прогон звена.

    def _empty_progress(self):
        # Поля фаз (прогресс v2, спека §7): что за единица работы идёт сейчас
        # (unit_label/phase_plan), в какой фазе она и сколько внутри фазы сделано.
        # produced/produced_new/left — результат шага в его собственных единицах
        # (ролики, размеченные строки, сценарии); duration — сколько шаг шёл.
        return {key: {"status": "idle", "count": 0, "last_count": 0,
                      "done": None, "total": None,
                      "started_at": None, "eta_median": None, "duration": None,
                      "produced": None, "produced_new": None, "left": None,
                      "left_unknown": False, "idle_reason": "",
                      "unit_label": "", "phase_plan": None, "phase": None,
                      "phase_done": None, "phase_total": None,
                      "phase_started_at": None}
                for key in PRODUCING_STEPS}

    def _producing_steps_for_stage(self, stage):
        """Производящие шаги ленты, которые ведёт это звено (raw→collect;
        factory→classify/analyze/briefs/review; stats/publish — downstream, []).
        """
        return [s["key"] for s in PIPELINE_STEPS
                if s["producing"] and s["stage"] == stage]

    def _stage_etas(self, steps):
        """Ожидаемая длительность шагов (сек) для кривой прогресса.

        Порядок источников — от точного к грубому:
        1. собственные замеры прошлых прогонов (`_record_duration`) — раннер сам
           засекает старт и финиш шага, точнее этого ничего нет;
        2. медиана из Run Log по агенту шага (collect-tiktok, niche-classifier,
           niche-pipeline, brief-generator, brief-reviewer) — с появлением
           `cf log-run --started-at` она наконец не нулевая, но покрывает лишь
           прогоны, где агент/CLI передали старт; строки старого формата
           (started_at == completed_at) median_duration отбрасывает как нулевые;
        3. DEFAULT_ETA_SEC.

        Пункт 2 и был причиной «шаг обещал полторы минуты, а шёл 18» (жалоба
        оператора 2026-07-25): 90 секунд — это дефолт «истории нет», а не замер.
        Best-effort: сбой Sheets → идём дальше по списку.
        """
        try:
            rows = self.sheets.read_rows("run_log")
        except Exception:
            rows = []
        etas = {}
        for s in PIPELINE_STEPS:
            if s["key"] not in steps:
                continue
            measured = self._measured_eta(s["key"])
            logged = median_duration(rows, s["agent"]) if s.get("agent") else None
            etas[s["key"]] = measured or logged or DEFAULT_ETA_SEC
        return etas

    def _measured_eta(self, key):
        """Медиана собственных замеров шага (последние DURATION_HISTORY), или None."""
        values = sorted(self.step_durations.get(key) or ())
        if not values:
            return None
        mid = len(values) // 2
        return (values[mid] if len(values) % 2
                else (values[mid - 1] + values[mid]) / 2.0)

    def _record_duration(self, key, seconds):
        """Запомнить длительность успешно завершённого шага (под _progress_lock).

        Только `done`: прерванный или упавший шаг говорит о сбое, а не о том,
        сколько работа занимает, — на такой медиане кривая следующего прогона
        врала бы.
        """
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return
        history = self.step_durations.setdefault(key, [])
        history.append(round(float(seconds), 1))
        del history[:-DURATION_HISTORY]
        self._save_durations()

    def _save_durations(self):
        """Замеры на диск (best-effort, вызывается под _progress_lock)."""
        if self.durations_path is None:
            return
        tmp = self.durations_path.with_name(
            f"{self.durations_path.name}.{os.getpid()}.tmp")
        try:
            self.durations_path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(self.durations_path.with_name(
                    self.durations_path.name + ".lock")):
                tmp.write_text(json.dumps(self.step_durations, ensure_ascii=False,
                                          indent=1), encoding="utf-8")
                os.replace(tmp, self.durations_path)
        except Exception:
            logger.warning("замеры длительностей %s не записаны",
                           self.durations_path, exc_info=True)

    def _load_durations(self):
        """Замеры с диска: битый/чужой файл — не авария, начнём копить заново."""
        try:
            data = json.loads(self.durations_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("файл замеров %s не прочитан", self.durations_path,
                           exc_info=True)
            return
        if not isinstance(data, dict):
            return
        for key in PRODUCING_STEPS:
            values = data.get(key)
            if isinstance(values, list):
                clean = [float(v) for v in values
                         if isinstance(v, (int, float)) and v > 0]
                if clean:
                    self.step_durations[key] = clean[-DURATION_HISTORY:]

    def _reset_steps_locked(self, steps, etas=None):
        """Обнулить шаги за прогон (под _progress_lock): last_count←count, count←0,
        полоска, счётчик, таймер и фазы гаснут. ``etas=None`` — eta_median не
        трогаем: сбросу на старте цикла кривые ещё не нужны, их поставит
        _progress_begin, когда звено реально стартует.
        """
        for key in steps:
            st = self.run_progress[key]
            st["last_count"] = st.get("count") or st.get("last_count") or 0
            st.update(status="idle", count=0, done=None, total=None,
                      started_at=None, duration=None, produced=None,
                      produced_new=None, left=None, left_unknown=False,
                      # причина простоя — свойство ПРОШЛОГО прогона; оставленная
                      # висеть, она объясняла бы новый прогон старыми словами
                      idle_reason="",
                      unit_label="", phase_plan=None, phase=None,
                      phase_done=None, phase_total=None, phase_started_at=None)
            if etas is not None:
                st["eta_median"] = etas.get(key)

    def _progress_begin(self, stage):
        """Старт звена обнуляет его производящие шаги за прогон: last_count←count,
        count←0, done/total/started_at сбрасываются, eta берётся из медиан. No-op
        для звеньев без производящих шагов (stats/publish)."""
        try:
            steps = self._producing_steps_for_stage(stage)
            if not steps:
                return
            etas = self._stage_etas(steps)
            with self._progress_lock:
                self._reset_steps_locked(steps, etas)
                self._save_progress_locked()
        except Exception:
            logger.warning("сброс прогресса звена «%s» не удался", stage, exc_info=True)

    def _progress_reset_cycle(self):
        """Обнулить полоски ВСЕХ шагов цикла — в момент старта, а не когда до
        звена дойдёт очередь.

        ▶ «Полный цикл» обещает прогон целиком, но `_progress_begin` сбрасывает
        только шаги стартующего звена: пока идёт `raw`, шаги `factory` держали бы
        зелёные полоски, счётчики и «шёл 19:18» ПРОШЛОГО прогона — оператор читает
        это как «уже сделано сейчас» (замечание 2026-07-25). Без Sheets: здесь
        нужен только визуальный ноль, медианы кривых подтянет `_progress_begin`.
        """
        try:
            steps = [key for stage in AUTO_CYCLE
                     for key in self._producing_steps_for_stage(stage)]
            with self._progress_lock:
                self._reset_steps_locked(steps)
                self._save_progress_locked()
        except Exception:
            logger.warning("сброс прогресса цикла не удался", exc_info=True)

    def _progress_update(self, key, **fields):
        """Точечно обновить поля шага run_progress (status/count/done/total/started_at).

        Переход в терминальный статус здесь же засекает `duration`: раннер —
        единственный, кто знает и старт, и финиш шага. Она нужна дважды: ленте
        (таймер остаётся после финиша — «шёл 18:03») и следующему прогону (медиана
        замеров = ETA кривой).
        """
        try:
            with self._progress_lock:
                st = self.run_progress.get(key)
                if st is None:
                    return
                self._stamp_duration_locked(key, st, fields)
                st.update(fields)
                self._save_progress_locked()
        except Exception:
            logger.warning("обновление прогресса шага «%s» не удалось", key, exc_info=True)

    def _stamp_duration_locked(self, key, state, fields):
        """Дописать в `fields` длительность шага, если он сейчас финиширует."""
        status = fields.get("status")
        if status is None or status == "running" or state.get("status") != "running":
            return
        started = state.get("started_at")
        if not isinstance(started, (int, float)):
            return
        seconds = self.now() - started
        if seconds < 0:
            return
        fields.setdefault("duration", round(seconds, 1))
        if status == "done":
            self._record_duration(key, seconds)

    def _progress_line_reader(self, step):
        """Колбэк для строк stdout сбора: маркер `CF_PROGRESS` → прогресс шага.

        Единственный контракт формата — cf.collect.progress (там же и печать).
        Смена фазы двигает `phase_started_at`, чтобы кривая внутри новой фазы
        считалась от её начала, а не от начала всей команды. Не-маркерные строки
        игнорируем: сводки сбора живут в «Отчётах этапов», а не в прогрессе."""
        def on_line(line):
            marker = collect_progress.parse(line)
            if not marker:
                return
            fields = {}
            if "phase" in marker:
                st = self.run_progress.get(step) or {}
                if st.get("phase") != marker["phase"]:
                    fields["phase_started_at"] = self.now()
                    # новая фаза — счётчики прошлой не переносим
                    fields["phase_done"] = None
                    fields["phase_total"] = None
                fields["phase"] = marker["phase"]
            if "done" in marker:
                fields["phase_done"] = marker["done"]
            if "total" in marker:
                fields["phase_total"] = marker["total"]
            # Итог единицы работы (роликов записано / из них новых) копится по
            # платформам: лента показывает добычу всего прогона, а не последней
            # команды. Маркер с числами приходит один раз на команду — после upsert.
            if "rows" in marker or "new" in marker:
                st = self.run_progress.get(step) or {}
                if "rows" in marker:
                    fields["produced"] = (st.get("produced") or 0) + marker["rows"]
                if "new" in marker:
                    fields["produced_new"] = ((st.get("produced_new") or 0)
                                              + marker["new"])
            if fields:
                self._progress_update(step, **fields)
        return on_line

    def _progress_finish(self, stage, status):
        """Финализ производящих шагов звена: всё, что осталось running, получает
        итоговый статус (обычно error при падении фан-аута) и гасит started_at,
        чтобы лента не крутила бар несуществующего прогона."""
        try:
            steps = self._producing_steps_for_stage(stage)
            with self._progress_lock:
                for key in steps:
                    st = self.run_progress[key]
                    if st["status"] == "running":
                        fields = {"status": status, "started_at": None}
                        # длительность засекаем и у сорвавшегося шага: «сколько он
                        # прожил до сбоя» — это ответ оператору, а не мусор
                        self._stamp_duration_locked(key, st, fields)
                        st.update(fields)
                self._save_progress_locked()
        except Exception:
            logger.warning("финализ прогресса звена «%s» не удался", stage, exc_info=True)

    def _progress_file_lock_path(self):
        return self.progress_path.with_name(self.progress_path.name + ".lock")

    def _progress_payload(self):
        """Что кладём в снимок: шаги ленты плюс заметка цикла отдельным ключом.

        Заметка живёт рядом с шагами, а не в самих шагах (разбор 2026-07-27): она
        про прогон ЦЕЛИКОМ, и подмешивать её в run_progress нельзя — этот словарь
        уходит в build_timeline и в сброс счётчиков за прогон.
        """
        return {**self.run_progress,
                CYCLE_NOTE_KEY: {"note": self.cycle_note, "at": now_iso()}}

    def _save_cycle_note(self):
        """Заметка цикла на диск, тем же атомарным путём, что и прогресс.

        Без этого cycle_note жила только в памяти процесса (атрибут
        StageRunner.cycle_note и больше нигде), а
        cf-dashboard ходит с Restart=always: ночью 27.07 единственное объяснение
        оборванного цикла исчезло вместе с рестартом сервиса. Best-effort — сбой
        записи не трогает исход прогона.
        """
        try:
            with self._progress_lock:
                self._save_progress_locked()
        except Exception:
            logger.warning("заметка цикла не сохранена в %s", self.progress_path,
                           exc_info=True)

    def _save_progress_locked(self):
        """Снимок run_progress на диск (под _progress_lock). Best-effort: сбой не
        влияет на прогон — прогресс остаётся в памяти. Атомарность (tmp+os.replace
        под file_lock) — как у «Отчётов звеньев»."""
        if self.progress_path is None:
            return
        tmp = self.progress_path.with_name(
            f"{self.progress_path.name}.{os.getpid()}.tmp")
        try:
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(self._progress_file_lock_path()):
                tmp.write_text(json.dumps(self._progress_payload(),
                                          ensure_ascii=False, indent=1),
                               encoding="utf-8")
                os.replace(tmp, self.progress_path)
        except Exception:
            logger.warning("снимок прогресса %s не записан", self.progress_path,
                           exc_info=True)

    def _load_progress(self):
        """Восстановить прогресс после рестарта: idle-числа = «прошлый прогон».

        Прогон прошлого процесса мёртв, но «мёртв» и «не начинался» — разные вещи:
        running→interrupted (а не idle), чтобы лента честно сказала «прогон прерван»,
        а не показывала недозаполненную полоску, которую оператор читает как
        «висит» (инцидент 2026-07-25). started_at/eta гасим — анимировать нечего.
        Битый/чужой файл — не авария: прогресс начнётся заново."""
        try:
            data = json.loads(self.progress_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("файл прогресса %s не прочитан — начнём заново",
                           self.progress_path, exc_info=True)
            return
        if not isinstance(data, dict):
            return
        # Заметка прошлого цикла переживает рестарт вместе с числами шагов: утром
        # оператор должен прочитать, чем кончилась ночь, а не пустую строку
        # (разбор 2026-07-27).
        saved_note = data.get(CYCLE_NOTE_KEY)
        if isinstance(saved_note, dict) and isinstance(saved_note.get("note"), str):
            self.cycle_note = saved_note["note"]
        interrupted = []
        for key in PRODUCING_STEPS:
            saved = data.get(key)
            if not isinstance(saved, dict):
                continue
            st = self.run_progress[key]
            st["count"] = _int(saved.get("count"))
            st["last_count"] = _int(saved.get("last_count"))
            st["done"] = saved["done"] if isinstance(saved.get("done"), int) else None
            st["total"] = saved["total"] if isinstance(saved.get("total"), int) else None
            # итоги прошлого прогона переживают рестарт вместе со счётчиками:
            # «сколько роликов собрали и сколько это заняло» после перезапуска
            # сервиса — тот же вопрос, что и до него
            for field in ("produced", "produced_new", "left", "duration"):
                value = saved.get(field)
                st[field] = value if isinstance(value, (int, float)) else None
            # признак «остаток сосчитать не удалось» переживает рестарт вместе с
            # числами: иначе прошлый прогон после перезапуска начинает врать
            st["left_unknown"] = bool(saved.get("left_unknown"))
            status = saved.get("status")
            if status == "running":
                st["status"] = "interrupted"
                interrupted.append(key)
            else:
                st["status"] = "idle" if status is None else str(status)
            st["started_at"] = None
            st["eta_median"] = None
        if interrupted:
            self._close_interrupted_runs(interrupted)

    def _close_interrupted_runs(self, steps):
        """Закрыть след прерванного прогона: нормализация файла + запись в Run Log.

        Процесс умер посреди прогона (рестарт сервиса), поэтому закрывающая строка
        `dashboard-<звено>` в CF Run Log не появилась — дыра в железном правиле №6.

        Порядок важен. Сперва СИНХРОННО сохраняем нормализованный прогресс (это
        локальный файл, он же гарантия от дублей строк при повторных рестартах), и
        только потом пишем в Sheets — в ФОНЕ. Синхронная запись жила в __init__, и
        лежачий Sheets задерживал подъём дашборда именно тогда, когда оператор
        рестартует сервис, чтобы разгрести застрявший прогон.

        Всё best-effort: недоступный Sheets не должен мешать дашборду подняться;
        смерть процесса в первые миллисекунды может стоить строки в Run Log —
        это дешевле, чем неотвечающий дашборд.
        """
        stages = {}
        for step in steps:
            meta = next((s for s in PIPELINE_STEPS if s["key"] == step), None)
            stage = (meta or {}).get("stage")
            if stage:
                stages.setdefault(stage, []).append(
                    (meta or {}).get("label") or step)
        try:
            with self._progress_lock:
                self._save_progress_locked()
        except Exception:
            logger.warning("нормализованный прогресс не сохранён", exc_info=True)
        self._startup_log_thread = threading.Thread(
            target=self._log_interrupted_stages, args=(stages,),
            name="cf-startup-runlog", daemon=True)
        self._startup_log_thread.start()

    def _log_interrupted_stages(self, stages):
        """Строки «прогон прерван» в CF Run Log (фоновый поток старта)."""
        for stage, labels in stages.items():
            try:
                log_run(self.sheets, agent=f"dashboard-{stage}", status="failed",
                        input_summary="прогон прерван: сервис перезапущен "
                                      f"(незакрытые шаги: {', '.join(labels)})",
                        errors=["прогон прерван рестартом сервиса"],
                        trigger_type="dashboard")
            except Exception:
                logger.warning("след прерванного прогона звена «%s» не записан",
                               stage, exc_info=True)

    def _run3(self, argv, on_line=None):
        """run_command с контрактом (code, stdout, stderr).

        on_line — колбэк на каждую строку stdout по мере её появления (живой
        прогресс сбора, спека §7). Инжектированные run_command (тесты, кастомные
        раннеры) могут не знать про него и/или по-старому возвращать (code, out) —
        обе формы поддерживаем: колбэк передаём только тому, кто его принимает
        (`_accepts_on_line`), иначе прогресс просто остаётся дискретным."""
        if on_line is not None and _accepts_on_line(self.run_command):
            res = self.run_command(argv, on_line=on_line)
        else:
            res = self.run_command(argv)
        if res is None:
            # инжектированный раннер вернул None: раньше это молча вызывало
            # команду второй раз — теперь падаем громко и один раз
            raise RuntimeError("run_command не вернул (code, stdout[, stderr])")
        if len(res) == 2:
            return res[0], res[1], ""
        return res

    def _acquire_stage(self, stage):
        """Взять межпроцессный ОС-лок звена и удержать его на время прогона.

        False — лок держит другой процесс (второй StageRunner / slash-команда):
        звено занято снаружи. Лок отпускается в _finish (уже в фоновом потоке) —
        держится процессом всё время прогона, независимо от потока."""
        lock = ProcessLock(stage_lock_path(self.locks_dir, stage))
        if lock.acquire():
            self._stage_locks[stage] = lock
            return True
        return False

    def _release_stage(self, stage):
        """Отпустить ОС-лок звена. No-op, если прогон шёл без захвата (прямой run_sync)."""
        lock = self._stage_locks.pop(stage, None)
        if lock is not None:
            lock.release()

    def _claim(self, stage):
        """Захват звена. False — звено уже выполняется (в этом процессе или в другом)
        либо мьютекс raw↔factory блокирует старт (M6: проверка действует на всех
        путях захвата — start, reply, цикл; причина остаётся в cycle_note).

        Внутрипроцессный слой — self._lock + проверка state; кросс-процессный —
        ОС-лок stage-<name>.lock, удерживаемый до _finish."""
        with self._lock:
            block = _MUTEX_BLOCK.get(stage)
            if block and self.state[block[0]]["status"] == "running":
                self.cycle_note = block[1]
                return False
            return self._claim_locked(stage)

    def _claim_locked(self, stage):
        """Тело _claim при уже взятом self._lock. Нужно start(), где под тем же
        локом сперва делается взаимная проверка raw↔factory: self._lock не
        реентерабельный, повторный self._claim здесь дал бы дедлок (P2.7)."""
        if self.state[stage]["status"] == "running":
            return False
        if not self._acquire_stage(stage):
            return False   # звено держит другой процесс
        self.state[stage] = {"status": "running", "detail": "выполняется…"}
        return True

    def start(self, stage):
        """Фоновый запуск. False — звено уже выполняется."""
        if stage not in STAGES:
            raise KeyError(stage)
        with self._lock:
            # взаимное исключение raw↔factory — обе стороны под ОДНИМ локом (P2.7):
            # ни TOCTOU, ни односторонней дыры (raw во время factory тоже блокируется).
            # _claim_locked, а не _claim: self._lock не реентерабельный.
            block = _MUTEX_BLOCK.get(stage)
            if block and self.state[block[0]]["status"] == "running":
                self.cycle_note = block[1]
                return False
            if not self._claim_locked(stage):
                return False
        self.cycle_note = ""  # успешный ручной старт снимает устаревшую заметку
        threading.Thread(target=self.run_sync, args=(stage,),
                         kwargs={"already_marked": True}, daemon=True).start()
        return True

    def start_cycle(self):
        with self._lock:
            # гейтим по конвейеру, НЕ по any_running: идущий ритуал циклу не помеха
            if self._pipeline_busy():
                return False
            # первое звено цикла помечаем напрямую (не через _claim) — но ОС-лок
            # берём так же, иначе второй процесс мог бы стартовать тот же цикл
            if not self._acquire_stage(AUTO_CYCLE[0]):
                return False
            self.state[AUTO_CYCLE[0]] = {"status": "running", "detail": "выполняется…"}
        # синхронно, ДО потока: POST /cycle/run отвечает редиректом, и страница,
        # которую оператор увидит следующей, обязана быть уже с нулями — иначе
        # первые секунды цикла лента показывает итоги прошлого прогона
        self._progress_reset_cycle()
        threading.Thread(target=self.run_cycle_sync,
                         kwargs={"first_marked": True}, daemon=True).start()
        return True

    def reply(self, stage, session_id, text):
        """Ответ продюсера в рамках уже начатой сессии claude. False — звено занято
        либо мьютекс raw↔factory блокирует (M6 — проверка внутри _claim).
        Недопустимый session_id -> ValueError (M22: значение уходит в argv claude)."""
        if stage not in STAGES:
            raise KeyError(stage)
        if not _SESSION_ID_RE.fullmatch(str(session_id)):
            raise ValueError(f"недопустимый session_id: {session_id!r}")
        if not self._claim(stage):
            return False
        threading.Thread(target=self._reply_sync, args=(stage, session_id, text),
                         daemon=True).start()
        return True

    def run_ritual(self, ritual):
        """Фоновый запуск недельного ритуала (P5.7). False — ритуал уже выполняется.

        KeyError для неизвестного ключа — как start()/reply() у звеньев."""
        if ritual not in RITUALS:
            raise KeyError(ritual)
        with self._lock:
            if self.ritual_state[ritual]["status"] == "running":
                return False
            self.ritual_state[ritual] = {"status": "running"}
        threading.Thread(target=self._run_ritual_sync, args=(ritual,),
                         daemon=True).start()
        return True

    def _run_ritual_sync(self, ritual):
        """Тело ритуала: один claude -p <команда> тем же путём, что _fanout_claude,
        с отчётом в канал reports["ritual-<key>"]. Отдельного механизма не заводим.

        Возвращает True при чистом исходе, False — если были проблемы. finally
        снимает running при любом исходе — иначе поллинг «Отчётов» не остановится."""
        spec = RITUALS[ritual]
        channel = f"ritual-{ritual}"
        problems = []
        try:
            self._fanout_claude(channel, spec["command"], spec["label"], problems,
                                expect_agent=spec.get("agent"))
            # Любая проблема ритуала обязана стать видимым отчётом: у ритуала нет
            # detail звена, и без этого она оседала бы только в логе процесса.
            # Покрывает и исключение run_command (нет claude в PATH, таймаут) —
            # тогда это вообще единственный след для оператора, и холостой прогон
            # (агент не оставил след в Run Log) — тогда рядом с отчётом агента.
            if problems:
                self._add_report(channel, spec["label"], "; ".join(problems))
        finally:
            self.ritual_state[ritual] = {"status": "idle"}
        return not problems

    def run_prompt_writer(self, niche):
        """Фоновый прогон агента brief-промпта по одной нише. False — уже выполняется.

        Недопустимое имя ниши -> ValueError: значение уходит в argv claude (M22, та
        же проверка, что у session_id звеньев)."""
        niche = str(niche or "")
        if not is_valid_niche_name(niche):
            raise ValueError(f"недопустимое имя ниши: {niche!r}")
        with self._lock:
            if self.prompt_writer_state.get(niche) == "running":
                return False
            self.prompt_writer_state[niche] = "running"
        threading.Thread(target=self._run_prompt_writer_sync, args=(niche,),
                         daemon=True).start()
        return True

    def _run_prompt_writer_sync(self, niche):
        """Тело прогона: один claude -p «/cf-write-brief-prompt <ниша>» тем же путём,
        что ритуал, с отчётом в канал reports["prompt-writer"].

        Перед вызовом проверяем, что слэш-команда установлена. Без файла claude
        получил бы «/cf-write-brief-prompt обувь» как обычный текст и мог сделать
        что угодно вместо звена — а отчёт выглядел бы успешным. Пока proposal
        2026-07-26-brief-prompt-writer-agent не применён оператором, это штатное
        состояние, поэтому отказ идёт отчётом, а не исключением."""
        problems = []
        try:
            command_file = self.root.joinpath(*PROMPT_WRITER["command_file"])
            if not command_file.is_file():
                self._add_report(
                    PROMPT_WRITER_CHANNEL, f"{PROMPT_WRITER['command']} {niche}",
                    f"Команда не установлена: нет {command_file.as_posix()}. "
                    "Примените proposal proposals/2026-07-26-brief-prompt-writer-agent.md "
                    "(агент prompts/agents/brief-prompt-writer.md + слэш-команда) — "
                    "оба пути закрыты deny-правилами и требуют решения оператора.")
                return False
            self._fanout_claude(PROMPT_WRITER_CHANNEL,
                                f"{PROMPT_WRITER['command']} {niche}",
                                f"{PROMPT_WRITER['label']} ({niche})", problems,
                                expect_agent=PROMPT_WRITER.get("agent"))
            # как у ритуала: проблема без видимого отчёта осела бы только в логе
            if problems:
                self._add_report(PROMPT_WRITER_CHANNEL,
                                 f"{PROMPT_WRITER['label']} ({niche})",
                                 "; ".join(problems))
        finally:
            self.prompt_writer_state.pop(niche, None)
        return not problems

    def _log(self, stage, status, errors=(), started_at=None, input_summary=""):
        """Best-effort запись в CF Run Log: сбой логирования не меняет результат звена.

        Возвращает True, если запись удалась, False — если лог не записался
        (звено при этом всё равно считается успешным).

        input_summary добавлен разбором 2026-07-27 ради строки цикла целиком
        (агент dashboard-cycle): у звена итог виден по статусу, а у цикла итог —
        это фраза («встал на воротах», «продолжен без сбора»), и её негде хранить.
        """
        try:
            log_run(self.sheets, agent=f"dashboard-{stage}", status=status,
                    errors=list(errors), trigger_type="dashboard",
                    started_at=started_at, input_summary=input_summary)
            return True
        except Exception:
            logger.warning("не удалось записать запуск «%s» в CF Run Log", stage,
                           exc_info=True)
            return False

    def _finish(self, stage, exc=None, detail=None, log_status="success", log_errors=()):
        """Общий хвост run_sync/_reply_sync: статус ok/warn/error + запись в Run Log.

        Единая точка снятия ОС-лока звена: сюда сходятся все исходы прогона (успех,
        warn, исключение), поэтому лок, взятый в _claim/start_cycle, отпускается тут."""
        self._release_stage(stage)
        # pop, а не get: прогон закрыт, а следующий поставит свой штамп; висящий
        # ключ иначе приписал бы новому прогону чужой старт.
        started = self._stage_started.pop(stage, None)
        if exc is not None:
            self.state[stage] = {"status": "error", "detail": str(exc)}
            self._log(stage, "failed", errors=[str(exc)], started_at=started)
            return False
        logged = self._log(stage, log_status, errors=log_errors, started_at=started)
        status = "ok" if log_status == "success" else "warn"
        if logged:
            self.state[stage] = {"status": status, "detail": detail or "успешно"}
        elif detail:
            self.state[stage] = {"status": "warn",
                                 "detail": f"{detail} · запись в Run Log не удалась"}
        else:
            self.state[stage] = {"status": "warn",
                                 "detail": "успешно, но запись в Run Log не удалась"}
        return True

    def run_sync(self, stage, already_marked=False):
        spec = STAGES[stage]  # KeyError для неизвестного звена
        self._stage_started[stage] = now_iso()
        if not already_marked:
            self.state[stage] = {"status": "running", "detail": "выполняется…"}
        if spec["kind"] == "fanout":
            return self.run_fanout_sync(stage)
        if spec["kind"] == "cli":
            return self._run_cli_sync(stage, spec["commands"])
        # kind == "n8n": после C3.2 так идёт только publish (до этапа 6)
        try:
            urls = self.config.get("dashboard", {}).get("workflows", {}).get(stage)
            if not urls:
                raise RuntimeError(
                    f"webhook для «{stage}» не настроен в cf.config.json (dashboard.workflows)")
            # одно звено может дёргать несколько воркфлоу
            for url in ([urls] if isinstance(urls, str) else urls):
                self.http_post(url)
                self._add_report(stage, "webhook", f"POST {url} → OK")
        except Exception as exc:
            return self._finish(stage, exc)
        return self._finish(stage)

    def _run_cli_sync(self, stage, commands):
        """CLI-звено (C3.2): последовательные `python -m cf <...>` вместо вебхука.

        Сбой одной команды НЕ отменяет следующие: TikTok и Instagram собираются
        независимо, и сводка каждой попадает в «Отчёты звеньев». Подкоманда
        collect сама пишет свою строку в CF Run Log (агент collect-tiktok и т.п.),
        а _finish пишет строку dashboard-<stage> про звено целиком — двойной лог
        ожидаем (разные агенты)."""
        # Фаза 2б: прогресс шага «Сбор» (raw). У stats производящих шагов нет —
        # _progress_begin/step no-op, звено остаётся downstream-бэклогом.
        self._progress_begin(stage)
        prod = self._producing_steps_for_stage(stage)   # raw→["collect"], stats→[]
        step = prod[0] if prod else None
        if step:
            self._progress_update(step, status="running", started_at=self.now(),
                                  done=0, total=len(commands))
        failed = []     # человекочитаемый перечень упавших команд
        summaries = []  # последняя строка stdout каждой успешной — в detail звена
        for index, cmd in enumerate(commands):
            title = " ".join(cmd)
            # Единица работы = одна команда сбора (платформа). Её план фаз и
            # подпись уходят в прогресс до запуска, чтобы лента сразу сказала,
            # что именно делается, а не «идёт…».
            if step:
                self._progress_update(
                    step, unit_label=PLATFORM_LABELS.get(cmd[-1], cmd[-1]),
                    phase_plan=cmd[-1], phase="prepare", phase_done=None,
                    phase_total=None, phase_started_at=self.now())
            on_line = self._progress_line_reader(step) if step else None
            try:
                # -u обязателен: без него print в подпроцессе буферизуется по 4–8 КБ
                # при выводе в pipe, маркеры прогресса приходят пачкой в конце —
                # и потоковое чтение (а с ним живая полоска) теряет смысл.
                code, out, err = self._run3(
                    [sys.executable, "-u", "-m", "cf", *cmd], on_line=on_line)
            except Exception as exc:
                logger.warning("«%s» упала", title, exc_info=True)
                self._add_report(stage, title, f"падение запуска: {exc}")
                failed.append(f"{title}: {exc}")
                if step:
                    self._progress_update(
                        step, done=(self.run_progress[step]["done"] or 0) + 1)
                continue
            # Маркеры прогресса — телеметрия, оператору в отчёт они не нужны. Но
            # если после отсева stdout пуст, причина падения живёт в stderr
            # (`cf collect` печатает «error: …» туда), иначе отчёт этапа выходит
            # пустым и «raw упало» приходит без причины.
            lines = [l for l in (out or "").strip().splitlines()
                     if not collect_progress.is_marker(l)]
            if not lines:
                lines = (err or "").strip().splitlines()
            self._add_report(stage, title, "\n".join(lines[-CLI_REPORT_TAIL:]))
            if code != 0:
                failed.append(f"{title}: код выхода {code}")
            elif lines:
                summaries.append(lines[-1])
            if step:
                self._progress_update(
                    step, done=(self.run_progress[step]["done"] or 0) + 1,
                    phase=None, phase_done=None, phase_total=None,
                    phase_started_at=self.now())
        # Звено падает, только если упали ВСЕ его команды (разбор 2026-07-27).
        # В ночь 27.07 Apify упёрся в потолок трат: Instagram не собрался, TikTok
        # принёс 84 ролика в Sheets — и звено ушло в error, обесценив собранное и
        # оборвав цикл до «Контент-завода». Частичный отказ — это warn (правило
        # №2: insufficient_data, «данных меньше, чем ждали»), а не авария; образец
        # исхода — хвост run_fanout_sync.
        all_failed = bool(failed) and len(failed) == len(commands)
        if step:
            self._progress_update(
                step, status=("error" if all_failed else "warn" if failed else "done"))
        if all_failed:
            return self._finish(stage, exc=RuntimeError("; ".join(failed)))
        if failed:
            # Причина деградации обязана быть в detail рядом с добычей: иначе
            # оператор видит жёлтое звено и «collect tiktok: success».
            detail = " · ".join(summaries + [f"частично: {'; '.join(failed)}"])
            return self._finish(stage, detail=detail,
                                log_status="insufficient_data", log_errors=failed)
        return self._finish(stage, detail=" · ".join(summaries) or None)

    # ── фан-аут звена «Контент-завод» ────────────────────────────────────────

    def _prompt_versions(self):
        """Строки CF Prompt Versions или None, если лист не прочитан.

        None важен: предикат готовности темы обязан отличать «версия не активна»
        от «мы не смогли посмотреть» (правило №2)."""
        try:
            return self.sheets.read_rows("prompt_versions")
        except Exception:
            logger.warning("prompt_versions не прочитаны — очереди считаем "
                           "по консервативной стороне", exc_info=True)
            return None

    def _run_prompt_draft_worker(self, stage, params, problems):
        """Воркер «Черновик промпта темы»: пишет черновики темам без промпта.

        Очередь — queues.niches_awaiting_prompt_draft: тема вне exclude_niches, с
        хотя бы одним утверждённым рецептом, без промпта и без уже лежащего
        черновика. Лимит на прогон (prompt_drafts_per_run) держит расход: иначе
        первый же прогон после расшивки написал бы черновики на все темы разом.

        Пустая очередь — не ошибка и не «работа сделана»: платного вызова нет, а
        в ленте остаётся причина, почему шаг пустой.
        """
        self._progress_update("prompt-draft", status="running",
                              started_at=self.now())
        exclude = {str(n).strip() for n in (params.get("exclude_niches") or [])}
        try:
            queue = niches_awaiting_prompt_draft(self.root,
                                                 self._prompt_versions(),
                                                 exclude=exclude)
        except Exception:
            logger.warning("очередь черновиков промптов не посчитана", exc_info=True)
            problems.append("очередь черновиков промптов не посчитана")
            self._progress_update("prompt-draft", status="warn", produced=None)
            return
        limit = params.get("prompt_drafts_per_run") or 0
        planned, dropped = queue[:limit], queue[limit:]
        if not planned:
            self._progress_update(
                "prompt-draft", status="done", produced=0,
                idle_reason=("не запускается: тем без промпта нет"
                             if not queue else
                             "не запускается: лимит черновиков за прогон — 0"))
            return
        command_file = self.root.joinpath(*PROMPT_WRITER["command_file"])
        if not command_file.is_file():
            problems.append(f"черновики промптов не пишутся: нет "
                            f"{command_file.as_posix()}")
            self._progress_update("prompt-draft", status="warn", produced=0)
            return
        written = 0
        self._progress_update("prompt-draft", done=0, total=len(planned))
        for i, niche in enumerate(planned, start=1):
            if self._fanout_claude(stage, f"{PROMPT_WRITER['command']} {niche}",
                                   f"{PROMPT_WRITER['label']} ({niche})", problems,
                                   expect_agent=PROMPT_WRITER.get("agent")):
                written += 1   # упавший вызов черновика не пишет (ревью 14.09.2026)
            self._progress_update("prompt-draft", done=i, count=written)
        # Скрытых усечений не бывает: если очередь длиннее лимита, это видно.
        reason = (f"за прогон пишем {limit}, ещё {len(dropped)} "
                  f"{'тема' if len(dropped) == 1 else 'тем'} в очереди"
                  if dropped else "")
        self._progress_update("prompt-draft", status="done", produced=written,
                              idle_reason=reason)

    def _run_formula_gate_worker(self, stage, params, problems):
        """Воркер «Проверка рецептов»: машинные ворота одобрения (26.07, вечер).

        Очередь — черновики рецептов целевых тем. Пусто → платного вызова нет и в
        ленте остаётся причина (тот же контракт, что у остальных воркеров).

        Есть слэш-команда судьи → идём через неё: агент выносит суждение о
        бизнес-контексте (исполним ли рецепт брендом, не дубль ли он по сути) и сам
        зовёт `cf auto-approve-formula --review`. Команды нет → решаем только по
        детерминированным проверкам (`--no-judge`) и ГОВОРИМ об этом в ленте:
        молчаливое снижение строгости — ровно то, из-за чего заводы встают незаметно.
        """
        self._progress_update("formula-review", status="running",
                              started_at=self.now())
        exclude = {str(n).strip() for n in (params.get("exclude_niches") or [])}
        try:
            queue = proposed_formula_drafts(self.root, exclude)
        except Exception:
            logger.warning("очередь черновиков рецептов не посчитана", exc_info=True)
            problems.append("очередь черновиков рецептов не посчитана")
            self._progress_update("formula-review", status="warn", produced=None)
            return
        if not queue:
            self._progress_update(
                "formula-review", status="done", produced=0,
                idle_reason="не запускается: черновиков рецептов нет")
            return

        # Агент СУДИТ, решает код. Прогон 27.07 01:15 показал, почему это не
        # стилистика: судья написал четыре вердикта (2 recommend), но до хвостовых
        # шагов своей слэш-команды — «позови auto-approve-formula» и «залогируй» —
        # не дошёл. Ни одного одобрения, ни строки в Run Log. Тот же класс, что
        # открытый дефект Д1 у ревьюера сценариев: хвост длинного пер-предметного
        # цикла исполняется недетерминированно. Поэтому вызов решения вынесен сюда:
        # раннер знает и очередь, и где лежат вердикты.
        judge = self.root / ".claude" / "commands" / "cf-review-formula.md"
        self._progress_update("formula-review", done=0, total=len(queue))
        if judge.is_file():
            self._fanout_claude(stage, "/cf-review-formula --proposed",
                                "ревью рецептов", problems,
                                expect_agent="formula-reviewer")

        approved, no_verdict, why = 0, 0, []
        for i, (_niche, path) in enumerate(queue, start=1):
            argv = [sys.executable, "-u", "-m", "cf", "auto-approve-formula", str(path)]
            review, refused = (self._formula_review_file(path) if judge.is_file()
                               else (None, ""))
            if review is not None:
                argv += ["--review", str(review)]
            elif judge.is_file():
                # Судья установлен, но годного вердикта по этому рецепту нет —
                # решать без него нельзя (иначе «судья не сработал» молча =
                # «судья согласен»).
                no_verdict += 1
                if refused and refused not in why:
                    why.append(f"{path.stem}: {refused}")
            else:
                argv.append("--no-judge")
            code, out, err = self._run3(argv)
            report = "\n".join((out or err or "").strip().splitlines()[-14:])
            if refused:
                # Причина отказа обязана дойти до отчёта: сам чек-лист напишет
                # только «нет файла ревью (--review)» (cli._judge_check) — правду,
                # но не всю, и на воротах «протухший вердикт» неотличим от «судья
                # не запускался» (разбор 2026-07-27).
                report = f"вердикт судьи не принят: {refused}\n{report}"
            self._add_report(stage, f"рецепт {path.stem}", report)
            if code == 0 and self._formula_status(path) == "approved":
                approved += 1
            self._progress_update("formula-review", done=i, count=approved)

        if not judge.is_file():
            reason = ("судья не установлен (proposals/2026-07-26-formula-reviewer"
                      "-agent.md) — решаем только по детерминированным проверкам")
        elif no_verdict:
            reason = (f"{no_verdict} из {len(queue)} без вердикта судьи — "
                      f"на ваше решение")
            if why:
                reason += " (" + "; ".join(why[:2]) + ")"
        else:
            reason = ""
        self._progress_update("formula-review", status="done", produced=approved,
                              idle_reason=reason)

    def _formula_review_file(self, path):
        """Годный вердикт судьи по этому рецепту: (файл|None, причина отказа).

        Имя файла даёт агент (`YYYY-MM-DD-<имя>-review.json`), поэтому берём по
        суффиксу и выбираем последний по дате в префиксе. Вердикт ЧУЖОГО рецепта
        подставить нельзя: суффикс включает имя целиком.

        Совпадения имени МАЛО (разбор 2026-07-27). Черновик рецепта
        перезаписывается версией v+1 каждым прогоном темы, а ворота рецептов
        стоят в проде на "auto": сбой судьи на хвосте цикла (уже случался 27.07
        01:15) означал бы, что вчерашний recommend одобряет сегодняшний, никем не
        читанный рецепт. Отсюда две проверки:
        1. вердикт старше черновика по mtime — судья смотрел прошлую редакцию;
        2. вердикт сам называет версию/sha рецепта и они расходятся.
        Причина возвращается строкой: «протухший вердикт» и «судья не запускался»
        для человека на воротах — разные истории.
        """
        base = self.root / "agent-runtime" / "reviews-formula"
        try:
            matches = sorted(base.glob(f"*-{path.stem}-review.json"))
        except OSError:
            return None, ""
        if not matches:
            return None, ""
        review = matches[-1]
        try:
            stale = review.stat().st_mtime < Path(path).stat().st_mtime
        except OSError:
            return None, "файл вердикта или рецепта не прочитан"
        if stale:
            return None, ("вердикт написан до правки черновика — судья не видел "
                          "эту версию рецепта")
        mismatch = self._verdict_version_mismatch(review, path)
        if mismatch:
            return None, mismatch
        return review, ""

    def _verdict_version_mismatch(self, review, path):
        """Расходятся ли версия/отпечаток рецепта в вердикте и в самом рецепте.

        Пустая строка — сверять нечего или всё сошлось. Forward-compatible: полей
        version/formula_version/formula_sha судья сегодня не пишет (его промпт под
        deny-правилами харнесса и правится отдельно), и их отсутствие отказом НЕ
        считается — иначе ворота рецептов встали бы целиком. Появится поле —
        сверка заработает сама, без второй правки раннера (разбор 2026-07-27).
        """
        try:
            verdict = json.loads(review.read_text(encoding="utf-8"))
            formula = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return "вердикт судьи или рецепт не разобраны как JSON"
        if not isinstance(verdict, dict) or not isinstance(formula, dict):
            return "вердикт судьи или рецепт не разобраны как JSON"
        version = str(formula.get("version") or "").strip().lstrip("v")
        for field in ("version", "formula_version"):
            claimed = str(verdict.get(field) or "").strip().lstrip("v")
            if claimed and claimed != version:
                return (f"вердикт о версии {field}={claimed}, "
                        f"а у черновика версия {version or '—'}")
        claimed_sha = str(verdict.get("formula_sha") or "").strip().lower()
        if claimed_sha and claimed_sha != self._formula_sha(path):
            return "вердикт о другом отпечатке рецепта (formula_sha не сошёлся)"
        return ""

    def _formula_sha(self, path):
        """sha256 файла рецепта — отпечаток той редакции, которую судили.

        Считается ТОЛЬКО когда вердикт заявил formula_sha: лишнее чтение файла на
        каждом прогоне ворот никому не нужно."""
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return ""

    def _formula_status(self, path):
        """Статус рецепта на диске — чем закончилось решение по черновику."""
        try:
            return str(json.loads(Path(path).read_text(encoding="utf-8"))
                       .get("status") or "")
        except Exception:
            return ""

    def _run_prompt_apply_worker(self, stage, params, problems):
        """Воркер «Включение промпта темы»: авто-режим ворот (правило №3, ред. 26.07).

        Включаем только черновики с ПОЛНОСТЬЮ зелёным чек-листом preflight
        (семь отказов аппликатора + покрытие всех утверждённых рецептов + свежесть
        ссылок на снапшоты). Красный чек-лист — тема остаётся на воротах с
        причиной; человек может включить её кнопкой, зная о предупреждении.
        """
        self._progress_update("prompt-apply", status="running", started_at=self.now())
        if not is_auto(self.config, "prompt"):
            self._progress_update(
                "prompt-apply", status="done", produced=0,
                idle_reason="ворота в ручном режиме (gates.policy.prompt=manual)")
            return
        exclude = {str(n).strip() for n in (params.get("exclude_niches") or [])}
        try:
            drafts = {n: p for n, p in prompt_drafts(self.root).items()
                      if n not in exclude}
            candidates = {n: v for n, v in prompt_candidate_drafts(self.root).items()
                          if n not in exclude}
        except Exception:
            logger.warning("очередь черновиков промптов не посчитана", exc_info=True)
            problems.append("очередь черновиков промптов не посчитана")
            self._progress_update("prompt-apply", status="warn", produced=None)
            return
        if not drafts and not candidates:
            self._progress_update(
                "prompt-apply", status="done", produced=0,
                idle_reason="не запускается: черновиков промптов тем нет")
            return

        versions = self._prompt_versions()
        applied, held = 0, []
        # Две очереди в одном шаге: первое включение темы (черновик …-reel.md) и
        # ПРАВКА действующего промпта (…-reel-v{N}.md). Вторая не заменяет активную
        # версию, а встаёт кандидатом в A/B — вердикт выносит eval, а не чтение.
        work = [(niche, path, apply_prompt) for niche, path in sorted(drafts.items())]
        work += [(niche, path, apply_candidate)
                 for niche, (_v, path) in sorted(candidates.items())]
        self._progress_update("prompt-apply", done=0, total=len(work))
        for i, (niche, path, applicator) in enumerate(work, start=1):
            try:
                applicator(self.root, path.name, sheets=self.sheets,
                           require_green=True, versions=versions)
            except PromptApplyError as exc:
                held.append(f"{niche}: {exc}")
            except Exception as exc:      # noqa: BLE001 — одна тема не роняет прогон
                logger.warning("включение промпта темы %s упало", niche, exc_info=True)
                problems.append(f"включение промпта «{niche}» упало: {exc}")
            else:
                applied += 1
            self._progress_update("prompt-apply", done=i, count=applied)
        self._progress_update(
            "prompt-apply", status="done", produced=applied,
            idle_reason=("на ваше решение: " + "; ".join(held[:2])) if held else "")

    def _run_briefs_worker(self, stage, problems):
        """Воркеры «Сценарии» и «Ревью сценариев» с предикатом очереди.

        Реального done/total у них нет — прогресс симулированный, но результат
        считается по листу брифов до и после шага.

        Ключевое отличие от прежнего кода: при пустой очереди тем платный вызов НЕ
        делается вовсе, и лента говорит почему. Раньше генератор запускался каждый
        прогон и честно уходил в skipped — деньги тратились на ответ «работы нет».
        """
        versions = self._prompt_versions()
        if versions is None:
            # Правило №2: лист не прочитан — судить нельзя. Гасить производство по
            # НЕпрочитанным данным значило бы делать вывод «готовых тем нет» из
            # отсутствия данных, то есть повторять ровно тот молчаливый скип, ради
            # которого всё и переделывалось. Один лишний платный вызов дешевле
            # незаметно вставшего завода; сам генератор перепроверит и отчитается.
            ready = None
        else:
            try:
                ready = niches_ready_for_briefs(self.root, versions)
            except Exception:
                logger.warning("очередь сценариев не посчитана", exc_info=True)
                ready = None
        if ready is not None and not ready:
            blocked = 0
            try:
                blocked = len(prompt_gate(self.root, versions)["rows"])
            except Exception:
                pass
            reason = ("не запускается: нет тем с включённым промптом"
                      if not blocked else
                      f"не запускается: {blocked} "
                      f"{plural_ru(blocked, 'тема', 'темы', 'тем')} "
                      f"{plural_ru(blocked, 'ждёт', 'ждут', 'ждут')} "
                      f"включения промпта")
            self._progress_update("briefs", status="done", produced=0,
                                  idle_reason=reason)
            self._progress_update("review", status="done", produced=0,
                                  idle_reason="не запускается: сценариев не написано")
            return
        # Отложенные капом сценарии возвращаются в игру ДО генерации: окно капа
        # сдвинулось — значит квота освободилась, и новый сценарий по этой формуле
        # писать не надо, уже написанный ждёт. Дешёвая CLI-команда, без claude.
        try:
            self._run3([sys.executable, "-m", "cf", "retry-deferred"])
        except Exception:      # noqa: BLE001 — страховка не важнее производства
            logger.warning("retry-deferred упал", exc_info=True)
            problems.append("retry-deferred упал")
        total_before, _pending_before = self._brief_counts()
        self._progress_update("briefs", status="running", started_at=self.now())
        self._fanout_claude(stage, "/cf-generate-briefs", "генерация брифов",
                            problems, expect_agent="brief-generator")
        total_after, pending_after = self._brief_counts()
        written = (max(total_after - total_before, 0)
                   if None not in (total_before, total_after) else None)
        self._progress_update("briefs", status="done", produced=written)
        # Доработка ДО ревью: один прогон ревьюера покрывает и новые сценарии, и
        # переписанные, без второго платного вызова.
        pending_after = self._run_fix_worker(stage, problems, pending_after)
        self._progress_update("review", status="running", started_at=self.now())
        self._fanout_claude(stage, "/cf-review-brief --pending", "ревью брифов",
                            problems, expect_agent="brief-reviewer")
        _total_end, pending_end = self._brief_counts()
        # проверено = ушло из очереди pending (новых брифов ревью не создаёт)
        reviewed = (max(pending_after - pending_end, 0)
                    if None not in (pending_after, pending_end) else None)
        self._progress_update("review", status="done", produced=reviewed)

    def _run_fix_worker(self, stage, problems, pending_before):
        """Воркер «Доработка сценариев»: переписывает забракованные по замечаниям.

        Очередь — брифы «доработка», которых завод ещё не правил (одна попытка на
        бриф, метку держит `cf revise-brief`). Пустая очередь платного вызова не
        делает. Агент не установлен — говорим об этом в ленте и идём дальше: без
        него конвейер работает как раньше, просто «доработка» снова становится
        тупиком, и молчать об этом нельзя.

        Возвращает обновлённый счётчик pending: переписанные брифы возвращаются в
        очередь ревью, и без пересчёта шаг «Ревью» отчитался бы отрицательной
        разницей.
        """
        self._progress_update("fix", status="running", started_at=self.now())
        try:
            from cf.cli import brief_was_fixed, norm_status
            rows = self.sheets.read_rows("briefs")
            queue = [r for r in rows
                     if norm_status(r.get("review_status")) == "revised"
                     and not brief_was_fixed(r)]
        except Exception:
            logger.warning("очередь доработки не посчитана", exc_info=True)
            problems.append("очередь доработки не посчитана")
            self._progress_update("fix", status="warn", produced=None)
            return pending_before
        if not queue:
            self._progress_update(
                "fix", status="done", produced=0,
                idle_reason="не запускается: сценариев в доработке нет")
            return pending_before
        command_file = self.root / ".claude" / "commands" / "cf-fix-brief.md"
        if not command_file.is_file():
            self._progress_update(
                "fix", status="warn", produced=0,
                idle_reason=f"{len(queue)} в доработке, но агент не установлен "
                            f"(proposals/2026-07-27-brief-fixer-agent.md)")
            return pending_before
        agent_ran = self._fanout_claude(stage, "/cf-fix-brief --revised",
                                        "доработка сценариев", problems,
                                        expect_agent="brief-fixer")
        # Агент ПИШЕТ исправленный сценарий, применяет его КОД. Прогон 27.07 02:44
        # показал зачем: агент переписал 5 из 6 и честно отчитался «возврат на ревью
        # не выполнен: cf revise-brief отклонён разрешениями харнесса» — новые
        # команды не внесены в allowlist .claude/settings.json, и headless-агент
        # вызвать их не может. Раннер зовёт ту же команду из своего процесса, минуя
        # харнесс: работа агента не пропадает из-за настройки, которую он не видит.
        applied, applied_ids = 0, set()
        for row in queue:
            brief_id = str(row.get("brief_id") or "")
            fixed = self.root / "agent-runtime" / "briefs" / f"{brief_id}-fixed.json"
            if not brief_id or not fixed.is_file():
                continue
            try:
                code, out, err = self._run3(
                    [sys.executable, "-u", "-m", "cf", "revise-brief", brief_id,
                     "--file", str(fixed), "--notes", "по замечаниям ревьюера"])
            except Exception as exc:   # noqa: BLE001 — один бриф не роняет звено
                logger.warning("применение доработки %s упало", brief_id, exc_info=True)
                problems.append(f"доработка {brief_id}: не применилась ({exc})")
                continue
            self._add_report(stage, f"доработка {brief_id}",
                             "\n".join((out or err or "").strip().splitlines()[-6:]))
            if code == 0:
                applied += 1
                applied_ids.add(brief_id)
        if not agent_ran:
            # Агент не отработал (таймаут, квота, недоверенный воркспейс): «не оставил
            # версию» здесь — сбой, а не отказ. Метку «одна попытка» не жжём, очередь
            # остаётся на следующий цикл; причина уже в problems.
            _total, pending_now = self._brief_counts()
            self._progress_update(
                "fix", status="warn", produced=applied,
                idle_reason=f"агент не отработал — {len(queue) - applied} "
                            f"в доработке ждут следующего цикла")
            return pending_now if pending_now is not None else pending_before
        refused = self._mark_refused_fixes(stage, queue, applied_ids)
        _total, pending_now = self._brief_counts()
        # Считаем ПРИМЕНЁННЫЕ доработки, а не дельту pending: дельта врёт, если в
        # том же прогоне ревьюер уже разобрал часть очереди.
        parts = []
        if applied < len(queue):
            parts.append(f"переписано {applied} из {len(queue)}")
        if refused:
            parts.append(f"{refused} снято с доработки к вам")
        self._progress_update("fix", status="done", produced=applied,
                              idle_reason=" · ".join(parts))
        return pending_now if pending_now is not None else pending_before

    def _mark_refused_fixes(self, stage, queue, applied_ids):
        """Снять с очереди фиксера брифы, которых он так и не переписал.

        Отказ фиксера — штатный и правильный исход (сценарий требует события,
        которого у бренда нет). Но метку FIX_MARKER ставит ТОЛЬКО успешная правка
        (`cf revise-brief <id> --file`, cli.revise_brief), поэтому отказ не
        оставлял следа вовсе: бриф вечно висел в
        очереди «доработки» и жёг платный вызов агента каждый цикл — бессрочно.
        На 27.07 так заперты b-own-event-announcement-20260726-founder-opening и
        …-20260727-popup-offers (разбор 2026-07-27).

        Очередь ПЕРЕЧИТЫВАЕТСЯ: метку мог поставить и сам агент, если разрешения
        харнесса ему позволили, — второй раз такой бриф трогать нечего. Свои
        успешные правки исключаем отдельно: боевой лист мы только что обновили, но
        полагаться на то, что чтение сразу это увидит, нельзя.

        Возвращает число помеченных брифов.
        """
        try:
            from cf.cli import brief_was_fixed, norm_status
            rows = self.sheets.read_rows("briefs")
        except Exception:
            logger.warning("очередь доработки не перечитана — отказы фиксера не "
                           "помечены", exc_info=True)
            return 0
        stuck = {str(r.get("brief_id") or "") for r in rows
                 if norm_status(r.get("review_status")) == "revised"
                 and not brief_was_fixed(r)}
        marked = 0
        for row in queue:
            brief_id = str(row.get("brief_id") or "")
            if not brief_id or brief_id in applied_ids or brief_id not in stuck:
                continue
            try:
                code, out, err = self._run3(
                    [sys.executable, "-u", "-m", "cf", "revise-brief", "--refused",
                     "--brief-id", brief_id, "--notes", FIXER_REFUSED_NOTE])
            except Exception as exc:   # noqa: BLE001 — уборка не роняет прогон
                # Это бухгалтерия хвоста: прогон уже сделал работу (переписал
                # часть брифов), и падение пометки не должно её обнулять.
                logger.warning("пометка отказа по брифу %s не удалась", brief_id,
                               exc_info=True)
                self._add_report(stage, f"отказ доработки {brief_id}",
                                 f"пометка не поставлена: {exc}")
                continue
            self._add_report(stage, f"отказ доработки {brief_id}",
                             "\n".join((out or err or "").strip().splitlines()[-6:]))
            if code == 0:
                marked += 1
        return marked

    def _fanout_params(self):
        cfg = self.config.get("dashboard", {}).get("fanout", {})
        params = {k: cfg.get(k, v) for k, v in FANOUT_DEFAULTS.items()}
        # Легаси-ключ growth (мультипликативный гейт роста, снят 2026-07-26).
        # Авто-конвертации нет: единицы разные (множитель против строк), любой
        # пересчёт был бы догадкой. Молча игнорировать нельзя — оператор будет
        # считать, что порог настроен, а он не влияет ни на что.
        if "growth" in cfg:
            logger.warning(
                "dashboard.fanout.growth больше не используется: гейт очереди"
                " аддитивный (min_new_rows=%s). Удалите ключ из cf.config.json.",
                params["min_new_rows"])
        return params

    def _previous_analysis(self, tab, niche):
        """Последний анализ ниши -> (generated_at|None, total_rows|None), либо None.

        None — анализа не было / файл нечитаем / ни одного пригодного сигнала:
        тогда гейт молчит и ниша идёт в очередь (как и раньше при отсутствии файла).
        Сигналы независимы, любой может отсутствовать по отдельности.
        """
        slug = re.sub(r'[^\w-]+', '-', niche).strip('-')
        matches = sorted(self.analysis_dir.glob(f"*-{tab}-{slug}-analysis.json"))
        if not matches:
            return None
        path = matches[-1]   # имена начинаются с даты — свежайший лексикографически последний
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("не прочитать прошлый анализ %s", path, exc_info=True)
            return None
        if not isinstance(data, dict):   # битый/чужой JSON не должен ронять фан-аут
            logger.warning("прошлый анализ %s не объект — гейт пропускаем", path)
            return None
        meta = data.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        at = _parse_date_string(str(meta.get("generated_at") or "").strip())
        if at is None:
            # Легаси-файл без meta: дата из имени (полночь UTC). Ошибка «в бОльшую
            # сторону» — часть строк того же дня зачтётся новыми; промах даёт лишний
            # прогон, а не навсегда замолчавшую нишу.
            at = _parse_date_string(path.name[:10])
        total = data.get("total_rows")
        # ВАЖНО: проверка на truthy, а не на наличие ключа — since присутствует во
        # ВСЕХ боевых analysis-файлах со значением null. `if "since" in meta`
        # обнулил бы total_rows разом у всех ниш и оставил гейт на одном сигнале.
        if not isinstance(total, (int, float)) or bool(meta.get("since")):
            # analyze-batch --since считал ЧАСТЬ среза (apply_filters режет по
            # posted_at ДО analyze_rows) — total_rows тогда не равен размеру ниши,
            # и дельта по нему завысила бы приток.
            total = None
        if at is None and total is None:
            return None
        return (at, int(total) if total is not None else None)

    def _new_rows_since(self, group, prev):
        """Сколько строк ниши прибавилось после прошлого анализа — max двух сигналов.

        1) collected_at: штамп first-seen — coalesce_row при перезаписи строки
           сохраняет ПРЕЖНИЙ collected_at, поэтому пересбор старого ролика не
           считается притоком. Сигнал переживает ротацию `cf archive --older-than
           45d`: старые raw-строки уходят из Sheets, len(group) перестаёт расти, и
           чистая дельта по размеру среза навсегда осталась бы нулевой.
        2) len(group) − total_rows прошлого анализа: ловит строки, собранные ДО
           анализа, но получившие нишу позже — классификатор ходит батчами.
        Берём max: сигналы ловят разные механизмы, и любой из них, набравший
        min_new_rows, обязан вернуть нишу в очередь.
        """
        since, total = prev
        by_collected = 0
        if since is not None:
            for row in group:
                ts = _parse_date_string(str(row.get("collected_at", "")).strip())
                if ts is not None and ts >= since:
                    by_collected += 1
        # клампим: ре-классификация и архивная ротация могут ужать срез ниже
        # прошлого замера — отрицательное число не должно попадать в сравнение.
        by_total = max(len(group) - total, 0) if total is not None else 0
        return max(by_collected, by_total)

    def _eligible_niches(self, problems=None, rows_by_tab=None):
        """Очередь (tab, niche): достаточно материала и >= min_new_rows новых строк с прошлого анализа.

        ``rows_by_tab`` — уже прочитанные строки вкладок (снимок сразу после
        разметки). Читать их повторно нечего: мьютекс raw↔factory не пускает сбор
        во время фан-аута, а классификация следующей вкладки в эту не пишет.

        Счёт по дедуплицированным строкам (как вход analyze-batch): дубли source_url
        не считаются ни материалом, ни притоком. Все вкладки недоступны → заметка
        в problems (warn), пустая очередь.
        """
        params = self._fanout_params()
        # Нецелевые ниши в производство не попадают (см. FANOUT_DEFAULTS.exclude_niches):
        # классификатор их всё равно размечает (роль фильтра — не пускать в целевые),
        # но очередь фан-аута их пропускает.
        exclude = {str(n).strip() for n in (params.get("exclude_niches") or [])}
        # M20 (аудит 2026-07-24): одна ниша — ОДИН прогон. Ниша из двух вкладок
        # раньше вставала в очередь дважды и при workers=2 два агента писали
        # formulas/<niche>/ наперегонки (lost update). Берём вкладку с бОльшим
        # материалом; при равенстве — порядок RAW_TABS (TikTok первым).
        candidates = {}  # niche -> (rows_in_group, -tab_order, tab)
        failed_tabs = 0
        for order, (tab, _label) in enumerate(RAW_TABS):
            rows = (rows_by_tab or {}).get(tab)
            if rows is None:      # вызов без снимка (или тогда не прочиталось) — читаем
                rows = self._raw_rows(tab)
            if rows is None:
                failed_tabs += 1
                continue
            groups = {}
            for row in rows:
                niche = str(row.get("niche", "")).strip()
                groups.setdefault(niche, []).append(row)
            for niche, group in groups.items():
                if niche in ("", "other") or niche in exclude:
                    continue
                group = dedupe_rows(group)
                # M19: строка считается только целиком «чистой» — просмотры выше
                # порога И все критические поля профайлера непусты. Иначе ниша
                # проходит очередь, но вечно валит `cf profile` (insufficient_data),
                # сжигая платный вызов claude каждый цикл.
                eligible = sum(
                    1 for r in group
                    if _num(r.get("views")) >= params["min_views"]
                    and all(str(r.get(f, "")).strip() for f in CRITICAL_FIELDS))
                if eligible < params["min_rows"]:
                    continue
                # Аддитивный гейт (2026-07-26): ниша возвращается в очередь, когда
                # с прошлого анализа набралось min_new_rows новых строк. Прежний
                # мультипликативный ×growth при линейном притоке растил интервал
                # экспоненциально и полностью задушил очередь (0 ниш из 13).
                prev = self._previous_analysis(tab, niche)
                if prev is not None and \
                        self._new_rows_since(group, prev) < params["min_new_rows"]:
                    continue
                cand = (len(group), -order, tab)
                if niche not in candidates or cand > candidates[niche]:
                    candidates[niche] = cand
        if failed_tabs == len(RAW_TABS) and problems is not None:
            problems.append("raw-вкладки недоступны")
        return [(tab, niche) for niche, (_n, _o, tab) in candidates.items()]

    def _vision_step(self, stage, tab, niche, title, problems):
        """Шаг vision прогона ниши (этап 2 кадров, решение №8): строится выключенным.

        vision.in_cycle=false (умолчание, ключа может не быть) — цикл байт-в-байт
        прежний, пауза завода не трогается. При true — vision CLI по этой нише
        и вкладке ДО платного вызова анализа: платим ровно за то, что будет
        прочитано; очередь ниш уже отфильтрована единственным источником, второй
        предикат «кого анализируем» не заводится. Отказ или лимит vision анализ
        НЕ блокирует (зеркало правила «по непрочитанным данным не гасим»):
        причина уходит в ленту и problems, прогон ниши едет дальше. Свою строку
        Run Log пишет сам vision CLI (правило №6)."""
        if not ((self.config or {}).get("vision") or {}).get("in_cycle"):
            return
        try:
            code, out, err = self._run3(
                [sys.executable, "-u", "-m", "cf", "vision",
                 "--tab", tab, "--niche", niche])
        except Exception as exc:
            logger.warning("шаг vision ниши «%s» упал", niche, exc_info=True)
            self._add_report(stage, f"визуал: {title}", f"падение запуска: {exc}")
            if problems is not None:
                problems.append(f"vision ниши {niche}: падение запуска")
            return
        # Итог vision в ленту прогона ниши: последние строки stdout несут числа
        # разбора (queued/done/failed/deferred) и причину останова движка.
        lines = [line for line in (out or "").strip().splitlines()
                 if line.strip()]
        if not lines:
            lines = (err or "").strip().splitlines()
        self._add_report(stage, f"визуал: {title}",
                         "\n".join(lines[-CLI_REPORT_TAIL:]))
        if code != 0 and problems is not None:
            problems.append(f"vision ниши {niche}: код выхода {code} — "
                            "анализ идёт по разобранному")

    def _run_niche(self, stage, tab, niche, problems=None):
        """Один прогон ниши в пуле. Возврат: {niche, tab, status, formulas, patterns}."""
        label = dict(RAW_TABS).get(tab, tab)
        title = f"ниша {niche} ({label})"
        error = {"niche": niche, "tab": tab, "status": "error",
                 "formulas": 0, "patterns": 0}
        # P2.9: имя из Sheets идёт в аргумент слэш-команды — недопустимое (пробел,
        # ';', перевод строки) сломало бы разбор аргументов, поэтому в error без claude.
        if not _NICHE_NAME_RE.fullmatch(niche):
            logger.warning("ниша «%s» отклонена: недопустимое имя, claude не запускаем", niche)
            self._add_report(stage, title, "недопустимое имя ниши — прогон не запущен")
            return error
        self._vision_step(stage, tab, niche, title, problems)
        since = now_iso()
        try:
            code, out, err = self._run3(
                ["claude", "-p", f"/cf-niche-run {tab} {niche}", "--output-format", "json"])
        except Exception as exc:
            logger.warning("прогон ниши «%s» упал", niche, exc_info=True)
            self._add_report(stage, title, f"падение прогона: {exc}")
            return error
        # пустой stdout (claude упал до ответа) — в отчёт идёт stderr
        text, session_id = _parse_claude_output(out if out.strip() else err)
        if _UNTRUSTED_MARKER in err:
            # агент не мог выполнить ни одной cf-команды — самоотчёту не верим;
            # обрезка до префикса: _add_report режет текст с начала
            logger.warning("прогон ниши «%s»: воркспейс не доверен — холостой", niche)
            self._add_report(stage, title,
                             f"⚠ {UNTRUSTED_PROBLEM}\n\n"
                             f"{text[-(REPORT_TEXT_LIMIT - 200):]}", session_id)
            return error
        self._add_report(stage, title, text, session_id)
        parsed = _parse_niche_result(text)
        if code != 0:
            status = "error"          # код выхода приоритетнее самоотчёта
        elif parsed is None:
            # P2.6: битый/отсутствующий NICHE_RESULT при коде 0 — не выдаём за
            # успех (иначе неотличимо от чистого прогона с formulas=0)
            status = "error"
            self._add_report(stage, title,
                             "NICHE_RESULT не распознан (unparseable) — ниша в error")
        else:
            # P2.6: "success" — синоним "ok"; прочие статусы (insufficient_data,
            # error) сохраняем как есть
            raw = parsed.get("status") or "ok"
            status = "ok" if raw in ("ok", "success") else raw
            # правило №6 и для ниш: /cf-niche-run обязан логировать niche-pipeline.
            # Проверка неточная при параллельных нишах (агент общий, окна вызовов
            # пересекаются — чужая строка может зачесться), но ловит одиночные
            # прогоны и полную блокировку CLI.
            if not self._agent_logged("niche-pipeline", since):
                status = "error"
                self._add_report(stage, title,
                                 "агент niche-pipeline не оставил след в Run Log "
                                 "(правило №6) — прогон холостой или лог пропущен")
        parsed = parsed or {}
        return {"niche": niche, "tab": tab, "status": status,
                "formulas": _int(parsed.get("formulas")),
                "patterns": _int(parsed.get("patterns"))}

    def _agent_logged(self, agent, since_iso):
        """Контракт правила №6: агент обязан оставить строку в CF Run Log.

        True — есть строка агента с completed_at >= since_iso. Сравниваем по
        МОМЕНТУ ЗАПИСИ, а не по started_at: с появлением `cf log-run --started-at`
        started_at стал моментом старта РАБОТЫ агента, и агент, начавший до since
        (перезапуск, чужая параллельная ниша), давал бы ложное «след не оставлен»
        -> звено error на ровном месте. completed_at всегда ставит сам log_run в
        момент append — он гарантированно позже since. Обе метки от now_iso(),
        один хост и формат — сравнение строк корректно. Недоступность Run Log —
        True с warning: проверка best-effort, сбой Sheets не роняет звено."""
        try:
            rows = self.sheets.read_rows("run_log")
        except Exception:
            logger.warning("вкладка Run Log недоступна — контроль «%s» пропущен",
                           agent, exc_info=True)
            return True
        return any(str(r.get("agent", "")).strip() == agent
                   and str(r.get("completed_at", "")) >= since_iso
                   for r in rows)

    def _fanout_claude(self, stage, prompt, note, problems, expect_agent=None):
        """Одиночный вызов claude в конвейере: отчёт + мягкая деградация в problems.

        Код выхода claude — не доказательство работы: 2026-07-24 агенты выходили
        нулём, «вежливо» доложив о полном блоке пермишенов, и звено легло в Run Log
        как success. Поэтому два детектора холостого прогона: stderr-маркер
        недоверенного воркспейса и (expect_agent) след агента в CF Run Log —
        правило №6 обязывает логировать каждый запуск при любом исходе."""
        since = now_iso()
        started = self.now()
        # Возвращает True, если агент ОТРАБОТАЛ (код 0, воркспейс доверен, без
        # исключения). Кто решает по итогам вызова «агент отказался» — обязан это
        # проверять: иначе таймаут или квота выдаются за решение агента (ревью 14.09.2026).
        try:
            code, out, err = self._run3(
                ["claude", "-p", prompt, "--output-format", "json"])
            # Подход к дедлайну — деградация, а не молчание (разбор 2026-07-27).
            # Шаг «Сценарии» четыре прогона подряд рос к потолку (1230 -> 1700) и
            # упёрся в него на пятом; цифры лежали в step-durations.json, но никому
            # ничего не говорили, и сутки без сценариев обнаружились постфактум.
            spent = self.now() - started
            if spent >= SUBPROCESS_WARN_SHARE * self.subprocess_timeout:
                problems.append(
                    f"{note}: {spent:.0f}с из дедлайна "
                    f"{self.subprocess_timeout:.0f}с — поднимите "
                    f"dashboard.fanout.subprocess_timeout, пока шаг не начали убивать")
            # пустой stdout (claude упал до ответа) — в отчёт идёт stderr, как
            # раньше делал сам _default_run («stdout or stderr»)
            text, session_id = _parse_claude_output(out if out.strip() else err)
            untrusted = _UNTRUSTED_MARKER in err
            if untrusted:
                # префикс + обрезка: _add_report режет текст С НАЧАЛА, иначе
                # маркер срезался бы у отчётов длиннее REPORT_TEXT_LIMIT
                text = (f"⚠ {UNTRUSTED_PROBLEM}\n\n"
                        f"{text[-(REPORT_TEXT_LIMIT - 200):]}")
                if UNTRUSTED_PROBLEM not in problems:
                    problems.append(UNTRUSTED_PROBLEM)
            self._add_report(stage, prompt, text, session_id)
            if code != 0:
                problems.append(f"{note} не удалась")
                return False
            if untrusted:
                return False  # причина холостого прогона уже в problems, Run Log не тревожим
            if expect_agent and not self._agent_logged(expect_agent, since):
                problems.append(f"{note}: агент {expect_agent} не оставил след "
                                f"в Run Log (правило №6) — прогон холостой "
                                f"или лог пропущен")
            return True
        except Exception:
            logger.warning("%s упала", note, exc_info=True)
            problems.append(f"{note} упала")
            return False

    def _fanout_guard(self, problems):
        """formula-guard (страховка + авто-пауза) и formula-perf (витрина own_performance).

        Шаг 5 фан-аута — единственная точка ежедневного запуска обеих команд: цикл
        дёргается планировщиком в 07:30 (P5.3, POST /cycle/run), поэтому и правила
        гвардии (P5.10: 3 reject из 5 и performance-медиана за 28 дн.), и накопление
        own_performance формул (P5.11) гоняются каждый день без отдельного schtasks.
        Порядок важен: сначала гвардия (может убрать формулу из индекса), потом perf
        считает витрину только по выжившим approved-формулам. Изменения index.json
        подхватит коммит шага 6. Best-effort, вне статуса звена."""
        try:
            code, _out, _err = self._run3([sys.executable, "-m", "cf", "formula-guard"])
            if code != 0:
                problems.append("formula-guard: расхождения")
        except Exception:
            logger.warning("formula-guard упал", exc_info=True)
            problems.append("formula-guard упал")
        try:
            code, _out, _err = self._run3([sys.executable, "-m", "cf", "formula-perf"])
            if code != 0:
                problems.append("formula-perf: сбой")
        except Exception:
            logger.warning("formula-perf упал", exc_info=True)
            problems.append("formula-perf упал")

    def _git_commit(self, message, cwd=None):
        """Единственная точка git в конвейере — агентам коммитить запрещено.

        Скоуп жёстко на formulas/ (pathspec): посторонние staged-файлы продюсера
        в коммит не попадают. Пустой индекс — не ошибка (тихо выходим). cwd=None —
        текущая директория (дашборд запускается из корня репозитория).

        Вся последовательность git берётся под локом мутаций репозитория: два
        коммиттера не дерутся за .git/index.lock, и коммит не наезжает на запись
        index.json (та держит тот же лок). Сбой git больше не проглатывается тихо:
        возвращаем строку-проблему (None — успех), которую run_fanout_sync поднимает
        в problems звена (виден на дашборде, не только в логе).
        """
        try:
            with file_lock(repo_mutation_lock_path(cwd if cwd is not None else ".")):
                subprocess.run(["git", "add", "formulas/"],
                               capture_output=True, text=True, cwd=cwd)
                staged = subprocess.run(
                    ["git", "diff", "--cached", "--quiet", "--", "formulas/"], cwd=cwd)
                if staged.returncode == 0:
                    return None  # нечего коммитить
                full = f"{message}\n\nCo-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
                res = subprocess.run(["git", "commit", "-m", full, "--", "formulas/"],
                                     capture_output=True, text=True, cwd=cwd)
                if res.returncode != 0:
                    logger.warning("git commit формул: код %s: %s",
                                   res.returncode, res.stderr)
                    return "git-коммит формул не удался"
        except LockTimeout:
            logger.warning("git-коммит формул: лок репозитория занят", exc_info=True)
            return "git-коммит формул: репозиторий занят"
        except Exception:
            logger.warning("git-коммит формул не удался", exc_info=True)
            return "git-коммит формул не удался"
        return None

    def _raw_rows(self, tab):
        """Строки raw-вкладки или None при сбое чтения (best-effort, как счётчики)."""
        try:
            return self.sheets.read_rows(tab)
        except Exception:
            logger.warning("не удалось прочитать «%s»", tab, exc_info=True)
            return None

    def _unclassified_count(self, tab):
        """Сколько строк вкладки без темы, или None при сбое чтения.

        Этим числом лента считает работу «Разметки»: до шага — сколько предстоит,
        после — сколько осталось; разница и есть «размечено N роликов». Сам агент
        отчитывается прозой, а прозу в счётчик не превратишь.
        """
        rows = self._raw_rows(tab)
        return None if rows is None else _unclassified_in(rows)

    def _brief_counts(self):
        """(всего брифов, из них ждут ревью) или (None, None) при сбое чтения."""
        try:
            rows = self.sheets.read_rows("briefs")
        except Exception:
            logger.warning("не удалось прочитать брифы для счётчика", exc_info=True)
            return None, None
        pending = sum(1 for b in rows
                      if str(b.get("review_status", "")).strip().lower() == "pending")
        return len(rows), pending

    def _pending_briefs_count(self):
        """Дешёвый подсчёт pending-брифов для сводки уведомления, best-effort.

        None — если briefs прочитать не удалось: сводка всё равно уходит (без M),
        уведомление не должно падать из-за недоступности Sheets."""
        try:
            return sum(1 for b in self.sheets.read_rows("briefs")
                       if str(b.get("review_status", "")).strip().lower() == "pending")
        except Exception:
            logger.warning("не удалось посчитать pending-брифы для уведомления",
                           exc_info=True)
            return None

    def _cycle_summary(self, stage, formulas, detail):
        """Небольшая JSON-сводка о завершении цикла для notify_url (P5.3).

        Ключи payload остаются машинными — их читает notify_url. Человеческий
        `text` собирает cf.messages: формулировки живут в одном месте, и правка
        слов не требует лезть в раннер.
        """
        from cf.messages import cycle_summary

        pending = self._pending_briefs_count()
        return {"stage": stage, "formulas": formulas, "briefs_pending": pending,
                "detail": detail,
                "text": cycle_summary(formulas, pending, self.config)}

    def _notify(self, stage, formulas, detail):
        """Best-effort POST сводки на dashboard.notify_url (P5.3).

        notify_url пуст/отсутствует → тихий no-op ДО построения сводки: _cycle_summary
        читает briefs (pending-счёт), поэтому при незаданном notify_url этот читатель
        не должен тратиться впустую. Любой сбой отправки лишь пишет warning и НЕ влияет
        на исход звена — уведомление сугубо информационное."""
        from cf.notify import notify_telegram, telegram_configured

        url = (self.config.get("dashboard", {}) or {}).get("notify_url")
        telegram = telegram_configured(self.config)
        if not url and not telegram:
            return
        summary = self._cycle_summary(stage, formulas, detail)
        if url:
            try:
                self.http_notify(url, summary)
            except Exception:
                logger.warning("уведомление о завершении цикла не отправлено (%s)", url,
                               exc_info=True)
        if telegram:
            # спека §5: сводка цикла оператору напрямую в Telegram (этап 3);
            # notify_telegram сам не бросает — best-effort сохранён
            notify_telegram(summary["text"], self.config)

    def run_fanout_sync(self, stage):
        """Конвейер «Контент-завода»: классификация → пул ниш → брифы → страховка → git."""
        problems = []
        try:
            # P2.7: _fanout_params ВНУТРИ try — иначе его исключение убивает фоновый
            # поток и звено навсегда остаётся running (зомби), не деградируя в error.
            params = self._fanout_params()
            # Фаза 2б: обнуляем производящие шаги фан-аута (Разметка→Ревью) за прогон.
            self._progress_begin(stage)
            # 1. классификация КАЖДОЙ raw-вкладки: без явной вкладки classifier берёт
            #    только raw_tiktok, и строки Instagram навсегда остаются без niche.
            #    Падение одной вкладки деградирует только её (заметка в problems),
            #    вторая всё равно классифицируется; старые ниши уже размечены.
            self._progress_update("classify", status="running", started_at=self.now(),
                                  done=0, total=len(RAW_TABS))
            classified = left = 0
            counted = False        # ни одной вкладки не сосчитали → числа не выдумываем
            uncounted = []         # вкладки, где счёт не удался: остаток неизвестен
            after_rows = {}        # снимок вкладки ПОСЛЕ разметки — он же вход очереди
            for i, (tab, label) in enumerate(RAW_TABS, start=1):
                # «сколько разметили» считаем сами: до и после — строки без темы
                before = self._unclassified_count(tab)
                self._fanout_claude(stage, f"/cf-classify-niche {tab}",
                                    f"классификация {label}", problems,
                                    expect_agent="niche-classifier")
                rows = self._raw_rows(tab)
                after_rows[tab] = rows
                after = None if rows is None else _unclassified_in(rows)
                if before is not None and after is not None:
                    classified += max(before - after, 0)
                    left += after
                    counted = True
                else:
                    # правило №2: остаток по ЧАСТИ вкладок — это не остаток. Раньше
                    # хватало одной сосчитанной вкладки, чтобы лента назвала сумму,
                    # пока в непрочитанной лежали строки без темы.
                    uncounted.append(label)
                self._progress_update(
                    "classify", done=i, count=i,
                    produced=classified if counted else None,
                    left=(left if counted and not uncounted else None),
                    left_unknown=bool(uncounted))
            if uncounted:
                problems.append("счёт разметки неполный: не прочитаны вкладки "
                                + ", ".join(uncounted))
            self._progress_update("classify", status="done")
            # 2. очередь ниш (недоступность всех raw-вкладок → заметка в problems)
            queue = self._eligible_niches(problems, rows_by_tab=after_rows)
            # 3. пул прогонов ниш с живым прогрессом — единственный шаг с честным done/total
            results = []
            # Считается до пула: шаг «Черновики рецептов» ниже читает его и при
            # пустой очереди тоже — иначе пустой прогон падал бы UnboundLocalError.
            formulas_sum = 0
            # phase_started_at — начало ТЕКУЩЕЙ единицы (ниши): кривая внутри
            # сегмента считается от неё, иначе elapsed приезжает от старта всего
            # шага и сегмент сразу упирается в кэп
            self._progress_update("analyze", status="running", started_at=self.now(),
                                  done=0, total=len(queue), count=0,
                                  phase_started_at=self.now())
            if queue:
                total = len(queue)
                done = 0
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=params["workers"]) as pool:
                    futures = [pool.submit(self._run_niche, stage, tab, niche,
                                           problems)
                               for tab, niche in queue]
                    for fut in concurrent.futures.as_completed(futures):
                        res = fut.result()
                        results.append(res)
                        done += 1
                        formulas_sum += res["formulas"]
                        self.state[stage] = {
                            "status": "running",
                            "detail": f"ниши {done}/{total} · формул {formulas_sum}"}
                        self._progress_update("analyze", done=done, count=formulas_sum,
                                              phase_started_at=self.now())
            self._progress_update("analyze", status="done")
            # 3а. Черновики рецептов — своя строка ленты. Отдельный агент их не
            #     пишет: их пишет тот же прогон темы, — но под ними стоят ворота
            #     «Одобрение рецептов», и без своей строки ворота висели бы в ленте
            #     без видимого источника работы.
            self._progress_update("formulas", status="running", started_at=self.now())
            self._progress_update("formulas", status="done", produced=formulas_sum)
            # 3б. Ворота «Одобрение рецептов» в машинном режиме: чек-лист + судья.
            #     Стоят ДО черновиков промптов не случайно — промпт темы пишется по
            #     УТВЕРЖДЁННЫМ рецептам, и до 26.07 свежий рецепт ждал решения
            #     человека целые сутки, прежде чем попасть в промпт своей темы.
            self._run_formula_gate_worker(stage, params, problems)
            # 4. Черновики промптов тем — воркер от 2026-07-26. Главная правка
            #    порядка: промпт темы переехал из ручного ритуала в лаборатории
            #    внутрь цикла.
            self._run_prompt_draft_worker(stage, params, problems)
            # 4а. Ворота «Включение промпта темы» в машинном режиме: включаем только
            #     полностью зелёный чек-лист, остальное уходит человеку с причиной.
            self._run_prompt_apply_worker(stage, params, problems)
            # 5. Сценарии и ревью. Воркер с ПУСТОЙ очередью платного вызова не
            #    делает: до 2026-07-26 генератор запускался каждый прогон и уходил
            #    в skipped, потому что ни одна тема не имела включённого промпта.
            self._run_briefs_worker(stage, problems)
            # 5. страховка целостности формул
            self._fanout_guard(problems)
            # 6. единственный коммит — от раннера; сбой git → в problems (не только лог)
            git_problem = self._git_commit("formulas: черновики прогона фан-аута")
            if git_problem:
                problems.append(git_problem)
        except Exception as exc:
            # Фаза 2б: падение фан-аута гасит бар шага, застрявшего в running,
            # чтобы лента не крутила прогресс несуществующего прогона.
            self._progress_finish(stage, "error")
            return self._finish(stage, exc)
        # 7. финал
        if not queue:
            detail = "очередь ниш пуста"
            err = 0
            formulas_sum = 0
        else:
            ok = sum(1 for r in results if r["status"] == "ok")
            ins = sum(1 for r in results if r["status"] == "insufficient_data")
            err = sum(1 for r in results if r["status"] == "error")
            formulas_sum = sum(r["formulas"] for r in results)
            detail = (f"ниши ok {ok} · insufficient {ins} · error {err} "
                      f"· формул {formulas_sum}")
        if problems:
            detail += " · " + "; ".join(problems)
        # P5.3: best-effort уведомление о завершении цикла (после него — только
        # проставление статуса звена; сбой уведомления звено не роняет). Сводка
        # (с чтением briefs) строится внутри _notify, ЛИШЬ если notify_url задан.
        # Свой try: неожиданное исключение здесь иначе вешало бы звено running
        # навсегда вместе с его ОС-локом (аудит, low).
        try:
            self._notify(stage, formulas_sum, detail)
        except Exception:  # noqa: BLE001
            logger.warning("уведомление о завершении фан-аута упало", exc_info=True)
        if err > 0 or problems:
            # звено отработало, но с изъянами — warn (как insufficient у raw)
            errors = problems or [f"фан-аут: ниш с ошибкой {err}"]
            return self._finish(stage, detail=detail,
                                log_status="insufficient_data", log_errors=errors)
        return self._finish(stage, detail=detail)

    def _reply_sync(self, stage, session_id, text):
        self._stage_started[stage] = now_iso()
        self._add_report(stage, "вопрос продюсера", text, session_id)
        try:
            # M22: «--» отделяет свободный текст от флагов — ответ, начинающийся
            # с «-» («-20% просмотров...»), не парсится claude как опция и не
            # даёт инъекции флагов вроде --dangerously-skip-permissions.
            code, out, err = self._run3(
                ["claude", "-p", "--output-format", "json",
                 "--resume", session_id, "--", text])
            # пустой stdout (claude упал до ответа) — в отчёт идёт stderr
            reply_text, reply_session = _parse_claude_output(out if out.strip() else err)
            untrusted = _UNTRUSTED_MARKER in err
            if untrusted:
                reply_text = (f"⚠ {UNTRUSTED_PROBLEM}\n\n"
                              f"{reply_text[-(REPORT_TEXT_LIMIT - 200):]}")
            self._add_report(stage, "ответ агента", reply_text, reply_session)
            if code != 0:
                raise RuntimeError(
                    f"claude --resume {session_id}: код выхода {code}: {reply_text[-200:]}")
            if untrusted:
                # агент не мог выполнить ни одной cf-команды — как у остальных
                # claude-путей, это не success (иначе Run Log врёт про звено)
                raise RuntimeError(UNTRUSTED_PROBLEM)
        except Exception as exc:
            return self._finish(stage, exc)
        return self._finish(stage)

    def run_cycle_sync(self, first_marked=False):
        """Автоматический цикл: сбор → «Контент-завод». Сбой звена его НЕ обрывает.

        Разбор 2026-07-27, цена ошибки — сутки производства. Apify упёрся в
        потолок трат, звено «Сбор» ушло в error — и цикл вернулся прямо здесь, не
        добравшись до фан-аута. Между тем фан-аут читает Google Sheets и диск, а
        не результат СЕГОДНЯШНЕГО сбора (см. run_fanout_sync/_raw_rows): 84
        свежих ролика TikTok уже лежали в таблице, тем было чем заняться. Это и
        есть правило CLAUDE.md «верх конвейера (шаги 1-4) не гейтится никогда:
        данные копятся». Жёсткий выход остался ровно один — звено уже занято
        другим прогоном: там ехать дальше значит писать в те же данные вдвоём.
        """
        started_at = now_iso()
        self.cycle_note = ""
        if not first_marked:
            # first_marked == «пришли из start_cycle», а он уже обнулил ленту
            # синхронно; прямой вызов (тесты, будущий планировщик) делает это сам
            self._progress_reset_cycle()
        # P2.7: флаг активного цикла держит any_running() в зазоре между звеньями
        # (raw уже финишировал, factory ещё не помечен running) — так HTMX-поллинг
        # не обрывается посреди цикла. Снимается в finally при любом исходе.
        self._cycle_active = True
        degraded = []     # звенья, отработавшие с изъяном: цикл поехал без них
        stopped = ""      # причина ЖЁСТКОГО останова (только занятое звено)
        try:
            for i, stage in enumerate(AUTO_CYCLE):
                # каждое звено захватывается под локом, иначе цикл может перетереть
                # звено, которое продюсер уже занял через reply()
                already_claimed = first_marked and i == 0   # захвачено в start_cycle
                if not already_claimed:
                    note_before = self.cycle_note
                    if not self._claim(stage):
                        # мьютекс-отказ оставляет причину в cycle_note сам (M6);
                        # прочие отказы получают общую формулировку
                        if self.cycle_note == note_before:
                            self.cycle_note = (f"цикл остановлен: звено «{stage}» "
                                               f"уже выполняется")
                        stopped = self.cycle_note
                        break
                ok = self.run_sync(stage, already_marked=True)
                # warn — это тоже деградация (частичный сбор, проблемы фан-аута):
                # молчать о ней нельзя, но и останавливаться из-за неё незачем.
                if not ok or self.state[stage]["status"] == "warn":
                    degraded.append(stage)
            self.cycle_note = self._cycle_note(degraded, stopped)
        finally:
            self._cycle_active = False
            self._close_cycle(started_at, degraded, stopped)

    def _cycle_note(self, degraded, stopped=""):
        """Заметка цикла: итог И деградация, а не одно вместо другого.

        Прежняя строка «цикл остановлен: ошибка на этапе …» была одновременно и
        причиной, и итогом — и умалчивала о главном: дальше не делалось ничего.
        Теперь итог всегда честный (где встал, чего ждут от вас), а деградация
        приписана хвостом (разбор 2026-07-27).

        Слово «ошибка» в хвосте не стилистика: лента красит заметку красным
        именно по нему (partials/stages.html:104), и «сбой» вместо него молча
        превратил бы аварию в обычную серую строку.
        """
        note = stopped or self._cycle_end_note()
        if not degraded:
            return note
        one = len(degraded) == 1
        labels = ", ".join(f"«{STAGE_LABELS.get(s, s)}»" for s in degraded)
        return (f"{note} · ошибка на {'этапе' if one else 'этапах'} {labels} — "
                f"конвейер продолжен без {'него' if one else 'них'}")

    def _close_cycle(self, started_at, degraded, stopped=""):
        """Хвост цикла: заметка на диск, уведомление оператору, строка в Run Log.

        До разбора 2026-07-27 про обрыв цикла не узнавал никто: ветка обрыва не
        звала _notify, единственный call-site уведомления сидел в хвосте фан-аута
        (который в ту ночь не запускался), cycle_note жила в памяти процесса, а в
        CF Run Log у цикла КАК ЦЕЛОГО не было строки вовсе — расхождение с
        железным правилом №6. Всё best-effort: телеметрия не роняет прогон.
        """
        self._save_cycle_note()
        if degraded:
            self._notify_cycle_degraded(degraded)
        errors = [f"деградация звена «{STAGE_LABELS.get(s, s)}»" for s in degraded]
        if stopped:
            errors.append(stopped)
        # skipped — «прогон осознанно не состоялся» (звено занято), у него свой
        # статус в VALID_STATUSES; деградация — insufficient_data, как у warn-звена.
        status = "skipped" if stopped else \
            ("insufficient_data" if degraded else "success")
        self._log(CYCLE_STAGE, status, errors=errors, started_at=started_at,
                  input_summary=self.cycle_note)

    def _notify_cycle_degraded(self, degraded):
        """Telegram оператору: цикл поехал дальше без сбойного звена.

        Формулировка человеческая, без слагов: сообщение читают с телефона утром,
        и оно должно отвечать на «что сломалось и что теперь». notify_telegram сам
        не бросает (best-effort по контракту модуля), но свой try всё равно нужен —
        телеметрия не имеет права ронять цикл (образец: хвост run_fanout_sync).
        """
        from cf.messages import cycle_degraded
        from cf.notify import notify_telegram

        text = cycle_degraded([STAGE_LABELS.get(s, s) for s in degraded],
                              self.config)
        try:
            notify_telegram(text, self.config)
        except Exception:      # noqa: BLE001 — уведомление не важнее прогона
            logger.warning("уведомление о деградации цикла не отправлено",
                           exc_info=True)

    def _cycle_end_note(self):
        """Чем закончился цикл — фактом, а не заготовкой.

        Прежняя строка «цикл дошёл до ручного одобрения — ждёт продюсера» была
        неправдой в обе стороны: цикл ничего не ждал (он просто закончился), а
        решения, которых от оператора действительно ждали — рецепты и промпты
        тем, — в ней не упоминались вовсе.
        """
        try:
            gates = build_gates(self.root, self._prompt_versions())
        except Exception:
            logger.warning("сводка ворот для заметки цикла не посчитана",
                           exc_info=True)
            return "цикл прошёл автоматические этапы"
        holding = blocking_gates(gates)
        if holding:
            gate = holding[0]
            return (f"цикл прошёл автоматику и встал на воротах "
                    f"«{gate['label']}»: {gate['count']} "
                    f"{plural_ru(gate['count'], 'тема', 'темы', 'тем')} "
                    f"{plural_ru(gate['count'], 'ждёт', 'ждут', 'ждут')} вас")
        pending = (gates.get("briefs") or {}).get("count") or 0
        if pending:
            return (f"цикл прошёл целиком · {pending} "
                    f"{plural_ru(pending, 'сценарий', 'сценария', 'сценариев')} "
                    f"{plural_ru(pending, 'ждёт', 'ждут', 'ждут')} "
                    f"вашего решения")
        return "цикл прошёл целиком — открытых ворот нет"

    def continue_after_gate(self, reason=""):
        """Догнать конвейер после того, как оператор закрыл ворота.

        Выбор оператора 2026-07-26 — «ехать сразу» вместо кнопки «Продолжить».
        Цена выбора: решение по рецепту или промпту НЕМЕДЛЕННО тратит деньги на
        вызовы агентов, поэтому здесь три страховки:

        1. выключатель `dashboard.fanout.autocontinue` (по умолчанию включено) —
           откат стоит одну строку в конфиге, а не правку кода;
        2. тот же мьютекс, что у ▶: занятый конвейер отказывает, а не встаёт в
           очередь вторым прогоном;
        3. гоняются ТОЛЬКО сценарии и ревью — без повторного платного сбора и
           анализа, которые к решению оператора отношения не имеют.

        Возвращает True, если прогон запущен.
        """
        params = self._fanout_params()
        if not params.get("autocontinue", True):
            return False
        try:
            if not niches_ready_for_briefs(self.root, self._prompt_versions()):
                return False        # закрывать было нечего: производить всё равно некому
        except Exception:
            logger.warning("автопродолжение: очередь сценариев не посчитана",
                           exc_info=True)
            return False
        with self._lock:
            if self._pipeline_busy():
                self.cycle_note = ("продолжение не запущено: конвейер занят — "
                                   "нажмите ▶ у «Сценариев», когда освободится")
                return False
        if not self._claim("factory"):
            return False
        self.cycle_note = f"продолжаем после ворот: {reason}" if reason else \
            "продолжаем после ворот"
        threading.Thread(target=self._continue_scripts_sync, daemon=True).start()
        return True

    def _continue_scripts_sync(self):
        """Тело автопродолжения: только «Сценарии» и «Ревью сценариев»."""
        stage = "factory"
        # как run_sync/_reply_sync: без отметки старта строка Run Log шла с нулевой
        # длительностью (started_at == completed_at) — ревью 14.09.2026
        self._stage_started[stage] = now_iso()
        problems = []
        try:
            self._progress_update("briefs", status="idle", produced=None,
                                  idle_reason="")
            self._progress_update("review", status="idle", produced=None,
                                  idle_reason="")
            self._run_briefs_worker(stage, problems)
            git_problem = self._git_commit("formulas: черновики прогона фан-аута")
            if git_problem:
                problems.append(git_problem)
        except Exception as exc:
            self._progress_finish(stage, "error")
            return self._finish(stage, exc)
        detail = "продолжение после ворот"
        if problems:
            detail += " · " + "; ".join(problems)
            return self._finish(stage, detail=detail,
                                log_status="insufficient_data", log_errors=problems)
        return self._finish(stage, detail=detail)
