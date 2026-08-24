/* The Ribbon — meercal's own view, and the reason this program exists.

   Two problems shape it, and both come from having many calendars rather than
   one.

   **Long events.** A month grid wraps time every seven days, so an event that
   runs three weeks is chopped into three bars that have to be re-recognised on
   each line, and a week grid buries it in an all-day strip that scrolls out of
   sight. Neither is a drawing of the thing that is actually happening, which
   is one continuous stretch of time.

   The Ribbon does not wrap. Days run down the page continuously, so a
   three-week event is *one bar*, three weeks tall, in a rail down the left —
   unbroken because time is unbroken. Its label is `position: sticky`, so it
   rides along beside whatever day you are reading and tells you where you are
   inside it: "day 4 of 19". Overlapping long events are packed into parallel
   lanes the way a commit graph packs branches, so "what am I in the middle
   of" is answerable at a glance, at any scroll position.

   **Many calendars.** Every day is a row that is only as tall as it needs to
   be, and runs of empty days collapse into one line, so a fortnight of twenty
   calendars fits on a screen without any of it being drawn twice. Colour is
   per calendar throughout, and events from different calendars that collide
   are marked as a clash rather than left to be noticed.

   Week boundaries are still there — a rule and an ISO week number in the
   gutter — because losing the week's rhythm was the one thing a continuous
   view could not afford to cost. */

window.App = window.App || {};

