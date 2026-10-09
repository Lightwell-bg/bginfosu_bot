// Немного ванильного JS: строки в формах, подтверждения, подсказки по фактам.
// Подключается как файл (CSP не допускает inline-скриптов).
(function () {
  "use strict";
  var counter = 0;

  function token() {
    counter += 1;
    return "n" + Date.now().toString(36) + counter;
  }

  // «+ добавить»: клонируем <template>, подставляя уникальный индекс (__I__) и префикс условия (__P__).
  function addRow(btn) {
    var tpl = document.getElementById(btn.dataset.template);
    var target = document.getElementById(btn.dataset.target);
    if (!tpl || !target) return;
    var html = tpl.innerHTML.split("__I__").join(token());
    html = html.split("__P__").join(btn.dataset.prefix || "");
    target.insertAdjacentHTML("beforeend", html);
    initRows(target);
  }

  function catalog() {
    var el = document.getElementById("fact-catalog");
    if (!el) return {};
    try { return JSON.parse(el.dataset.json); } catch (e) { return {}; }
  }

  function updateHint(select) {
    var row = select.closest(".leaf");
    if (!row) return;
    var hint = row.querySelector(".fact-hint");
    var f = catalog()[select.value];
    if (!hint) return;
    if (!f) { hint.textContent = ""; return; }
    var text = "Тип: " + f.type;
    if (f.options && f.options.length) {
      text += ". Варианты: " + f.options.map(function (o) { return o.value + " — " + o.label; }).join("; ");
    }
    if (f.type === "bool") text += ". Значение: true или false";
    hint.textContent = text;
  }

  function syncLeafType(row) {
    var sel = row.querySelector(".leaf-type");
    if (sel) row.dataset.type = sel.value;
  }

  function initRows(scope) {
    (scope || document).querySelectorAll(".leaf").forEach(syncLeafType);
    (scope || document).querySelectorAll(".fact-select").forEach(updateHint);
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!(t instanceof Element)) return;
    var add = t.closest("[data-add]");
    if (add) { ev.preventDefault(); addRow(add); return; }
    var rm = t.closest("[data-remove]");
    if (rm) {
      ev.preventDefault();
      var row = rm.closest(".row-item");
      if (row) row.remove();
    }
  });

  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (!(t instanceof Element)) return;
    if (t.classList.contains("leaf-type")) syncLeafType(t.closest(".leaf"));
    if (t.classList.contains("fact-select")) updateHint(t);
  });

  document.addEventListener("submit", function (ev) {
    var f = ev.target;
    if (f instanceof HTMLFormElement && f.dataset.confirm && !window.confirm(f.dataset.confirm)) {
      ev.preventDefault();
    }
  });

  document.addEventListener("DOMContentLoaded", function () { initRows(document); });
})();
