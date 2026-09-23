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
     pill lives near its corner, with the lower fields close to the bottom.

     Separate from `popover` because it has to be callable again. A menu that
     fills itself in after it opens -- the time list does, and then resizes on
     every keystroke as it narrows -- is never the size of the empty box that
     was placed, and a popover measured once is one that hangs off the bottom
     of the screen exactly when it has the most to show. */
  function anchorTo(box, anchor) {
    const rect = anchor.getBoundingClientRect();
    const left = Math.min(rect.left, window.innerWidth - box.offsetWidth - 8);
    const below = rect.bottom + 6;
    const height = box.offsetHeight;
    const top = below + height > window.innerHeight - 8 ? rect.top - height - 6 : below;
    box.style.left = `${Math.max(left, 8)}px`;
    box.style.top = `${Math.max(top, 8)}px`;
  }

  function popover(anchor, content) {
    close();
    const box = App.el("div", { class: "pop" }, content);
    document.body.append(box);
    anchorTo(box, anchor);

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

  /* Zero-padded, because this string is not only read: it is what the editor
     keeps in `when.startTime` and pastes after the "T" of an ISO datetime, and
     "9:15" is not a time any ISO parser accepts. Padding also lines the menu up
     with the pill beside it, which shows the same HH:MM. */
  function label(minutes) {
    const m = ((minutes % 1440) + 1440) % 1440;
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
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

  /* What somebody means when they type a time. Generous on purpose: "9", "09",
     "9:15", "9.15", "9 15", "915", "0915", "1430", "2pm" and "9:15 pm" are all
     things people type at a field that already reads 09:00, and refusing any
     of them sends the reader back to the list they were trying to skip past.

     Minutes from midnight, or null. Null is "not a time", which covers the
     half-typed as well as the wrong: "9:" on the way to "9:15" is somebody in
     the middle of a word, so the caller must not shout at it. */
  function parseTime(text) {
    const s = String(text === null || text === undefined ? "" : text)
      .trim().toLowerCase()
      // "9:" is on the way to "9:15". Dropping the dangling separator makes it
      // the hour it already is, so the field never reddens at a half-typed
      // time and Enter on one still means something sensible.
      .replace(/[:.h\s]+$/, "");
    if (!s) return null;
    const m = s.match(/^(\d{1,4})(?:(?:\s*[:.h]\s*|\s+)(\d{1,2}))?\s*(am|pm|a|p)?$/);
    if (!m) return null;
    const [, lead, tail, mer] = m;
    let hh, mm;
    if (tail !== undefined) {
      if (lead.length > 2) return null;          // "0930:15" is nothing anybody meant
      hh = Number(lead);
      mm = Number(tail);
    } else if (lead.length <= 2) {
      hh = Number(lead);
      mm = 0;
    } else {
      // "915" and "0915": the last two digits are the minutes, there being no
      // three-digit hour for them to be confused with.
      hh = Number(lead.slice(0, -2));
      mm = Number(lead.slice(-2));
    }
    if (mer) {
      if (hh < 1 || hh > 12) return null;        // "13pm" is a typo, not one o'clock
      if (mer[0] === "p" && hh < 12) hh += 12;
      if (mer[0] === "a" && hh === 12) hh = 0;
    }
    if (hh > 23 || mm > 59) return null;
    return hh * 60 + mm;
  }

  /* `from` is the start of the event in minutes, for an end-time list: the
     options begin one step after it and run a full day, so an event ending
     after midnight is a scroll away rather than impossible. `onPick` is given
     the time and whether it landed on the following day.

     The menu is a list *and* a field, because the two ways of answering "what
     time" are not the same question. Picking 14:00 off a list of quarters is
     one aimed click and the list is the right shape for it. Getting to 07:45
     is three flicks of a scroll wheel, and 09:07 is not there at all. So the
     field has focus the moment the menu opens: type "745", press Enter, done,
     and nothing about the list has changed for the people who would rather
     look than type.

     Typing narrows to the hour rather than to one line: the quarters either
     side of what was typed are usually the real answer, and they are the rows
     worth keeping under the cursor. A time off the quarter grid gets a row of
     its own at the top, since wanting one is the only reason to have typed
     it. */
  function timeMenu(anchor, value, { from = null, onPick }) {
    const field = App.el("input", {
      class: "pop-time-in", type: "text", value: value || "", placeholder: "Type a time",
      autocomplete: "off", spellcheck: "false", "aria-label": "Time",
    });
    const list = App.el("div", { class: "pop-times" });
    const box = popover(anchor, App.el("div", { class: "pop-clock" }, field, list));
    const current = minutesOf(value);
    const base = from === null ? 0 : from + STEP_MIN;
    let hot = null;              // the row Enter would take
    /* The field starts out holding the current value, and that is a *label*,
       not a query: a menu that opened already narrowed to the hour it is on
       would have thrown away the list before anyone had asked it to. So it
       opens as the list it has always been, scrolled to where it stands, and
       becomes a filter at the first keystroke. */
    let touched = false;

    /* The list runs one whole day from `base`, so a typed time belongs to
       whichever of today and tomorrow falls inside it. On an end list that is
       exactly the rule the reader already expects of the pills: an end at or
       before the start is the next morning, not a negative event. */
    const inFrame = (mins) => (mins >= base ? mins : mins + 1440);

    function commit(t) {
      close();
      onPick(label(t), t >= 1440);
    }

    function row(t, exact) {
      const nextDay = t >= 1440;
      const text = label(t);
      return App.el("button", {
        class: "pop-time" + (!nextDay && t % 1440 === current ? " on" : "") + (exact ? " exact" : ""),
        type: "button",
        dataset: { min: String(t) },
        onclick: () => commit(t),
      },
        App.el("span", { text: nextDay ? `${text} (next day)` : text }),
        from === null ? null : App.el("span", { class: "pop-dur", text: duration(t - from) }),
      );
    }

    /* Two different statements, and a row can carry both: `on` is the value
       the pill currently holds, `hot` is what Enter would take. */
    function highlight(node, scroll) {
      hot = node || null;
      list.querySelectorAll(".pop-time.hot").forEach((n) => n.classList.remove("hot"));
      if (!hot) return;
      hot.classList.add("hot");
      if (scroll) hot.scrollIntoView({ block: "nearest" });
    }

    function draw() {
      const typed = touched ? field.value.trim() : "";
      const wanted = parseTime(typed);
      field.classList.toggle("bad", Boolean(typed) && wanted === null);

      if (typed && wanted === null) {
        list.replaceChildren(App.el("div", { class: "pop-none", text: "Not a time" }));
        highlight(null);
        anchorTo(box, anchor);
        return;
      }

      const rows = [];
      if (wanted === null) {
        for (let t = base; t < base + 1440; t += STEP_MIN) rows.push(row(t));
      } else {
        const exact = inFrame(wanted);
        if (exact % STEP_MIN !== 0) rows.push(row(exact, true));
        /* The hour that was typed, both times round when the list crosses
           midnight: "10" on an end list can mean tonight or tomorrow morning,
           and the row marked "(next day)" is the one that says which. */
        for (let t = base; t < base + 1440; t += STEP_MIN) {
          if (Math.floor((t % 1440) / 60) === Math.floor(wanted / 60)) rows.push(row(t));
        }
      }
      list.replaceChildren(...rows);

      const want = inFrame(wanted === null ? current : wanted);
      highlight(rows.find((n) => Number(n.dataset.min) === want) || rows[0], false);
      // Opens on the value it holds rather than at the top of the day.
      list.scrollTop = hot ? Math.max(hot.offsetTop - 90, 0) : 0;
      anchorTo(box, anchor);
    }

    function step(delta) {
      const rows = Array.from(list.querySelectorAll(".pop-time"));
      if (!rows.length) return;
      const at = rows.indexOf(hot);
      if (at === -1) return highlight(rows[delta < 0 ? rows.length - 1 : 0], true);
      highlight(rows[Math.min(Math.max(at + delta, 0), rows.length - 1)], true);
    }

    field.addEventListener("input", () => { touched = true; draw(); });
    field.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        step(ev.key === "ArrowDown" ? 1 : -1);
      } else if (ev.key === "Enter") {
        // The panel behind this is a form, and a stray Enter in it would save
        // the event rather than answer the question being asked.
        ev.preventDefault();
        if (hot) commit(Number(hot.dataset.min));
      }
    });

    draw();
    // Selected, not just focused: the menu opens on the value it holds, and
    // the first keystroke should replace it rather than land beside it.
    field.focus();
    field.select();
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

  return { dateMenu, timeMenu, colorMenu, close, label, duration, minutesOf, parseTime, STEP_MIN };
})();
