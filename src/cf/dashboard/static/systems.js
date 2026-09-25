// Всплывашка «СОСТОЯНИЕ СИСТЕМ» в сайдбаре (решение владельца 2026-07-28).
//
// Раскрывается по-прежнему средствами CSS (:hover / :focus-within) — здесь
// только КООРДИНАТЫ. Панель position: fixed, потому что у сайдбара
// overflow-y: auto: абсолютная панель обрезалась ровно по его границе, и со
// стороны это выглядело как «страница перекрывает всплывашку». Фиксированной
// панели «рядом с кнопкой» приходится считать по месту самой кнопки — ровно
// как соседним «Уведомлениям» (notifications.js).
//
// Слушатели висят на document, а не на кнопке: партиал health.html htmx меняет
// целиком раз в 60 с, и обработчик, привязанный к самому элементу, пережил бы
// только первый своп.
(function () {
  function place(btn) {
    var pop = btn.querySelector('.sys-pop');
    if (!pop) return;
    // Узкий экран: панель раскрывается в потоке под кнопкой (см. @media),
    // координаты ей не нужны и только мешали бы.
    if (getComputedStyle(pop).position !== 'fixed') return;
    var box = btn.getBoundingClientRect();
    pop.style.left = Math.round(box.left) + 'px';
    pop.style.bottom = Math.round(window.innerHeight - box.top + 8) + 'px';
  }

  function target(evt) {
    var el = evt.target;
    return el && el.closest ? el.closest('.sys-btn') : null;
  }

  // mouseover/focusin всплывают (в отличие от mouseenter/focus), поэтому
  // делегирование работает и для кнопки, приехавшей свежим свопом.
  ['mouseover', 'focusin'].forEach(function (type) {
    document.addEventListener(type, function (evt) {
      var btn = target(evt);
      if (btn) place(btn);
    });
  });

  // Кнопка уезжает вместе с окном, а с ней и «рядом»: пересчёт дешевле, чем
  // панель, повисшая в стороне от своей кнопки.
  window.addEventListener('resize', function () {
    var btn = document.querySelector('.sys-btn');
    if (btn) place(btn);
  });
})();
