"""Медиа-контур сбора: обложка + кадры каждой прошедшей гейт строки.

Тикет 03 плана 2026-08-14-visual-contour. Ссылки выдачи скоропортящиеся
(обложка TikTok ~5–6 ч, видео ~2–4 суток): что не скачано в момент сбора,
потеряно навсегда — ретроспективы не существует, только пересбор. Поэтому
скачивание живёт внутри collect сразу после гейта, симметрично субтитрам:
параллельный пул, инжектируемый скачивальщик, та же политика ретраев.

Сырое видео удаляется сразу после нарезки кадров — в том числе при ошибке
нарезки (решение гриля №3, retention сырца не заводим); кадры и обложки
(~1 МБ/ролик) хранятся вечно в agent-runtime/media/ (правило №4, не
версионируется). Медиа — обогащение, не условие сбора: любой отказ здесь
даёт честный статус строки и счётчик в сводке, но не роняет сбор и не
деградирует статус рана (правило №2 действует через счётчики).

Идемпотентность: raw_id с манифестом на диске повторно не качается — манифест
пишется только когда что-то добыто (saved/partial), поэтому полный отказ
(failed) на следующем сборе честно ретраится свежими ссылками. Осознанный
предел: partial не дозакачивает недостающее видео при пересборе (манифест
есть — пропускаем); если пилот покажет заметную долю partial, правило
пересматривается по его числам (тикет 10).
"""
import json
import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx

from cf.io import write_json_atomic
from cf.retry import with_retry

MEDIA_ROOT_DEFAULT = Path("agent-runtime/media")
FRAME_FPS = 1
FRAME_CAP = 120
MANIFEST_NAME = "manifest.json"
_RAW_VIDEO_NAME = "raw-video.tmp"

STATS_KEYS = ("saved", "partial", "failed", "reused",
              "covers", "videos", "frames", "expired")


def download_media(url, client=None, token=None):
    """GET медиа-файла -> (ok, bytes, http_status). Ошибка сети -> (False, b'', 0).

    Та же политика, что download_subtitle (maxTries=2/1s), но статус проверяем:
    мусорное тело страницы ошибки парсером не отсеять, 403 протухшей ссылки
    обязан стать честным отказом, а не «обложкой». token подписывает ТОЛЬКО
    api.apify.com (clockworks хранит обложки в KV-store рана — без подписи 403,
    приём субтитров 10.08); CDN-ссылки TikTok секрет видеть не должны
    (правило №5)."""
    own = client is None
    if own:
        client = httpx.Client(timeout=60.0, follow_redirects=True)
    headers = None
    if token and urlparse(url).hostname == "api.apify.com":
        headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = with_retry(lambda: client.get(url, headers=headers),
                          attempts=2, base_delay=1.0, label="download media")
        if not (200 <= resp.status_code < 300) or not resp.content:
            return False, b"", resp.status_code
        return True, resp.content, resp.status_code
    except Exception:  # noqa: BLE001 — отказ скачивания = честный статус, не авария
        return False, b"", 0
    finally:
        if own:
            client.close()


def cut_plan(duration_sec):
    """Чистая функция плана нарезки: длительность (сек | None) -> параметры.

    Тикет 01 этапа 2 (кадры): до 2 минут — честная 1 кадр/сек с потолком 120
    (прежний потолок 60 терял хвост длинных роликов, а там обычно CTA — и
    терял навсегда, сырец удаляется сразу после нарезки); длиннее — ровно 120
    кадров равномерно по всей длительности (первый в начале, последний у
    конца). Невалидная длительность — деградация в посекундный режим без
    длительности в плане (правило №2 живёт в статусах, не в падении)."""
    try:
        duration = float(duration_sec) if duration_sec is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration is None or duration <= 0:
        return {"mode": "per-second", "fps": float(FRAME_FPS), "cap": FRAME_CAP,
                "duration_sec": None, "expected_frames": None}
    if duration <= FRAME_CAP / FRAME_FPS:
        return {"mode": "per-second", "fps": float(FRAME_FPS), "cap": FRAME_CAP,
                "duration_sec": round(duration, 2),
                "expected_frames": min(FRAME_CAP,
                                       math.ceil(duration * FRAME_FPS))}
    return {"mode": "stretch", "fps": FRAME_CAP / duration, "cap": FRAME_CAP,
            "duration_sec": round(duration, 2), "expected_frames": FRAME_CAP}


