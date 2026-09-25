# 02 — Схема визуальных фактов

**Blocked by:** нет — можно начинать сразу
**Status:** done
**Type:** enhancement

Спека: docs/plans/2026-08-14-visual-contour/spec.md (§Схема визуальных фактов).

## Что построить

Схема визуальных фактов — новый валидируемый kind завода: `cf validate`
умеет проверить JSON визуальных фактов, и контракт честности вшит в саму
схему, а не только в промпт. Состав полей — по спеке (референс — vision-агент
архивного n8n): `observed_media`, `visual_evidence[]` с обязательным
`media_ref`, `visual_hook`, `screen_text`, `shooting_format`, `main_emotion`,
`formatting_pattern`, `visual_hypotheses`, `unsupported_visual_claims`,
`engine`/`model`/`analyzed_at`.

Этой схемой vision-звено (тикет 05) валидирует выход движка: невалидный
ответ = `vision_failed`, мусор в Sheets не попадает.

## Критерии приёмки

- [ ] Новый файл схемы в `schemas/`, kind зарегистрирован в валидаторе и
      доступен через `cf validate`.
- [ ] Каждая запись `visual_evidence` обязана нести `media_ref`
      (`cover` | `frame:N`) — факт без ссылки на конкретное медиа невалиден.
- [ ] Гипотезы (`visual_hypotheses`) и «что нельзя утверждать»
      (`unsupported_visual_claims`) — отдельные поля, структурно не смешиваются
      с `visual_evidence`; `observed_media` ∈ {cover, frames, none}.
- [ ] Тесты: валидная фикстура проходит; фикстуры-нарушители (evidence без
      media_ref, неизвестный observed_media, произвольный мусор) падают.

## Комментарии

2026-08-14, агент: реализовано в коммите 1666e34 (сессия /implement 14.08; сьют 2166 passed).
