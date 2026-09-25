# Дашборд-кит CF — дизайн-система пульта

Дата: 2026-07-25. Источник истины для того, **как выглядит и говорит дашборд CF**
(`src/cf/dashboard/`). Собран после UX-доводки (Фазы 1–6, спека
[2026-07-25-cf-dashboard-ux-design.md](../superpowers/specs/2026-07-25-cf-dashboard-ux-design.md)),
чтобы дальше не расходиться.

**Чем это НЕ является.** Это не `DESIGN.md` бренда: спецификация бренда JE LA PECHE
(фирменные Pantone, лицензированный Aeroport, логотипы, компоненты витрины) живёт в
сестринском репозитории `CF DS` — см. [README, раздел 12](../../README.md#12-дизайн-система-cf-ds).
Здесь описан только «дашборд-кит»: подмножество токенов и компонентов, реально
реализованное в `src/cf/dashboard/static/style.css`. При расхождении с брендовым
DESIGN.md прав DESIGN.md — сюда вносится правка.

## 1. Токены

Все значения — в `:root` (style.css, начало файла). В шаблонах цвета берутся только
через `var(--…)`; хардкод hex в шаблонах не допускается.

| Группа | Токены |
|---|---|
| Основа | `--primary` #131417 (сайдбар, кнопка), `--ink` #1D232E (текст), `--frost` #EEF0F2 (фон рабочей области), `--press` #E4E7EA, `--silver` #C9CED1 (границы), `--gray` #99999B, `--gray-text` #5A6068 (второстепенный текст), `--hairline-dark` #39414E |
| Акцент | `--accent-blue` #759AC4, `--accent-blue-deep` #6489B3, `--accent-blue-text` #476C96 (ссылки), `--info-soft` #EAF1F8 |
| Семантика | успех `--success`/`--success-text`/`--success-soft`, предупреждение `--warning`/`--warning-text`/`--warning-soft`, ошибка `--error`/`--error-text`/`--error-soft` |
| Тени, фокус | `--shadow-1` (карточка), `--shadow-2` (поповер), `--focus-ring` (видимый фокус) |
| Движение | `--ease` `cubic-bezier(.2,0,0,1)` |

## 2. Типографика — три роли

| Роль | Токен | Гарнитура | Где |
|---|---|---|---|
| Display | `--font-display` | Aeroport Medium 500 (самохостится, `.ttf`) | `h1`, `.hook-title`, `.brand-name` |
| UI | `--font-ui` | Montserrat 400–600 (самохостится, вариативный `woff2`, сабсеты кириллица+латиница) | текст интерфейса, подписи, кнопки |
| Mono | `--font-mono` | Aeroport Mono 400 | числа, даты, ID, слаги агентов |

Шрифты подключены `@font-face` **локально, без CDN** (офлайн-дружелюбно и без
внешних запросов). Aeroport лицензирован — новые веса не добавлять без файла из `CF DS`.
Montserrat под SIL OFL.

> **Витринная копия.** Файлы Aeroport (коммерческая лицензия Brownfox) не публикуются:
> `--font-display` откатывается на Montserrat, `--font-mono` — на системный моноширинный.
> Лицензии поставляемых шрифтов — `src/cf/dashboard/static/fonts/THIRD-PARTY.md` и `OFL.txt`.

Кегли (фактические): `h1` 32px (≤767px — 26px, ≤479px — 22px), `.card-title` 15/600,
`.stat-value` 28px mono, тело 15px, `.lead`/`.hint-plain` 12.5–13px,
`.hint`/`.metric-line` 11.5–12px, `.row-note`/`.meta-inline` 10.5px,
`.eyebrow`/`.field-label`/`.stat-label` 10–11px с разрядкой `.16–.3em`.

## 3. Отступы и утилиты

Рабочая область: `.main` 28/32px (≤1023px — 20/16px, ≤479px — 16/12px), вертикальный
ритм между секциями — `gap: 24px` (≤479px — 16px). Карточка `.card` 20/24px.

Утилиты вместо инлайн-стилей: `.mt-8` `.mt-12` `.mt-14` `.mt-16`, `.w-full`, `.grow`,
`.inline-form`, `.notice-line`, `.sr-only`.

**Инвариант:** инлайновый `style=` в шаблонах допустим ТОЛЬКО для вычисляемого
значения (ширина `.progress-fill`, `.tl-fill`, высота `.chart-bar`). Проверяется
тестом `test_no_static_inline_styles_left_in_templates`.

## 4. Компоненты

**Каркас.** `.shell` → `.nav-toggle` (чекбокс бургера) + `.sidebar` (`.brand`,
`.nav`/`.nav-item`/`.nav-badge`, `.sidebar-foot`) + `.main#main`.

**Шапка раздела.** `.page-head` → `.eyebrow` (назначение раздела капсом), `h1`,
`.lead` (одна строка «что это и что делать» — обязательна в каждом разделе),
справа `.chips` (вкладки/фильтры) или `.period` (селектор периода).

**Карточки.** `.card`; `.card--table` для таблиц (`padding:0` + `overflow-x:auto`);
`.card-head` → `h2.card-title` + `.card-sub`.

**Метрики.** `.stat-row` (4 колонки → 2 → 1) → `.stat` (`.stat-label`, `.stat-value`,
`.stat-sub`); темп — `.tempo`, `.progress`/`.progress-fill`, `.funnel`, `.aging`.

**Лента конвейера.** `.timeline` → `.tl-step` (`.tl-icon`, `.tl-name`, `.tl-actor`,
`.tl-count`, `.tl-bar`/`.tl-fill`, `.tl-phase`/`.tl-took`, `.tl-actions`, `.tl-open`),
статусы `.tl-done`, `.tl-running`, `.tl-warn`, `.tl-backlog` (ход за продюсером —
акцентный значок и счётчик), `.tl-interrupted`; `.tl-divider` перед ручными этапами.
Колонка `.tl-actions` держит `min-width` (72px, на мобильном 96px) — иначе правые
края полосок разъезжаются у строк с кнопкой ▶ и без неё. Цели нажатия — 32×32,
на ≤767px 44×44. Плитки-звенья удалены (25.07): в разметке от них остались только
`.stage-run`, `.cycle-note`, `.loop-note`.

**Таблицы.** `table.queue` внутри `.card--table`; `th[scope=col]`,
`caption.sr-only`; `.num` — правое выравнивание + `tabular-nums`; вторая строка
ячейки со служебным ID — `.row-note.mono`.

**Статусы.** `.badge` + модификатор (`pending`/`approved`/`rejected`/`revised`/
`published`/`success`/`failed`/`insufficient`), `.pill` (`auto`/`niche`/`niche-new`/
`no-prompt`), `.tag` (нейтральная метка: тема, площадка, «пример этого сценария»).
Зелёный `.badge.approved` — только про решение, не про «это референс».

**Кнопки и поля.** `.btn-primary`, `.btn-secondary`, `.btn-danger-outline`,
`.btn-outline-dark` (в сайдбаре), `.stage-run` (▶ у этапа); `textarea.note`,
`.reason-input`, `select.reason-select`; группа действий — `.actions`
(≤479px — в столбец на всю ширину).

**Сообщения и пустота — три разных класса, не путать:**

| Класс | Смысл | Водяной знак |
|---|---|---|
| `.empty-state` | данных реально нет | да (логотип 96px, opacity .08) |
| `.banner.error` | ошибка загрузки + следующий шаг | нет |
| `.banner.warn` | данные могли устареть, предупреждение | нет |
| `.table-foot` | подпись «показано N из M» | нет |

**Раскрытие деталей.** `details.tech` и `details.report-dump` — техническая
причина/сырой дамп/служебные ID (правило «прятать, не удалять»). `details.more` +
`.clamp` — длинный текст на 3 строки с «показать полностью» (полный текст остаётся
в DOM). Порог обёртки в `details.more` (`long_field(limit=340)`) синхронизирован с
обрезкой в 3 строки: при 160 две трети раскрывашек не раскрывали ничего.

**Ссылки-референсы.** `.url-row` (перенос длинных URL) с ярлыком из
`labels.link_label` («TikTok · @dancox_7») вместо сырого адреса.

## 5. Брейкпоинты

| Ширина | Что меняется |
|---|---|
| ≥1280px | десктоп-эталон: сайдбар 240px sticky, `.stat-row` 4×, `.briefs-layout` `minmax(0,1fr)`/`minmax(320px,430px)` |
| ≤1279px | `.briefs-layout` → 1 колонка: на 1024 очередь ужималась до 270px и резала колонки «Статус»/«Дата» |
| ≤1023px | сайдбар → верхняя панель, `.nav-burger` открывает меню (`.nav-toggle:checked`), `.two-col` → 1 колонка, `.sys-pop` раскрывается в потоке |
| ≤767px | `.stat-row` → 2 колонки, `.page-head` в столбец, `.chips` переносятся, `h1` 26px, таблицы держат `min-width: 440px` и скроллят, ячейки 10/8px |
| ≤479px | `.stat-row` → 1 колонка, `.card` 16px, кнопки решений на всю ширину, `h1` 22px |

Плюс `@media (prefers-reduced-motion: reduce)` — гасит анимацию ленты и переходы
ширины у всех полосок (`.tl-fill`, `.progress-fill`).

**Инвариант ширины:** `.main` обязан иметь `min-width: 0` — без него flex-элемент
не сжимается ниже min-content таблицы, и вбок едет вся страница, а не содержимое
карточки (проверено: 1139px при вьюпорте 1024).

## 6. Доступность (порог)

- `.skip-link` «Перейти к содержимому» → `<main id="main">`, первый таб-стоп.
- Каждое поле имеет `<label for>` (или `.sr-only`-подпись); иконка-ссылка «↗» —
  `aria-label` + `title`.
- Таблицы: `caption.sr-only` + `th[scope=col]`.
- Живой регион объявляет только то, что коротко и осмысленно, и живёт в DOM ДО
  изменения: `#review-say` (`role="status"`) на странице сценариев. Вешать
  `aria-live` на блок, который сам же и подменяется (`hx-swap="outerHTML"`),
  бесполезно — регион приезжает вместе с содержимым и не озвучивается.
- Активный пункт навбара и активная вкладка — `aria-current`.
- Видимый фокус: `:focus-visible { outline: 2px solid transparent; box-shadow:
  var(--focus-ring) }` — не убирать. Кольцо двойное (белый + `--accent-blue-text`),
  на тёмном сайдбаре — `--focus-ring-dark`; прозрачный `outline` обязателен, иначе
  в forced-colors фокус исчезает совсем. Правило-исключение
  `.nav-item.active:focus-visible` возвращает кольцо на текущем разделе (иначе
  `.active` перебивает его по специфичности).
- Свопаемые htmx-блоки: у интерактивов внутри обязан быть стабильный `id` — htmx
  возвращает фокус после свопа только по нему (лента опрашивается раз в 2 с, и без
  id фокус клавиатуры улетал на `<body>`). Действия, пишущие в Sheets, несут
  `hx-disabled-elt` — иначе второй клик даёт вторую запись.
- Заголовки: `h1` раздела → `h2` карточек/тем → `h3` карточек сценариев.

## 7. Словарь «система → пользователь» (§2 спеки, утверждён 2026-07-25)

| Система | Интерфейс |
|---|---|
| бриф / скрипт / рил | **сценарий**; опубликованное — **ролик** |
| ниша | **тема** |
| формула | **рецепт ролика** |
| паттерн | **приём** |
| RAW / raw-видео | **собранные ролики** |
| ER | **вовлечённость** (в процентах) |
| звено | **этап** |
| eval | **Eval-петля** |
| фан-аут, approved-индекс, prompt version, пути к конфигам | не показываем продюсеру |
| approved / rejected / revised / pending / recommend / insufficient_data | утверждено / отклонено / доработать / ожидает / рекомендовать / данных мало |
| agent_id (`collect-tiktok`) | человеческое имя задачи (`Сбор роликов из TikTok`) |

Переводы и форматы живут в **одном** модуле — `src/cf/dashboard/labels.py`
(`fmt_views`, `fmt_pct`, `fmt_date`, `fmt_datetime`, `plural_ru`, `niche_label`,
`confidence_label`, `agent_label`, `trigger_label`, `link_label`, `run_view`,
`report_view`) и подключаются как Jinja-фильтры в `app.create_app`. Новый текст с
данными — через фильтр, не через формат в шаблоне.

**Инварианты:** одна сущность — одно слово (сценарий; ролик; тема); числа —
неразрывные разряды и `tabular-nums`; проценты — русская запятая; даты — `ДД.ММ.ГГГГ`;
имена инструментов в actor-плитках сохраняем (сбор — Apify).

## 8. Как проверять

`pytest tests/test_dashboard_routes.py tests/test_dashboard_labels.py
tests/test_dashboard_sections.py tests/test_lab_template.py` — там же живут
регресс-проверки этой ДС:

- `test_no_static_inline_styles_left_in_templates` — инлайны только вычисляемые;
- `test_empty_state_split_into_three_meanings`, `test_pagination_footer_has_no_watermark_class`;
- `test_declared_ui_font_is_actually_self_hosted`;
- `test_layout_has_breakpoints_for_tablet_and_phone`, `test_burger_toggle_is_in_shell_before_sidebar`,
  `test_wide_tables_scroll_inside_card_instead_of_being_clipped`;
- `test_skip_link_leads_to_main_content`, `test_period_selector_and_form_fields_have_labels`,
  `test_tables_have_caption_and_column_scopes`, `test_icon_only_links_have_accessible_names`,
  `test_async_regions_are_announced`, `test_navbar_marks_current_section`;
- `test_producer_sections_have_no_system_jargon_in_visible_text`,
  `test_every_section_opens_with_a_lead_line`.

Визуальная приёмка — локальный превью на фейковых данных
(`create_app(sheets=FakeSheets(...))` на свободном порту) и скриншоты на
375/768/1280/1920; прод для этого не нужен.
