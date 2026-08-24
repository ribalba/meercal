/* Year — twelve months, and the long things laid across all of them.

   A year view is usually a wall of tiny numbers that answers only "which day
   is the 3rd on". The useful question at this zoom is a different one: *what
   is this year shaped like* — when am I away, when is the school shut, which
   month has nothing in it. So the year opens with a band of the long events
   drawn across a twelve-month axis, the same events the Ribbon puts in its
   rail, and the months below are tinted by what covers them.

   Everything here is one request: 365 days of occurrences, which is what the
   expansion table makes cheap. */

window.App = window.App || {};

App.year = (() => {
  const T = () => App.time;
  // Rows in the band before it stops and says how many are left. Twenty is
  // already a tall band; past that the answer is the Ribbon filtered to
  // is:span, which the "+n more" goes to.
  const MAX_SPAN_ROWS = 20;
  const DOTS = 3;

  let root = null;

  function dayData(events, from, to) {
    const byDay = new Map();
    const cover = new Map();     // ymd -> the colour of the long thing over it
    events.forEach((e) => {
      const start = T().parse(e.start);
      const end = T().parse(e.end);
      const startDay = T().day(start);
      const endDay = T().day(new Date(Math.max(end - 1, start.getTime())));
      const days = T().daysBetween(startDay, endDay) + 1;
      const cal = App.state.calendar(e.cal) || { color: "#888" };
      if (days > 1) {
        for (let i = 0; i < days; i++) {
          const d = T().addDays(startDay, i);
          if (d < from || d >= to) continue;
          const key = T().ymd(d);
          if (!cover.has(key)) cover.set(key, cal.color);
        }
      } else {
        const key = T().ymd(startDay);
        if (!byDay.has(key)) byDay.set(key, []);
        byDay.get(key).push({ ...e, color: cal.color, startAt: start });
      }
    });
    byDay.forEach((list) => list.sort((a, b) => a.startAt - b.startAt));
    return { byDay, cover };
  }

  function spanBand(events, from, to) {
    const total = T().daysBetween(from, to);
    const longs = events
      .map((e) => {
        const start = T().parse(e.start);
        const end = T().parse(e.end);
        const startDay = T().day(start);
        const endDay = T().day(new Date(Math.max(end - 1, start.getTime())));
        return { ...e, startDay, endDay, days: T().daysBetween(startDay, endDay) + 1 };
      })
      .filter((e) => e.days > 1)
      .sort((a, b) => a.startDay - b.startDay || b.days - a.days);
    if (!longs.length) return null;

    const band = App.el("div", { class: "yr-band" });
    // The month ruler the bars are read against.
    const ruler = App.el("div", { class: "yr-ruler" });
    for (let m = 0; m < 12; m++) {
      const first = new Date(from.getFullYear(), m, 1);
      const days = new Date(from.getFullYear(), m + 1, 0).getDate();
      ruler.append(App.el("div", {
        class: "yr-ruler-cell",
        style: `flex: ${days} 0 0`,
        text: first.toLocaleDateString(undefined, { month: "short" }),
      }));
    }
    band.append(App.el("div", { class: "yr-band-head" },
      App.el("div", { class: "yr-band-title", text: "Running through the year" }), ruler));

    longs.slice(0, MAX_SPAN_ROWS).forEach((e) => {
      const cal = App.state.calendar(e.cal) || { color: "#888", name: "" };
      const offset = Math.max(T().daysBetween(from, e.startDay), 0);
      const end = Math.min(T().daysBetween(from, e.endDay) + 1, total);
      const row = App.el("div", { class: "yr-row" },
        App.el("div", { class: "yr-row-label", title: `${e.title} · ${cal.name}` },
          App.el("span", { class: "cal-dot", style: `--c:${cal.color}` }),
          App.el("span", { class: "yr-row-name", text: e.title }),
        ),
        App.el("div", { class: "yr-track" },
          App.el("button", {
            class: "yr-bar" + (e.free ? " free" : ""),
            style: `--c:${cal.color};--c-weak:${App.tint(cal.color, 0.25)};` +
                   `left:${(offset / total) * 100}%;width:${Math.max(((end - offset) / total) * 100, 0.4)}%`,
            title: `${e.title} · ${e.days} days · ${e.start.slice(0, 10)} → ${e.end.slice(0, 10)}`,
            onclick: () => App.shell.goTo(T().parse(e.start), "ribbon"),
          }, App.el("span", { class: "yr-bar-days", text: `${e.days}d` })),
        ),
      );
      band.append(row);
    });

    if (longs.length > MAX_SPAN_ROWS) {
      band.append(App.el("button", {
        class: "yr-more",
        text: `+${longs.length - MAX_SPAN_ROWS} more — open them in the Ribbon`,
        onclick: () => {
          document.getElementById("filter-input").value = "is:span";
          App.search.apply("is:span");
          App.shell.setView("ribbon");
        },
      }));
    }
    return band;
  }

  function miniMonth(year, month, data) {
    const first = new Date(year, month, 1);
    const gridStart = T().startOfWeek(first, App.state.weekStart);
    const box = App.el("div", { class: "yr-month" },
      App.el("button", {
        class: "yr-month-name",
        title: `Open ${first.toLocaleDateString(undefined, { month: "long" })} — or press g ${month + 1}`,
        onclick: () => App.shell.goTo(first, "month"),
      },
        // The number, in a bubble: it is what `g 1`–`g 12` takes, so the view
        // that shows all twelve months is where that key ought to be legible.
        App.el("span", { class: "yr-month-no", text: String(month + 1) }),
        App.el("span", { text: first.toLocaleDateString(undefined, { month: "long" }) }),
      ),
    );
    const head = App.el("div", { class: "yr-dows" });
    for (let i = 0; i < 7; i++) {
      const d = T().addDays(gridStart, i);
      head.append(App.el("div", { text: T().weekday(d).slice(0, 1) }));
    }
    box.append(head);

    const grid = App.el("div", { class: "yr-days" });
    for (let i = 0; i < 42; i++) {
      const date = T().addDays(gridStart, i);
      if (date.getMonth() !== month) {
        // Blank rather than the neighbouring month's numbers: at this size two
        // months of grey digits in one box is unreadable.
        grid.append(App.el("div", { class: "yr-day blank" }));
        continue;
      }
      const key = T().ymd(date);
      const events = data.byDay.get(key) || [];
      const covered = data.cover.get(key);
      const cell = App.el("button", {
        class: "yr-day" + (T().isToday(date) ? " today" : "") + (covered ? " covered" : ""),
        style: covered ? `--cover:${App.tint(covered, 0.22)};--cover-line:${covered}` : null,
        title: events.length
          ? `${events.length} event${events.length === 1 ? "" : "s"}`
          : (covered ? "Inside a longer event" : ""),
        onclick: () => App.shell.goTo(date, "ribbon"),
      },
        App.el("span", { class: "yr-dnum", text: String(date.getDate()) }),
        App.el("span", { class: "yr-dots" },
          events.slice(0, DOTS).map((e) => App.el("i", { style: `--c:${e.color}` })),
        ),
      );
      grid.append(cell);
    }
    box.append(grid);
    return box;
  }

  function render() {
    if (!root || !App.state.range) return;
    const from = T().day(App.state.range.start);
    const to = T().day(App.state.range.end);
    const year = from.getFullYear();
    const data = dayData(App.state.events, from, to);

    const months = App.el("div", { class: "yr-months" });
    for (let m = 0; m < 12; m++) months.append(miniMonth(year, m, data));

    const band = spanBand(App.state.events, from, to);
    root.replaceChildren(...(band ? [band, months] : [months]));
  }

  async function show(container) {
    root = container;
    root.className = "year";
    const year = App.state.cursor.getFullYear();
    await App.load.events(new Date(year, 0, 1), new Date(year + 1, 0, 1));
    render();
  }

  return { show, render };
})();
