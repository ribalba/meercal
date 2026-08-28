/* The event panel: read it, change it, or write a new one.

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

  /* Who is invited, one bubble each.

     This was a comma-separated text box, and the two things wrong with it were
     really the same thing: text holds an address and nothing else. A reply that
     had come back from the server -- Anna accepted, Bo declined -- had nowhere
     to be drawn, and saving rebuilt every attendee out of that text, which
     threw the reply away and reset everyone to NEEDS-ACTION. A bubble is a
     *person*: it carries whatever the server last said about them, shows it,
     and hands it back on save untouched. */
  const PARTSTAT = {
    "ACCEPTED": { cls: "yes", says: "Accepted" },
    "DECLINED": { cls: "no", says: "Declined" },
    "TENTATIVE": { cls: "maybe", says: "Answered tentatively" },
    "DELEGATED": { cls: "maybe", says: "Delegated to someone else" },
    "NEEDS-ACTION": { cls: "wait", says: "Not answered yet" },
  };

  /* Deliberately loose. An internal address has no dot in it (`bo@intranet` is
     a real address on plenty of networks) and a field that rejects those is
     wrong more often than one that lets a typo through to a bounce. */
  const LOOKS_LIKE_MAIL = /^[^\s@,;<>]+@[^\s@,;<>]+$/;

  function attendeeField(initial, placeholder, readOnly) {
    const people = (initial || []).map((p) => Object.assign({}, p));
    const listeners = [];
    /* A read-only calendar still shows who is on the event and who accepted --
       that is worth reading either way -- but not the controls for changing it.
       An × that cannot be saved is worse than no × at all. */
    const input = App.el("input", {
      class: "token-in", placeholder: readOnly ? "" : placeholder,
      autocomplete: "off", disabled: readOnly || null, hidden: readOnly || null,
    });
    const box = App.el("div", {
      class: "in tokens",
      /* It is drawn as a text field, so it has to behave like one: a click on
         the empty part of it lands in the input rather than nowhere. Guarded on
         the target, or a click meant for a bubble's × would be stolen. */
      onmousedown: (e) => {
        if (readOnly || e.target !== box) return;
        e.preventDefault();
        input.focus();
      },
    }, input);

    const held = (email) => people.some((p) => p.email.toLowerCase() === String(email).trim().toLowerCase());

    function bubble(person, index) {
      const state = PARTSTAT[String(person.status || "").toUpperCase()] || PARTSTAT["NEEDS-ACTION"];
      const shown = person.name || person.email;
      /* A CalDAV server is entitled to identify a guest by a principal URI
         rather than an address -- iCloud does, and it is 80 characters of
         base64 -- so the hover shows the address only when there is one worth
         reading. The name is what the bubble says either way. */
      const address = LOOKS_LIKE_MAIL.test(person.email) && person.email !== shown ? person.email : "";
      return App.el("span", {
        class: `tok st-${state.cls}`,
        // The two things a bubble cannot fit: the reply in words, and the
        // address behind a name that is only a display name.
        title: address ? `${address} · ${state.says}` : `${shown} · ${state.says}`,
      },
        App.el("span", {
          class: "tok-mark", "aria-hidden": "true",
          html: state.cls === "yes" ? App.icon("check", 11) : "",
        }),
        App.el("span", { class: "tok-name", text: shown }),
        readOnly ? null : App.el("button", {
          class: "tok-x", type: "button", title: `Remove ${shown}`,
          "aria-label": `Remove ${shown}`, html: App.icon("close", 11),
          onclick: () => { people.splice(index, 1); draw(); input.focus(); },
        }),
      );
    }

    function draw() {
      box.querySelectorAll(".tok").forEach((node) => node.remove());
      /* Inserted before the input rather than through replaceChildren: that
         would take the focused input out of the document and put it back,
         which drops the caret in the middle of typing an address. */
      people.forEach((person, i) => box.insertBefore(bubble(person, i), input));
      listeners.forEach((fn) => fn());
    }

    /* One entry, as a bubble. False when it is not an address, which is the
       caller's cue to leave it in the box where it can be fixed. A duplicate is
       not a failure: the person is already invited, which is what was asked
       for, so the entry is swallowed and the field moves on. */
    function add(entry, name) {
      let text = String(entry || "").trim();
      // "Anna Meier <anna@example.com>", which is what a copy out of a mail
      // client gives you, and quoted when the name has a comma in it.
      const angled = /^(.*)<([^>]*)>$/.exec(text);
      if (angled) {
        name = name || angled[1].trim().replace(/^["']|["']$/g, "").trim();
        text = angled[2].trim();
      }
      const email = text.replace(/^mailto:/i, "").trim();
      if (!email) return true;
      if (held(email)) return true;
      if (!LOOKS_LIKE_MAIL.test(email)) return false;
      people.push({ email, name: name || "", status: "NEEDS-ACTION", role: "REQ-PARTICIPANT" });
      draw();
      return true;
    }

    /* Whatever is half-typed, turned into bubbles. Returns the first entry it
       could not read, which is also what stays in the box: an address with a
       typo in it should be visible and fixable, and never silently dropped
       because Save happened to be the next thing clicked. */
    function commit() {
      const parts = input.value.split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean);
      const bad = parts.filter((part) => !add(part));
      input.value = bad.join(", ");
      box.classList.toggle("bad", bad.length > 0);
      return bad[0] || "";
    }

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === "," || e.key === ";" || e.key === "Tab") {
        if (!input.value.trim()) return;      // Tab out of an empty box still tabs
        e.preventDefault();
        commit();
        return;
      }
      // Backspace in an empty box takes the last one back, which is what every
      // other address field on the machine does.
      if (e.key === "Backspace" && !input.value && people.length) {
        e.preventDefault();
        people.pop();
        draw();
      }
    });
    // Leaving the field commits too: a typed address with the panel's Save
    // clicked straight after is the single most likely way to use this.
    input.addEventListener("blur", commit);
    input.addEventListener("paste", (e) => {
      const text = (e.clipboardData || window.clipboardData || { getData: () => "" }).getData("text");
      if (!text || !/[,;\n]/.test(text)) return;   // one address types itself in
      e.preventDefault();
      input.value += text;
      commit();
    });

    draw();
    return {
      node: box,
      input,
      add,
      commit,
      has: held,
      focus: () => input.focus(),
      clear: () => { input.value = ""; box.classList.remove("bad"); },
      typed: () => input.value.trim(),
      emails: () => people.map((p) => p.email),
      // Copies, and the whole person: PARTSTAT, the display name, the CUTYPE
      // that says this one is a room. All of it goes back to the server.
      values: () => people.map((p) => Object.assign({}, p)),
      onChange: (fn) => listeners.push(fn),
    };
  }

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
    // Shown as the last day rather than the exclusive end; see nextDay().
    if (event && event.all_day) end = T().addDays(end, -1);

    const title = App.el("input", { class: "in title-in", value: event ? event.title : "", placeholder: "Title" });
    /* Which calendar, in the calendar's own colour. The colour is how every
       other view says which calendar something is in, so the one control that
       *decides* that is the last place it should be missing: the dot beside
       the list follows the choice, and each name is written in its own hue for
       the browsers that will draw an option that way. */
    const calSelect = App.el("select", { class: "in" },
      cals.map((c) => App.el("option", {
        value: String(c.id), selected: event && event.cal === c.id, text: c.name,
        style: `color:${c.color}`,
      })));
    const calDot = App.el("span", { class: "cal-dot" });
    const paintCalDot = () => {
      const chosen = cals.find((c) => String(c.id) === calSelect.value) || cals[0];
      calDot.style.setProperty("--c", chosen ? chosen.color : "#888");
    };
    calSelect.addEventListener("change", paintCalDot);
    paintCalDot();
    const calField = App.el("div", { class: "cal-pick" }, calDot, calSelect);
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
       a short list in practice (the office, the room, the same meeting link)
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
    // A rule the presets do not cover ("every Tuesday", "the last Friday of
    // the month") gets an option of its own, carrying the original text.
    // Without it the select falls back to "Does not repeat", and saving an
    // unrelated edit silently deletes the recurrence.
    const rule = (detail && detail.rrule) || "";
    const known = REPEATS.some(([value]) => value === rule);
    const options = known ? REPEATS : [...REPEATS, [rule, `Repeats: ${rule}`]];
    const repeat = App.el("select", { class: "in" },
      options.map(([value, label]) => App.el("option", {
        value, text: label, selected: value === rule,
      })));
    const invite = attendeeField((detail && detail.attendees) || [],
      App.state.meerail ? "Invite: starts typing from your mail" : "Invite: email addresses",
      readOnly);
    const people = App.el("div", { class: "people" });
    // The offers, and the one line that explains an empty field. Both are
    // built whether or not meerail is configured, because the panel's layout
    // should not depend on which half of the suite is installed.
    const withRow = App.el("div", { class: "with-row" });
    const peopleNote = App.el("div", { class: "people-note", hidden: true });
    if (App.state.meerail && !readOnly) App.contacts.attach(invite, people, withRow, peopleNote);

    const error = App.el("div", { class: "modal-error", hidden: true });
    const note = App.el("div", { class: "modal-note" });
    if (detail && detail.rrule) note.textContent = "This repeats: a change here changes every occurrence.";
    if (readOnly) note.textContent = "This calendar is read-only.";

    const save = App.el("button", { class: "btn primary", text: event ? "Save" : "Create", disabled: readOnly });
    const remove = event
      ? App.el("button", { class: "btn danger", text: "Delete", disabled: readOnly })
      : null;

    async function submit() {
      error.hidden = true;
      // An address still being typed counts as invited: nobody expects Save to
      // drop the name they just wrote because they did not press Enter first.
      const stray = invite.commit();
      if (stray) {
        error.textContent = `Not an email address: ${stray}`;
        error.hidden = false;
        invite.focus();
        return;
      }
      const body = {
        calendar_id: Number(calSelect.value),
        title: title.value.trim() || "(no title)",
        // All-day sends dates, with DTEND the day after the last one. The
        // panel shows the last day, because that is what "until" means.
        start: when.allDay ? when.startDate : `${when.startDate}T${when.startTime}`,
        end: when.allDay ? nextDay(when.endDate) : `${when.endDate}T${when.endTime}`,
        all_day: when.allDay,
        location: location.value.trim(),
        description: description.value,
        rrule: repeat.value,
        attendees: invite.values(),
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

    const reminderSlot = App.el("div", { class: "rem-slot" });
    if (event && App.reminders) {
      App.reminders.section(event.event_id)
        .then((node) => { if (node) reminderSlot.replaceChildren(node); })
        .catch(() => {});
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
          field("Calendar", calField),
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
        /* A div rather than field()'s label: a label wrapping the bubbles would
           put a click on someone's × through to the input as well. */
        readOnly && !invite.emails().length ? null : App.el("div", { class: "fld" },
          App.el("span", { class: "fld-label", text: "Invite" }),
          invite.node,
          App.state.meerail && !readOnly
            ? App.el("span", { class: "fld-hint", text: "from the people you write to in meerail" })
            : null,
        ),
        people,
        withRow,
        peopleNote,
        field("Notes", description),
        // Built after the card is on screen: it needs a round trip to resolve
        // what the rules currently say about this event, and the panel must
        // not wait on that before drawing the parts it already knows.
        reminderSlot,
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

  /* `at` is where the click landed: double-clicking 14:00 on a Tuesday should
     not then ask what time was meant. Passed into the panel rather than poked
     into it afterwards, so the pills are right on first paint.

     `until` is the other end, for the gesture that drew one: a drag down the
     week grid has already said how long the thing is, and asking again with a
     default hour would be ignoring the answer. */
  function create(at, until) {
    const start = at || new Date(new Date().setMinutes(0, 0, 0) + 3600000);
    const end = until && until > start ? until : new Date(start.getTime() + 3600000);
    mount(build(null, null, { start, end }));
  }

  return { open, create, close, get current() { return current; } };
})();

/* Attendee autocomplete, from meerail's address book, and the row of offers
   that follows from it: once one person is on the invitation, meerail's
   co-recipient graph already knows who normally comes with them, and the same
   five names every week is exactly the typing worth not doing.

   Silent when meerail is not configured: the field is still a field, it just
   stops guessing. Deliberately *not* silent when meerail is configured and does
   not answer -- a field that quietly offers nobody looks identical to an
   address book with nobody in it, which is the whole reason this had to be
   debugged from the outside rather than read off the screen. */
App.contacts = {
  attach(invite, list, chips, note) {
    let timer = null;
    let matchSeq = 0;        // the typeahead's race guard
    let relatedSeq = 0;      // and the suggestion row's
    let relatedKey = null;   // who was last asked about: same people, same answer

    /* The people already on the invitation. The bubbles and nothing else: what
       is under the caret is the typeahead's business and not a person yet. */
    const entered = () => invite.emails();

    /* Both endpoints answer with the same shape, so one place decides whether
       there is something wrong to say. */
    function say(payload) {
      const message = payload && payload.error
        ? `meerail did not answer: ${payload.error}` : "";
      note.textContent = message;
      note.hidden = !message;
    }

    function refresh() {
      lookup();
      related();
    }

    async function lookup() {
      const term = invite.typed();
      if (term.length < 2) { list.replaceChildren(); return; }
      const seq = ++matchSeq;
      let payload;
      try { payload = await App.api.get(`/api/contacts?q=${encodeURIComponent(term)}`); }
      catch (e) { return; }
      if (seq !== matchSeq) return;         // a later keystroke won the race
      say(payload);
      list.replaceChildren(...payload.people.map((p) => App.el("button", {
        class: "person",
        type: "button",
        // The typeahead finishes the address being typed, so what was typed
        // goes and a bubble takes its place -- carrying the name from the
        // address book, which is what the bubble is then labelled with.
        onclick: () => {
          invite.add(p.email, p.name);
          invite.clear();
          list.replaceChildren();
          invite.focus();
        },
      },
        App.el("span", { class: "person-name", text: p.name || p.email }),
        App.el("span", { class: "person-mail", text: p.email }),
        App.el("span", { class: "person-count", text: `${p.count}` }),
      )));
    }

    async function related() {
      const people = entered();
      const key = people.join(",");
      if (key === relatedKey) return;       // the same people, so the same answer
      relatedKey = key;
      if (!people.length) { chips.replaceChildren(); return; }

      const seq = ++relatedSeq;
      let payload;
      try {
        payload = await App.api.get("/api/contacts/related?"
          + people.map((e) => `address=${encodeURIComponent(e)}`).join("&"));
      } catch (e) {
        relatedKey = null;                  // let the next keystroke try again
        return;
      }
      if (seq !== relatedSeq) return;
      say(payload);
      // The endpoint already leaves out whoever is invited, but it matches on
      // the exact address: a bubble that differs only in case would come back
      // as an offer to invite someone who is standing right there.
      const offers = payload.people.filter((p) => !invite.has(p.email));
      if (!offers.length) { chips.replaceChildren(); return; }
      chips.replaceChildren(
        App.el("span", { class: "with-label", text: "Usually with" }),
        ...offers.map((p) => App.el("button", {
          class: "with-chip",
          type: "button",
          title: `Invite ${p.email}`,
          // An offer adds a person: unlike the typeahead it is not finishing
          // what is being typed, so a half-written address is left alone.
          onclick: () => { invite.add(p.email, p.name); invite.focus(); },
        },
          App.el("span", { class: "with-plus", text: "+" }),
          App.el("span", { class: "with-name", text: p.name || p.email }),
        )),
      );
    }

    invite.input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(refresh, 180);
    });
    /* A bubble arriving or leaving changes who is worth suggesting exactly as
       typing does. Short debounce rather than none: adding one person from the
       "usually with" row is usually followed by adding another. */
    invite.onChange(() => {
      clearTimeout(timer);
      timer = setTimeout(related, 120);
    });
    // An event being edited arrives with people already on it, and those are
    // the best seeds there are: ask before a key is ever pressed.
    related();
  },
};
