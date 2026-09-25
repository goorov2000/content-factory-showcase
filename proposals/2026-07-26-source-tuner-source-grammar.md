---
status: proposed
prompt_id: source-tuner
created: 2026-07-26
---
# Proposal: Source Tuner — зафиксировать грамматику поля `source` в proposal

## Current version
`prompts/agents/source-tuner.md` (агентские промпты в CF Prompt Versions не
версионируются — истина в файле репозитория).

Промпт нигде не говорит, КАК записывается поле `source` в
`proposals/YYYY-MM-DD-sources-<platform>.json`. Проверка:

```
$ grep -n "hashtag:\|query:\|source_marker\|source_query" prompts/agents/source-tuner.md
$ echo $?
1
```

Ни одного упоминания. Шаг 3 («Кандидаты на добавление: candidate_hashtags с
count >= 3…», `prompts/agents/source-tuner.md:11-13`) прямо отсылает агента к полю
отчёта, значение которого в proposal класть НЕЛЬЗЯ. Поля `niche` промпт тоже не
упоминает.

## Proposed changes

```diff
 3. Кандидаты на добавление: candidate_hashtags с count >= 3, которых нет в реестре
    `sources/<platform>.json` (любой status) и которые по смыслу — целевая ниша
    (не бренд-мусор, не generic вроде #fyp/#viral/#fashion).
+   Значение из отчёта копировать в proposal как есть нельзя: в
+   `candidate_hashtags[].hashtag` лежит `#тег` без префикса kind, а в поле `source`
+   нужен полный маркер — см. «Грамматика поля source».
```

```diff
 7. Proposal → `proposals/YYYY-MM-DD-sources-<platform>.json` по
    `schemas/source-proposal.schema.json`, status=pending.
    Валидация: `.venv/bin/python -m cf validate source-proposal <файл>`.
+   Схема ловит грамматику маркера, но не ловит промах мимо реестра. Поэтому
+   вторым шагом — сухой прогон применения:
+   `.venv/bin/python -m pytest -q tests/test_proposals_shipped.py`.
+   Красный тест = proposal при `cf apply-sources` сработает вхолостую.
```

```diff
 ## Правила
+- **Грамматика поля `source` — одна на remove и add**, ровно та же, что в колонке
+  `source_query` сырых вкладок; канон строит `cf.collect.sources.source_marker`:
+  - хэштег → `hashtag:#<тег>` — префикс kind И решётка обязательны;
+  - поисковый запрос → `query:<запрос>` — пробелы внутри запроса законны
+    (`query:pov парень`);
+  - `kind` в add-записи — `hashtag` либо `query`. Реестровый `search` пишет
+    аппликатор сам, в proposal его нет.
+  Голое имя тега (`mensoutfits`) — не сокращение, а поломка: `cf.sourcepatch._strip`
+  берёт хвост ПОСЛЕ первого двоеточия, поэтому в реестр уедет пустой `query`, а
+  несколько таких записей схлопнутся дедупликацией по ключу `(kind, '')` в одну.
+- **Где брать маркер.** Для remove — копируй `sources[].source` из отчёта
+  source-stats: там уже готовый маркер. Для add маркера НЕТ ни в одном поле отчёта:
+  `candidate_hashtags[].hashtag` — это `#тег`, префикс kind добавляешь сам.
+- **`niche` в add-записи заполняй всегда.** Без него аппликатор наследует нишу
+  первой active-записи реестра; порядок записей в файле — не твоё решение и меняется
+  применениями. Ниша должна быть из `prompts/agents/niche-taxonomy.json`.
```

## Evidence

**Ошибка уже произошла и была поймана только тестом.** В
`proposals/2026-07-26-sources-instagram.json` (прогон 2026-07-26T07:09) поле `source`
было записано в двух разных грамматиках одним и тем же агентом за один проход:
`remove[0].source = "hashtag:#outfitideas"` — верно, все три `add[].source`
(`"mensoutfits"`, `"mensstyle"`, `"мужскойстилист"`) — без префикса kind.

Красный тест:

```
$ .venv/bin/python -m pytest -q tests/test_proposals_shipped.py
FAILED tests/test_proposals_shipped.py::test_shipped_source_proposals_apply_cleanly[2026-07-26-sources-instagram.json]
E       AssertionError: 2026-07-26-sources-instagram.json: после применения реестр не проходит схему
E       assert ["80/query: '' should be non-empty"] == []
```

Что случилось бы при `cf apply-sources`: `_strip` (`src/cf/sourcepatch.py:15-18`)
делает `partition(":")` и берёт часть после двоеточия — для `"mensoutfits"` это
пустая строка. Дальше дедупликация по ключу `(kind, query)`
(`src/cf/sourcepatch.py:30`, `:61-63`) схлопывает три записи в одну — в реестр уехал
бы один мусорный источник `{"query": "", "kind": "hashtag", ...}` вместо трёх тегов.
Сбор по нему ничего не собрал бы, а оператор увидел бы «применено».

**Агент скопировал то, что видел.** `src/cf/sourcestats.py` отдаёт две разные формы
в одном отчёте (`agent-runtime/source-stats/2026-07-26-raw_instagram-source-stats.json`,
он же вход того прогона):

- `sources[].source` — ПОЛНЫЙ маркер: `source = str(r.get("source_query", ""))…`
  (`src/cf/sourcestats.py:17-19`), в отчёте это `hashtag:#outfitideas`;
