# Превью-инстанс и безопасный рестарт

## 1. Почему нельзя смотреть боевой 8787

Кнопки ▶ на ленте запускают настоящие производственные прогоны: платные вызовы
агентов и Apify, запись в боевые Google Sheets. Автопродолжение включено — одно
случайное нажатие «Утвердить» или «Включить промпт» уводит завод дальше по
конвейеру. Для осмотра поднимается изолированное превью на 8899.

## 2. Скрипт превью

Класть в `agent-runtime/preview/preview_dashboard.py` (runtime-артефакт, правило
№4). Обязательные условия:

- `sheets=FakeSheets(tables=..., headers=..., config=...)` — данные брать из
  свежего `agent-runtime/backups/*.jsonl`, заголовки задавать ЯВНО (иначе фейк
  выведет их из ключей строк и скроет дрейф схемы);
- `runner=StageRunner(..., locks_dir=<tmp>, root=<tmp>, reports_path=<tmp>,
  progress_path=<tmp>)` — **корни только временные**: иначе POST-решения из
  превью запишут в настоящие `formulas/**` и `prompts/**` и сделают git-коммит;
- `lab_root=<tmp>`;
- `port=8899` передавать явно — из порта выводятся разрешённые origin для CSRF,
  иначе все POST получат 403;
- `health=None` (или не стартовать монитор) — фоновые проверки полезут в боевую
  таблицу.

Скелет:

```python
import json, glob, tempfile, uvicorn
from pathlib import Path
from cf.config import load_config
from cf.dashboard.app import create_app
from cf.dashboard.data import DataCache
from cf.dashboard.runner import StageRunner
from tests.fakes import FakeSheets   # запускать из корня репозитория

tmp = Path(tempfile.mkdtemp(prefix="cf-preview-"))
cfg = load_config()

def rows(tab):
    files = sorted(glob.glob(f"agent-runtime/backups/*-{tab}.jsonl"))
    return [json.loads(l) for l in open(files[-1])] if files else []

sheets = FakeSheets(
    tables={t: rows(t) for t in ("briefs", "prompt_versions", "run_log",
                                 "reels", "performance", "seeds")},
    headers={  # живые заголовки — задавать явно
        "reels": ["published_id", "brief_id", "platform", "post_url", "published_at",
                  "creator", "content_owner", "prompt_version", "status", "notes"],
    },
    config=cfg,
)
runner = StageRunner(sheets, cfg, locks_dir=tmp / "locks", root=tmp,
                     reports_path=tmp / "reports.json",
                     progress_path=tmp / "progress.json")
app = create_app(sheets=sheets, cache=DataCache(sheets), runner=runner,
                 lab_root=tmp, port=8899,
                 weekly_target=cfg.get("dashboard", {}).get("weekly_target", 70))
uvicorn.run(app, host="127.0.0.1", port=8899)
```

Живые состояния ленты (бегущий шаг, завершённый с таймером, длинные тексты в
очереди) задаются прямо в `runner.run_progress` — визуальные дефекты видны
только на них.

## 3. Запуск и осмотр

Запускать через Bash с `run_in_background: true` — обычный `nohup`/`setsid` в
песочнице умирает вместе с командой.

```bash
.venv/bin/python agent-runtime/preview/preview_dashboard.py     # run_in_background: true
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8899/overview   # ждём 200
```

Осмотр — playwright-MCP. Скриншоты падают в `.playwright-mcp/` в корне
репозитория (каталог в `.gitignore`), копировать в
`agent-runtime/ux-shots/<дата>-{before,after}-<раздел>-<ширина>.png`.

Ширины: 375 / 768 / 1280 / 1920. Разделы: `/overview`, `/briefs`,
`/briefs/shooting-list`, `/lab`, `/performance`, `/sources`, `/runs`.

Агентам-ревьюерам в промпте **явно запрещать** порт 8787.

После осмотра:

```bash
git status --porcelain formulas/ prompts/    # обязано быть пусто
```

## 4. Что подхватывается без рестарта, а что нет

| Что правим | Видно после |
|---|---|
| `static/style.css`, `static/timeline.js` | перезагрузки страницы; для браузерного кэша бампнуть `?v=` в `base.html` |
| шаблоны Jinja | **только рестарта** (авто-перезагрузка выключена, шаблоны компилируются на старте) |
| Python | **только рестарта** |

Обратная сторона компиляции на старте: синтаксическая ошибка в шаблоне роняет
дашборд в 500 прямо при запуске (так было 25.07). Поэтому превью на 8899
проверяется до рестарта прода.

## 5. Рестарт боевого дашборда

Рестарт убивает StageRunner вместе с идущими прогонами — они живут потоками
внутри процесса. Порядок:

```bash
# 1) конвейер свободен?
for f in agent-runtime/locks/stage-*.lock; do flock -n "$f" true || echo "ЗАНЯТО: $f"; done

# 2) штатный путь (сам проверяет локи дважды: до и после pull/pip/тестов)
bash deploy/deploy.sh

# 3) минимальный путь
sudo systemctl restart cf-dashboard && systemctl is-active cf-dashboard

# 4) верификация ВСЕХ страниц — иначе ошибка шаблона утечёт в 500 на поллинге
for p in / /overview /briefs /briefs/shooting-list /lab /performance /sources /runs; do
  printf "%-26s " "$p"; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8787$p"
done
```

Правило: сначала зелёный `pytest`, затем зелёное превью, только потом рестарт — и
только вне окна 07:30 (`cf-cycle.timer`).

Откат: `git checkout -- src/cf/dashboard/` + рестарт.
