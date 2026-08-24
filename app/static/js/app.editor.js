/* The event panel — read it, change it, or write a new one.

   Two things it says out loud rather than hiding:

   * **Editing a series edits the series.** Changing one instance of a repeat
     means writing an override, and doing that half-right duplicates the
     meeting instead of moving it. Until that path is written and tested, the
     panel says which it is doing.
   * **A change is queued, not sent.** The web layer holds no credentials; the
     agent pushes it. The panel says "queued" so that a calendar server that is
     down looks like a calendar server that is down, and not like a save that
     silently did nothing. */

window.App = window.App || {};

App.editor = (() => {
  const T = () => App.time;
  let modal = null;
  let current = null;

  const REPEATS = [
    ["", "Does not repeat"],
    ["FREQ=DAILY", "Every day"],
    ["FREQ=WEEKLY", "Every week"],
    ["FREQ=WEEKLY;INTERVAL=2", "Every two weeks"],
    ["FREQ=MONTHLY", "Every month"],
    ["FREQ=YEARLY", "Every year"],
  ];

  function field(label, control, hint) {
    return App.el("label", { class: "fld" },
      App.el("span", { class: "fld-label", text: label }),
      control,
      hint ? App.el("span", { class: "fld-hint", text: hint }) : null,
    );
  }

  /* iCalendar's all-day DTEND is the day *after* the last one. The panel shows
     the last day, because that is the one people mean by "until". */
  function nextDay(value) {
    if (!value) return value;
    const [y, m, d] = value.slice(0, 10).split("-").map(Number);
    const next = new Date(y, m - 1, d + 1);
    return App.time.ymd(next);
  }

  const hhmm = (d) => `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;

  function localValue(date) {
    const p = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}` +
           `T${p(date.getHours())}:${p(date.getMinutes())}`;
  }

  function close() {
    if (modal) modal.remove();
    modal = null;
    current = null;
  }

  function writableCalendars() {
    return App.state.calendars.filter((c) => !c.read_only);
  }

  function build(event, detail, seed = null) {
    const cals = writableCalendars();
    const readOnly = event && event.read_only;
    const start = event ? T().parse(event.start) : (seed ? seed.start : new Date());
    let end = event ? T().parse(event.end) : (seed ? seed.end : new Date(start.getTime() + 3600000));
    // Shown as the last day rather than the exclusive end — see nextDay().
    if (event && event.all_day) end = T().addDays(end, -1);

    const title = App.el("input", { class: "in title-in", value: event ? event.title : "", placeholder: "Title" });
    const calSelect = App.el("select", { class: "in" },
      cals.map((c) => App.el("option", { value: String(c.id), selected: event && event.cal === c.id, text: c.name })));
    /* --- when ---------------------------------------------------------------

       A date pill and two time pills rather than one `datetime-local`: that
       control is two decisions in one field and every browser draws it
       differently. Here the date opens a month you can see and a time opens a
       list you can scroll, with each end time labelled by the length it makes.

       All day is a different shape, not a disabled version of the same one: an
       all-day event has no times, so the time pills go and a second date pill
       arrives. Leaving a time control on screen for something that has no time
       invites setting one and then wondering why it was ignored. */
    const when = {
      allDay: Boolean(event && event.all_day),
      startDate: T().ymd(start),
      startTime: hhmm(start),
      endDate: T().ymd(end),
      endTime: hhmm(end),
    };
    const whenRow = App.el("div", { class: "when-row" });
    const allDay = App.el("input", { type: "checkbox", checked: when.allDay });

    function pill(cls, text, onclick, title) {
      return App.el("button", { class: `pill ${cls}`, text, type: "button", title, onclick });
    }

    function longDate(key) {
      return T().parse(`${key}T00:00:00`)
        .toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    }

    const shiftDate = (key, days) => T().ymd(T().addDays(T().parse(`${key}T00:00:00`), days));

    function paintWhen() {
      const nodes = [
        pill("pill-date", longDate(when.startDate), (e) =>
          App.picker.dateMenu(e.currentTarget, when.startDate, (key) => {
            // Moving the start moves the end with it: the length of the event
            // is what somebody has in mind, not its far edge.
            const days = T().daysBetween(T().parse(`${when.startDate}T00:00:00`),
                                         T().parse(`${key}T00:00:00`));
            when.startDate = key;
            when.endDate = shiftDate(when.endDate, days);
            paintWhen();
          })),
      ];

      if (when.allDay) {
        nodes.push(App.el("span", { class: "when-dash", text: "–" }));
        nodes.push(pill("pill-date", longDate(when.endDate), (e) =>
          App.picker.dateMenu(e.currentTarget, when.endDate, (key) => {
            // An end before the start is not a shorter event, it is a mistake.
            when.endDate = key < when.startDate ? when.startDate : key;
            paintWhen();
          }), "The last day, inclusive"));
      } else {
        nodes.push(pill("pill-time", when.startTime, (e) =>
          App.picker.timeMenu(e.currentTarget, when.startTime, {
            onPick: (value) => {
              const delta = App.picker.minutesOf(value) - App.picker.minutesOf(when.startTime);
              when.startTime = value;
              // Carry the end along, rolling over midnight if it has to.
              const endAt = App.picker.minutesOf(when.endTime) + delta;
              when.endTime = App.picker.label(endAt);
              if (endAt >= 1440) when.endDate = shiftDate(when.endDate, 1);
              if (endAt < 0) when.endDate = shiftDate(when.endDate, -1);
              paintWhen();
            },
          })));
        nodes.push(App.el("span", { class: "when-dash", text: "–" }));
        const overnight = when.endDate !== when.startDate;
        nodes.push(pill("pill-time", when.endTime + (overnight ? " +1" : ""), (e) =>
          App.picker.timeMenu(e.currentTarget, when.endTime, {
            from: App.picker.minutesOf(when.startTime),
            onPick: (value, nextDay) => {
              when.endTime = value;
              when.endDate = nextDay ? shiftDate(when.startDate, 1) : when.startDate;
              paintWhen();
            },
          }), overnight ? "Ends the next day" : ""));
      }
      whenRow.replaceChildren(...nodes);
    }

    allDay.addEventListener("change", (e) => {
      when.allDay = e.currentTarget.checked;
      // An all-day event that came from a timed one covers the days it touched.
      if (when.allDay && when.endDate === when.startDate) when.endDate = when.startDate;
      paintWhen();
    });
    paintWhen();

    /* The places you keep typing, from meercal.toml. A calendar's locations are
       a short list in practice — the office, the room, the same meeting link —
       and typing them out again is the sort of thing a program should notice.
       Clicking a chip fills the field; clicking the one already in the field
       clears it, so it toggles rather than being a one-way door. */
    const places = App.el("div", { class: "places" });

    const location = App.el("input", { class: "in", value: event ? event.location || "" : "", placeholder: "Where" });
    function paintPlaces() {
      places.replaceChildren(...(App.state.places || []).map((place) => App.el("button", {
        class: "place" + (location.value.trim() === place.value ? " on" : ""),
        type: "button",
        title: place.value,
        text: place.name,
        onclick: () => {
          location.value = location.value.trim() === place.value ? "" : place.value;
          paintPlaces();
        },
      })));
    }
    location.addEventListener("input", paintPlaces);
    paintPlaces();

    const description = App.el("textarea", { class: "in", rows: "4", text: (detail && detail.description) || "" });
    // A rule the presets do not cover — "every Tuesday", "the last Friday of
    // the month" — gets an option of its own, carrying the original text.
    // Without it the select falls back to "Does not repeat", and saving an
    // unrelated edit silently deletes the recurrence.
    const rule = (detail && detail.rrule) || "";
    const known = REPEATS.some(([value]) => value === rule);
    const options = known ? REPEATS : [...REPEATS, [rule, `Repeats — ${rule}`]];
    const repeat = App.el("select", { class: "in" },
      options.map(([value, label]) => App.el("option", {
        value, text: label, selected: value === rule,
      })));
    const attendees = App.el("input", {
      class: "in", placeholder: App.state.meerail ? "Invite — starts typing from your mail" : "Invite — email addresses",
      value: (detail && detail.attendees || []).map((a) => a.email).join(", "),
    });
    const people = App.el("div", { class: "people" });
    if (App.state.meerail) App.contacts.attach(attendees, people);

    const error = App.el("div", { class: "modal-error", hidden: true });
    const note = App.el("div", { class: "modal-note" });
    if (detail && detail.rrule) note.textContent = "This repeats — a change here changes every occurrence.";
    if (readOnly) note.textContent = "This calendar is read-only.";

    const save = App.el("button", { class: "btn primary", text: event ? "Save" : "Create", disabled: readOnly });
    const remove = event
      ? App.el("button", { class: "btn danger", text: "Delete", disabled: readOnly })
      : null;

    async function submit() {
      error.hidden = true;
      const body = {
        calendar_id: Number(calSelect.value),
        title: title.value.trim() || "(no title)",
        // All-day sends dates, with DTEND the day after the last one — the
        // panel shows the last day, because that is what "until" means.
        start: when.allDay ? when.startDate : `${when.startDate}T${when.startTime}`,
        end: when.allDay ? nextDay(when.endDate) : `${when.endDate}T${when.endTime}`,
        all_day: when.allDay,
        location: location.value.trim(),
        description: description.value,
        rrule: repeat.value,
        attendees: attendees.value.split(",").map((s) => s.trim()).filter(Boolean)
          .map((email) => ({ email, status: "NEEDS-ACTION" })),
      };
      try {
        if (event) await App.api.patch(`/api/events/${event.event_id}`, body);
        else await App.api.post("/api/events", body);
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
        return;
      }
      close();
      App.shell.refresh();
    }

    save.onclick = submit;
    if (remove) {
      remove.onclick = async () => {
        if (!confirm("Delete this event?")) return;
        await App.api.del(`/api/events/${event.event_id}`);
        close();
        App.shell.refresh();
      };
    }

    const cal = event && App.state.calendar(event.cal);
    return App.el("div", { class: "modal-card", onclick: (e) => e.stopPropagation() },
      App.el("div", { class: "modal-head" },
        cal ? App.el("span", { class: "cal-dot", style: `--c:${cal.color}` }) : null,
        App.el("span", { class: "modal-title", text: event ? "Event" : "New event" }),
        App.el("button", { class: "icon-btn", html: App.icon("close"), onclick: close }),
      ),
      App.el("div", { class: "modal-body" },
        title,
        App.el("div", { class: "fld-row" },
          field("Calendar", calSelect),
          field("Repeats", repeat),
        ),
        App.el("div", { class: "fld" },
          App.el("span", { class: "fld-label", text: "When" }),
          whenRow,
          App.el("label", { class: "fld inline all-day" }, allDay, App.el("span", { text: "All day" })),
        ),
        App.el("div", { class: "fld" },
          App.el("span", { class: "fld-label", text: "Where" }),
          location,
          places,
        ),
        field("Invite", attendees, App.state.meerail ? "from the people you write to in meerail" : ""),
        people,
        field("Notes", description),
        note.textContent ? note : null,
        error,
      ),
      App.el("div", { class: "modal-foot" },
        remove,
        App.el("span", { class: "grow" }),
        App.el("button", { class: "btn", text: "Cancel", onclick: close }),
        save,
      ),
    );
  }

  function mount(card) {
    close();
    modal = App.el("div", { class: "modal-backdrop", onclick: close }, card);
    document.body.append(modal);
    const first = modal.querySelector("input");
    if (first) setTimeout(() => first.focus(), 0);
  }

  async function open(event) {
    current = event;
    let detail = null;
    try { detail = await App.api.get(`/api/events/${event.event_id}`); } catch (e) { /* keep the summary */ }
    mount(build(Object.assign({}, event, detail || {}), detail));
  }

  /* `at` is where the click landed — double-clicking 14:00 on a Tuesday should
     not then ask what time was meant. Passed into the panel rather than poked
     into it afterwards, so the pills are right on first paint. */
  function create(at) {
    const start = at || new Date(new Date().setMinutes(0, 0, 0) + 3600000);
    const end = new Date(start.getTime() + 3600000);
    mount(build(null, null, { start, end }));
  }

  return { open, create, close, get current() { return current; } };
})();

/* Attendee autocomplete, from meerail's address book. Silent when meerail is
   not configured: the field is still a field, it just stops guessing. */
App.contacts = {
  attach(input, list) {
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const term = input.value.split(",").pop().trim();
      if (term.length < 2) { list.replaceChildren(); return; }
      timer = setTimeout(async () => {
        let payload;
        try { payload = await App.api.get(`/api/contacts?q=${encodeURIComponent(term)}`); }
        catch (e) { return; }
        list.replaceChildren(...payload.people.map((p) => App.el("button", {
          class: "person",
          onclick: () => {
            const parts = input.value.split(",");
            parts[parts.length - 1] = ` ${p.email}`;
            input.value = parts.join(",").replace(/^\s+/, "") + ", ";
            list.replaceChildren();
            input.focus();
          },
        },
          App.el("span", { class: "person-name", text: p.name || p.email }),
          App.el("span", { class: "person-mail", text: p.email }),
          App.el("span", { class: "person-count", text: `${p.count}` }),
        )));
      }, 180);
    });
  },
};
