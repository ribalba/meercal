/* Keyboard. The program is for people who would rather not reach for a mouse,
   so every view-level thing has a key, and the cheat sheet in the sidebar is
   generated from the same table that binds them — a shortcut cannot drift out
   of step with its own documentation. */

window.App = window.App || {};

App.keys = (() => {
  const BINDINGS = [
    { key: "r", label: "Ribbon", run: () => App.shell.setView("ribbon") },
    { key: "w", label: "Week", run: () => App.shell.setView("week") },
    { key: "m", label: "Month", run: () => App.shell.setView("month") },
    { key: "d", label: "Day", run: () => App.shell.setView("day") },
    { key: "y", label: "Year", run: () => App.shell.setView("year") },
    { key: "t", label: "Today", run: () => App.shell.today() },
    { key: "←/→", label: "Back / forward", match: (e) => e.key === "ArrowLeft" || e.key === "ArrowRight",
      run: (e) => App.shell.step(e.key === "ArrowLeft" ? -1 : 1) },
    { key: "n", label: "New event", run: () => App.editor.create() },
    { key: "/", label: "Filter", run: () => document.getElementById("filter-input").focus() },
    { key: "0–9", label: "Calendar set", match: (e) => /^[0-9]$/.test(e.key),
      run: (e) => {
        const set = App.state.sets.find((s) => s.hotkey === Number(e.key));
        if (set) App.shell.applySet(set.id);
        // 0 means everything even when no set has claimed it — the one key
        // that should always get you back to seeing the lot.
        else if (e.key === "0") App.shell.setVisible(App.state.calendars.map((c) => c.id));
      } },
    { key: "q", label: "Quiet days", run: () => {
      App.state.prefs.collapseQuiet = !App.state.prefs.collapseQuiet;
      App.load.prefs();
      App.shell.refresh();
    } },
    { key: ".", label: "Sync now", run: () => document.getElementById("btn-refresh").click() },
    { key: "?", label: "Shortcuts", run: () => document.getElementById("shortcut-box").classList.toggle("open") },
  ];

  function typing(target) {
    return target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" ||
                      target.tagName === "SELECT" || target.isContentEditable);
  }

  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape") {
      App.editor.close();
      const filter = document.getElementById("filter-input");
      if (document.activeElement === filter) filter.blur();
      return;
    }
    if (typing(e.target)) return;
    for (const binding of BINDINGS) {
      const hit = binding.match ? binding.match(e) : e.key === binding.key;
      if (hit) { e.preventDefault(); binding.run(e); return; }
    }
  }

  function cheatSheet() {
    const box = document.getElementById("shortcut-box");
    if (!box) return;
    box.replaceChildren(
      App.el("div", { class: "shortcut-title", text: "Shortcuts" }),
      ...BINDINGS.map((b) => App.el("div", { class: "shortcut-row" },
        App.el("kbd", { text: b.key }),
        App.el("span", { text: b.label }),
      )),
    );
  }

  function init() {
    document.addEventListener("keydown", onKey);
    cheatSheet();
  }

  return { init, BINDINGS };
})();
