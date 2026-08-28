/* The shell: the sidebar of calendars, the toolbar, and which view is up.

   The sidebar is where "I have a lot of calendars" is answered. Three things
   do the work:

   * **Sets.** A named group of calendars with a number key on it. You do not
     think in calendars, you think in situations (work, family, the release)
     and switching to one is one keystroke rather than eleven tickboxes.
   * **Solo.** Alt-click (or the ⌾ button) shows one calendar and hides the
     rest, the way a layer solo works in an editor. Alt-click again to put them
     back: the previous set is remembered, so soloing is a peek and not a
     decision.
   * **Nothing is lost by hiding.** Visibility is about drawing only: search
     covers hidden calendars, and the toolbar says how many are switched off,
     because a calendar you forgot you hid is worse than one you never had. */

window.App = window.App || {};

App.shell = (() => {
  const T = () => App.time;
  const VIEWS = { ribbon: "Ribbon", week: "Week", month: "Month", day: "Day", year: "Year" };

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
      // The dot is the colour, so the dot is where the colour is changed. Its
      // own button rather than a click on the row, which already means "show
      // or hide this calendar", and wrapped so the target is a pointer's size
      // rather than the ten pixels the dot is drawn at.
      App.el("button", {
        class: "cal-paint",
        title: "Change this calendar's colour",
        onclick: (ev) => {
          ev.stopPropagation();
          App.picker.colorMenu(ev.currentTarget, cal.color, (color) => setColor(cal.id, color));
        },
      }, App.el("span", { class: "cal-dot", style: `--c:${cal.color}` })),
      App.el("span", { class: "cal-name", text: cal.name }),
      cal.error ? App.el("span", { class: "cal-warn", text: "!", title: cal.error }) : null,
      App.el("button", {
        class: "cal-solo",
        title: "Only this calendar (alt-click the row)",
        html: App.icon("solo", 14),
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
      // In key order, keyless ones last. The list then reads the way the
      // keyboard does, 0 at the top, and "which key was that" is answerable
      // by looking rather than by remembering.
      const ordered = App.state.sets.slice().sort((a, b) => {
        if ((a.hotkey === null) !== (b.hotkey === null)) return a.hotkey === null ? 1 : -1;
        if (a.hotkey !== b.hotkey) return a.hotkey - b.hotkey;
        return a.name.localeCompare(b.name);
      });
      ordered.forEach((s) => {
        const active = sameSet(s.calendars, App.state.visibleIds());
        nodes.push(App.el("div", { class: "set-row" + (active ? " active" : "") },
          App.el("button", {
            class: "set-apply",
            onclick: () => applySet(s.id),
            title: `${s.calendars.length} calendar${s.calendars.length === 1 ? "" : "s"}`,
          },
            App.el("span", { class: "set-name", text: s.name }),
            s.hotkey !== null ? App.el("kbd", { text: String(s.hotkey) }) : null,
          ),
          App.el("button", {
            class: "set-edit",
            html: App.icon("pencil", 15),
            title: "Rename it, move its key, change what is in it",
            onclick: (ev) => { ev.stopPropagation(); App.sets.open(s); },
          }),
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
      App.el("button", { class: "link", text: "New set…", onclick: () => App.sets.open(null) }),
      App.el("button", {
        class: "link", text: "Import…",
        title: "Read an .ics file in. Dropping one on the window does the same.",
        onclick: () => App.importer.choose(),
      }),
      hidden ? App.el("span", { class: "muted small", text: `${hidden} hidden` }) : null,
    ));

    tree.replaceChildren(...nodes);
  }

  /* A calendar's colour, changed here and kept. Drawn first and written
     after: the colour is the one thing in this program that is purely how it
     looks, and looking at a round trip before it changes would be absurd. */
  async function setColor(id, color) {
    const cal = App.state.calendar(id);
    if (!cal || cal.color === color) return;
    cal.color = color;
    renderSidebar();
    repaint();
    try {
      await App.api.patch(`/api/calendars/${id}`, { color });
    } catch (err) {
      alert(`Could not change the colour: ${err.message}`);
      await App.load.state();
      renderSidebar();
      repaint();
    }
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
    closeDrawer();
    const payload = await App.api.post(`/api/sets/${setId}/apply`, {});
    const wanted = new Set(payload.visible);
    App.state.calendars.forEach((c) => { c.visible = wanted.has(c.id); });
    renderSidebar();
    refresh();
  }

  /* Whether what is on screen *is* this set, so the sidebar can say which one
     you are in. Order is not meaningful, so compare as sets. */
  function sameSet(a, b) {
    if (a.length !== b.length || !a.length) return false;
    const other = new Set(b);
    return a.every((id) => other.has(id));
  }

  // --- toolbar --------------------------------------------------------------

  function title() {
    const c = App.state.cursor;
    if (App.state.view === "year") return String(c.getFullYear());
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

  /* The title, written as nodes so the month view's month can carry the same
     numbered bubble the views themselves use.

     Only the month view gets it. Every other view already names its months
     inside the view -- the Ribbon in its sticky headers, the week and day
     grids in their day labels, the year view in all twelve -- and the month
     grid is the one that names its month nowhere but here. The week's title is
     a *range*, so it would want two bubbles for one week, which is noise for a
     number the day labels below it are already showing. */
  function paintTitle() {
    const el = document.getElementById("view-title");
    const c = App.state.cursor;
    if (App.state.view !== "month") {
      el.replaceChildren(title());
      el.removeAttribute("title");
      return;
    }
    el.replaceChildren(App.monthNo(c), T().monthName(c));
    el.title = `Press g ${c.getMonth() + 1} to jump to this month`;
  }

  function paintToolbar() {
    paintTitle();
    document.querySelectorAll("[data-view]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === App.state.view);
    });
    const hidden = App.state.calendars.filter((c) => !c.visible).length;
    const note = document.getElementById("hidden-note");
    note.hidden = hidden === 0;
    note.textContent = `${hidden} calendar${hidden === 1 ? "" : "s"} hidden`;
    paintZoneNote();
  }

  /* --- "these times are in the wrong zone" ----------------------------------

     The server does all the zone arithmetic and sends wall-clock strings, which
     is what keeps a calendar from disagreeing with itself twice a year. The
     cost of that rule is that a server told the wrong zone draws a calendar
     that is *entirely plausible* and entirely wrong: every event is there, at
     the wrong time, and nothing on screen says so. It is easy to be told the
     wrong zone, too -- a container's own system zone is UTC, so `timezone =
     "system"` in Docker means UTC unless something passes the host's zone in.

     The browser is the only thing in the room that knows where the reader
     actually is, so it is the one that can notice. */

  // The same instant as a wall clock in a named zone. `sv-SE` for a sortable
  // "2026-08-25 14:00:00" rather than a locale's own idea of a date.
  function wallIn(tz, at) {
    try { return new Date(at).toLocaleString("sv-SE", { timeZone: tz }); } catch (e) { return null; }
  }

  /* How far apart two zones are at a given moment, in minutes. Both wall times
     are parsed as if they were local, so whatever this browser's own offset is
     cancels out and what is left is the gap between the two zones. */
  function zoneGap(here, there, at) {
    const a = wallIn(there, at);
    const b = wallIn(here, at);
    if (a === null || b === null) return null;
    return Math.round((new Date(a.replace(" ", "T")) - new Date(b.replace(" ", "T"))) / 60000);
  }

  function zoneMismatch() {
    let here = "";
    try { here = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* older */ }
    const there = App.state.timezone || "";
    if (!here || !there) return null;
    // Two probes, six months apart: the names differ far more often than the
    // clocks do (UTC and Etc/UTC, Berlin and Amsterdam), and a zone that agrees
    // today but keeps different daylight-saving rules is still worth saying.
    const now = zoneGap(here, there, Date.now());
    const later = zoneGap(here, there, Date.now() + 180 * 86400000);
    if (now === null || later === null) return null;
    if (now === 0 && later === 0) return null;
    return { here, there, minutes: now };
  }

  function gapPhrase(minutes) {
    if (!minutes) return "";
    const off = Math.abs(minutes);
    const hours = Math.floor(off / 60);
    const rest = off % 60;
    const size = hours ? (rest ? `${hours}h ${rest}m` : `${hours} hour${hours === 1 ? "" : "s"}`)
                       : `${rest} minutes`;
    return `, ${size} ${minutes < 0 ? "behind" : "ahead of"} yours`;
  }

  function paintZoneNote() {
    const note = document.getElementById("zone-note");
    if (!note) return;
    const bad = zoneMismatch();
    note.hidden = !bad;
    if (!bad) return;
    note.textContent = `Times in ${bad.there}`;
    note.title = `Every time here is drawn in ${bad.there}${gapPhrase(bad.minutes)}, ` +
                 `and this browser is in ${bad.here}. Set server.timezone in meercal.toml, ` +
                 `or give the container a TZ; see docker-compose.yml.`;
  }

  // --- views ----------------------------------------------------------------

  /* Renders are serialised, and a stale one is dropped rather than drawn.

     Every view fetches its range before it draws, so two quick steps (a wheel
     over a month grid, a held-down arrow) overlap. Left alone the slower of
     the two finishes last and owns the screen, which is how pressing `w` after
     a wheel could leave the month drawn over the week: the week rendered, and
     the month's older request landed on top of it.

     `showSeq` names the newest request; `inflight` is the render currently
     holding the stage. A request waits for that to finish, then checks whether
     it is still the newest before touching anything. */
  let showSeq = 0;
  let inflight = Promise.resolve();

  async function show() {
    const mine = ++showSeq;
    stage = document.getElementById("stage");
    paintToolbar();

    const previous = inflight;
    let release;
    inflight = new Promise((resolve) => { release = resolve; });
    try {
      await previous;
      if (mine !== showSeq) return;    // overtaken while queued; drop it
      // The Ribbon and the week grid both listen to the stage scrolling; take
      // the handler off before another view is drawn into the same element.
      stage.onscroll = null;
      if (App.state.view === "ribbon") await App.ribbon.show(stage);
      else if (App.state.view === "month") await App.month.show(stage);
      else if (App.state.view === "year") await App.year.show(stage);
      else await App.week.show(stage, App.state.view === "day" ? 1 : 7);
      paintToolbar();
    } finally {
      release();
    }
  }

  async function setView(view) {
    if (!VIEWS[view]) return;
    App.wheel.silence();
    App.state.view = view;
    App.load.prefs();
    await show();
  }

  /* Draw the range again from what is already loaded. A colour, a density, a
     collapsed run of quiet days: none of them change which events are in the
     window, and a fetch to redraw them would be a round trip to say nothing. */
  function repaint() {
    if (App.state.view === "ribbon") App.ribbon.render();
    else if (App.state.view === "month") App.month.render();
    else if (App.state.view === "year") App.year.render();
    else App.week.render();
    paintToolbar();
  }

  async function refresh() {
    if (!App.state.range) return show();
    await App.load.events(App.state.range.start, App.state.range.end);
    repaint();
  }

  /* One period forward or back. `fromWheel` says the wheel asked, which is the
     one caller that must not silence the wheel: its own cooldown already
     paces it, and the longer settle would make a deliberate scroll crawl. */
  function step(direction, fromWheel) {
    if (!fromWheel) App.wheel.silence();
    const c = App.state.cursor;
    if (App.state.view === "year") App.state.cursor = new Date(c.getFullYear() + direction, c.getMonth(), 1);
    else if (App.state.view === "month") App.state.cursor = T().addMonths(c, direction);
    else if (App.state.view === "day") App.state.cursor = T().addDays(c, direction);
    else if (App.state.view === "week") App.state.cursor = T().addDays(c, 7 * direction);
    else App.state.cursor = T().addDays(c, 14 * direction);
    // The continuous views scroll to it; the paged ones redraw. Same journey
    // either way, but in the Ribbon and the week grid the reader gets to see
    // themselves make it.
    if (App.state.view === "ribbon") App.ribbon.goto(App.state.cursor).then(paintToolbar);
    else if (App.state.view === "week" || App.state.view === "day") App.week.goto(App.state.cursor).then(paintToolbar);
    else show();
  }

  /* Somewhere in particular: `t`, `g 1`-`g 12`, the date picker, a click on a
     day in the year view. Silencing the wheel first is what makes `t` mean
     today even when a trackpad flick is still coasting -- without it the tail
     of a gesture that ended before the keypress stepped straight off again. */
  async function goTo(date, view) {
    App.wheel.silence();
    App.state.cursor = T().day(date);
    if (view && view !== App.state.view) return setView(view);
    if (App.state.view === "ribbon") { await App.ribbon.goto(date); paintToolbar(); }
    else if (App.state.view === "week" || App.state.view === "day") { await App.week.goto(date); paintToolbar(); }
    else await show();
  }

  function today() { closeDrawer(); return goTo(new Date()); }

  // --- the narrow-layout drawer --------------------------------------------

  function closeDrawer() {
    document.body.classList.remove("drawer-open");
    document.getElementById("scrim").hidden = true;
  }

  function toggleDrawer() {
    const open = document.body.classList.toggle("drawer-open");
    // `hidden` comes off first so the fade has something to fade.
    document.getElementById("scrim").hidden = !open;
  }

  // --- start ----------------------------------------------------------------

  async function init() {
    App.paintIcons();
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
      btn.onclick = () => { setView(btn.dataset.view); closeDrawer(); };
    });

    // The drawer. Only reachable below the breakpoint, where the sidebar sits
    // over the calendar rather than beside it, so anything that acts on the
    // calendar closes it again.
    document.getElementById("btn-menu").onclick = toggleDrawer;
    document.getElementById("scrim").onclick = closeDrawer;
    document.getElementById("btn-new-fab").onclick = () => App.editor.create();

    // Wheel-to-page, everywhere but the Ribbon, which is one continuous
    // scroll by design and has no next period to move to. Attached to the
    // stage element, which outlives every view drawn into it: attaching per
    // render handed each wheel event a fresh cooldown, and one flick walked
    // through half a year.
    App.wheel.attach(document.getElementById("stage"), (dir) => {
      // Only the month and the year are pages. The Ribbon and the week grid
      // are one continuous scroll each, so there is no "next period" for a
      // wheel to jump to -- reaching the next week *is* scrolling into it.
      if (App.state.view === "month" || App.state.view === "year") step(dir, true);
    });

    // A view that scrolls through time owns the date while it is being
    // scrolled -- the Ribbon, and now the week and day grids -- so the toolbar
    // follows it rather than the other way round.
    App.bus.on("view-position", (date) => {
      App.state.cursor = date;
      paintTitle();
    });

    await show();
    App.keys.init();
    App.search.init();
    // Window-wide: a file can be dropped anywhere, including on a view that
    // has not been drawn yet.
    App.importer.init();

    // Nothing polls or refreshes while the window is behind another one; on the
    // way back the calendar is reloaded rather than believed. See app.power.js,
    // and electron/main.js for where the focus signal comes from.
    App.power.init();
    App.power.whenSuspended(() => App.status.stop());
    App.power.whenResumed(() => { App.status.start(); refresh(); });
    App.status.start();
    // Not stood down with the rest: an in-app reminder exists precisely to
    // arrive while the window is behind something else.
    if (App.reminders) App.reminders.start();
  }

  return { init, show, setView, refresh, repaint, goTo, today, step, renderSidebar, setVisible,
           solo, applySet, closeDrawer, VIEWS };
})();

/* Agent health, in the one place it matters: a calendar that has quietly
   stopped syncing looks exactly like a calendar with nothing in it. */
App.status = {
  // Every thirty seconds while the window is in front. Held so it can be
  // stopped, which is the whole of this app's background cost.
  timer: null,

  start() {
    this.stop();
    this.timer = setInterval(() => this.poll(), 30000);
    this.poll();
  },

  stop() {
    clearInterval(this.timer);
    this.timer = null;
  },

  async poll() {
    let status;
    try { status = await App.api.get("/api/sync/status"); } catch (e) { return; }
    const stale = status.accounts.filter((a) => a.stale);
    const bar = document.getElementById("agent-warning");
    if (!bar) return;
    if (!stale.length && !status.failing) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.textContent = stale.length
      ? `${stale.map((a) => a.label).join(", ")}: not syncing`
      : `${status.failing} change(s) could not be sent`;
  },
};
