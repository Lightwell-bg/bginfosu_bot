/* Мини-апп BGINFO: тонкий клиент. Сервер ведёт анкету, здесь только рисуем экраны.
   Без сборки и фреймворков; серверный HTML проходит только через санитайзер (sanitizeInto). */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  var app = document.getElementById('app');
  var toastEl = document.getElementById('toast');
  var FREE_LIMIT = 1500;
  var ref = { sid: null, rev: null };
  var busy = false;
  var mainHandler = null;
  var backHandler = null;
  var toastTimer = null;

  /* ---------- DOM-помощники ---------- */

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function safeUrl(u) {
    return typeof u === 'string' && /^https?:\/\//i.test(u) ? u : null;
  }

  function openLink(url) {
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, '_blank', 'noopener');
  }

  function makeLink(url, label) {
    var a = el('a', null, label);
    var u = safeUrl(url);
    if (u) {
      a.setAttribute('href', u);
      a.addEventListener('click', function (ev) { ev.preventDefault(); openLink(u); });
    }
    return a;
  }

  /* ---------- Санитайзер: белый список тегов, остальное текстом ---------- */

  var ALLOWED = { B: 1, STRONG: 1, I: 1, EM: 1, U: 1, S: 1, CODE: 1, PRE: 1, BR: 1, A: 1 };
  var SKIP = { SCRIPT: 1, STYLE: 1, TEMPLATE: 1, NOSCRIPT: 1 };

  function addText(parent, text) {
    var parts = String(text).split('\n');
    for (var i = 0; i < parts.length; i++) {
      if (i > 0) parent.appendChild(document.createElement('br'));
      if (parts[i]) parent.appendChild(document.createTextNode(parts[i]));
    }
  }

  function walk(src, dst) {
    for (var n = src.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) {
        addText(dst, n.nodeValue);
      } else if (n.nodeType === 1) {
        if (SKIP[n.tagName]) continue;
        if (ALLOWED[n.tagName]) {
          if (n.tagName === 'BR') { dst.appendChild(document.createElement('br')); continue; }
          var copy = document.createElement(n.tagName.toLowerCase());
          if (n.tagName === 'A') {
            var u = safeUrl(n.getAttribute('href') || '');
            if (u) {
              copy.setAttribute('href', u);
              (function (url) {
                copy.addEventListener('click', function (ev) { ev.preventDefault(); openLink(url); });
              })(u);
            }
          }
          walk(n, copy);
          dst.appendChild(copy);
        } else {
          walk(n, dst); // неразрешённый тег: оставляем только его содержимое как текст
        }
      }
    }
  }

  function sanitizeInto(parent, html) {
    var doc = new DOMParser().parseFromString('<!doctype html><body>' + String(html || ''), 'text/html');
    walk(doc.body, parent);
    return parent;
  }

  function rich(html, cls) {
    return sanitizeInto(el('div', 'rich' + (cls ? ' ' + cls : '')), html);
  }

  /* ---------- Тост, кнопки Telegram ---------- */

  function toast(msg) {
    if (!msg) return;
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, 4000);
  }

  function setMain(text, handler) {
    if (!tg) return;
    var mb = tg.MainButton;
    if (mainHandler) mb.offClick(mainHandler);
    mainHandler = function () { if (!busy) handler(); };
    mb.onClick(mainHandler);
    mb.setText(text);
    mb.show();
  }

  function hideMain() {
    if (!tg) return;
    if (mainHandler) tg.MainButton.offClick(mainHandler);
    mainHandler = null;
    tg.MainButton.hide();
  }

  function setBack(handler) {
    if (!tg || !tg.BackButton) return;
    if (backHandler) tg.BackButton.offClick(backHandler);
    backHandler = null;
    if (handler) {
      backHandler = function () { if (!busy) handler(); };
      tg.BackButton.onClick(backHandler);
      tg.BackButton.show();
    } else {
      tg.BackButton.hide();
    }
  }

  function setBusy(on) {
    busy = on;
    app.classList.toggle('busy', on);
    var ctl = app.querySelectorAll('button, input, textarea');
    for (var i = 0; i < ctl.length; i++) ctl[i].disabled = on;
    var opts = app.querySelectorAll('.opt');
    for (var j = 0; j < opts.length; j++) opts[j].classList.toggle('disabled', on);
    if (tg) {
      if (on) tg.MainButton.showProgress(false);
      else tg.MainButton.hideProgress();
    }
  }

  /* ---------- Сеть ---------- */

  function post(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'tma ' + tg.initData },
      body: JSON.stringify(body || {})
    });
  }

  function call(path, body) {
    if (busy) return;
    setBusy(true);
    post(path, body).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        setBusy(false);
        if (resp.status === 401) return showMessage('Откройте форму заново из бота', 'Сессия Telegram устарела или недействительна. Закройте окно и откройте форму из бота @bginfosu_bot.');
        if (resp.status === 503) return showMessage('Сервис временно недоступен', 'Попробуйте через несколько минут.', true);
        if (resp.status === 409) {
          render(data);
          toast('Анкета изменилась в другом окне — показываю актуальный шаг');
          return;
        }
        if (!resp.ok) { toast('Не получилось обработать запрос'); return; }
        render(data);
      });
    }).catch(function () {
      setBusy(false);
      toast('Нет связи, попробуйте ещё раз');
    });
  }

  function open() { call('/api/open', {}); }

  function step(kind, action, arg, text) {
    var body = { kind: kind };
    if (ref.sid !== null && ref.rev !== null) { body.sid = ref.sid; body.rev = ref.rev; }
    if (kind === 'button') { body.action = action; body.arg = arg || ''; }
    else body.text = text || '';
    call('/api/step', body);
  }

  function btn(action, arg) { step('button', action, arg || ''); }

  /* ---------- Экраны ---------- */

  function screen() {
    clear(app);
    window.scrollTo(0, 0);
    return app;
  }

  function showMessage(title, text, retry) {
    hideMain(); setBack(null);
    var root = screen();
    var box = el('div', 'center');
    box.appendChild(el('h1', null, title));
    if (text) box.appendChild(el('p', null, text));
    root.appendChild(box);
    if (retry) {
      var b = el('button', 'btn secondary', 'Повторить');
      b.addEventListener('click', function () { if (ref.sid === null) open(); else btn('show'); });
      root.appendChild(b);
    }
  }

  function render(data) {
    ref.sid = data.sid === undefined ? null : data.sid;
    ref.rev = data.rev === undefined ? null : data.rev;
    var view = data.view || null;
    if (!view && !(data.replies || []).length && data.alert) {
      // только уведомление (например, исход заявки): текущий экран остаётся, показываем тост
      toast(data.alert);
      if (busy) setBusy(false);
      return;
    }
    if (view && view.question) renderQuestion(view.question, data);
    else if (view && view.review) renderReview(view.review, data);
    else if (view && view.result) renderResult(view.result, data);
    else if (view && view.welcome) renderWelcome(view.welcome, data);
    else renderGeneric(data);
    if (data.alert) toast(data.alert);
    if (busy) setBusy(false);
  }

  function replyButtons(parent, replies, skip) {
    (replies || []).forEach(function (r) {
      (r.buttons || []).forEach(function (row) {
        row.forEach(function (b) {
          if (skip && skip(b)) return;
          if (b.action === 'webapp') return;
          var x = el('button', 'btn secondary', b.text);
          x.addEventListener('click', function () { btn(b.action, b.arg); });
          parent.appendChild(x);
        });
      });
    });
  }

  /* Сопровождающие сообщения («Сумма не может быть отрицательной», «Правила обновились…»):
     все ответы, кроме последнего, показываем над экраном как заметки (через санитайзер). */
  function infoNotes(root, data) {
    var rs = data.replies || [];
    for (var i = 0; i < rs.length - 1; i++) {
      if (rs[i] && rs[i].html) root.appendChild(rich(rs[i].html, 'info-note'));
    }
  }

  /* Ссылка-переключатель «Рассказать своими словами»: показывает/скрывает поле свободного текста в конце экрана. */
  function freeLink(root) {
    var freeWrap = null;
    var fb = el('button', 'linkbtn', 'Рассказать своими словами');
    fb.addEventListener('click', function () {
      if (freeWrap) { freeWrap.remove(); freeWrap = null; return; }
      freeWrap = freeBlock();
      root.appendChild(freeWrap);
      freeWrap.querySelector('textarea').focus();
    });
    return fb;
  }

  function freeBlock() {
    var wrap = el('div', 'free');
    wrap.appendChild(el('h2', null, 'Расскажите своими словами'));
    var ta = el('textarea', 'field');
    ta.maxLength = FREE_LIMIT;
    ta.rows = 5;
    ta.placeholder = 'Опишите вашу ситуацию — я предзаполню ответы, а вы их проверите';
    var cnt = el('div', 'counter', '0 / ' + FREE_LIMIT);
    ta.addEventListener('input', function () { cnt.textContent = ta.value.length + ' / ' + FREE_LIMIT; });
    var send = el('button', 'btn', 'Отправить');
    send.addEventListener('click', function () {
      var v = ta.value.trim();
      if (!v) { toast('Напишите хотя бы пару слов'); return; }
      step('text', '', '', v);
    });
    wrap.appendChild(ta);
    wrap.appendChild(cnt);
    wrap.appendChild(send);
    wrap.style.display = 'flex';
    wrap.style.flexDirection = 'column';
    wrap.style.gap = '10px';
    return wrap;
  }

  function openExternal(url) {
    var u = safeUrl(url);
    if (!u || !/^https:\/\//i.test(u)) return;
    if (tg && tg.openTelegramLink && /^https:\/\/t\.me\//i.test(u)) tg.openTelegramLink(u);
    else openLink(u);
  }

  /* Стартовый экран: заголовок, перечень, кнопки и «Полезное от BGINFO». Кнопки ответа сервера не рисуем. */
  function renderWelcome(w, data) {
    hideMain(); setBack(null);
    var root = screen();
    if (w.demo) root.appendChild(el('div', 'badge', 'ДЕМО-база. Тестовые карточки, не для реальных решений.'));
    root.appendChild(el('h1', null, w.title));
    root.appendChild(el('p', 'w-intro', w.intro));
    var ul = el('ul', 'w-points');
    (w.points || []).forEach(function (p) {
      var li = el('li');
      li.appendChild(el('span', 'w-icon', p.icon));
      li.appendChild(el('span', 'w-text', p.text));
      ul.appendChild(li);
    });
    root.appendChild(ul);
    (w.notes || []).forEach(function (n) { root.appendChild(el('p', 'note', n)); });
    if (w.cta) root.appendChild(el('p', 'w-cta', w.cta));

    var go = el('button', 'btn', 'Подобрать вариант');
    go.addEventListener('click', function () { btn('go'); });
    root.appendChild(go);
    var freeWrap = null;
    var fb = el('button', 'btn secondary', 'Рассказать своими словами');
    fb.addEventListener('click', function () {
      if (freeWrap) { freeWrap.remove(); freeWrap = null; return; }
      freeWrap = freeBlock();
      fb.parentNode.insertBefore(freeWrap, fb.nextSibling);
      freeWrap.querySelector('textarea').focus();
    });
    root.appendChild(fb);

    var links = (w.links || []).filter(function (l) { return l && typeof l.url === 'string' && /^https:\/\//i.test(l.url); });
    if (links.length) {
      var sec = el('div', 'w-links');
      sec.appendChild(el('h3', null, 'Полезное от BGINFO'));
      links.forEach(function (l) {
        var row = el('a', 'w-link');
        row.setAttribute('href', l.url);
        row.addEventListener('click', function (ev) { ev.preventDefault(); openExternal(l.url); });
        row.appendChild(el('span', 'w-icon', l.icon));
        row.appendChild(el('span', 'w-text', l.title));
        row.appendChild(el('span', 'w-chev', '›'));
        sec.appendChild(row);
      });
      root.appendChild(sec);
    }
  }

  /* Универсальный экран: приветствие, подтверждения, консультация, удаление и т. п. */
  function renderGeneric(data) {
    hideMain(); setBack(null);
    var root = screen();
    var replies = data.replies || [];
    replies.forEach(function (r) { root.appendChild(rich(r.html)); });
    replyButtons(root, replies);
    var hasButtons = replies.some(function (r) { return (r.buttons || []).length; });
    if (!replies.length && !hasButtons) root.appendChild(el('p', 'note', 'Нет данных для показа.'));
  }

  /* Рамка выбранного варианта классом .checked (без CSS :has — старые WebView его не знают). */
  function syncOpts(list) {
    var labs = list.querySelectorAll('.opt');
    for (var i = 0; i < labs.length; i++) {
      var inp = labs[i].querySelector('input');
      labs[i].classList.toggle('checked', !!(inp && inp.checked));
    }
  }

  function renderQuestion(q, data) {
    var root = screen();
    infoNotes(root, data);
    var multi = q.type === 'multi';
    var textual = q.type === 'money' || q.type === 'int' || q.type === 'date';
    var selected = {};
    (q.selected || []).forEach(function (v) { selected[v] = true; });

    if (q.who) root.appendChild(el('span', 'chip', q.who));
    if (q.recognized) root.appendChild(el('div', 'badge', 'Предзаполнено из вашего текста — проверьте'));
    root.appendChild(rich(q.text_html, 'question'));
    root.appendChild(el('div', 'progress', 'Ответов: ' + (q.answered || 0)));

    var help = null;
    var toggles = el('div', 'row');
    if (q.help_html) {
      help = rich(q.help_html, 'help');
      help.hidden = true;
      var hb = el('button', 'linkbtn', 'Пояснение');
      hb.addEventListener('click', function () { help.hidden = !help.hidden; });
      toggles.appendChild(hb);
    }
    if (help) root.appendChild(help);

    var read; // функция, возвращающая и валидирующая ответ → объект запроса или null
    if (textual) {
      var inp = el('input', 'field');
      if (q.type === 'date') {
        inp.type = 'month';
      } else {
        inp.type = 'text';
        inp.setAttribute('inputmode', q.type === 'money' ? 'decimal' : 'numeric');
        inp.setAttribute('autocomplete', 'off');
        inp.placeholder = q.type === 'money' ? 'Сумма в евро' : 'Число';
      }
      inp.setAttribute('aria-label', 'Ваш ответ');
      root.appendChild(inp);
      var go = function () {
        var v = inp.value.trim();
        if (!v) { toast('Введите ответ'); return null; }
        var m = /^(\d{4})-(\d{2})$/.exec(v);
        if (q.type === 'date' && m) v = m[2] + '.' + m[1];
        return { kind: 'text', text: v };
      };
      read = go;
      inp.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') { var r = go(); if (r) step('text', '', '', r.text); }
      });
    } else {
      var list = el('div', 'options');
      list.setAttribute('role', multi ? 'group' : 'radiogroup');
      var chosen = null;
      (q.options || []).forEach(function (o) {
        var lab = el('label', 'opt');
        var inp2 = el('input');
        inp2.type = multi ? 'checkbox' : 'radio';
        inp2.name = 'opt';
        inp2.value = o.value;
        if (multi) inp2.checked = !!selected[o.value];
        inp2.addEventListener('change', function () {
          if (!multi) { chosen = o.value; syncOpts(list); return; }
          if (inp2.checked) {
            if (o.value === 'none') {
              selected = { none: true };
            } else {
              delete selected.none;
              selected[o.value] = true;
            }
          } else {
            delete selected[o.value];
          }
          var boxes = list.querySelectorAll('input');
          for (var i = 0; i < boxes.length; i++) boxes[i].checked = !!selected[boxes[i].value];
          syncOpts(list);
        });
        lab.appendChild(inp2);
        lab.appendChild(el('span', null, o.label));
        list.appendChild(lab);
      });
      syncOpts(list);
      root.appendChild(list);
      read = function () {
        if (multi) {
          var vals = Object.keys(selected);
          if (!vals.length) { toast('Отметьте хотя бы один пункт или «Ничего из этого»'); return null; }
          return { kind: 'button', action: 'multi', arg: vals.join(',') };
        }
        if (chosen === null) { toast('Выберите вариант'); return null; }
        return { kind: 'button', action: 'ans', arg: chosen };
      };
    }

    var submit = function () {
      var r = read();
      if (!r) return;
      if (r.kind === 'text') step('text', '', '', r.text);
      else btn(r.action, r.arg);
    };
    setMain(multi ? 'Готово' : 'Далее', submit);

    if (q.allow_unknown) {
      var ub = el('button', 'linkbtn', 'Не знаю');
      ub.addEventListener('click', function () { btn('unk'); });
      toggles.appendChild(ub);
    }
    toggles.appendChild(freeLink(root));
    if (q.can_fix) {
      var xb = el('button', 'linkbtn', 'Изменить ответы');
      xb.addEventListener('click', function () { btn('fix'); });
      toggles.appendChild(xb);
    }
    // ссылки-переключатели идут сразу после прогресса, до пояснения и полей ответа
    root.insertBefore(toggles, help || root.querySelector('.progress').nextSibling);
    setBack(q.can_back ? function () { btn('back'); } : null);
  }

  function renderReview(rv, data) {
    var root = screen();
    infoNotes(root, data);
    root.appendChild(el('h1', null, 'Проверьте ответы'));
    (rv.lines || []).forEach(function (ln) {
      var row = el('div', 'review-line');
      var body = el('div', 'body');
      if (ln.who) body.appendChild(el('div', 'who', ln.who));
      body.appendChild(el('div', 'lbl', ln.label));
      body.appendChild(el('div', 'val', ln.value));
      row.appendChild(body);
      if (ln.index !== null && ln.index !== undefined) {
        var e = el('button', 'edit', '✏️');
        e.setAttribute('aria-label', 'Изменить: ' + ln.label);
        e.addEventListener('click', function () { btn('edit', String(ln.index)); });
        row.appendChild(e);
      }
      root.appendChild(row);
    });
    var tg1 = el('div', 'row');
    tg1.appendChild(freeLink(root));
    root.appendChild(tg1);
    setMain('Показать результат', function () { btn('review_ok'); });
    setBack(null);
  }

  /* ---------- Результат ---------- */

  function list(tag, items) {
    var l = el(tag);
    items.forEach(function (t) { l.appendChild(el('li', null, t)); });
    return l;
  }

  function section(card, title, items, ordered) {
    var arr = (items || []).filter(function (x) { return typeof x === 'string' && x; });
    if (!arr.length) return;
    card.appendChild(el('h3', null, title));
    card.appendChild(list(ordered ? 'ol' : 'ul', arr));
  }

  function linkList(card, title, links) {
    var arr = (links || []).filter(function (l) { return l && safeUrl(l.url); });
    if (!arr.length) return;
    card.appendChild(el('h3', null, title));
    var ul = el('ul');
    arr.forEach(function (l) {
      var li = el('li');
      li.appendChild(makeLink(l.url, l.title || l.url));
      ul.appendChild(li);
    });
    card.appendChild(ul);
  }

  function pairSection(card, title, pairs) {
    var arr = (pairs || []).filter(function (p) { return Array.isArray(p) ? p[0] : typeof p === 'string' && p; });
    if (!arr.length) return;
    card.appendChild(el('h3', null, title));
    var ul = el('ul');
    arr.forEach(function (p) {
      var a = Array.isArray(p) ? p[0] : p;
      var b = Array.isArray(p) ? p[1] : '';
      ul.appendChild(el('li', null, a + (b ? ' — ' + b : '')));
    });
    card.appendChild(ul);
  }

  /* Практические замечания: текст + источник (ссылка, если источник — URL, иначе подпись). */
  function notesSection(card, title, notes) {
    var arr = (notes || []).map(function (n) { return Array.isArray(n) ? n : [n, '']; })
      .filter(function (n) { return typeof n[0] === 'string' && n[0]; });
    if (!arr.length) return;
    card.appendChild(el('h3', null, title));
    var ul = el('ul');
    arr.forEach(function (n) {
      var li = el('li', null, n[0]);
      if (typeof n[1] === 'string' && n[1]) {
        li.appendChild(document.createTextNode(' '));
        if (safeUrl(n[1])) {
          li.appendChild(makeLink(n[1], n[1]));
        } else {
          li.appendChild(el('em', null, '(' + n[1] + ')'));
        }
      }
      ul.appendChild(li);
    });
    card.appendChild(ul);
  }

  /* Карточка основания. full=true повторяет present._ground_block(full=True) (+ документы и сведения
     из «Почему этот вариант?»), full=false — краткий вид: статус, название, пояснение, ссылки. */
  function groundCard(g, full) {
    var card = el('div', 'card');
    var head = el('div');
    head.style.display = 'flex'; head.style.flexDirection = 'column'; head.style.gap = '6px';
    var chip = el('span', 'chip st-' + String(g.status).replace(/[^a-z_]/g, ''), g.status_label);
    chip.style.alignSelf = 'flex-start';
    head.appendChild(chip);
    head.appendChild(el('h2', null, g.title));
    card.appendChild(head);
    if (g.personal) card.appendChild(el('p', null, g.personal));
    if (g.status === 'not_eligible') section(card, 'Почему не подходит', g.reasons);
    if (full) {
      if (g.summary) card.appendChild(el('p', null, g.summary));
      section(card, 'Что уже совпадает', g.met);
      pairSection(card, 'Что нужно уточнить или подтвердить документами', g.unknown);
      pairSection(card, 'Что нужно выполнить', g.unmet);
      if ((g.individual || []).length) {
        section(card, 'Требует индивидуальной проверки', g.individual);
        if (g.review_reason) card.appendChild(el('p', 'note', g.review_reason));
      }
      section(card, 'Важные ограничения', g.restrictions);
      section(card, 'Следующие шаги', g.steps, true);
      section(card, 'Документы', g.documents);
      notesSection(card, 'На практике', g.practice_notes);
    }
    if (g.family_note) card.appendChild(el('p', 'note', g.family_note));
    linkList(card, 'Подробнее', g.links);
    if (full) linkList(card, 'Официальные источники', g.official);
    return card;
  }

  function renderResult(res, data) {
    hideMain(); setBack(null);
    var root = screen();
    infoNotes(root, data);
    var rv = res.view;
    if (!rv || typeof rv !== 'object') {
      root.appendChild(rich(res.html));
    } else {
      root.appendChild(el('h1', null, 'Результат'));
      if (rv.demo) root.appendChild(el('div', 'badge', 'Демонстрационная версия базы знаний — не для реальных решений'));
      if (rv.kind === 'unsupported' || rv.kind === 'none') {
        if (rv.message) root.appendChild(el('p', null, rv.message));
        (rv.not_suitable || []).slice(0, 3).forEach(function (g) {
          root.appendChild(groundCard(g, false));
        });
        root.appendChild(el('p', 'note', 'Можно заказать индивидуальную консультацию — кнопка ниже.'));
      } else {
        if (rv.intro) root.appendChild(el('p', null, rv.intro));
        if (rv.primary) {
          root.appendChild(el('h2', null, 'Что стоит проверить первым'));
          root.appendChild(groundCard(rv.primary, true));
          if ((rv.primary.criteria || []).length) {
            root.appendChild(el('p', 'note', 'Почему первым: ' + rv.primary.criteria.join('; ') + '.'));
          }
        } else if (rv.kind === 'clarify') {
          if (rv.message) root.appendChild(el('p', null, rv.message));
        } else if (rv.kind === 'individual') {
          root.appendChild(el('p', null, 'Ваша ситуация требует индивидуального разбора: по части условий нельзя ' +
            'сделать уверенный вывод на основе проверенных правил.'));
        }
        if (rv.alternatives && rv.alternatives.length) {
          root.appendChild(el('h2', null, rv.primary ? 'Другие варианты' : 'Что можно проверить'));
          rv.alternatives.forEach(function (g) { root.appendChild(groundCard(g, true)); });
        }
        if (rv.family && rv.family.length) {
          root.appendChild(el('h2', null, 'Семья'));
          if (rv.family_intro) root.appendChild(el('p', 'note', rv.family_intro));
          rv.family.forEach(function (m) {
            if (m.note) {
              var c = el('div', 'card');
              c.appendChild(el('h2', null, m.label));
              c.appendChild(el('p', null, m.note));
              root.appendChild(c);
            } else if ((m.grounds || []).length) {
              m.grounds.forEach(function (g) {
                var gc = groundCard(g, false);
                gc.insertBefore(el('div', 'sub', m.label), gc.firstChild);
                root.appendChild(gc);
              });
            } else {
              var c2 = el('div', 'card');
              c2.appendChild(el('h2', null, m.label));
              c2.appendChild(el('p', null, 'Данных недостаточно — уточним при консультации.'));
              root.appendChild(c2);
            }
          });
          root.appendChild(el('p', 'note', 'Основание основного заявителя на семью автоматически не распространяется.'));
        }
        if (rv.not_suitable && rv.not_suitable.length) {
          var d = el('details');
          d.appendChild(el('summary', null, 'Не подходит по указанным данным (' + rv.not_suitable.length + ')'));
          rv.not_suitable.forEach(function (g) {
            var gc = groundCard(g, false);
            gc.style.marginTop = '10px';
            d.appendChild(gc);
          });
          root.appendChild(d);
        }
      }
      if (res.disclaimer) root.appendChild(el('p', 'note', res.disclaimer));
    }
    var actions = el('div', 'options');
    replyButtons(actions, (data.replies || []).slice(-1));
    root.appendChild(actions);
    var tg2 = el('div', 'row');
    tg2.appendChild(freeLink(root));
    root.appendChild(tg2);
  }

  /* ---------- Старт ---------- */

  function start() {
    if (!tg || !tg.initData) {
      var root = screen();
      var box = el('div', 'center');
      box.appendChild(el('h1', null, 'Откройте форму из бота @bginfosu_bot'));
      box.appendChild(el('p', null, 'Эта страница работает только внутри Telegram.'));
      root.appendChild(box);
      return;
    }
    tg.ready();
    tg.expand();
    open();
  }

  start();
})();
