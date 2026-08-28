/* Week and day: the familiar grid, kept because it is genuinely the right
   drawing for "what is my Tuesday like".

   The long events live in a strip above the grid rather than inside it, and
   the strip draws them *continuously across the week* with an arrow where they
   run past its edge. That is the one thing the usual all-day row gets wrong:
   it re-states the same event as a fresh bar every week, so a fortnight-long
   trip looks like two unrelated things.

   **It is one scroll, not a stack of pages.** Consecutive weeks are drawn one
   under the other in a single scroller, so eleven at night on Sunday is
   followed by midnight on Monday, the way it is followed in life. Each week's
   day labels are `position: sticky`, so the week you are reading names itself
   at the top of the screen and is pushed up by the next week's labels as you
   scroll into it. Nothing pages, nothing swaps under a header that stayed
   still: the date at the top of the toolbar changes because you scrolled
   there. The loaded window slides as you go, the same way the Ribbon's does.

   `day_start`..`day_end` from meercal.toml are the hours you keep, and the grid
   greys the rest of the day rather than dropping it: the working day is what
   the eye lands on, and the six o'clock flight is still drawn, still in its own
   place, still draggable. Hiding those hours would have been the other reading
   of the setting, and the wrong one -- a calendar that leaves things out is a
   calendar you have to remember to check.

   The grid is also the one view you can *draw on*: dragging empty space writes
   a new event over the hours you swept, dragging an event moves it, and
   dragging either end of one changes just that end. A time grid already draws
   time as a distance, so the mouse saying "from here to here" is the shortest
   sentence available; the panel is still there for everything that is not a
   time. See the drag section below for what each gesture costs. */

window.App = window.App || {};

