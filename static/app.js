// Small helpers: confirm forms marked with data-confirm, auto-submit filters marked with data-autosubmit,
// and show chart tooltips for elements with data-tip-value / data-tip-label (on hover and keyboard focus).
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.dataset.confirm)) event.preventDefault();
      });
    });

    document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
      select.addEventListener("change", function () { select.form.submit(); });
    });

    var tip = document.querySelector(".viz-tip");
    if (!tip) return;
    var value = tip.querySelector("strong");
    var label = tip.querySelector("span");

    function show(target, x, y) {
      // Labels come from user data: set them as text, never as HTML.
      value.textContent = target.dataset.tipValue || "";
      label.textContent = target.dataset.tipLabel || "";
      tip.hidden = false;
      var box = tip.getBoundingClientRect();
      var left = Math.min(x + 14, window.innerWidth - box.width - 8);
      var top = y - box.height - 12 < 8 ? y + 18 : y - box.height - 12;
      tip.style.left = Math.max(8, left) + "px";
      tip.style.top = top + "px";
    }

    function hide() { tip.hidden = true; }

    document.querySelectorAll("[data-tip-value]").forEach(function (el) {
      el.addEventListener("pointermove", function (event) { show(el, event.clientX, event.clientY); });
      el.addEventListener("pointerleave", hide);
      el.addEventListener("focus", function () {
        var r = el.getBoundingClientRect();
        show(el, r.left + r.width / 2, r.top);
      });
      el.addEventListener("blur", hide);
    });
  });
})();
