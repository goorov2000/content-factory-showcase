"""cf vision — разбор скачанного медиа в визуальные факты (тикет 05).

Платное и лимитное звено живёт ОТДЕЛЬНО от сбора: его падение или лимит сбор
не трогают, а vision не зовётся ни одним таймером — до расконсервации только
руками (включение в дневной цикл — отдельное решение после пилота).

Ключевой принцип контура: картинки НЕ попадают в контекст анализатора.
Движок превращает медиа в структурированные текстовые факты, ответ
валидируется схемой visual-facts (тикет 02) — невалидный ответ или
недоступное медиа дают честный ``visual_status=failed``, мусор в Sheets
не попадает и строка не выглядит разобранной.

Интерфейс движка — шов v1: «контекст строки + пути медиа + строка метаданных
подачи -> текст ответа» (``engine(cfg, context, media_paths, media_note)``).
Реализации выбираются блоком ``vision`` в cf.config.json — переключение
движка/модели стоит одну строку конфига. v1 подаёт движку только обложку
(решение гриля №1); ``frames_enabled`` (этап 2) добавляет кадры под капом
``frames_cap`` через второй шов — выборщик кадров (``frames_sampler``,
реестр SAMPLERS); контракт выхода уже умеет ``frame:N``.

Инфраструктурный сбой движка (нет CLI, нет промпта, лимит подписки) —
VisionEngineError: прогон останавливается ГРОМКО, очередь не помечается
failed — иначе лимитная ночь навсегда выкинула бы строки из разбора
(failed из очереди не возвращается).
"""
import base64
import json
import shutil
import subprocess
from pathlib import Path

from cf.collect.normalize import CELL_TEXT_CAP, js_slice
from cf.config import resolve_path
from cf.io import read_jsonl
from cf.runlog import log_run, now_iso
from cf.sheets import UnknownFieldsError
from cf.validate import validate_json_data

AGENT = "vision"
RAW_TABS = ("raw_tiktok", "raw_instagram")
ENGINE_TIMEOUT = 300.0
API_MAX_TOKENS = 16000   # потолок ответа движка anthropic-api без стриминга

DEFAULTS = {
    "engine": "headless-subscription",
    "model": "claude-haiku-4-5-20251001",
    "batch_limit": 10,
    "frames_enabled": False,
    "frames_sampler": "uniform",
    # frames_cap НАМЕРЕННО без дефолта: его выставит вердикт пилота (тикет 09
    # этапа 2); включение кадров без капа — громкий инфраструктурный отказ.
    "prompt_path": "prompts/agents/vision-cover.md",
    "api_key_file": "~/.cf/secrets/anthropic-api-key.txt",
}

# Поля строки, которые движок видит как контекст. Транскрипт не подаём:
# задача движка — описать НАБЛЮДАЕМОЕ на медиа, а не пересказать текстовые
# поля, которые анализатор и так получает.
CONTEXT_FIELDS = ("source_url", "caption", "hook_text", "niche")


class VisionEngineError(RuntimeError):
    """Движок не смог ответить (нет CLI/промпта/ключа, лимит, таймаут)."""


def vision_cfg(config):
    return {**DEFAULTS, **((config or {}).get("vision") or {})}