- `candidate_hashtags[].hashtag` — `#тег` без префикса kind:
  `re.findall(r"#[\w\d_]+", …)` (`src/cf/sourcestats.py:32-34`, `:41-43`), в отчёте
  это `#mensoutfits`.

Шаги промпта устроены так, что remove берётся из первого списка, add — из второго
(`prompts/agents/source-tuner.md:9-13`). При отсутствии правила о грамматике
расхождение remove/add воспроизводится ДЕТЕРМИНИРОВАННО, а не по невнимательности:
это ровно то, что видно в отчёте.

**Канон однозначен и задан кодом в трёх местах:**
`source_marker` (`src/cf/collect/sources.py:301-305`) — `"hashtag:#" + query` либо
`"query:" + query`; `make_batches` (`src/cf/collect/sources.py:335`) кладёт тот же
маркер в `batch_sources`; `apply_proposal_to_sources`
(`src/cf/sourcepatch.py:72-75`) фильтрует по `startswith("hashtag:")` /
`startswith("query:")`. То есть промпт — единственное звено цепочки, где грамматика
не записана.

**Валидатор молчал.** У поля `source` в `schemas/source-proposal.schema.json` не было
`pattern`, и `cf validate source-proposal` выдавал `OK` на сломанном файле. `pattern`
добавлен (`$defs/source_marker`) и покрыт тестами
`tests/test_validate.py::test_add_source_without_kind_prefix_rejected`,
`::test_remove_source_without_kind_prefix_rejected`,
`::test_hashtag_marker_without_hash_rejected`. Схема — сеть безопасности, но она
ловит ошибку ПОСЛЕ того, как агент потратил прогон; правило в промпте не даёт её
совершить. Без правки промпта ошибка вернётся на следующем еженедельном прогоне —
инструкция, из-за которой она возникла, не изменилась.

**Про `niche`.** `apply_proposal_to_registry` подставляет нишу первой active-записи
реестра, если её нет в proposal (`src/cf/sourcepatch.py:48-49`, `:61-63`). Для
IG-прогона 2026-07-26 унаследованное значение случайно совпало с верным (все 20
активных записей `sources/instagram.json` несут `мужские-образы`), но зависимость от
порядка записей в файле — не то, на чём должна держаться разметка источника.

## Confidence
high — дефект воспроизведён на боевом файле, механика поломки прослежена до строк
кода, канон подтверждён тремя независимыми местами в коде, а причина «агент
скопировал форму из отчёта» подтверждается тем, что remove (другой источник формы)
записан верно.

## Risks
Правка чисто инструктивная: +1 команда в шаге 7 (`pytest` по одному файлу, секунды,
бесплатно) и три пункта в «Правилах». Риск — рост длины промпта; он умеренный, а
альтернатива (полагаться только на схему) оставляет ошибку повторяемой, просто
шумной.

Второй риск: требование заполнять `niche` заставит агента выбирать нишу для тега,
про который у него мало данных. Смягчение — ниша берётся из
`prompts/agents/niche-taxonomy.json` и всегда может быть исправлена оператором в
момент ревью; это лучше молчаливого наследования от первой строки файла.

Применение: правки файла промпта руками оператора (deny-правила запрещают агентам
писать в `prompts/agents/**`) + git commit.