App.week = (() => {
  const T = () => App.time;
  const MIN_HEIGHT = 18;       // a 15-minute meeting still has to be readable
  const PX_PER_HOUR = 44;
  const SNAP = 15;             // minutes: the grid people actually keep diaries on
  const SLOP = 4;              // px of movement before a press is a drag, not a click
  const EDGE = 30;             // px from the end of the scroller where a drag scrolls it

  /* How much is drawn either side of the week you are looking at, in periods:
     a period is a week here and a day in the day view, and either way it is a
     screenful and then some, so counting them in days would mean two sets of
     numbers that had to agree. The window slides rather than grows, for the
     same reason the Ribbon's does: a reader only ever looks at one screen. */
  const BACK = 2;
  const FORWARD = 5;
  const SLIDE = 2;
  const TOTAL = BACK + FORWARD + 1;

  /* The hours outside `day_start`..`day_end`, in minutes from midnight. Read
     per render rather than cached: the settings arrive after the first paint,
     and a grid that greyed the wrong half until something else redrew it would
     be worse than one that greyed nothing. Clamped, because a config with the
     end before the start is still a config somebody has to look at. */
  const offStart = () => Math.min(Math.max(App.state.dayStart, 0), 23) * 60;
  const offEnd = () => Math.min(Math.max(App.state.dayEnd, App.state.dayStart + 1), 24) * 60;

  let root = null;          // the scrolling container: #stage itself
  let days = 7;             // columns in a period
  let periods = [];         // {start, node, head, body, cols, top, bodyTop}
  let extending = false;
  /* Where in a period the view is parked: pixels from that period's midnight
     to the first hour actually visible, which is *under* the sticky header
     rather than under the top of the window. Measuring it from the visible
     edge is what lets it be carried to another week whose all-day strip is a
     different height and still land on the same hour. Kept across renders, so
     a re-render, a colour change or an arrow key leaves you looking at the
     same time of day rather than back at eight in the morning. */
  let parked = null;
  // Until when a scroll is ours rather than the reader's. Programmatic scrolls
  // fire the same event, and acting on them is what turns one jump into a loop
  // of window slides.
  let suppressUntil = 0;

  /* Overlapping events into side-by-side columns. The classic sweep: a cluster
     is a run of events that touch, and within it each takes the first column
     free at its start. */
  function layout(events) {
    const sorted = events.slice().sort((a, b) => a.startAt - b.startAt || b.endAt - a.endAt);
    let cluster = [];
    let clusterEnd = null;
    const out = [];
    const flush = () => {
      const columns = [];
      cluster.forEach((e) => {
        let col = columns.findIndex((end) => end <= e.startAt);
        if (col === -1) { col = columns.length; columns.push(null); }
        columns[col] = e.endAt;
        e.col = col;
      });
      cluster.forEach((e) => { e.cols = columns.length; });
      out.push(...cluster);
      cluster = [];
      clusterEnd = null;
    };
    sorted.forEach((e) => {
      if (clusterEnd && e.startAt >= clusterEnd) flush();
      cluster.push(e);
      clusterEnd = clusterEnd ? new Date(Math.max(clusterEnd, e.endAt)) : e.endAt;
    });
    if (cluster.length) flush();
    return out;
  }

  function eventNode(e, startOfDay) {
    const cal = App.state.calendar(e.cal) || { color: "#888", name: "" };
    const top = ((e.startAt - startOfDay) / 3600000) * PX_PER_HOUR;
    const height = Math.max(((e.endAt - e.startAt) / 3600000) * PX_PER_HOUR, MIN_HEIGHT);
    const width = 100 / e.cols;
    return App.el(
      "button",
      {
        class: "wk-event" + (height < 30 ? " short" : "") + (e.free ? " free" : "") +
               (e.status === "CANCELLED" ? " cancelled" : "") +
               // The mark is drawn in the corner rather than in the flow: this
               // block is a column at full height and a row when it is too
               // short for two lines, and an inline item lands on a line of
               // its own in the first and in the middle of the title in the
               // second. The class is what buys it the room, so the title
               // ellipsises before the corner instead of underneath it.
               (App.guests.on(e) ? " has-guests" : ""),
        style: `--c:${cal.color};--c-weak:${App.tint(cal.color, 0.16)};` +
               `top:${top}px;height:${height}px;left:${e.col * width}%;width:calc(${width}% - 3px)`,
        title: `${e.title} · ${cal.name}`,
        dataset: { id: String(e.id) },
        onclick: (ev) => { ev.stopPropagation(); App.editor.open(e, ev.currentTarget); },
      },
      App.el("span", { class: "wk-time", text: T().time(e.startAt) }),
      App.el("span", { class: "wk-title", text: e.title }),
      App.guests.mark(e, "wk-guests", 11),
      // The two ends, as targets of their own. A few pixels each, and only on
      // events this calendar will actually let you change: a grip on a
      // read-only event is an invitation to an error message.
      e.read_only ? null : App.el("div", { class: "wk-grip top" }),
      e.read_only ? null : App.el("div", { class: "wk-grip bottom" }),
    );
  }

  /* --- drawing on the grid --------------------------------------------------

     Three gestures, told apart by where the pointer went down: empty grid
     draws a new event, the body of an event moves it, and the grips at either
     end change that end alone. All three snap to the quarter hour, which is
     the resolution a diary is kept at and, not by accident, about the smallest
     block this grid can draw legibly.

     A press that never moves is left alone, so the click that opens an event
     and the double-click that creates one both still work. A drag that does
     move suppresses the click it would otherwise end with -- the button under
     the pointer would otherwise open the panel for the event just dropped.

     Moving and resizing manipulate the real node rather than a ghost: the
     event *is* the thing being dragged, and drawing a second one beside it
     would say there are two. Only the new-event gesture gets a ghost, because
     until it is dropped there is nothing else to move. */

  let byId = new Map();     // occurrence id -> the prepared event drawn for it
  let live = null;          // the gesture in flight, if any

  const snap = (m) => Math.round(m / SNAP) * SNAP;
  const minuteOf = (d) => d.getHours() * 60 + d.getMinutes();
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  const atMinute = (day, minutes) =>
    new Date(day.getFullYear(), day.getMonth(), day.getDate(), 0, minutes);

  /* Where a pointer is, in days and minutes. Down the stack first and then
     across it, because with the weeks drawn one under the other there are
     several Tuesdays on the page and only the vertical position tells them
     apart. The *nearest* of each rather than the one under the pointer: a drag
     that wanders into the hour gutter or off the right-hand edge still means
     the day it started over, and stopping dead at the edge of a column would
     read as the grid having lost the drag. A drag that wanders past the bottom
     of its week lands in the next one, which is how an event moves a week. */
  function slotAt(clientX, clientY) {
    if (!periods.length) return null;
    let period = periods[0];
    let best = Infinity;
    periods.forEach((p) => {
      const r = p.body.getBoundingClientRect();
      const away = clientY < r.top ? r.top - clientY : Math.max(clientY - r.bottom, 0);
      if (away < best) { best = away; period = p; }
    });
    let hit = period.cols[0];
    best = Infinity;
    period.cols.forEach((c) => {
      const r = c.node.getBoundingClientRect();
      const away = clientX < r.left ? r.left - clientX : Math.max(clientX - r.right, 0);
      if (away < best) { best = away; hit = c; }
    });
    const r = hit.node.getBoundingClientRect();
    return {
      date: hit.date,
      node: hit.node,
      minutes: clamp(((clientY - r.top) / PX_PER_HOUR) * 60, 0, 24 * 60),
    };
  }

  function ghost() {
    return App.el("div", { class: "wk-ghost" }, App.el("span", { class: "wk-ghost-time" }));
  }

  /* Put a box on the grid, in one column, from one minute to another. Used for
     the new-event ghost and for the event being dragged, which is why it takes
     a node rather than making one. */
  function place(node, colNode, from, to, caption) {
    if (node.parentElement !== colNode) colNode.append(node);
    node.style.top = `${(from / 60) * PX_PER_HOUR}px`;
    node.style.height = `${Math.max(((to - from) / 60) * PX_PER_HOUR, MIN_HEIGHT)}px`;
    const text = node.querySelector(".wk-ghost-time, .wk-time");
    if (text) text.textContent = caption;
  }

  function label(from, to) {
    const clock = (m) => `${Math.floor(m / 60) % 24}:${String(m % 60).padStart(2, "0")}`;
    const hours = Math.floor((to - from) / 60);
    const rest = (to - from) % 60;
    const length = hours ? (rest ? `${hours}h ${rest}m` : `${hours}h`) : `${rest}m`;
    return `${clock(from)}–${clock(to)} · ${length}`;
  }

  function paint(x, y) {
    const slot = slotAt(x, y);
    if (!slot) return;
    const g = live;
    if (g.kind === "create") {
      const now = snap(slot.minutes);
      g.from = Math.min(now, g.anchor);
      g.to = Math.max(now, g.anchor);
      if (g.to === g.from) g.to = g.from + SNAP;
      g.day = g.anchorDay;              // a new event stays in the day it started in
      place(g.node, g.anchorNode, g.from, g.to, label(g.from, g.to));
      return;
    }
    const length = g.endMin - g.startMin;
    if (g.kind === "move") {
      const shifted = snap(g.startMin + (snap(slot.minutes) - g.grabbed));
      g.from = clamp(shifted, 0, 24 * 60 - length);
      g.to = g.from + length;
      g.day = slot.date;                // sideways is a different day, which is the point
      place(g.node, slot.node, g.from, g.to, T().time(atMinute(g.day, g.from)));
    } else if (g.kind === "start") {
      g.from = clamp(snap(slot.minutes), 0, g.endMin - SNAP);
      g.to = g.endMin;
      place(g.node, g.colNode, g.from, g.to, T().time(atMinute(g.day, g.from)));
    } else {
      g.from = g.startMin;
      g.to = clamp(snap(slot.minutes), g.startMin + SNAP, 24 * 60);
      place(g.node, g.colNode, g.from, g.to, T().time(atMinute(g.day, g.from)));
    }
    g.node.title = label(g.from, g.to);
  }

  /* The scroller follows a drag that reaches its edge, so that an eight
     o'clock meeting can be dragged to six in the evening without letting go.
     A frame loop rather than the move events: the pointer held still at the
     edge is exactly the case that has to keep scrolling. */
  function tick() {
    if (!live) return;
    if (live.scrollBy && root) {
      const before = root.scrollTop;
      root.scrollTop += live.scrollBy;
      if (root.scrollTop !== before) paint(live.x, live.y);
    }
    live.raf = requestAnimationFrame(tick);
  }

  function onDown(ev) {
    if (live || ev.button !== 0 || ev.ctrlKey) return;
    const slot = slotAt(ev.clientX, ev.clientY);
    if (!slot) return;
    const node = ev.target.closest(".wk-event");
    const grip = ev.target.closest(".wk-grip");
    if (node) {
      const e = byId.get(node.dataset.id);
      if (!e || e.read_only) return;
      const startMin = minuteOf(e.startAt);
      live = {
        kind: grip ? (grip.classList.contains("top") ? "start" : "end") : "move",
        node, e, startMin,
        endMin: startMin + Math.round((e.endAt - e.startAt) / 60000),
        day: T().day(e.startAt),
        colNode: node.parentElement,
        grabbed: snap(slot.minutes),
      };
    } else {
      live = { kind: "create", node: ghost(), anchor: snap(slot.minutes),
               anchorDay: slot.date, anchorNode: slot.node };
    }
    Object.assign(live, { moved: false, x: ev.clientX, y: ev.clientY, x0: ev.clientX, y0: ev.clientY });
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("keydown", onDragKey, true);
  }

  function onMove(ev) {
    const g = live;
    if (!g) return;
    g.x = ev.clientX;
    g.y = ev.clientY;
    if (!g.moved) {
      if (Math.abs(ev.clientX - g.x0) < SLOP && Math.abs(ev.clientY - g.y0) < SLOP) return;
      g.moved = true;
      document.body.classList.add("dragging-time");
      if (g.kind !== "create") {
        g.node.classList.add("wk-dragging");
        // Out of the side-by-side column it was packed into: while it is in the
        // air it is the only event in that day that this drag is about, and a
        // half-width box jumping between lanes is the thing that reads as a bug.
        g.node.style.left = "0";
        g.node.style.width = "calc(100% - 3px)";
      }
      g.raf = requestAnimationFrame(tick);
    }
    // The text selection a drag across a grid would otherwise leave behind.
    ev.preventDefault();
    if (root) {
      const r = root.getBoundingClientRect();
      g.scrollBy = ev.clientY < r.top + EDGE ? -14 : ev.clientY > r.bottom - EDGE ? 14 : 0;
    }
    paint(ev.clientX, ev.clientY);
  }

  function unbind() {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", cancel);
    window.removeEventListener("keydown", onDragKey, true);
    document.body.classList.remove("dragging-time");
    if (live && live.raf) cancelAnimationFrame(live.raf);
  }

  function cancel() {
    if (!live) return;
    const moved = live.moved;
    unbind();
    live = null;
    // Put everything back exactly as the data says it is.
    if (moved) render();
  }

  function onDragKey(ev) {
    if (ev.key !== "Escape" || !live) return;
    ev.stopPropagation();     // Escape here means this drag, not the drawer or the panel
    ev.preventDefault();
    cancel();
  }

  function onUp() {
    const g = live;
    if (!g) return;
    unbind();
    live = null;
    // A press that never moved is a click, and the click is somebody else's:
    // an event opens its panel, and the empty grid is waiting for the second
    // half of a double-click.
    if (!g.moved) { if (g.kind === "create") g.node.remove(); return; }
    // The click this drag is about to fire lands on whatever is under the
    // pointer, which after a move is the event that was just dropped.
    window.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); },
                            { capture: true, once: true });
    if (g.kind === "create") {
      g.node.remove();
      App.editor.create(atMinute(g.anchorDay, g.from), atMinute(g.anchorDay, g.to));
      return;
    }
    commit(g);
  }

  /* The new times, to the server. The event is redrawn where it was dropped
     first and asked about afterwards, because a drag that snapped back for a
     third of a second while a round trip happened would read as a drag that
     failed. `refresh` at the end is what makes it true rather than hopeful:
     if the write was refused, the next paint says so by putting it back. */
  async function commit(g) {
    const e = g.e;
    const start = atMinute(g.day, g.from);
    const end = atMinute(g.day, g.to);
    if (Number(start) === Number(e.startAt) && Number(end) === Number(e.endAt)) return render();
    if (e.recurring &&
        !confirm(`"${e.title}" repeats: this changes every occurrence.`)) return render();

    const row = App.state.events.find((x) => x.id === e.id);
    if (row) { row.start = T().iso(start); row.end = T().iso(end); }
    render();
    try {
      // The whole record, not the two fields that changed: PATCH replaces what
      // it is given, and the summary this view was drawn from has neither the
      // notes nor the guest list on it.
      const detail = await App.api.get(`/api/events/${e.event_id}`);
      await App.api.patch(`/api/events/${e.event_id}`, {
        calendar_id: detail.cal || e.cal,
        title: detail.title || e.title,
        start: T().iso(start),
        end: T().iso(end),
        all_day: false,
        location: detail.location || "",
        description: detail.description || "",
        rrule: detail.rrule || "",
        attendees: detail.attendees || [],
      });
    } catch (err) {
      alert(`Could not move "${e.title}": ${err.message}`);
    }
    App.shell.refresh();
  }

  // --- drawing ---------------------------------------------------------------

  const periodStart = (date) =>
    (days === 1 ? T().day(date) : T().startOfWeek(date, App.state.weekStart));

  /* Every event in the loaded window, parsed once and filed by the day it
     starts on. Once per render rather than once per column: with eight weeks
     on the page a filter per column is fifty-six passes over the same list. */
  function prepare() {
    const prepared = App.state.events.map((e) => Object.assign({}, e, {
      startAt: T().parse(e.start),
      endAt: T().parse(e.end),
    }));
    const byDay = new Map();
    const bars = [];
    prepared.forEach((e) => {
      e.startDay = T().day(e.startAt);
      e.endDay = T().day(new Date(Math.max(e.endAt - 1, e.startAt.getTime())));
      e.days = T().daysBetween(e.startDay, e.endDay) + 1;
      if (e.all_day || e.days > 1) { bars.push(e); return; }
      const key = T().ymd(e.startDay);
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(e);
    });
    bars.sort((a, b) => b.days - a.days || a.startAt - b.startAt);
    return { byDay, bars };
  }

  /* One period: a week (or a day), drawn as its own block in the stack. The
     labels and the all-day strip are one sticky unit at the top of it, so they
     name the week for as long as the week is on screen and are pushed off by
     the next week's rather than being replaced under the reader. */
  function periodBlock(start, { byDay, bars }) {
    const cols = [];
    for (let i = 0; i < days; i++) cols.push(T().addDays(start, i));

    const header = App.el("div", { class: "wk-head" },
      App.el("div", { class: "wk-corner", text: `W${T().isoWeek(start).week}` }),
      cols.map((d) => App.el("div", { class: "wk-daylabel" + (T().isToday(d) ? " today" : "") },
        App.el("span", { class: "wk-dow", text: T().weekday(d) }),
        App.el("span", { class: "wk-dnum", text: String(d.getDate()) }),
        // Which month, on the day the month turns and on the first day of the
        // block. With weeks running on without a break, "1" on its own stops
        // being enough to say where in the year you have scrolled to.
        d.getDate() === 1 || T().ymd(d) === T().ymd(start)
          ? App.el("span",
              { class: "wk-mon", title: `Press g ${d.getMonth() + 1} to jump to this month` },
              App.monthNo(d, "sm"),
              App.el("span", { text: T().monthShort(d) }))
          : null,
      )),
    );

    // --- the strip: all-day and multi-day, drawn across the columns ---
    const strip = App.el("div", { class: "wk-strip" }, App.el("div", { class: "wk-striplabel", text: "all day" }));
    const grid = App.el("div", { class: "wk-stripgrid" });
    const laneEnds = [];
    bars.forEach((e) => {
      const from = Math.max(T().daysBetween(start, e.startDay), 0);
      const to = Math.min(T().daysBetween(start, e.endDay), days - 1);
      if (to < 0 || from > days - 1) return;
      let lane = laneEnds.findIndex((end) => end <= from);
      if (lane === -1) { lane = laneEnds.length; laneEnds.push(null); }
      laneEnds[lane] = to + 1;
      const cal = App.state.calendar(e.cal) || { color: "#888" };
      grid.append(App.el("button", {
        class: "wk-bar" + (e.startDay < start ? " open-left" : "") +
               (T().daysBetween(start, e.endDay) > days - 1 ? " open-right" : "") + (e.free ? " free" : ""),
        style: `--c:${cal.color};--c-weak:${App.tint(cal.color, 0.2)};` +
               `grid-column:${from + 1} / ${to + 2};grid-row:${lane + 1}`,
        title: `${e.title} · ${e.days} days`,
        onclick: (ev) => App.editor.open(e, ev.currentTarget),
      }, App.guests.mark(e, "wk-bar-guests", 11), App.el("span", { text: e.title })));
    });
    strip.append(grid);

    // --- the time grid ---
    const dayFrom = offStart();
    const dayTo = offEnd();
    const body = App.el("div", { class: "wk-body" });
    // The labels outside the working day are dimmed with the hours they name,
    // so the gutter and the grid say the same thing.
    const hours = App.el("div", { class: "wk-hours" });
    for (let h = 0; h < 24; h++) {
      const off = h * 60 < dayFrom || h * 60 >= dayTo;
      hours.append(App.el("div", { class: "wk-hour" + (off ? " off" : ""), style: `height:${PX_PER_HOUR}px` },
        App.el("span", { text: `${h}:00` })));
    }
    body.append(hours);

    /* The hours outside the working day, as a wash over the column. One
       definition for the whole period rather than one per column, the way the
       events are filed once per render: seven columns a week and eight weeks on
       the page is fifty-six of everything that is written inside this loop. */
    const shade = (from, to) => (to <= from ? null : App.el("div", { class: "wk-off",
      style: `top:${(from / 60) * PX_PER_HOUR}px;height:${((to - from) / 60) * PX_PER_HOUR}px` }));

    const colNodes = [];
    cols.forEach((d) => {
      const column = App.el("div", { class: "wk-col" + (T().isToday(d) ? " today" : ""),
        style: `height:${24 * PX_PER_HOUR}px`,
        // Still here beside the drag: a double-click is the shortest way to
        // say "something at about four", when the length is the panel's
        // business. Measured against the column rather than from `offsetY`,
        // which is relative to whatever was under the pointer -- and an hour
        // rule is one pixel tall, so landing on one put the event at midnight.
        ondblclick: (ev) => {
          const r = column.getBoundingClientRect();
          const minutes = Math.round((((ev.clientY - r.top) / PX_PER_HOUR) * 60) / 30) * 30;
          App.editor.create(new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, minutes));
        },
      });
      // Before the rules and the events, so both stay on top of it: an event
      // drawn through the shade would be an event this grid had an opinion
      // about, and it has none -- six in the morning is just early.
      column.append(shade(0, dayFrom), shade(dayTo, 24 * 60));
      for (let h = 1; h < 24; h++) {
        column.append(App.el("div", { class: "wk-line", style: `top:${h * PX_PER_HOUR}px` }));
      }
      layout(byDay.get(T().ymd(d)) || []).forEach((e) => {
        byId.set(String(e.id), e);
        column.append(eventNode(e, d));
      });
      if (T().isToday(d)) {
        const now = new Date();
        const top = ((now.getHours() * 60 + now.getMinutes()) / 60) * PX_PER_HOUR;
        column.append(App.el("div", { class: "wk-now", style: `top:${top}px` }));
      }
      body.append(column);
      colNodes.push({ node: column, date: d });
    });

    // One listener for the whole block: there are seven columns and any number
    // of events in them, and which gesture this is comes from what the pointer
    // went down on anyway.
    body.addEventListener("pointerdown", onDown);

    const head = App.el("div", { class: "wk-fixed" }, header, strip);
    // --cols on the block, not on one grid inside it: the labels, the strip and
    // the hour grid are three grids that have to agree about how many columns
    // a period has, and a day view where only one of them knew was a header
    // drawn over the wrong column.
    const node = App.el("div", { class: "wk-period", style: `--cols:${days}` }, head, body);
    periods.push({ start, node, head, body, cols: colNodes });
    return node;
  }

  /* Period tops, cached in the scroller's own coordinates, which is what
     `scrollTop` speaks. Measured once per render rather than per scroll event:
     with eight blocks on the page, asking the layout engine where they are on
     every wheel notch is the one thing here that could cost a frame. */
  function measure() {
    const base = root.getBoundingClientRect().top - root.scrollTop;
    periods.forEach((p) => {
      p.top = p.node.getBoundingClientRect().top - base;
      p.bodyTop = p.body.getBoundingClientRect().top - base;
    });
  }

  /* The period the top of the viewport is in, and how far into its hours. The
     pair is what a render has to put back: the date alone would keep the week
     and lose the time of day. */
  const visibleTop = (p) => root.scrollTop + p.head.offsetHeight - p.bodyTop;

  function anchor() {
    const p = topPeriod();
    return p ? { start: p.start, offset: visibleTop(p) } : null;
  }

  function topPeriod() {
    if (!periods.length) return null;
    let found = periods[0];
    // Blocks are in date order and so are their tops, so the last one that has
    // passed the top of the viewport is the week being read.
    for (const p of periods) { if (p.top <= root.scrollTop + 1) found = p; else break; }
    return found;
  }

  function park(period, offset, behavior = "auto") {
    const top = Math.max(period.bodyTop + offset - period.head.offsetHeight, 0);
    // A scroll long enough to be a blur is not showing anyone the journey, so
    // past a couple of screens it stops pretending and jumps. Which also keeps
    // the animation short enough to be covered by the guard below.
    const how = behavior === "smooth" && Math.abs(top - root.scrollTop) < root.clientHeight * 2.5
      ? "smooth" : "auto";
    /* The scroll this causes is ours, not the reader's: it must not be read as
       "they have reached the edge, slide the window", which is how one jump
       turns into a run of slides and a view that will not settle. Never
       shortened, and long enough to outlast a smooth scroll: a slide that
       lands while the animation is still running replaces the grid under it
       and leaves the reader wherever the animation had got to. */
    const guard = performance.now() + (how === "smooth" ? 900 : 350);
    suppressUntil = Math.max(suppressUntil, guard);
    root.scrollTo({ top, behavior: how });
    parked = offset;
  }

  function render(keep) {
    if (!root || !App.state.range) return;
    const held = keep || anchor();
    const from = periodStart(App.state.range.start);
    const to = App.state.range.end;
    const filed = prepare();

    periods = [];
    byId = new Map();
    const blocks = [];
    for (let s = from; s < to; s = T().addDays(s, days)) blocks.push(periodBlock(s, filed));
    root.replaceChildren(...blocks);
    // Synchronously: everything that scrolls reads these offsets immediately
    // afterwards, and a deferred measure would hand it the previous render's.
    measure();

    const back = held && periods.find((p) => T().ymd(p.start) === T().ymd(held.start));
    if (back) park(back, held.offset);
    else scrollToDate(App.state.cursor, "auto");
  }

  // --- scrolling -------------------------------------------------------------

  function scrollToDate(date, behavior = "smooth") {
    const key = T().ymd(periodStart(date));
    const period = periods.find((p) => T().ymd(p.start) === key);
    if (!period) return false;
    // The hour you were already looking at, or the start of the working day
    // the first time round.
    const offset = parked === null ? App.state.dayStart * PX_PER_HOUR : parked;
    park(period, offset, behavior);
    App.state.cursor = T().day(date);
    App.bus.emit("view-position", App.state.cursor);
    return true;
  }

  /* Slide the loaded window, keeping its size. Growing it instead would mean
     an ever-taller page for a reader who is only ever looking at one screen. */
  async function extend(direction) {
    // Not while a scroll of ours is still in flight: sliding the window
    // re-renders, and a re-render mid-animation cancels it half way there.
    if (extending || performance.now() < suppressUntil) return;
    extending = true;
    const held = anchor();
    const first = T().addDays(App.state.range.start, SLIDE * days * direction);
    try {
      await App.load.events(first, T().addDays(first, TOTAL * days));
      render(held);
    } finally {
      extending = false;
    }
  }

  function onScroll() {
    // #stage is shared with every other view, and replacing its contents fires
    // a scroll event of its own; without this guard, switching away from the
    // week fired this handler, which saw itself near the edge of its own old
    // window and fetched the week back over whatever had replaced it.
    if (!root || extending || (App.state.view !== "week" && App.state.view !== "day")) return;
    const p = topPeriod();
    if (!p) return;
    // Where in the week we are is worth keeping even when the scroll was ours:
    // it is what the next render puts back.
    parked = visibleTop(p);
    if (performance.now() < suppressUntil) return;
    if (T().ymd(p.start) !== T().ymd(periodStart(App.state.cursor))) {
      App.state.cursor = days === 1 ? p.start : T().day(p.start);
      App.bus.emit("view-position", App.state.cursor);
    }
    const i = periods.indexOf(p);
    if (i <= 0) extend(-1);
    else if (i >= periods.length - 2) extend(1);
  }

  // --- api -------------------------------------------------------------------

  async function show(container, count) {
    root = container;
    days = count || 7;
    root.className = "week";
    root.onscroll = onScroll;
    // Nothing between here and the scroll at the end is the reader moving.
    suppressUntil = performance.now() + 1200;
    periods = [];
    const first = T().addDays(periodStart(App.state.cursor), -BACK * days);
    await App.load.events(first, T().addDays(first, TOTAL * days));
    render();
    scrollToDate(App.state.cursor, "auto");
  }

  /* Somewhere else in time. Inside the loaded window it is a scroll rather than
     a redraw, which is the whole point of drawing the weeks in one column: the
     arrows and `g 11` move the view the same way the wheel does, so a week
     boundary looks the same however you cross it. */
  async function goto(date) {
    App.state.cursor = T().day(date);
    if (root && periods.length && scrollToDate(date)) return;
    await show(root, days);
  }

  return { show, render, goto };
})();
