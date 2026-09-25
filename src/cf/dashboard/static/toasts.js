// Всплывающие уведомления (решение владельца 2026-07-28, С1).
//
// Причина, словами владельца: «сообщения в виде полос на странице ломают
// страницу, сдвигая элементы ниже». Поэтому контейнер — position: fixed, и
// появление сообщения не двигает ни одного элемента вёрстки.
//
// Параметры зафиксированы владельцем: справа снизу, 6 секунд, полоса исчезания,
// наведение ПРИОСТАНАВЛИВАЕТ таймер, крестик закрывает вручную, исчезая тост
// улетает влево — в кнопку «Уведомления», в стопке одновременно не больше трёх.
//
// Таймер и полоса — ОДИН механизм: срок жизни тоста считает та самая CSS-анимация,
// которая рисует полосу (animationend → закрыть). Поэтому пауза по наведению
// (animation-play-state) останавливает и полосу, и срок — двум независимым
// счётчикам разъезжаться было бы не на чем, но и синхронизировать их не нужно.
(function () {
  var MAX_VISIBLE = 3;             // остальные ждут очереди
  var FLY_MS = 320;                // столько длится полёт в кнопку

  var host = document.getElementById('toasts');
  var data = document.getElementById('toast-data');
  if (!host || !data) return;

  var reduce = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var queue = [];
  try {
    queue = JSON.parse(data.textContent || '[]') || [];
  } catch (e) {
    return;                        // битый payload молча не ломает страницу
  }

  function remove(toast) {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
    pump();                        // освободилось место — пускаем следующий
  }

  function dismiss(toast) {
    if (toast.dataset.leaving) return;
    toast.dataset.leaving = '1';
    if (reduce) { remove(toast); return; }
    // Полёт влево, в кнопку «Уведомления»: цель у сообщения одна, и она видна.
    var btn = document.getElementById('notif-btn');
    var from = toast.getBoundingClientRect();
    if (btn) {
      var to = btn.getBoundingClientRect();
      toast.style.transform = 'translate(' +
        Math.round(to.left + to.width / 2 - (from.left + from.width / 2)) + 'px, ' +
        Math.round(to.top + to.height / 2 - (from.top + from.height / 2)) + 'px) scale(.4)';
    } else {
      toast.style.transform = 'translateX(-40px) scale(.8)';
    }
    toast.style.opacity = '0';
    window.setTimeout(function () { remove(toast); }, FLY_MS);
  }

  function build(item) {
    var toast = document.createElement('div');
    toast.className = 'toast toast--' + (item.tone === 'error' ? 'error' : 'warn');

    var body = document.createElement('div');
    body.className = 'toast-body';
    var text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = item.text || '';
    body.appendChild(text);
    if (item.href && item.action) {
      var link = document.createElement('a');
      link.className = 'toast-action';
      link.href = item.href;
      link.textContent = item.action;
      body.appendChild(link);
    }

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Закрыть уведомление');
    close.textContent = '×';
    close.addEventListener('click', function () { dismiss(toast); });

    var bar = document.createElement('div');
    bar.className = 'toast-bar';
    bar.addEventListener('animationend', function () { dismiss(toast); });

    toast.appendChild(body);
    toast.appendChild(close);
    toast.appendChild(bar);
    return toast;
  }

  function pump() {
    while (queue.length && host.children.length < MAX_VISIBLE) {
      host.appendChild(build(queue.shift()));
    }
  }

  pump();
})();
