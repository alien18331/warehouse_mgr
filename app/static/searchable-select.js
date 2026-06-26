// Auto-augment any <select> that has > THRESHOLD options with a typeahead filter input.
// - Works on selects rendered initially AND those injected later (dynamic line rows).
// - Re-applies the filter when a select's options are re-populated (e.g. brand→product cascade).
// - Also picks up selects that START below threshold and grow past it (cascading dropdowns
//   that begin with a single "-- pick something --" placeholder).
// - The original <select> keeps its name/value, so form submission is unchanged.
(function () {
  const THRESHOLD = 10;
  const MARK = "data-sel-filter-applied";
  const WATCH = "data-sel-filter-watched";

  function decorate(sel) {
    if (sel.hasAttribute(MARK)) return;
    if (sel.multiple) return;
    if (sel.options.length <= THRESHOLD) return;
    sel.setAttribute(MARK, "1");

    const wrap = document.createElement("div");
    wrap.style.display = "flex";
    wrap.style.flexDirection = "column";
    wrap.style.gap = "2px";
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);

    const filter = document.createElement("input");
    filter.type = "text";
    filter.placeholder = "🔍 鍵入篩選…";
    filter.autocomplete = "off";
    filter.style.fontSize = "12px";
    filter.style.padding = "2px 6px";
    wrap.insertBefore(filter, sel);

    function apply() {
      const q = (filter.value || "").trim().toLowerCase();
      let firstVisible = null;
      for (const opt of sel.options) {
        const keep =
          !opt.value ||
          opt.selected ||
          !q ||
          opt.text.toLowerCase().includes(q);
        opt.hidden = !keep;
        if (keep && opt.value && !firstVisible) firstVisible = opt;
      }
      if (q && firstVisible && (sel.selectedOptions[0]?.hidden ?? false)) {
        sel.value = firstVisible.value;
      }
    }
    filter.addEventListener("input", apply);
    sel._selFilterApply = apply;
  }

  function watch(sel) {
    if (sel.hasAttribute(WATCH)) return;
    if (sel.multiple) return;
    sel.setAttribute(WATCH, "1");
    // Decorate now if eligible; otherwise wait for option count to grow.
    decorate(sel);
    new MutationObserver(() => {
      if (!sel.hasAttribute(MARK)) decorate(sel);
      if (sel._selFilterApply) sel._selFilterApply();
    }).observe(sel, { childList: true });
  }

  function scan(root) {
    (root || document).querySelectorAll("select").forEach(watch);
  }

  document.addEventListener("DOMContentLoaded", () => {
    scan(document);
    new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.tagName === "SELECT") watch(n);
          else scan(n);
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  });
})();
