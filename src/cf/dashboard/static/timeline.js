// Клиентский тикер полосок ленты конвейера (прогресс v2, спека §7).
//
// Сервер по каждому бегущему шагу отдаёт:
//   data-frac — якорь: доля, подтверждённая фактами на момент ответа,
//   data-ceil — ближайшая точка, до которой сервер дойдёт сам (следующий батч
//               или предел кривой фазы) — выше неё кривая не имеет права,
//   data-tau  — характерное время до этой точки, сек,
//   data-run  — метка запуска шага: сменилась → полоска начинается заново.
// Между опросами тикер приближает долю к потолку: p = ceil − (ceil − from)·e^(−dt/τ).
//
// Главное правило: кривую ведёт КЛИЕНТ, сервер даёт только якоря. htmx каждые 2 с
// подменяет разметку, и в свежем элементе стоит серверный якорь — он НИЖЕ уже
// показанного (между фактами сервер честно стоит на месте). Поэтому:
//   1) якорь — это пол, а не точка перезапуска: свой отсчёт t0 не сбрасывается,
//      пока сервер не прислал новый якорь (иначе бар получал бы только первые
//      2 с кривой снова и снова и почти не двигался между фактами);
//   2) ширина проставляется прямо в htmx:afterSwap — до первой отрисовки, иначе
//      кадр с серверным якорем успевает попасть на экран и полоску «отталкивает
//      назад» каждые 2 с (замечание оператора на живом прогоне 2026-07-25).
// Уважаем prefers-reduced-motion: при reduce не анимируем, но подписи обновляем.
(function () {
  var reduce = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ключ шага → состояние кривой: якорь сервера, точка старта, показанное,
  // момент старта отсчёта. Живёт в замыкании, поэтому переживает свопы htmx.
  var bars = Object.create(null);

  // Насколько якорь вправе оказаться ниже показанного, оставаясь тем же прогоном.
  // Интерполяция отстаёт от следующего факта на доли процента; провал на четверть
  // полоски означает новую полоску — страховка на случай, когда метки запуска нет.
  var RESET_GAP = 0.25;

  function stepKey(el) {
    var li = el.closest ? el.closest('.tl-step') : null;
    var name = li && li.querySelector('.tl-name');
    return (name && name.textContent.trim()) || 'step';
  }

  function approach(from, ceil, dt, tau) {
    if (!(tau > 0)) return from;
    if (!(ceil > from)) return from;
    return ceil - (ceil - from) * Math.exp(-dt / tau);
  }

  function fmtDuration(total) {
    var t = Math.max(0, Math.floor(total));
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    var mm = h ? (m < 10 ? '0' + m : String(m)) : String(m);
    var ss = s < 10 ? '0' + s : String(s);
    return h ? h + ':' + mm + ':' + ss : mm + ':' + ss;
  }

  function paintBar(el, now) {
    var frac = parseFloat(el.getAttribute('data-frac'));
    if (isNaN(frac)) return;
    var ceil = parseFloat(el.getAttribute('data-ceil'));
    if (isNaN(ceil)) ceil = frac;
    var tau = parseFloat(el.getAttribute('data-tau'));
    var key = stepKey(el);
    var run = el.getAttribute('data-run') || '';
    var st = bars[key];
    // другой запуск шага (или якорь, рухнувший ниже показанного) — забываем
    // накопленное, полоска начинается с нуля
    if (!st || st.run !== run || frac + RESET_GAP < st.shown) {
      st = bars[key] = {run: run, frac: null, ceil: 0, tau: 0,
                        from: 0, shown: 0, t0: now};
    }
    // новый якорь от сервера — новый отрезок кривой; тот же якорь — продолжаем
    // свой отсчёт, а не начинаем его заново на каждом свопе
    if (st.frac !== frac || st.ceil !== ceil || st.tau !== tau) {
      st.frac = frac; st.ceil = ceil; st.tau = tau;
      st.t0 = now; st.from = Math.max(frac, st.shown);
    }
    var value = reduce
      ? st.from
      : approach(st.from, ceil, (now - st.t0) / 1000, tau);
    value = st.shown = Math.max(st.shown, value);   // назад полоска не ходит
    el.style.width = (value * 100).toFixed(2) + '%';
    var row = el.closest('.tl-step');
    var pct = row && row.querySelector('.tl-pct');
    if (pct) pct.textContent = Math.round(value * 100) + '%';
    var bar = el.parentNode;
    if (bar && bar.setAttribute) {
      bar.setAttribute('aria-valuenow', String(Math.round(value * 100)));
    }
  }

  function tick() {
    var now = Date.now();
    var fills = document.querySelectorAll('.tl-fill[data-sim]');
    for (var i = 0; i < fills.length; i++) paintBar(fills[i], now);
    // «идёт 2:14» тикает своим клоком, чтобы шаг не выглядел зависшим между опросами
    var timers = document.querySelectorAll('.tl-elapsed[data-elapsed]');
    for (var j = 0; j < timers.length; j++) {
      var node = timers[j];
      var base = parseFloat(node.getAttribute('data-elapsed'));
      if (isNaN(base)) continue;
      var since = parseFloat(node.getAttribute('data-t'));
      if (isNaN(since)) { since = now; node.setAttribute('data-t', String(since)); }
      node.textContent = fmtDuration(base + (now - since) / 1000);
    }
  }

  // Скрипт подключён из base.html на КАЖДОЙ странице, а лента есть только на
  // обзоре: интервал, крутящийся на /briefs или /sources, — работа впустую.
  // Включаем таймер, только когда в DOM есть бегущая полоска или живой таймер;
  // htmx-своп сам включит его обратно, когда прогон начнётся. При reduce анимации
  // нет, подписи достаточно обновлять раз в секунду.
  var TICK_MS = reduce ? 1000 : 200;
  var timer = null;

  function schedule() {
    var live = document.querySelector('.tl-fill[data-sim], .tl-elapsed[data-elapsed]');
    if (live && timer === null) {
      timer = setInterval(tick, TICK_MS);
    } else if (!live && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  // Синхронно после подмены разметки — до отрисовки кадра: свежий элемент
  // сразу получает накопленную ширину вместо серверного якоря.
  document.addEventListener('htmx:afterSwap', function () { tick(); schedule(); });

  tick();
  schedule();
})();
