/* Date and time pickers: the pills in the event panel and what drops out of
   them.

   The browser's own `datetime-local` control is one field holding two different
   decisions, and every browser draws it differently. Splitting it into a date
   pill and two time pills makes each decision one click: the date opens a month
   you can see, and a time opens a list you can scroll, with the end times
   labelled by how long that makes the event, which is the number you actually
   have in mind ("half an hour", not "13:30").

   The popover is deliberately plain: no library, one absolutely-positioned box
   that closes on Escape, on an outside click, and on picking something. */

window.App = window.App || {};

App.picker = (() => {
  const T = () => App.time;
  const STEP_MIN = 15;

  let open = null;   // { box, anchor }

  function close() {
    if (!open) return;
    open.box.remove();
    document.removeEventListener("mousedown", outside, true);
    document.removeEventListener("keydown", onKey, true);
    open = null;
  }

  function outside(e) {
    if (open && !open.box.contains(e.target) && !open.anchor.contains(e.target)) close();
  }

  function onKey(e) {
    // Captured and stopped: Escape in an open popover closes the popover, not
    // the panel behind it.
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  }

  /* Anchored under the pill that opened it, nudged back inside the window if
     that would put it off an edge: the panel is 560px wide and the end-time
     pill lives near its corner, with the lower fields close to the bottom. */
  function popover(anchor, content) {
    close();
    const box = App.el("div", { class: "pop" }, content);
    document.body.append(box);
    const rect = anchor.getBoundingClientRect();
    const left = Math.min(rect.left, window.innerWidth - box.offsetWidth - 8);
    const below = rect.bottom + 6;
    const height = box.offsetHeight;
    const top = below + height > window.innerHeight - 8 ? rect.top - height - 6 : below;
    box.style.left = `${Math.max(left, 8)}px`;
    box.style.top = `${Math.max(top, 8)}px`;

    open = { box, anchor };
    document.addEventListener("mousedown", outside, true);
    document.addEventListener("keydown", onKey, true);
    return box;
  }

  // --- dates ---------------------------------------------------------------

  function monthGrid(shown, selected, onPick) {
    const grid = App.el("div", { class: "pop-days" });
    const first = new Date(shown.getFullYear(), shown.getMonth(), 1);
    const start = T().startOfWeek(first, App.state.weekStart);
    for (let i = 0; i < 7; i++) {
      grid.append(App.el("div", { class: "pop-dow", text: T().weekday(T().addDays(start, i)).slice(0, 2) }));
    }
    for (let i = 0; i < 42; i++) {
      const date = T().addDays(start, i);
      const key = T().ymd(date);
      grid.append(App.el("button", {
        class: "pop-day" + (date.getMonth() === shown.getMonth() ? "" : " other") +
               (key === selected ? " on" : "") + (T().isToday(date) ? " today" : ""),
        text: String(date.getDate()),
        type: "button",
        onclick: () => { close(); onPick(key); },
      }));
    }
    return grid;
  }

  function dateMenu(anchor, value, onPick) {
    let shown = value ? T().parse(`${value}T00:00:00`) : new Date();
    const box = popover(anchor, App.el("div", { class: "pop-cal" }));
    const cal = box.firstChild;

    function draw() {
      cal.replaceChildren(
        App.el("div", { class: "pop-head" },
          App.el("span", { class: "pop-month" },
            App.monthNo(shown),
            App.el("span", { text: T().monthName(shown) })),
          App.el("button", { class: "pop-nav", text: "‹", title: "Previous month", type: "button",
            onclick: (e) => { e.stopPropagation(); shown = T().addMonths(shown, -1); draw(); } }),
          App.el("button", { class: "pop-nav", text: "›", title: "Next month", type: "button",
            onclick: (e) => { e.stopPropagation(); shown = T().addMonths(shown, 1); draw(); } }),
        ),
        monthGrid(shown, value, onPick),
      );
    }
    draw();
  }

  // --- times ---------------------------------------------------------------

  function label(minutes) {
    const m = ((minutes % 1440) + 1440) % 1440;
    return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
  }

  /* "45 min", "1 hr", "1 hr 30 min": how long the event becomes if you pick
     this end time. The end time is a means; the length is what is actually
     being decided, so the list says both. */
  function duration(minutes) {
    if (minutes <= 0) return "";
    const h = Math.floor(minutes / 60), m = minutes % 60;
    if (!h) return `${m} min`;
    return m ? `${h} hr ${m} min` : `${h} hr`;
  }

  function minutesOf(value) {
    const [h, m] = (value || "09:00").split(":").map(Number);
    return (h || 0) * 60 + (m || 0);
  }

  /* `from` is the start of the event in minutes, for an end-time list: the
     options begin one step after it and run a full day, so an event ending
     after midnight is a scroll away rather than impossible. `onPick` is given
     the time and whether it landed on the following day. */
  function timeMenu(anchor, value, { from = null, onPick }) {
    const box = popover(anchor, App.el("div", { class: "pop-times" }));
    const list = box.firstChild;
    const current = minutesOf(value);
    const base = from === null ? 0 : from + STEP_MIN;
    let selected = null;

    for (let t = base; t < base + 1440; t += STEP_MIN) {
      const nextDay = t >= 1440;
      const text = label(t);
      const node = App.el("button", {
        class: "pop-time" + (!nextDay && t % 1440 === current ? " on" : ""),
        type: "button",
        onclick: () => { close(); onPick(text, nextDay); },
      },
        App.el("span", { text: nextDay ? `${text} (next day)` : text }),
        from === null ? null : App.el("span", { class: "pop-dur", text: duration(t - from) }),
      );
      if (!selected && !nextDay && t % 1440 === current) selected = node;
      list.append(node);
    }
    // Opens on the value it holds rather than at the top of the day.
    if (selected) list.scrollTop = Math.max(selected.offsetTop - 90, 0);
  }

  // --- colours -------------------------------------------------------------

  /* The ten a calendar can be, and a way out of them. The ten are the ones the
     server gives new calendars (core/models.py), so a recoloured calendar
     still belongs to the same set of hues and the sidebar keeps looking like
     one list; the well beside them is for the person whose team colour is not
     one of ten. */
  function colorMenu(anchor, current, onPick) {
    const box = popover(anchor, App.el("div", { class: "pop-colors" }));
    const list = box.firstChild;
    const now = (current || "").toLowerCase();
    (App.state.calendarColors || []).forEach((hex) => {
      list.append(App.el("button", {
        class: "pop-color" + (hex.toLowerCase() === now ? " on" : ""),
        style: `--c:${hex}`,
        type: "button",
        title: hex,
        onclick: () => { close(); onPick(hex); },
      }));
    });
    const any = App.el("input", { type: "color", class: "pop-color-any", value: current || "#1d6ff2",
                                  title: "Any other colour" });
    // `change`, not `input`: the system's colour picker streams a value while
    // the pointer moves across it, and one request per hue is one too many.
    any.addEventListener("change", () => { close(); onPick(any.value); });
    list.append(any);
  }

  return { dateMenu, timeMenu, colorMenu, close, label, duration, minutesOf, STEP_MIN };
})();
