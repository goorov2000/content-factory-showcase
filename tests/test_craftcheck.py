"""Детерминированные проверки ремесла сценария (cf.craftcheck).

Опорные точки калибровки — реальные вердикты brief-reviewer от 03.08, единственные
два случая, когда норма «хук не произносим за 3с» вообще применялась:
  seven-white-tees — ОТКЛОНЁН, 151 знак в окне 3с;
  sales-window     — пропущен тем же ревьюером в тот же день, 253 знака.
Проверка обязана ловить оба. И обязана молчать на лучших сценариях корпуса — иначе
это не гейт, а остановка конвейера (порог 1.0 давал красный у 94% брифов).
"""
import pytest

from cf.craftcheck import (craft_defects, duplicate_content, hook_is_stage_direction,
                           hook_overrun, parse_timing, segment_overruns,
                           self_contradiction, unfilled_placeholders)


# --- parse_timing -----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("0-3s", 3.0), ("0–3с", 3.0), ("0-5 сек", 5.0), ("2-8с", 6.0),
    ("", None), (None, None), ("первые секунды", None), ("3-3с", None),
])
def test_parse_timing(raw, expected):
    assert parse_timing(raw) == expected


# --- хук: тайминг -----------------------------------------------------------------

def test_hook_overrun_catches_the_brief_the_reviewer_rejected():
    # seven-white-tees: 151 знак в окне 0-3с — ревьюер отклонил его руками 03.08
    hook = ("«Лучшие белые футболки прямо сейчас — топ-7, без рейтинга: порядок не "
            "важен, важно, за что каждая в списке. Досмотри — в конце скажешь, кого "
            "я пропустил»")
    assert hook_overrun(hook, 3.0) is not None


def test_hook_overrun_catches_the_brief_the_reviewer_missed():
    # sales-window: 253 знака в том же окне 0-3с — одобрен в тот же день
    hook = ("«Думаешь, [масс-якорь: WB/Ozon/Zara-аналог] — единственное место, где "
            "мужчине одеться на распродаже? Началась финальная распродажа лета — и "
            "именно сейчас так думать дороже всего. Три бренда, которые прямо сейчас "
            "стоят как [якорь], а выглядят втрое дороже»")
    assert hook_overrun(hook, 3.0) is not None


def test_hook_overrun_silent_on_a_working_hook():
    # tee-fit — верх корпуса по консенсусу трёх судей; гейт обязан его пропустить
    hook = ("Большинство футболок сидят на тебе неправильно. Не «не тот бренд» — "
            "просто неправильно, и вот по каким трём признакам это видно")
    assert hook_overrun(hook, 3.0) is None


def test_hook_overrun_needs_a_window():
    # рецепт без парсибельного timing -> fail-open, это забота ревьюера, а не гейта
    assert hook_overrun("любой сколь угодно длинный хук " * 20, None) is None


def test_hook_overrun_ignores_quote_marks():
    # кавычки — разметка речи, а не знаки: «X» и X обязаны считаться одинаково
    plain = "а" * 100
    assert (hook_overrun(f"«{plain}»", 2.0) is None) == (hook_overrun(plain, 2.0) is None)


# --- хук: ремарка вместо реплики --------------------------------------------------

@pytest.mark.parametrize("hook", [
    "0-1с: автор уже в кадре целиком в летнем светлом образе, без интро и заставки",
    "Плашка на первом кадре: «Летние костюмы»",
    "Текстом на экране с первого кадра: «Знакомьтесь — [имя модели]»",
    "Первый образ уже на авторе в кадре, поверх — плашка «мои рабочие образы»",
    "Разговорное исполнение, автор в кадре. Хронометраж 35-45 секунд",
])
def test_hook_is_stage_direction(hook):
    # v4-промпт требовал «взять элемент из hook_structure.elements», а elements —
    # описания кадра: в 12 из 13 брифов v4 в поле hook попала ремарка
    assert hook_is_stage_direction(hook) is not None


