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

  function build(event, detail) {
    const cals = writableCalendars();
    const readOnly = event && event.read_only;
    const start = event ? T().parse(event.start) : new Date();
    const end = event ? T().parse(event.end) : new Date(start.getTime() + 3600000);

    const title = App.el("input", { class: "in title-in", value: event ? event.title : "", placeholder: "Title" });
    const calSelect = App.el("select", { class: "in" },
      cals.map((c) => App.el("option", { value: String(c.id), selected: event && event.cal === c.id, text: c.name })));
    const allDay = App.el("input", { type: "checkbox", checked: event ? event.all_day : false });
    const startIn = App.el("input", { type: "datetime-local", class: "in", value: localValue(start) });
    const endIn = App.el("input", { type: "datetime-local", class: "in", value: localValue(end) });
    const location = App.el("input", { class: "in", value: event ? event.location || "" : "", placeholder: "Where" });
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
        start: startIn.value,
        end: endIn.value,
        all_day: allDay.checked,
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
        App.el("button", { class: "icon-btn", text: "✕", onclick: close }),
      ),
      App.el("div", { class: "modal-body" },
        title,
        App.el("div", { class: "fld-row" },
          field("Calendar", calSelect),
          field("Repeats", repeat),
        ),
        App.el("label", { class: "fld inline" }, allDay, App.el("span", { text: "All day" })),
        App.el("div", { class: "fld-row" }, field("Start", startIn), field("End", endIn)),
        field("Where", location),
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

  function create(when) {
    const start = when || new Date(new Date().setMinutes(0, 0, 0) + 3600000);
    const end = new Date(start.getTime() + 3600000);
    mount(build(null, null));
    // Fill the times we were opened with — double-clicking 14:00 on a Tuesday
    // should not then ask what time you meant.
    const [startIn, endIn] = modal.querySelectorAll('input[type="datetime-local"]');
    startIn.value = localValue(start);
    endIn.value = localValue(end);
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
