/* The shell: the sidebar of calendars, the toolbar, and which view is up.

   The sidebar is where "I have a lot of calendars" is answered. Three things
   do the work:

   * **Sets.** A named group of calendars with a number key on it. You do not
     think in calendars, you think in situations — work, family, the release —
     and switching to one is one keystroke rather than eleven tickboxes.
   * **Solo.** Alt-click (or the ⌾ button) shows one calendar and hides the
     rest, the way a layer solo works in an editor. Alt-click again to put them
     back — the previous set is remembered, so soloing is a peek and not a
     decision.
   * **Nothing is lost by hiding.** Visibility is about drawing only: search
     covers hidden calendars, and the toolbar says how many are switched off,
     because a calendar you forgot you hid is worse than one you never had. */

window.App = window.App || {};

App.shell = (() => {
  const T = () => App.time;
  const VIEWS = { ribbon: "Ribbon", week: "Week", month: "Month", day: "Day" };

  let stage = null;
  let lastVisible = null;   // what to restore when a solo is undone

  // --- sidebar --------------------------------------------------------------

  function calendarRow(cal) {
    const row = App.el("div", {
      class: "cal-row" + (cal.visible ? "" : " off"),
      title: cal.error || cal.name,
      onclick: (ev) => {
        if (ev.altKey) { solo(cal.id); return; }
        toggle(cal.id, !cal.visible);
      },
    },
      App.el("span", { class: "cal-dot", style: `--c:${cal.color}` }),
      App.el("span", { class: "cal-name", text: cal.name }),
      cal.error ? App.el("span", { class: "cal-warn", text: "!", title: cal.error }) : null,
      App.el("button", {
        class: "cal-solo",
        title: "Only this calendar (alt-click the row)",
        text: "◎",
        onclick: (ev) => { ev.stopPropagation(); solo(cal.id); },
      }),
    );
    return row;
  }

  function renderSidebar() {
    const tree = document.getElementById("cal-tree");
    if (!tree) return;
    const nodes = [];

    if (App.state.sets.length) {
      nodes.push(App.el("div", { class: "tree-section", text: "Sets" }));
      App.state.sets.forEach((s) => {
        nodes.push(App.el("button", {
          class: "set-row",
          onclick: () => applySet(s.id),
          title: `${s.calendars.length} calendars`,
        },
          App.el("span", { class: "set-name", text: s.name }),
          s.hotkey ? App.el("kbd", { text: String(s.hotkey) }) : null,
        ));
      });
    }

    App.state.accounts.forEach((account) => {
      const cals = App.state.calendars.filter((c) => c.account_id === account.id);
      if (!cals.length) return;
      nodes.push(App.el("div", { class: "tree-section" },
        App.el("span", { text: account.label }),
        account.error ? App.el("span", { class: "cal-warn", text: "!", title: account.error }) : null,
      ));
      cals.forEach((c) => nodes.push(calendarRow(c)));
    });

    const hidden = App.state.calendars.filter((c) => !c.visible).length;
    nodes.push(App.el("div", { class: "cal-actions" },
      App.el("button", { class: "link", text: "All", onclick: () => setVisible(App.state.calendars.map((c) => c.id)) }),
      App.el("button", { class: "link", text: "None", onclick: () => setVisible([]) }),
      App.el("button", { class: "link", text: "Save as set…", onclick: saveSet }),
      hidden ? App.el("span", { class: "muted small", text: `${hidden} hidden` }) : null,
    ));

    tree.replaceChildren(...nodes);
  }

  async function toggle(id, visible) {
    const cal = App.state.calendar(id);
    if (cal) cal.visible = visible;
    renderSidebar();
    await App.api.patch(`/api/calendars/${id}`, { visible });
    refresh();
  }

  async function setVisible(ids) {
    const wanted = new Set(ids);
    App.state.calendars.forEach((c) => { c.visible = wanted.has(c.id); });
    renderSidebar();
    await App.api.post("/api/calendars/visibility", { visible: [...wanted] });
    refresh();
  }

  function solo(id) {
    const only = App.state.visibleIds();
    if (only.length === 1 && only[0] === id && lastVisible) {
      const restore = lastVisible;
      lastVisible = null;
      setVisible(restore);
      return;
    }
    lastVisible = only;
    setVisible([id]);
  }

  async function applySet(setId) {
    const payload = await App.api.post(`/api/sets/${setId}/apply`, {});
    const wanted = new Set(payload.visible);
    App.state.calendars.forEach((c) => { c.visible = wanted.has(c.id); });
    renderSidebar();
    refresh();
  }

  async function saveSet() {
    const name = prompt("Name this set of calendars");
    if (!name) return;
    const hotkeyRaw = prompt("A number key for it (1-9), or leave empty", String(App.state.sets.length + 1));
    const hotkey = hotkeyRaw && /^[1-9]$/.test(hotkeyRaw.trim()) ? Number(hotkeyRaw.trim()) : null;
    await App.api.post("/api/sets", { name, hotkey, calendars: App.state.visibleIds() });
    await App.load.state();
    renderSidebar();
  }

  // --- toolbar --------------------------------------------------------------

  function title() {
    const c = App.state.cursor;
    if (App.state.view === "week") {
      const start = T().startOfWeek(c, App.state.weekStart);
      const end = T().addDays(start, 6);
      const w = T().isoWeek(start);
      return `${start.getDate()} ${start.toLocaleDateString(undefined, { month: "short" })} – ` +
             `${end.getDate()} ${end.toLocaleDateString(undefined, { month: "short" })} · ${w.year}-W${String(w.week).padStart(2, "0")}`;
    }
    if (App.state.view === "day") return c.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    return T().monthName(c);
  }

  function paintToolbar() {
    document.getElementById("view-title").textContent = title();
    document.querySelectorAll("[data-view]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === App.state.view);
    });
    const hidden = App.state.calendars.filter((c) => !c.visible).length;
    const note = document.getElementById("hidden-note");
    note.hidden = hidden === 0;
    note.textContent = `${hidden} calendar${hidden === 1 ? "" : "s"} hidden`;
  }

  // --- views ----------------------------------------------------------------

  async function show() {
    stage = document.getElementById("stage");
    paintToolbar();
    if (App.state.view === "ribbon") await App.ribbon.show(stage);
    else if (App.state.view === "month") await App.month.show(stage);
    else await App.week.show(stage, App.state.view === "day" ? 1 : 7);
  }

  async function setView(view) {
    if (!VIEWS[view]) return;
    App.state.view = view;
    App.load.prefs();
    await show();
  }

  async function refresh() {
    if (!App.state.range) return show();
    await App.load.events(App.state.range.start, App.state.range.end);
    if (App.state.view === "ribbon") App.ribbon.render();
    else if (App.state.view === "month") App.month.render();
    else App.week.render();
    paintToolbar();
  }

  function step(direction) {
    const c = App.state.cursor;
    if (App.state.view === "month") App.state.cursor = T().addMonths(c, direction);
    else if (App.state.view === "day") App.state.cursor = T().addDays(c, direction);
    else if (App.state.view === "week") App.state.cursor = T().addDays(c, 7 * direction);
    else App.state.cursor = T().addDays(c, 14 * direction);
    if (App.state.view === "ribbon") App.ribbon.goto(App.state.cursor).then(paintToolbar);
    else show();
  }

  async function goTo(date, view) {
    App.state.cursor = T().day(date);
    if (view && view !== App.state.view) return setView(view);
    if (App.state.view === "ribbon") { await App.ribbon.goto(date); paintToolbar(); }
    else await show();
  }

  function today() { return goTo(new Date()); }

  // --- start ----------------------------------------------------------------

  async function init() {
    await App.load.state();
    renderSidebar();

    document.getElementById("btn-today").onclick = today;
    document.getElementById("btn-prev").onclick = () => step(-1);
    document.getElementById("btn-next").onclick = () => step(1);
    document.getElementById("btn-new").onclick = () => App.editor.create();
    document.getElementById("btn-theme").onclick = () => App.theme.cycle();
    document.getElementById("btn-refresh").onclick = async (ev) => {
      ev.currentTarget.classList.add("spin");
      await App.api.post("/api/sync/now", {});
      setTimeout(async () => { await refresh(); ev.currentTarget.classList.remove("spin"); }, 1500);
    };
    document.querySelectorAll("[data-view]").forEach((btn) => {
      btn.onclick = () => setView(btn.dataset.view);
    });

    // The ribbon owns the date while it is scrolled, so the toolbar follows it
    // rather than the other way round.
    App.bus.on("ribbon-position", (date) => {
      App.state.cursor = date;
      document.getElementById("view-title").textContent = title();
    });

    await show();
    App.keys.init();
    App.search.init();
    setInterval(() => App.status.poll(), 30000);
    App.status.poll();
  }

  return { init, show, setView, refresh, goTo, today, step, renderSidebar, setVisible, solo, applySet, VIEWS };
})();

/* Agent health, in the one place it matters: a calendar that has quietly
   stopped syncing looks exactly like a calendar with nothing in it. */
App.status = {
  async poll() {
    let status;
    try { status = await App.api.get("/api/sync/status"); } catch (e) { return; }
    const stale = status.accounts.filter((a) => a.stale);
    const bar = document.getElementById("agent-warning");
    if (!bar) return;
    if (!stale.length && !status.failing) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.textContent = stale.length
      ? `${stale.map((a) => a.label).join(", ")} — not syncing`
      : `${status.failing} change(s) could not be sent`;
  },
};
