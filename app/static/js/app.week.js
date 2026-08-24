/* Week and day: the familiar grid, kept because it is genuinely the right
   drawing for "what is my Tuesday like".

   The long events live in a strip above the grid rather than inside it, and
   the strip draws them *continuously across the week* with an arrow where they
   run past its edge. That is the one thing the usual all-day row gets wrong:
   it re-states the same event as a fresh bar every week, so a fortnight-long
   trip looks like two unrelated things. */

window.App = window.App || {};

App.week = (() => {
  const T = () => App.time;
  const MIN_HEIGHT = 18;       // a 15-minute meeting still has to be readable
  const PX_PER_HOUR = 44;

  let root = null;
  let days = 7;

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
               (e.status === "CANCELLED" ? " cancelled" : ""),
        style: `--c:${cal.color};--c-weak:${App.tint(cal.color, 0.16)};` +
               `top:${top}px;height:${height}px;left:${e.col * width}%;width:calc(${width}% - 3px)`,
        title: `${e.title} · ${cal.name}`,
        onclick: (ev) => { ev.stopPropagation(); App.editor.open(e, ev.currentTarget); },
      },
      App.el("span", { class: "wk-time", text: T().time(e.startAt) }),
      App.el("span", { class: "wk-title", text: e.title }),
    );
  }

  function render() {
    if (!root || !App.state.range) return;
    const start = T().day(App.state.range.start);
    const cols = [];
    for (let i = 0; i < days; i++) cols.push(T().addDays(start, i));

    const prepared = App.state.events.map((e) => Object.assign({}, e, {
      startAt: T().parse(e.start),
      endAt: T().parse(e.end),
    }));
    prepared.forEach((e) => {
      e.startDay = T().day(e.startAt);
      e.endDay = T().day(new Date(Math.max(e.endAt - 1, e.startAt.getTime())));
      e.days = T().daysBetween(e.startDay, e.endDay) + 1;
    });

    const header = App.el("div", { class: "wk-head" },
      App.el("div", { class: "wk-corner", text: T().isoWeek(start).week ? `W${T().isoWeek(start).week}` : "" }),
      cols.map((d) => App.el("div", { class: "wk-daylabel" + (T().isToday(d) ? " today" : "") },
        App.el("span", { class: "wk-dow", text: T().weekday(d) }),
        App.el("span", { class: "wk-dnum", text: String(d.getDate()) }),
      )),
    );

    // --- the strip: all-day and multi-day, drawn across the columns ---
    const strip = App.el("div", { class: "wk-strip" }, App.el("div", { class: "wk-striplabel", text: "all day" }));
    const grid = App.el("div", { class: "wk-stripgrid", style: `--cols:${days}` });
    const bars = prepared.filter((e) => e.all_day || e.days > 1)
      .sort((a, b) => b.days - a.days || a.startAt - b.startAt);
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
      }, App.el("span", { text: e.title })));
    });
    strip.append(grid);

    // --- the time grid ---
    const body = App.el("div", { class: "wk-body" });
    const hours = App.el("div", { class: "wk-hours" });
    for (let h = 0; h < 24; h++) {
      hours.append(App.el("div", { class: "wk-hour", style: `height:${PX_PER_HOUR}px` },
        App.el("span", { text: `${h}:00` })));
    }
    body.append(hours);

    cols.forEach((d) => {
      const column = App.el("div", { class: "wk-col" + (T().isToday(d) ? " today" : ""),
        style: `height:${24 * PX_PER_HOUR}px`,
        ondblclick: (ev) => {
          const minutes = Math.round((ev.offsetY / PX_PER_HOUR) * 60 / 30) * 30;
          App.editor.create(new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, minutes));
        },
      });
      for (let h = 1; h < 24; h++) {
        column.append(App.el("div", { class: "wk-line", style: `top:${h * PX_PER_HOUR}px` }));
      }
      const timed = prepared.filter((e) => !e.all_day && e.days === 1 && T().ymd(e.startDay) === T().ymd(d));
      layout(timed).forEach((e) => column.append(eventNode(e, d)));
      if (T().isToday(d)) {
        const now = new Date();
        const top = ((now.getHours() * 60 + now.getMinutes()) / 60) * PX_PER_HOUR;
        column.append(App.el("div", { class: "wk-now", style: `top:${top}px` }));
      }
      body.append(column);
    });

    const scroller = App.el("div", { class: "wk-scroll" }, body);
    root.replaceChildren(App.el("div", { class: "wk-fixed" }, header, strip), scroller);
    body.style.setProperty("--cols", String(days));
    scroller.scrollTop = App.state.dayStart * PX_PER_HOUR;
  }

  async function show(container, count) {
    root = container;
    days = count || 7;
    root.className = "week";
    const start = days === 1 ? T().day(App.state.cursor) : T().startOfWeek(App.state.cursor, App.state.weekStart);
    await App.load.events(start, T().addDays(start, days));
    render();
  }

  return { show, render };
})();