def _claude_output_text(raw):
    """result из конверта `claude -p --output-format json`; не-JSON — как есть."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw
    result = obj.get("result") if isinstance(obj, dict) else None
    return result if isinstance(result, str) else raw


def _agent_prompt(cfg, context, tail=""):
    """Текст промпта движка: установленный агент + контекст строки (+хвост).

    Промпт агента — prompts/agents/ (устанавливается только машинным путём,
    cf install-agent — тикет 06); его отсутствие — инфраструктурный отказ,
    а не failed строки."""
    prompt_path = Path(cfg["prompt_path"])
    if not prompt_path.exists():
        raise VisionEngineError(
            f"промпт vision-агента не установлен: {prompt_path} "
            "(cf install-agent, тикет 06)")
    return (prompt_path.read_text(encoding="utf-8")
            + "\n\n## Контекст строки\n"
            + json.dumps(context, ensure_ascii=False) + tail)


def headless_engine(cfg, context, media_paths, media_note=""):
    """Дефолт-гипотеза: headless-подписка (`claude -p`), модель из конфига.

    media_note — строка метаданных подачи кадров (этап 2): пустая в
    cover-режиме, и тогда блок медиа байт-в-байт как в v1."""
    tail = "\n\n## Медиа (прочитай файлы инструментом Read)\n"
    if media_note:
        tail += media_note + "\n"
    tail += "\n".join(f"- {p}" for p in media_paths)
    prompt = _agent_prompt(cfg, context, tail=tail)
    if shutil.which("claude") is None:
        raise VisionEngineError("claude CLI не найден в PATH")
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", cfg["model"],
             "--output-format", "json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=ENGINE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise VisionEngineError(f"claude -p превысил {ENGINE_TIMEOUT:.0f}с") from exc
    if proc.returncode != 0:
        raise VisionEngineError(
            f"claude -p rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}")
    return _claude_output_text(proc.stdout or "")


def api_engine(cfg, context, media_paths, media_note=""):
    """Заготовка под API-ключ — вторая реализация того же шва (решение №4).

    Включается конфигом (`vision.engine = "anthropic-api"`), когда ключ
    появится; без ключа отвечает честным инфраструктурным отказом."""
    key_path = Path(resolve_path(cfg["api_key_file"]))
    if not key_path.exists():
        raise VisionEngineError(
            f"API-ключ Anthropic не настроен: нет файла {key_path} "
            "(движок за конфигом, см. решение гриля №4)")
    import httpx

    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.b64encode(
                               Path(p).read_bytes()).decode("ascii")}}
               for p in media_paths]
    tail = f"\n\n## Медиа\n{media_note}" if media_note else ""
    content.append({"type": "text", "text": _agent_prompt(cfg, context,
                                                          tail=tail)})
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key_path.read_text(encoding="utf-8").strip(),
                     "anthropic-version": "2023-06-01"},
            # 16000 — потолок ответа без стриминга (ревью 14.09.2026): при 2048
            # кадровый ответ (факты пилота 3,5–8,7 тыс. знаков JSON) обрезался, и
            # обрезанный JSON молча хоронил строку как failed.
            json={"model": cfg["model"], "max_tokens": API_MAX_TOKENS,
                  "messages": [{"role": "user", "content": content}]},
            timeout=ENGINE_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        stop = body.get("stop_reason")
        if stop in ("max_tokens", "refusal"):
            # обрезка и отказ модели — отказ движка, а не свойство строки
            raise VisionEngineError(f"anthropic api: stop_reason={stop}")
        blocks = body.get("content") or []
        return "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict))
    except VisionEngineError:
        raise
    except Exception as exc:  # noqa: BLE001 — сеть/квота: останов, не failed строк
        raise VisionEngineError(f"anthropic api: {exc}") from exc


ENGINES = {"headless-subscription": headless_engine, "anthropic-api": api_engine}

# Сколько подряд ответов «ни одного медиа прочитать не удалось» при реально
# поданных файлах останавливают прогон: это отказ движка (недоверенный воркспейс,
# запрет Read), а не свойство строк — платить дальше бессмысленно (ревью 14.09.2026).
READ_NOTHING_STOP = 3


def resolve_engine(cfg):
    engine = ENGINES.get(cfg["engine"])
    if engine is None:
        raise VisionEngineError(
            f"неизвестный vision.engine={cfg['engine']!r}; "
            f"доступны: {sorted(ENGINES)}")
    return engine


def uniform_sampler(frames, cap, manifest=None):
    """Равномерная детерминированная субвыборка кадров (этап 2, тикет 02).

    frames — [(исходный номер, путь)] по порядку; первый и последний кадры
    обязательны, кап не превышается, кадров ≤ капа — подаются все. Номера
    сохраняются: `frame:N` в фактах указывает на реальный файл на диске.
    manifest (метаданные нарезки) этой стратегии не нужен — он часть
    интерфейса шва для будущих стратегий."""
    frames = list(frames)
    if len(frames) <= cap:
        return frames
    last = len(frames) - 1
    picked = []
    for i in range(cap):
        j = round(i * last / (cap - 1))
        if not picked or j > picked[-1]:
            picked.append(j)
    return [frames[j] for j in picked]


# Шов выборщика кадров — реестр по образцу реестра движков: новая стратегия
# выборки стоит строку конфига (vision.frames_sampler), а не переделку звена.
SAMPLERS = {"uniform": uniform_sampler}


def resolve_sampler(cfg):
    sampler = SAMPLERS.get(cfg["frames_sampler"])
    if sampler is None:
        raise VisionEngineError(
            f"неизвестный vision.frames_sampler={cfg['frames_sampler']!r}; "
            f"доступны: {sorted(SAMPLERS)}")
    return sampler


# Сентинел «разбирать нечем СЕЙЧАС, но будет чем на этапе 2»: у строки на
# диске есть кадры, но нет обложки, а v1 разбирает только обложки. Пометить её
# failed значило бы похоронить навсегда (failed из очереди не возвращается) и
# помешать этапу 2 — а спека обязывает v1 этапу 2 не мешать (Out of Scope).
FRAMES_DEFERRED = "кадры есть, обложки нет — разбор ждёт этапа 2 (frames_enabled)"


def _feed_note(manifest, given, total):
    """Строка метаданных подачи кадров для блока медиа в запросе движка.

    Движок судит о структуре в терминах реальных секунд: сколько кадров
    подано из скольких, длительность (если манифест её знает) и правило
    нумерации. Старые манифесты без полей нарезки — посекундные по
    построению, поэтому честная строка без длительности остаётся верной."""
    duration = manifest.get("duration_sec")
    dur = (f"длительность ролика ≈ {duration:g} с"
           if isinstance(duration, (int, float)) else "длительность неизвестна")
    if manifest.get("cut_mode") == "stretch":
        rule = (f"нарезка растяжкой — {total} кадров равномерно по всей "
                f"длительности, номер кадра — позиция сетки, а не секунда")
    else:
        rule = "нарезка посекундная — номер кадра ≈ секунда ролика"
    return f"Кадры: подано {given} из {total}; {dur}; {rule}."


def media_plan_for(row, media_root, frames_enabled=False, frames_cap=None,
                   sampler=None):
    """Медиа строки по манифесту -> (пути, строка метаданных, причина отказа).

    v1 подаёт только обложку, строка метаданных пустая — запрос движка
    байт-в-байт прежний. С кадрами: обложка подаётся отдельно и в кап не
    входит; кадры сверх капа проходят выборщик (шов тикета 02) с сохранением
    исходных номеров в путях. Недоступное медиа (нет манифеста/обложки/файла)
    — причина для failed; FRAMES_DEFERRED — не failed, а «оставить в очереди
    до кадров»."""
    try:
        manifest = json.loads(row.get("media_manifest") or "")
    except ValueError:
        return [], "", "манифест медиа не читается"
    if not isinstance(manifest, dict) or not manifest.get("dir"):
        return [], "", "манифест медиа пуст"
    base = Path(media_root) / manifest["dir"]
    paths = []
    if manifest.get("cover"):
        cover = base / manifest["cover"]
        if cover.exists():
            paths.append(str(cover))
    note = ""
    if frames_enabled and manifest.get("frames_dir"):
        files = sorted((base / manifest["frames_dir"]).glob("*.jpg"))
        frames = []
        for pos, path in enumerate(files, start=1):
            try:
                number = int(path.stem)
            except ValueError:
                number = pos    # инвариант «frame:N = N-й файл кадров строки»
            frames.append((number, str(path)))
        if frames:
            given = frames
            if frames_cap is not None and len(frames) > frames_cap:
                given = (sampler or uniform_sampler)(frames, frames_cap,
                                                     manifest)
            paths.extend(path for _number, path in given)
            note = _feed_note(manifest, len(given), len(frames))
    if not paths:
        if (not frames_enabled and manifest.get("frames_dir")
                and any((base / manifest["frames_dir"]).glob("*.jpg"))):
            return [], "", FRAMES_DEFERRED
        return [], "", f"файлы медиа недоступны: {base}"
    return paths, note, ""


def media_paths_for(row, media_root, frames_enabled=False):
    """Контракт v1: пути медиа строки -> (список, причина). Без капа и метаданных."""
    paths, _note, reason = media_plan_for(row, media_root,
                                          frames_enabled=frames_enabled)
    return paths, reason


def parse_facts(text):
    """JSON визуальных фактов из ответа движка (терпит обрамляющий текст)."""
    text = str(text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def _queue(rows, niche=None, redo=False, raw_ids=()):
    """Очередь разбора. Обычный режим — неразмеченные строки со скачанным
    медиа; --redo — ре-разбор УЖЕ размеченных (done|failed) по явной
    адресации (решение №9 гриля: авто-догона done-строк нет нигде)."""
    out = []
    for r in rows:
        if str(r.get("media_status") or "") not in ("saved", "partial"):
            continue
        status = str(r.get("visual_status") or "").strip()
        if redo:
            if status not in ("done", "failed"):
                continue
        elif status:
            continue
        if niche and str(r.get("niche") or "").strip() != niche:
            continue
        if raw_ids and str(r.get("raw_id") or "") not in raw_ids:
            continue
        out.append(r)
    return out


def run(sheets, config=None, engine=None, limit=None, tabs=RAW_TABS,
        media_root="agent-runtime/media", jsonl_path=None, now=None,
        log=None, schema_root=".", niche=None, redo=False, raw_ids=(),
        frames_cap=None):
    """Прогон vision: очередь -> движок -> валидация -> апсерт по raw_id.

    Режим пилота: jsonl_path — строки dry-run батча; факты кладутся рядом
    (<имя>-visual.jsonl), Sheets не трогается. frames_cap (флаг --frames-cap)
    включает подачу кадров с этим капом поверх конфига — путь пилотных
    прогонов. niche ограничивает очередь нишей и снимает лимит по умолчанию;
    redo пере-разбирает done/failed по явной адресации (raw_ids | niche).
    Возвращает summary; каждый прогон — в Run Log (правило №6)."""
    log = log or (lambda *_: None)
    started = now or now_iso()
    cfg = vision_cfg(config if config is not None
                     else getattr(sheets, "config", {}) or {})
    raw_ids = tuple(str(r) for r in raw_ids or ())
    # Нишевый режим и адресный ре-разбор по умолчанию идут всей очередью
    # (решение №8 гриля: платим ровно за то, что будет прочитано); явный
    # --limit уважается.
    if limit is not None:
        limit = int(limit)
    elif not (niche or redo):
        limit = int(cfg["batch_limit"])

    # Инфраструктура ДО очереди (чтение вкладок/JSONL, неизвестный движок или
    # выборщик в конфиге, кадры без капа, --redo без адресации) — сбой с
    # обязательным следом в Run Log (правило №6): молчаливый exit 1 без строки
    # уже прятал ночные поломки сбора (прообраз — cmd_collect).
    try:
        if redo and not (niche or raw_ids):
            raise VisionEngineError(
                "--redo требует явной адресации (--raw-id, повторяемый, либо "
                "--niche): «ре-разбор всего» невозможен")
        frames_on = bool(cfg["frames_enabled"]) or frames_cap is not None
        cap = sampler = None
        if frames_on:
            cap = frames_cap if frames_cap is not None else cfg.get("frames_cap")
            if cap is None:
                raise VisionEngineError(
                    "кадры включены (vision.frames_enabled), а капа подачи "
                    "нет: дефолта vision.frames_cap в коде нет — его выставит "
                    "вердикт пилота (тикет 09); задай ключ в cf.config.json "
                    "или флаг --frames-cap")
            cap = int(cap)
            if cap < 2:
                raise VisionEngineError(
                    f"vision.frames_cap={cap}: кап подачи кадров — целое ≥ 2")
            sampler = resolve_sampler(cfg)
        if jsonl_path:
            # read_jsonl делит по "\n": splitlines() резал подписи с U+2028.
            source_rows = read_jsonl(jsonl_path)
            queued = [(None, i) for i, r in enumerate(source_rows)
                      if _queue([r], niche=niche, redo=redo, raw_ids=raw_ids)]
        else:
            source_rows = None
            queued = []
            for tab in tabs:
                queued.extend((tab, row) for row in
                              _queue(sheets.read_rows(tab), niche=niche,
                                     redo=redo, raw_ids=raw_ids))
        if queued:
            if not Path(media_root).is_dir():
                # Не тот корень (опечатка, запуск не из корня репо, перенесённый
                # каталог) раньше помечал очередь failed, а в --redo стирал
                # оплаченные факты. Это инфраструктура, а не строки.
                raise VisionEngineError(
                    f"каталога медиа {media_root} нет — проверь --media-root и "
                    f"что прогон идёт от корня репозитория")
            engine = engine or resolve_engine(cfg)
    except Exception as exc:  # noqa: BLE001 — след и честный failed, не трейс
        note = f"инфраструктурный сбой: {exc}"
        log(note)
        try:
            log_run(sheets, AGENT, "failed", input_summary=note,
                    errors=[str(exc)], trigger_type="cli", started_at=started)
        except Exception:  # noqa: BLE001 — Sheets может лежать, статус важнее
            pass
        return {"status": "failed", "done": 0, "failed": 0, "queued": 0,
                "engine_error": str(exc)}

    if not queued:
        # Существующее правило воркеров: пустая очередь — ни одного платного
        # вызова, причина честно в выводе и в Run Log.
        note = "очередь пуста: нет строк со скачанным медиа без разбора — платный вызов не делается"
        log(note)
        log_run(sheets, AGENT, "insufficient_data", input_summary=note,
                trigger_type="cli", started_at=started)
        return {"status": "insufficient_data", "done": 0, "failed": 0,
                "queued": 0, "note": note}

    if limit is not None:
        queued = queued[:limit]
    done = failed = deferred = read_nothing = 0
    read_nothing_streak = 0
    engine_error = ""
    for tab, item in queued:
        row = source_rows[item] if jsonl_path else item
        paths, note, reason = media_plan_for(row, media_root,
                                             frames_enabled=frames_on,
                                             frames_cap=cap, sampler=sampler)
        if not paths and reason == FRAMES_DEFERRED:
            # Не хороним: строка остаётся в очереди с пустым visual_status
            # и разберётся этапом 2 (включение кадров конфигом).
            deferred += 1
            log(f"{row.get('raw_id')}: отложено — {reason}")
            continue
        if not paths:
            fields = {"visual_status": "failed", "visual_facts": ""}
            log(f"{row.get('raw_id')}: failed — {reason}")
            if redo:
                # ре-разбор не отнимает прежний результат: пару меняет только
                # валидный новый разбор (ревью 14.09.2026)
                failed += 1
                continue
        else:
            context = {f: row.get(f, "") for f in CONTEXT_FIELDS}
            try:
                answer = engine(cfg, context, paths, note)
            except VisionEngineError as exc:
                # Инфраструктура, не строки: останавливаемся громко, очередь
                # остаётся нетронутой и уйдёт в следующий прогон.
                engine_error = str(exc)
                log(f"движок остановил прогон: {engine_error}")
                break
            data = parse_facts(answer)
            errors = (validate_json_data("visual-facts", data,
                                         schema_root=schema_root)
                      if isinstance(data, dict) else ["ответ движка не JSON"])
            if not errors and data.get("observed_media") == "none":
                # Файлы подали, а движок «ничего не прочитал» — отказ движка.
                # Строку не пишем: она остаётся в очереди и разберётся потом.
                read_nothing += 1
                read_nothing_streak += 1
                log(f"{row.get('raw_id')}: движок не прочитал поданные файлы "
                    f"({len(paths)} шт.) — строка оставлена в очереди")
                if read_nothing_streak >= READ_NOTHING_STOP:
                    engine_error = (f"{read_nothing_streak} ответа подряд «медиа "
                                    f"прочитать не удалось» при поданных файлах — "
                                    f"похоже на запрет чтения файлов движку")
                    log(f"движок остановил прогон: {engine_error}")
                    break
                continue
            if errors:
                fields = {"visual_status": "failed", "visual_facts": ""}
                log(f"{row.get('raw_id')}: failed — ответ не прошёл схему: "
                    f"{errors[0]}")
                if redo:
                    failed += 1
                    continue
            else:
                read_nothing_streak = 0
                compact = json.dumps(data, ensure_ascii=False,
                                     separators=(",", ":"))
                fields = {"visual_status": "done",
                          "visual_facts": js_slice(compact, CELL_TEXT_CAP)}
        if jsonl_path:
            source_rows[item] = {**row, **fields}
        else:
            # Точечная запись двух полей: прочие колонки строки не трогаются;
            # отсутствие visual-колонок в живом листе — громкий останов
            # (UnknownFieldsError, колонки добавляет оператор — тикет 13),
            # а не молчаливая потеря разбора.
            try:
                written = sheets.update_row_fields(tab, "raw_id", row.get("raw_id"),
                                                   fields)
            except UnknownFieldsError as exc:
                engine_error = str(exc)
                log(f"запись остановлена: {engine_error}")
                break
            if written is False:
                # строки нет в листе (например, её унёс cf archive): разбор не
                # записан — не выдаём его за done (ревью 14.09.2026)
                log(f"{row.get('raw_id')}: строка не найдена в листе — разбор не записан")
                failed += 1
                continue
        if fields["visual_status"] == "done":
            done += 1
        else:
            failed += 1

    out_path = ""
    if jsonl_path:
        out = Path(jsonl_path).with_name(Path(jsonl_path).stem + "-visual.jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                               for r in source_rows), encoding="utf-8")
        out_path = str(out)
        log(f"факты рядом с батчем: {out_path}")

    # Останов инфраструктурой (движок/колонки) — failed: прогон не доехал,
    # недоразобранная очередь цела и уйдёт в следующий. Иначе — по факту:
    # есть записанные разборы -> success, ни одного -> insufficient_data.
    status = ("failed" if engine_error
              else "success" if done else "insufficient_data")
    summary_line = (f"queued={len(queued)} done={done} failed={failed} "
                    f"deferred={deferred} "
                    + (f"read_nothing={read_nothing} " if read_nothing else "")
                    + f"engine={cfg['engine']} model={cfg['model']} "
                    f"frames={'on' if frames_on else 'off'}")
    if frames_on and cap:
        summary_line += f" cap={cap}"
    if niche:
        summary_line += f" niche={niche}"
    if redo:
        summary_line += " redo=on"
    if engine_error:
        summary_line += f" остановлен движком: {engine_error}"
    if out_path:
        summary_line += f" jsonl={out_path}"
    log_run(sheets, AGENT, status, input_summary=summary_line,
            errors=[engine_error] if engine_error else (),
            trigger_type="cli", started_at=started,
            output_paths=[out_path] if out_path else ())
    log(summary_line)
    return {"status": status, "done": done, "failed": failed,
            "deferred": deferred, "queued": len(queued),
            "engine_error": engine_error, "jsonl_out": out_path}
