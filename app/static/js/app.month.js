/* Month: the overview, with the one fix a month grid actually needs.

   A month grid wraps time every seven days, which is what makes long events
   hard: the usual drawing restarts the bar on each line with nothing to say it
   is the same event. Here a bar that continues is capped with an arrow and
   keeps its lane down the whole month, so the eye can follow one stretch of
   colour across the wraps, and the Ribbon is one key away when following it
   is not enough. */

window.App = window.App || {};

App.month = (() => {
  const T = () => App.time;
  const MAX_CHIPS = 3;   // per day, before "+n more"

  let root = null;

  function render() {
    if (!root || !App.state.range) return;
    const gridStart = T().day(App.state.range.start);
    const weeks = Math.round(T().daysBetween(gridStart, App.state.range.end) / 7);
    const month = App.state.cursor.getMonth();

    const prepared = App.state.events.map((e) => {
      const item = Object.assign({}, e, { startAt: T().parse(e.start), endAt: T().parse(e.end) });
      item.startDay = T().day(item.startAt);
      item.endDay = T().day(new Date(Math.max(item.endAt - 1, item.startAt.getTime())));
      item.days = T().daysBetween(item.startDay, item.endDay) + 1;
      return item;
    });
    // By start, then longest first, not by length. Packing longest-first puts
    // a one-day birthday behind every three-week event in the month, which
    // gives it lane six and makes every cell in its week reserve six lanes of
    // empty band. Chronological order keeps the lanes as shallow as the
    // overlaps actually are.
    const longs = prepared.filter((e) => e.days > 1 || e.all_day)
      .sort((a, b) => a.startDay - b.startDay || b.days - a.days);
    const timed = prepared.filter((e) => !(e.days > 1 || e.all_day));

    // Lanes are assigned once, over the whole month, so a bar keeps its height
    // on the line when it wraps to the next week.
    const laneEnds = [];
    longs.forEach((e) => {
      const from = T().daysBetween(gridStart, e.startDay);
      const to = T().daysBetween(gridStart, e.endDay);
      let lane = laneEnds.findIndex((end) => end <= from);
      if (lane === -1) { lane = laneEnds.length; }
      laneEnds[lane] = to + 1;
      e.lane = lane;
    });

    const head = App.el("div", { class: "mo-head" });
    for (let i = 0; i < 7; i++) {
      const d = T().addDays(gridStart, i);
      head.append(App.el("div", { class: "mo-dow", text: T().weekday(d) }));
    }

    const grid = App.el("div", { class: "mo-grid", style: `--weeks:${weeks}` });
    for (let w = 0; w < weeks; w++) {
      const weekStart = T().addDays(gridStart, w * 7);
      const row = App.el("div", { class: "mo-week" });
      const cells = App.el("div", { class: "mo-cells" });
      // How many lanes this week needs, worked out before the cells are built:
      // the band the bars sit in eats into the room a day has for its own
      // events, so the "+n more" cut has to know about it.
      const inWeek = longs.filter((e) => T().daysBetween(weekStart, e.endDay) >= 0 &&
                                         T().daysBetween(weekStart, e.startDay) <= 6);
      const laneCount = inWeek.reduce((n, e) => Math.max(n, e.lane + 1), 0);
      const room = Math.max(1, MAX_CHIPS - laneCount);
      for (let i = 0; i < 7; i++) {
        const date = T().addDays(weekStart, i);
        const key = T().ymd(date);
        const dayEvents = timed.filter((e) => T().ymd(e.startDay) === key)
          .sort((a, b) => a.startAt - b.startAt);
        const cell = App.el("div", {
          class: "mo-cell" + (date.getMonth() === month ? "" : " other") + (T().isToday(date) ? " today" : ""),
          ondblclick: () => App.editor.create(new Date(date.getFullYear(), date.getMonth(), date.getDate(), 9, 0)),
        },
          App.el("div", { class: "mo-dnum" },
            App.el("span", { text: String(date.getDate()) }),
            i === 0 ? App.el("span", { class: "mo-week-no", text: `W${T().isoWeek(date).week}` }) : null,
          ),
        );
        dayEvents.slice(0, room).forEach((e) => {
          const cal = App.state.calendar(e.cal) || { color: "#888" };
          cell.append(App.el("button", {
            class: "mo-chip" + (e.free ? " free" : ""),
            style: `--c:${cal.color}`,
            title: e.title,
            onclick: (ev) => { ev.stopPropagation(); App.editor.open(e, ev.currentTarget); },
          },
            App.el("span", { class: "mo-dot" }),
            App.el("span", { class: "mo-time", text: T().time(e.startAt) }),
            App.el("span", { class: "mo-title", text: e.title }),
          ));
        });
        if (dayEvents.length > room) {
          cell.append(App.el("button", {
            class: "mo-more",
            text: `+${dayEvents.length - room} more`,
            onclick: () => App.shell.goTo(date, "day"),
          }));
        }
        cells.append(cell);
      }

      // The bars for this week, over the cells.
      const bars = App.el("div", { class: "mo-bars" });
      inWeek.forEach((e) => {
        const from = T().daysBetween(weekStart, e.startDay);
        const to = T().daysBetween(weekStart, e.endDay);
        const c0 = Math.max(from, 0);
        const c1 = Math.min(to, 6);
        const cal = App.state.calendar(e.cal) || { color: "#888" };
        bars.append(App.el("button", {
          class: "mo-bar" + (from < 0 ? " open-left" : "") + (to > 6 ? " open-right" : "") +
                 (e.free ? " free" : ""),
          style: `--c:${cal.color};--c-weak:${App.tint(cal.color, 0.22)};` +
                 `grid-column:${c0 + 1} / ${c1 + 2};grid-row:${e.lane + 1}`,
          title: `${e.title} · ${e.days} days`,
          onclick: (ev) => { ev.stopPropagation(); App.editor.open(e, ev.currentTarget); },
        }, App.el("span", { text: from < 0 ? `… ${e.title}` : e.title })));
      });
      // The lane count goes on the week, not on the bar layer: the cells read
      // it too, and reserve exactly that much room under their date number.
      // Without that the bars are drawn over the day's own events, which is
      // how most month grids end up unreadable in a busy week.
      row.style.setProperty("--lanes", String(laneCount));
      row.append(cells, bars);
      grid.append(row);
    }

    root.replaceChildren(head, grid);
  }

  async function show(container) {
    root = container;
    root.className = "month";
    const first = new Date(App.state.cursor.getFullYear(), App.state.cursor.getMonth(), 1);
    const start = T().startOfWeek(first, App.state.weekStart);
    // Always six weeks: a grid that changes height between months makes every
    // month look like a different view.
    await App.load.events(start, T().addDays(start, 42));
    render();
  }

  return { show, render };
})();
