/* The filter bar. Types like meerail's search bar, on purpose.

   It is a *filter*, not a separate results page: what it narrows is the view
   you are already in, so "every dentist appointment this year" is the calendar
   with only those in it — still in date order, still with the long things in
   the rail beside them. Pressing Enter with nothing else in mind runs the same
   words as a search across every calendar, hidden ones included, because
   "which calendar was that on" is exactly what you do not remember. */

window.App = window.App || {};

App.search = (() => {
  let timer = null;

  function apply(value) {
    App.state.filter = value.trim();
    App.shell.refresh();
    document.getElementById("filter-clear").hidden = !App.state.filter;
  }

  async function fullSearch(value) {
    const params = new URLSearchParams({ q: value });
    if (App.state.regex) params.set("regex", "1");
    const payload = await App.api.get(`/api/search?${params}`);
    const panel = document.getElementById("search-results");
    if (!payload.hits.length) {
      panel.replaceChildren(App.el("div", { class: "muted small", text: "Nothing, on any calendar." }));
      panel.hidden = false;
      return;
    }
    panel.replaceChildren(...payload.hits.map((hit) => App.el("button", {
      class: "hit" + (hit.past ? " past" : ""),
      onclick: () => {
        panel.hidden = true;
        App.shell.goTo(App.time.parse(hit.start), "ribbon");
      },
    },
      App.el("span", { class: "cal-dot", style: `--c:${hit.color}` }),
      App.el("span", { class: "hit-date", text: hit.start.slice(0, 10) }),
      App.el("span", { class: "hit-title", text: hit.title }),
      App.el("span", { class: "hit-cal", text: hit.calendar }),
    )));
    panel.hidden = false;
  }

  function init() {
    const input = document.getElementById("filter-input");
    const clear = document.getElementById("filter-clear");
    const regex = document.getElementById("regex-toggle");
    const panel = document.getElementById("search-results");

    input.addEventListener("input", () => {
      clearTimeout(timer);
      panel.hidden = true;
      timer = setTimeout(() => apply(input.value), 250);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); fullSearch(input.value.trim()); }
      if (e.key === "Escape") { panel.hidden = true; }
    });
    clear.onclick = () => { input.value = ""; panel.hidden = true; apply(""); input.focus(); };
    regex.onchange = () => { App.state.regex = regex.checked; apply(input.value); };
    document.addEventListener("click", (e) => {
      if (!panel.hidden && !panel.contains(e.target) && e.target !== input) panel.hidden = true;
    });

    // The syntax, one keypress away rather than permanently on screen.
    const help = document.getElementById("filter-help-modal");
    const openHelp = () => { help.hidden = false; };
    const closeHelp = () => { help.hidden = true; };
    document.getElementById("filter-help-btn").onclick = openHelp;
    document.getElementById("filter-help-close").onclick = closeHelp;
    help.addEventListener("click", (e) => { if (e.target === help) closeHelp(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !help.hidden) closeHelp();
    });
  }

  return { init, apply };
})();