@pytest.mark.parametrize("hook", [
    "Заправка решает: один приём — и тот же образ выглядит собранным",
    "«Мне M. Я ношу M пятнадцать лет». — «Покажите, где»",
    "Рубашка с коротким рукавом взрослому мужчине идёт больше футболки",
])
def test_real_hooks_are_not_stage_directions(hook):
    assert hook_is_stage_direction(hook) is None


# --- сегменты раскадровки ---------------------------------------------------------

def test_segment_overrun_flags_impossible_speech():
    script = ('0-3с: хук — «' + "слово " * 40 + '». 3-9с: дальше по плану.')
    assert segment_overruns(script)


def test_segment_without_speech_is_not_flagged():
    # немой монтаж — законный формат половины рецептов, реплик там нет вовсе
    script = "0-3с: автор в кадре, плашка. 3-9с: смена образа под музыку, без речи."
    assert segment_overruns(script) == []


def test_trailing_target_range_is_not_a_segment():
    # «55-360 сек» в хвосте — целевой хронометраж рецепта, а не сегмент раскадровки
    script = '0-5с: «короткая реплика». 5-25с: показ. Хронометраж 55-360 сек.'
    assert segment_overruns(script) == []


def test_segment_overruns_are_capped():
    script = "".join(f'{i}-{i+1}с: «' + "слово " * 30 + '». ' for i in range(0, 10))
    assert len(segment_overruns(script, limit=3)) == 3


# --- незаполненные подстановки ----------------------------------------------------

def test_unfilled_placeholders_detected():
    out = unfilled_placeholders("Знакомьтесь — [ИМЯ МОДЕЛИ]", "цена [сумма] рублей")
    assert out and "[ИМЯ МОДЕЛИ]" in out and "[сумма]" in out


def test_placeholder_with_explicit_substitution_rule_is_allowed():
    # бриф честно говорит, что значения подставляет криэйтор -> это не бланк
    assert unfilled_placeholders(
        "Артикулы [номер] — криэйтор подставляет реальные номера по карточкам") is None


def test_no_placeholders_no_defect():
    assert unfilled_placeholders("обычный текст без скобок", "и ещё один") is None


# --- самопротиворечие -------------------------------------------------------------

def test_first_episode_script_with_fourth_episode_caption():
    # grwm-...-series: снять по такому брифу можно только соврав про три эпизода
    assert self_contradiction(
        "Это ПЕРВЫЙ эпизод серии: ссылок на прошлые голосования нет", "",
        "Эпизод 4: вещь выбрали вы, образ собираю я", "") is not None


def test_script_bans_dates_caption_announces_them():
    assert self_contradiction(
        "Дат, адресов, площадок и времени сбора нет", "",
        "ПОП-АП В ЭТУ СУББОТУ. Время, место и все офферы — на экране", "") is not None


def test_restating_a_prohibition_is_not_a_violation():
    # Дискриминирующий: «без ссылок, артикулов и промокодов» — это ПОВТОР запрета.
    # Без поправки на отрицание гейт наказывал брифы за аккуратность (slim-suit,
    # numeric-criteria попадали в красный именно так).
    assert self_contradiction(
        "Ни цен, ни артикулов в ролике", "",
        "Какой из четырёх критериев главный?",
        "Без ссылок, артикулов и промокодов.") is None


@pytest.mark.parametrize("caption", [
    "Снимаем в новом ТЦ, артикул 12345 ищите тут",   # «сНИмаем»
    "Они уже в наличии — артикул 12345",             # «оНИ»
    "В интернете артикул 12345",                     # «интерНЕТ»
    "Безопасная посадка, артикул 12345",             # «БЕЗопасная»
])
def test_negation_inside_ordinary_words_does_not_hide_violation(caption):
    # Ревью 14.09.2026: «без|ни|нет» искались без границ слова, и любое слово с этими
    # буквами перед нарушением читалось как повтор запрета — проверка молча гасла.
    assert self_contradiction("Правило: без цен и артикулов в кадре.", "",
                              caption, "") is not None


