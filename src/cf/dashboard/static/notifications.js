// Панель «Уведомления» в сайдбаре (решение владельца 2026-07-28, С2).
//
// Всплывает рядом с кнопкой поверх страницы, шире сайдбара; закрывается кликом
// вне, клавишей Esc и повторным кликом по кнопке. JS нужен именно ради этих трёх
// путей закрытия: CSS-раскрывашка соседнего индикатора систем (.sys-btn:hover)
// не умеет ни Esc, ни клик вне, ни удержание открытого состояния.
//
// Содержимое рендерит сервер и оно НЕ живёт в скрипте: строки — производная от
// состояния завода, они появляются и исчезают вместе с условием. Здесь только
// открыть/закрыть и фокус.
(function () {
  var btn = document.getElementById('notif-btn');
  var pop = document.getElementById('notif-panel');
  if (!btn || !pop) return;

  function isOpen() {
    return btn.getAttribute('aria-expanded') === 'true';
  }

  function place() {
    // Панель position: fixed (иначе её обрезал бы overflow сайдбара), поэтому
    // «рядом с кнопкой» приходится ставить по месту самой кнопки — оно зависит
    // от того, есть ли над ней индикатор систем.
    if (getComputedStyle(pop).position !== 'fixed') return;   // узкий экран: в потоке
    var box = btn.getBoundingClientRect();
    pop.style.left = Math.round(box.left) + 'px';
    pop.style.bottom = Math.round(window.innerHeight - box.top + 8) + 'px';
  }

  function open() {
    btn.setAttribute('aria-expanded', 'true');
    pop.hidden = false;
    place();
    // Фокус уходит в панель: иначе с клавиатуры до её ссылок не добраться, а
    // Esc некому было бы поймать.
    pop.focus();
  }

  function close(returnFocus) {
    if (!isOpen()) return;
    btn.setAttribute('aria-expanded', 'false');
    pop.hidden = true;
    // Фокус возвращается на кнопку — туда, откуда пользователь ушёл.
    if (returnFocus) btn.focus();
  }

  btn.addEventListener('click', function (evt) {
    evt.stopPropagation();      // свой же клик не должен сработать как «клик вне»
    if (isOpen()) close(true); else open();
  });

  document.addEventListener('click', function (evt) {
    if (!isOpen()) return;
    if (pop.contains(evt.target) || btn.contains(evt.target)) return;
    close(false);               // клик по странице — фокус не отбираем
  });

  document.addEventListener('keydown', function (evt) {
    if (evt.key === 'Escape' || evt.key === 'Esc') close(true);
  });

  // При смене размера окна кнопка уезжает, а вместе с ней и «рядом»: пересчёт
  // дешевле и честнее, чем панель, повисшая в стороне от своей кнопки.
  window.addEventListener('resize', function () { if (isOpen()) place(); });
})();
