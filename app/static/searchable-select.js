// Auto-augment any <select> that has > THRESHOLD options with a typeahead filter input.
// - Works on selects rendered initially AND those injected later (dynamic line rows).
// - Re-applies the filter when a select's options are re-populated (e.g. brand→product cascade).
// - The original <select> keeps its name/value, so form submission is unchanged.
(function () {
  const THRESHOLD = 10;
  const MARK = "data-sel-filter-applied";

  function decorate(sel) {
    if (sel.hasAttribute(MARK)) return;
    if (sel.multiple) return;
    if (sel.options.length <= THRESHOLD) return;
    sel.setAttribute(MARK, "1");

    // Wrapper so the input sits flush above the select.
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
        // Empty value (placeholder) and currently-selected option always stay visible.
        const keep =
          !opt.value ||
          opt.selected ||
          !q ||
          opt.text.toLowerCase().includes(q);
        opt.hidden = !keep;
        if (keep && opt.value && !firstVisible) firstVisible = opt;
      }
      // If the current selection is filtered out, jump to the first visible match.
      if (q && firstVisible && (sel.selectedOptions[0]?.hidden ?? false)) {
        sel.value = firstVisible.value;
      }
    }
    filter.addEventListener("input", apply);

    // Re-apply when options get swapped (e.g., brand→product cascade reassigns innerHTML).
    new MutationObserver(apply).observe(sel, { childList: true });
  }

  function scan(root) {
    (root || document).querySelectorAll("select").forEach(decorate);
  }

  document.addEventListener("DOMContentLoaded", () => {
    scan(document);
    // Watch for selects that get added later (dynamic line rows etc.).
    new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.tagName === "SELECT") decorate(n);
          else scan(n);
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  });
})();