def test_ordinary_words_do_not_read_as_a_ban():
    # Ревью 14.09.2026: запрет «(ни|без|нет) цен/артикул» тоже искался без границ
    # слова — «они цены держат» читалось как «ни цен», и честный капшен с ценой
    # получал дефект самопротиворечия на пустом месте.
    assert self_contradiction("Они цены держат, показываем три образа", "",
                              "Цена 4 990 ₽, артикул 12345", "") is None


def test_caption_that_really_gives_articles_is_flagged():
    assert self_contradiction(
        "Ни цен, ни артикулов голосом в ролике", "",
        "Сохраняй артикулы WB ⤵️ ОБРАЗ 1 — поло, шорты", "") is not None


# --- сборка -----------------------------------------------------------------------

def test_craft_defects_orders_blockers_first():
    brief = {"hook": "0-3с: автор в кадре",
             "script_text": "0-3с: «реплика». Знакомьтесь — [ИМЯ]",
             "visual_direction": "", "caption": "", "cta": ""}
    defects = craft_defects(brief, 3.0)
    assert "подстановки" in defects[0]        # бланк — тяжелее всего
    assert any("описание кадра" in d for d in defects)


def test_craft_defects_green_on_a_top_script():
    # silent-shortsleeve — второй по консенсусу судей; гейт обязан молчать
    brief = {
        "hook": "Рубашка с коротким рукавом взрослому мужчине идёт больше футболки. "
                "Это видно за секунду",
        "script_text": "0-3с. Тезис целиком — текстом на экране поверх первого кадра. "
                       "3-6с. Доказательство 1, встык. 6-9с. Доказательство 2.",
        "visual_direction": "Штатив, одна точка, один свет.",
        "caption": "Один тезис на ролик.", "cta": "Подписывайся",
    }
    assert craft_defects(brief, 3.0) == []


def test_craft_defects_empty_brief_does_not_crash():
    assert craft_defects({}, 3.0) == []


# --- пересъёмка -------------------------------------------------------------------

_ALPHA = "абвгдежзиклмнопрст"


def _words(prefix, n):
    """n РАЗНЫХ слов из букв: токенайзер craftcheck берёт [а-яёa-z]{4,} и цифры
    отбрасывает, поэтому «слово1/слово2» схлопнулись бы в один токен."""
    return [f"{prefix}{_ALPHA[i // len(_ALPHA)]}{_ALPHA[i % len(_ALPHA)]}" for i in range(n)]


def _script(words):
    return " ".join(words)


def test_clone_of_a_single_prior_is_flagged():
    prior = _script(_words("слово", 40))
    clone = _script(_words("слово", 40) + ["новое", "окончание"])
    assert duplicate_content(clone, [prior]) is not None


def test_reassembly_from_several_priors_is_flagged():
    # seven-pairs-mixed: пересобран из ТРЁХ брифов, парная близость всего 0.38 —
    # ловится только покрытием объединения
    p1 = _script(_words("перв", 30))
    p2 = _script(_words("втор", 30))
    p3 = _script(_words("трет", 30))
    mixed = _script(_words("перв", 12) + _words("втор", 12)
                    + _words("трет", 12) + ["свежее", "слово"])
    assert duplicate_content(mixed, [p1]) is None          # с одним — не повтор
    assert duplicate_content(mixed, [p1, p2, p3]) is not None


def test_genuinely_new_script_passes():
    prior = _script(_words("старое", 40))
    fresh = _script(_words("другое", 40))
    assert duplicate_content(fresh, [prior]) is None


def test_no_priors_no_verdict():
    assert duplicate_content(_script(_words("с", 40)), []) is None


def test_short_script_is_not_judged():
    # на коротком тексте лексическое пересечение недостоверно
    assert duplicate_content("три слова тут", ["три слова тут"]) is None


def test_duplicate_appears_in_craft_defects():
    prior = _script(_words("слово", 40))
    brief = {"hook": "нормальный короткий хук", "script_text": prior,
             "visual_direction": "", "caption": "", "cta": ""}
    defects = craft_defects(brief, 3.0, prior_scripts=[prior])
    assert any("повторяет" in d for d in defects)