App.ribbon = (() => {
  const T = () => App.time;

  // How much is loaded either side of the cursor, and when to widen it. The
  // window is generous because the whole point is scrolling through it, and a
  // fetch on every flick would make the view feel like a web page.
  const BACK_DAYS = 45;
  const FORWARD_DAYS = 135;
  // How near the edge of the loaded window the reader has to get before it
  // slides. In days, not pixels: runs of quiet days collapse to one line, so a
  // pixel threshold is met on the very first paint and the window then grows
  // without end.
  const EDGE_DAYS_BACK = 10;
  const EDGE_DAYS_FORWARD = 21;
  const SLIDE_DAYS = 45;

  let root = null;          // the scrolling container
  let rows = [];            // {date, kind, top}  — one per rendered grid row
  let dayRow = new Map();   // "YYYY-MM-DD" -> row index
  let spans = [];           // laid-out long events
  let lanes = 0;
  let rangeStart = null;
  let rangeEnd = null;
  let extending = false;
  // Until when a scroll event is ours rather than the reader's. Programmatic
  // scrolls fire the same event, and acting on them is what turned a jump into
  // a loop of window slides.
  let suppressUntil = 0;

  // --- layout ---------------------------------------------------------------

  /* Long events into parallel lanes. Greedy by start, longest first: a lane is
     reused only once the event in it has ended, so a bar never steps sideways
     halfway down, which is the whole reason to draw it as one bar. */
  function packLanes(items) {
    const open = [];   // lane -> end date (exclusive)
    items.forEach((item) => {
      let lane = open.findIndex((end) => end <= item.startDay);
      if (lane === -1) { lane = open.length; open.push(null); }
      open[lane] = item.endDay;
      item.lane = lane;
    });
    return open.length;
  }

  /* Which timed events collide with another calendar's. Two meetings on one
     calendar overlapping is usually deliberate (a block and a thing inside
     it); the same across two calendars is the double booking this program is
     supposed to catch. */
  function markClashes(items) {
    const sorted = items.filter((e) => !e.all_day && !e.free).sort((a, b) => a.startAt - b.startAt);
    for (let i = 0; i < sorted.length; i++) {
      for (let j = i + 1; j < sorted.length; j++) {
        if (sorted[j].startAt >= sorted[i].endAt) break;
        if (sorted[j].cal !== sorted[i].cal) { sorted[i].clash = true; sorted[j].clash = true; }
      }
    }
  }

  function prepare(events, start, end) {
    const timed = [];
    const longs = [];
    events.forEach((e) => {
      const item = Object.assign({}, e, {
        startAt: T().parse(e.start),
        endAt: T().parse(e.end),
      });
      item.startDay = T().day(item.startAt);
      // An all-day event's end is exclusive; a timed one ending at midnight
      // belongs to the day it started in. Both come down to "the last day this
      // covers", which is what the bar is drawn over.
      const lastMs = item.endAt - 1;
      item.endDay = T().day(new Date(Math.max(lastMs, item.startAt.getTime())));
      item.days = T().daysBetween(item.startDay, item.endDay) + 1;
      (item.days > 1 ? longs : timed).push(item);
    });

    longs.sort((a, b) => a.startDay - b.startDay || b.days - a.days);
    lanes = packLanes(longs);
    spans = longs;

    const byDay = new Map();
    timed.forEach((e) => {
      const key = T().ymd(e.startDay);
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(e);
    });
    byDay.forEach((list) => {
      list.sort((a, b) => (a.all_day === b.all_day ? a.startAt - b.startAt : a.all_day ? -1 : 1));
      markClashes(list);
    });
    return byDay;
  }

  // --- rendering ------------------------------------------------------------

  function dayCell(date, events) {
    const isWeekStart = ((date.getDay() === 0 ? 7 : date.getDay()) === App.state.weekStart);
    const today = T().isToday(date);
    const body = App.el("div", { class: "rb-day" + (today ? " today" : "") });

    const now = new Date();
    events.forEach((e) => body.append(chip(e, today && e.endAt < now)));
    if (!events.length) body.append(App.el("div", { class: "rb-empty", text: "" }));

    const gutter = App.el(
      "div",
      { class: "rb-gutter" + (today ? " today" : "") + (isWeekStart ? " week-start" : "") },
      App.el("div", { class: "rb-dnum", text: String(date.getDate()) }),
      App.el("div", { class: "rb-dow", text: T().weekday(date) }),
      isWeekStart
        ? App.el("div", {
            class: "rb-week",
            text: `W${String(T().isoWeek(date).week).padStart(2, "0")}`,
            title: "ISO week",
          })
        : null,
      // The clock, in the gutter. A line drawn across the row would be a lie:
      // a day row here is a list of what is on, not an axis of hours.
      today ? App.el("div", { class: "rb-now", text: T().time(new Date()) }) : null,
    );
    if (events.some((e) => e.clash)) {
      gutter.append(App.el("div", { class: "rb-clash", text: "!", title: "Two calendars want this time" }));
    }
    return { gutter, body, isWeekStart };
  }

  function chip(e, past) {
    const cal = App.state.calendar(e.cal) || { color: "#888", name: "" };
    const node = App.el(
      "button",
      {
        class: "chip" + (e.all_day ? " all-day" : "") + (e.free ? " free" : "") +
               (e.clash ? " clash" : "") + (past ? " past" : "") +
               (e.status === "CANCELLED" ? " cancelled" : ""),
        style: `--c:${cal.color};--c-weak:${App.tint(cal.color, 0.14)}`,
        title: `${e.title}${e.location ? " · " + e.location : ""} · ${cal.name}`,
        onclick: (ev) => { ev.stopPropagation(); App.editor.open(e, ev.currentTarget); },
      },
      e.all_day ? null : App.el("span", { class: "chip-time", text: T().time(e.startAt) }),
      App.el("span", { class: "chip-title", text: e.title }),
      e.location ? App.el("span", { class: "chip-where", text: e.location }) : null,
      e.recurring ? App.el("span", { class: "chip-mark", text: "↻", title: "Repeats" }) : null,
      e.attendee_count ? App.el("span", { class: "chip-mark", text: `${e.attendee_count}` }) : null,
    );
    return node;
  }

  function spanBar(item) {
    const cal = App.state.calendar(item.cal) || { color: "#888", name: "" };
    const openTop = item.startDay < rangeStart;
    const openBottom = item.endDay >= rangeEnd;
    const bar = App.el(
      "div",
      {
        class: "span" + (openTop ? " open-top" : "") + (openBottom ? " open-bottom" : "") +
               (item.free ? " free" : "") + (item.all_day ? " all-day" : ""),
        style: `--c:${cal.color};--c-weak:${App.tint(cal.color, 0.18)}`,
        title: `${item.title} · ${item.days} days · ${cal.name}`,
        onclick: (ev) => App.editor.open(item, ev.currentTarget),
      },
      App.el(
        "div",
        { class: "span-label" },
        App.el("span", { class: "span-title", text: item.title }),
        App.el("span", { class: "span-count", dataset: { for: String(item.id) } }, `${item.days} days`),
      ),
    );
    return bar;
  }

  function monthHeader(date) {
    return App.el("div", { class: "rb-month" }, App.el("span", { text: T().monthName(date) }));
  }

  function quietRow(from, to, count) {
    const label = count === 1 ? "1 quiet day" : `${count} quiet days`;
    return App.el(
      "button",
      {
        class: "rb-quiet",
        title: "Nothing here — click to open these days anyway",
        onclick: () => { App.state.prefs.collapseQuiet = false; App.load.prefs(); render(); },
      },
      App.el("span", { text: `${label} · ${from.getDate()}–${to.getDate()}` }),
    );
  }

  /* One pass over the loaded range, writing a CSS grid whose rows are days and
     whose left columns are span lanes. Placing every item explicitly is what
     lets a bar span rows of different heights without measuring anything: the
     grid does the arithmetic, and a re-render cannot drift out of step with
     the rail. */
  function render() {
    if (!root || !App.state.range) return;
    rangeStart = T().day(App.state.range.start);
    rangeEnd = T().day(App.state.range.end);
    const byDay = prepare(App.state.events, rangeStart, rangeEnd);

    const grid = App.el("div", { class: "ribbon-grid" });
    // Never zero. `repeat(0, …)` is invalid CSS, and an invalid value takes the
    // whole grid-template-columns declaration with it — which drops the grid
    // back to auto columns and draws the day rows and the gutter in the wrong
    // order. A fortnight with no long events in it is an ordinary fortnight,
    // so this was a view that broke on the *easy* case. The rail is collapsed
    // by width instead: .ribbon:not(.has-rail) sets --lane-w to 0.
    grid.style.setProperty("--lanes", String(Math.max(lanes, 1)));
    rows = [];
    dayRow = new Map();

    let rowIndex = 1;
    let quietFrom = null;
    let quietCount = 0;
    let lastMonth = null;
    const cells = [];

    const flushQuiet = (until) => {
      if (!quietFrom) return;
      const node = quietRow(quietFrom, until, quietCount);
      node.style.gridRow = String(rowIndex);
      node.style.gridColumn = "-2 / -1";
      cells.push(node);
      // Every collapsed day maps to this one row, so a bar crossing the run
      // still starts and ends on the right line.
      for (let i = 0; i < quietCount; i++) {
        dayRow.set(T().ymd(T().addDays(quietFrom, i)), rowIndex);
      }
      rows.push({ date: quietFrom, kind: "quiet", row: rowIndex, days: quietCount, node });
      rowIndex += 1;
      quietFrom = null;
      quietCount = 0;
    };

    const totalDays = T().daysBetween(rangeStart, rangeEnd);
    for (let i = 0; i < totalDays; i++) {
      const date = T().addDays(rangeStart, i);
      const key = T().ymd(date);
      const events = byDay.get(key) || [];
      const spanEdge = spans.some(
        (s) => T().ymd(s.startDay) === key || T().ymd(s.endDay) === key,
      );
      const month = `${date.getFullYear()}-${date.getMonth()}`;

      const quiet = App.state.prefs.collapseQuiet && !events.length && !spanEdge &&
                    !T().isToday(date) && month === lastMonth;
      if (quiet) {
        if (!quietFrom) quietFrom = date;
        quietCount += 1;
        continue;
      }
      flushQuiet(T().addDays(date, -1));

      if (month !== lastMonth) {
        // Sticky, and spanning every column: it is the only thing in the view
        // that says which month you are in once the grid has scrolled past it.
        const header = monthHeader(date);
        header.style.gridRow = String(rowIndex);
        header.style.gridColumn = "1 / -1";
        cells.push(header);
        rows.push({ date, kind: "month", row: rowIndex, node: header });
        rowIndex += 1;
        lastMonth = month;
      }

      const { gutter, body, isWeekStart } = dayCell(date, events);
      gutter.style.gridRow = body.style.gridRow = String(rowIndex);
      gutter.style.gridColumn = "1";
      body.style.gridColumn = "-2 / -1";
      if (isWeekStart) { gutter.classList.add("rule"); body.classList.add("rule"); }
      cells.push(gutter, body);
      dayRow.set(key, rowIndex);
      rows.push({ date, kind: "day", row: rowIndex, node: gutter });
      rowIndex += 1;
    }
    flushQuiet(T().addDays(rangeEnd, -1));

    spans.forEach((item) => {
      const startKey = T().ymd(item.startDay < rangeStart ? rangeStart : item.startDay);
      const endKey = T().ymd(item.endDay >= rangeEnd ? T().addDays(rangeEnd, -1) : item.endDay);
      const from = dayRow.get(startKey);
      const to = dayRow.get(endKey);
      if (!from || !to) return;
      const bar = spanBar(item);
      bar.style.gridRow = `${from} / ${to + 1}`;
      bar.style.gridColumn = String(2 + item.lane);
      cells.push(bar);
    });

    grid.append(...cells);
    root.replaceChildren(grid);
    root.classList.toggle("has-rail", lanes > 0);
    // Synchronously, not in a frame's time: everything that scrolls — Today,
    // the arrows, restoring the anchor after the window slides — reads these
    // offsets immediately after a render, and a deferred measure means it
    // reads the *previous* render's. Reading offsetTop forces the layout the
    // browser was going to do anyway, so this costs nothing it saved.
    measure();
    updateCounters();
  }

  // --- "day 4 of 19" --------------------------------------------------------

  /* Row tops, cached. Read from the nodes themselves rather than found again
     by selector: the grid places several items on one row and only the row's
     own cell knows where it starts. */
  function measure() {
    rows.forEach((r) => { r.top = r.node ? r.node.offsetTop : 0; });
  }

  /* The day the reader is actually looking at: the last row whose top has
     passed the top of the viewport. Everything sticky in the rail is labelled
     against it, which is what turns a long bar from "something is happening"
     into "you are on day 4 of 19". */
  function topDate() {
    const y = root.scrollTop + 8;
    let found = null;
    for (const r of rows) {
      if (r.kind !== "day" && r.kind !== "quiet") continue;
      // Rows are in date order and so are their tops, so the last one that has
      // passed the top of the viewport is the day being read. The `break` is
      // the whole point — without measured tops every row would look like it
      // qualifies and this would answer with the end of the range.
      if (r.top <= y) found = r; else break;
    }
    return found ? found.date : rangeStart;
  }

  function updateCounters() {
    const today = topDate();
    spans.forEach((item) => {
      const node = root.querySelector(`.span-count[data-for="${item.id}"]`);
      if (!node) return;
      const offset = T().daysBetween(item.startDay, today) + 1;
      node.textContent = offset >= 1 && offset <= item.days
        ? `day ${offset} of ${item.days}`
        : `${item.days} days`;
    });
  }

  // --- scrolling ------------------------------------------------------------

  /* Slide the loaded window, keeping its size. Growing it instead would mean
     an ever-larger query and an ever-taller grid for a reader who is only ever
     looking at one screenful — and the server refuses a range past 800 days
     anyway, so the growth has an end that arrives as an error. */
  async function extend(direction) {
    if (extending) return;
    extending = true;
    const anchor = topDate();
    const shift = SLIDE_DAYS * direction;
    const start = T().addDays(App.state.range.start, shift);
    const end = T().addDays(App.state.range.end, shift);
    try {
      await App.load.events(start, end);
      render();
      // Put the reader back on the day they were on, not at the pixel they
      // were at: the rows above them have changed height and the pixel means
      // nothing any more.
      scrollToDate(anchor, "auto");
    } finally {
      extending = false;
    }
  }

  function onScroll() {
    // Replacing the stage's contents resets its scrollTop, which fires a
    // scroll event — and #stage is shared with every other view. Without this
    // guard, switching away from the Ribbon fired this handler, which saw
    // itself near the edge of its old window, fetched, and drew the Ribbon
    // back over the week that had just replaced it.
    if (!root || extending || App.state.view !== "ribbon") return;
    // The counters are just what is on screen, so they follow every scroll.
    updateCounters();
    // The date does not. A programmatic scroll — the one every render ends
    // with, and the jump `t` makes — fires this too, and replacing the grid
    // resets scrollTop to 0 first. Reading the cursor from that moment is how
    // opening the Ribbon used to drag the date back to the top of the loaded
    // window: press `w` afterwards and you were six weeks in the past.
    if (performance.now() < suppressUntil) return;
    const date = topDate();
    App.bus.emit("ribbon-position", date);
    if (T().daysBetween(rangeStart, date) < EDGE_DAYS_BACK) extend(-1);
    else if (T().daysBetween(date, rangeEnd) < EDGE_DAYS_FORWARD) extend(1);
  }

  function scrollToDate(date, behavior = "smooth") {
    const key = T().ymd(T().day(date));
    const row = dayRow.get(key);
    if (!row) return false;
    const entry = rows.find((r) => r.row === row);
    if (!entry || !entry.node) return false;
    const top = entry.node.offsetTop;
    entry.top = top;
    // The scroll this causes is ours, not the reader's: it must not be read as
    // "they have reached the edge, slide the window", which is how one Today
    // used to turn into a run of slides and a view that would not settle.
    suppressUntil = performance.now() + 400;
    root.scrollTo({ top: Math.max(top - 12, 0), behavior });
    // Say where we went. The scroll events this causes are suppressed above,
    // so this is the only thing that tells the toolbar and the other views
    // which day the Ribbon is now showing.
    App.bus.emit("ribbon-position", T().day(date));
    return true;
  }

  // --- api ------------------------------------------------------------------

  async function show(container) {
    root = container;
    root.className = "ribbon";
    root.onscroll = onScroll;
    // Nothing between here and the scroll below is the reader moving.
    suppressUntil = performance.now() + 1500;
    const cursor = App.state.cursor;
    await App.load.events(T().addDays(cursor, -BACK_DAYS), T().addDays(cursor, FORWARD_DAYS));
    render();
    scrollToDate(cursor, "auto");
  }

  async function goto(date) {
    App.state.cursor = T().day(date);
    if (scrollToDate(date)) return;
    // Not in the loaded window — fetch one around it. `show` scrolls to the
    // cursor itself once the render has been measured.
    await show(root);
  }

  return { show, render, goto, scrollToDate, topDate };
})();