def probe_duration(video_path):
    """Длительность видео в секундах (ffprobe) | None — отказ замера не авария.

    ffprobe — системная зависимость рядом с ffmpeg; в тестах не зовётся
    (замерщик инжектируем через attach_media тем же швом, что нарезчик)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            check=True, capture_output=True, text=True)
        duration = float(proc.stdout.strip())
    except Exception:  # noqa: BLE001 — деградация в посекундный режим, не падение
        return None
    return duration if duration > 0 else None


def cut_frames(video_path, frames_dir, plan=None):
    """Детерминированная нарезка ffmpeg по плану cut_plan, JPEG, потолок 120.

    Первый кадр гарантирован конструкцией fps-фильтра (кадр на t=0). Возвращает
    число кадров. ffmpeg — системная зависимость VPS; в тестах не зовётся
    (нарезчик инжектируем через attach_media)."""
    plan = plan or cut_plan(None)
    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
         "-vf", f"fps={plan['fps']:.6f}", "-frames:v", str(plan["cap"]),
         "-q:v", "2", str(frames_dir / "%03d.jpg")],
        check=True, capture_output=True)
    return len(list(frames_dir.glob("*.jpg")))


def _manifest_row_fields(manifest):
    """Манифест с диска -> media-поля строки (компактный JSON без статуса)."""
    slim = {k: v for k, v in manifest.items() if k != "status"}
    return {"media_status": manifest.get("status", ""),
            "media_manifest": json.dumps(slim, ensure_ascii=False,
                                         separators=(",", ":"))}


def _fetch_one(row, platform, root, downloader, cutter, prober, http_client,
               stats):
    """Медиа одной строки -> media-поля; счётчики — в переданный stats.

    stats здесь ЛОКАЛЬНЫЙ на строку (см. attach_media): инкременты из пула
    потоков в общий словарь были бы гонкой. Любой отказ — статус, не исключение.
    """
    raw_id = str(row.get("raw_id") or "")
    if not raw_id:
        return {}
    media_dir = root / platform / raw_id
    manifest_path = media_dir / MANIFEST_NAME
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Усечённый манифест (обрыв на записи) — это «манифеста нет», а не
            # «медиа нет навсегда»: удаляем и качаем заново (ревью 14.09.2026).
            manifest_path.unlink(missing_ok=True)
            manifest = None
    if manifest is not None:
        # Идемпотентность пересбора: не перекачиваем, но поля строки несём —
        # dry-run/JSONL живёт без вкладки, строка обязана быть самодостаточной.
        stats["reused"] += 1
        return _manifest_row_fields(manifest)

    cover_ok = False
    frame_count = 0
    if row.get("thumbnail_url"):
        ok, body, http_status = downloader(str(row["thumbnail_url"]),
                                           client=http_client)
        if ok:
            media_dir.mkdir(parents=True, exist_ok=True)
            (media_dir / "cover.jpg").write_bytes(body)
            cover_ok = True
            stats["covers"] += 1
        elif http_status == 403:
            stats["expired"] += 1
    if row.get("video_url"):
        ok, body, http_status = downloader(str(row["video_url"]),
                                           client=http_client)
        if not ok and http_status == 403:
            stats["expired"] += 1
        if ok:
            stats["videos"] += 1
            media_dir.mkdir(parents=True, exist_ok=True)
            video_path = media_dir / _RAW_VIDEO_NAME
            try:
                # запись сырца внутри try (ревью 14.09.2026): при ENOSPC/обрыве
                # недописанный .tmp раньше оставался в каталоге навсегда
                video_path.write_bytes(body)
                # Один замер длительности перед нарезкой (тикет 01 этапа 2): отказ
                # замерщика — не отказ медиа, план деградирует в посекундный режим.
                try:
                    plan = cut_plan(prober(video_path))
                except Exception:  # noqa: BLE001 — замер = обогащение плана, не условие
                    plan = cut_plan(None)
                try:
                    frame_count = int(cutter(video_path, media_dir / "frames", plan))
                    stats["frames"] += frame_count
                except Exception:  # noqa: BLE001 — ошибка нарезки = частичность, не авария
                    frame_count = 0
            finally:
                # Решение гриля №3: сырец не живёт дольше нарезки — и при её ошибке.
                video_path.unlink(missing_ok=True)

    status = ("saved" if cover_ok and frame_count
              else "failed" if not cover_ok and not frame_count
              else "partial")
    stats[status] += 1
    if status == "failed":
        # Манифеста нет — следующий сбор честно попробует свежие ссылки.
        return {"media_status": "failed", "media_manifest": ""}
    manifest = {
        "status": status,
        "dir": f"{platform}/{raw_id}",
        "cover": "cover.jpg" if cover_ok else "",
        "frames_dir": "frames" if frame_count else "",
        "frame_count": frame_count,
        "raw_deleted": True,
    }
    if frame_count:
        # Даунстрим (vision, анализ) узнаёт цену номера кадра из манифеста:
        # per-second — номер ≈ секунда; stretch — позиция равномерной сетки.
        # Старые манифесты без этих полей легальны: прежние нарезки посекундные
        # по построению. При деградации замера длительности честно нет.
        manifest["cut_mode"] = plan["mode"]
        if plan["duration_sec"] is not None:
            manifest["duration_sec"] = plan["duration_sec"]
    # Атомарно (tmp + fsync + rename): усечённый манифест при обрыве процесса
    # раньше навсегда гасил строку для vision (ревью 14.09.2026).
    write_json_atomic(manifest_path, manifest)
    return _manifest_row_fields(manifest)


def attach_media(rows, platform, root=None, downloader=None, cutter=None,
                 prober=None, http_client=None, token=None, max_workers=4):
    """Скачать обложку и кадры каждой строки (параллельно). -> (строки, stats).

    Скачивание — всем прошедшим гейт строкам, без фильтров по просмотрам/нише
    (решение гриля №2: в момент сбора ниша неизвестна, просмотры набираются
    позже). Работает и в dry-run — это механизм пилота: медиа боевым режимом,
    строки уезжают только в JSONL.

    downloader(url, client=) -> (ok, bytes, status); cutter(video, frames_dir,
    plan) -> число кадров; prober(video) -> длительность в секундах | None.
    Все трое инжектируемы (тесты не зовут сеть, ffmpeg и ffprobe). Любой
    сбой строки схлопывается в media_status=failed — медиа не валит сбор."""
    root = Path(root) if root else MEDIA_ROOT_DEFAULT
    downloader = downloader or (
        lambda url, client=None: download_media(url, client=client, token=token))
    cutter = cutter or cut_frames
    prober = prober or probe_duration
    stats = {k: 0 for k in STATS_KEYS}
    if not rows:
        return list(rows), stats

    def fetch(i):
        # Счётчики строки копятся в локальный словарь и сливаются в общий уже
        # в главном потоке (как ok/failed у attach_transcripts): инкременты в
        # общий stats из пула были бы гонкой.
        row, local = rows[i], {k: 0 for k in STATS_KEYS}
        try:
            fields = _fetch_one(row, platform, root, downloader, cutter,
                                prober, http_client, local)
        except Exception:  # noqa: BLE001 — правило №2 живёт в счётчиках, не в падении
            local["failed"] += 1
            fields = {"media_status": "failed", "media_manifest": ""}
        return i, ({**row, **fields} if fields else row), local

    out = list(rows)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i, row, local in ex.map(fetch, range(len(out))):
            out[i] = row
            for k, v in local.items():
                stats[k] += v
    return out, stats


def summary_note(stats):
    """Счётчики медиа одной строкой — для сводки stdout и Run Log."""
    return ("media saved={saved} partial={partial} failed={failed} "
            "frames={frames} reused={reused} expired={expired}").format(**stats)
