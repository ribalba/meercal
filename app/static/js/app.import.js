/* Dropping an .ics file on the window.

   A calendar file arrives with no opinion about where it belongs -- an export
   from a phone, an invitation someone mailed, the school year somebody
   published -- so the drop cannot be the whole gesture. It is the *question*:
   the file is read on the server, and what comes back ("42 events, January to
   December, it calls itself Holidays") is shown beside the list of calendars
   so that picking one is a decision made with the file in front of you.

   The default is a new calendar rather than whichever one happens to be first.
   Importing into the wrong calendar is tedious to undo -- forty events mixed
   into your work calendar have to be found before they can be removed -- and a
   new calendar is one tickbox away from invisible if it was a mistake.

   The overlay is drawn from a counter rather than a boolean. `dragleave` fires
   every time the pointer crosses into a child element, so a plain flag turns
   the overlay off the moment the file passes over a single event block. */

window.App = window.App || {};

App.importer = (() => {
  const ACCEPT = ".ics,.ical,.ifb,text/calendar";
  let modal = null;
  let depth = 0;

  function close() {
    if (modal) modal.remove();
    modal = null;
  }

  function mount(card) {
    close();
    modal = App.el("div", { class: "modal-backdrop", onclick: close }, card);
    document.body.append(modal);
    const first = modal.querySelector("input:not([type=radio]), .btn.primary");
    if (first) setTimeout(() => { first.focus(); if (first.select) first.select(); }, 0);
  }

  function card(title, body, foot) {
    return App.el("div", { class: "modal-card", onclick: (e) => e.stopPropagation() },
      App.el("div", { class: "modal-head" },
        App.el("span", { class: "modal-title", text: title }),
        App.el("button", { class: "icon-btn", html: App.icon("close"), onclick: close }),
      ),
      App.el("div", { class: "modal-body" }, body),
      App.el("div", { class: "modal-foot" }, App.el("span", { class: "grow" }), foot),
    );
  }

  function complain(message) {
    mount(card("Import",
      App.el("div", { class: "modal-error", text: message }),
      App.el("button", { class: "btn primary", text: "Close", onclick: close })));
  }

  // --- the overlay ----------------------------------------------------------

  /* Only a drag carrying files. Dragging an event inside the calendar, or a
     selection of text, must leave the overlay alone. */
  function carriesFiles(ev) {
    return Array.from(ev.dataTransfer ? ev.dataTransfer.types : []).includes("Files");
  }

  function overlay(on) {
    document.getElementById("drop-zone").hidden = !on;
  }

  function icsFiles(list) {
    return Array.from(list || []).filter(
      (f) => /\.(ics|ical|ifb|icalendar)$/i.test(f.name) || f.type === "text/calendar",
    );
  }

  // --- the dialog -----------------------------------------------------------

  function human(iso) {
    const d = App.time.parse(iso);
    return d ? d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : iso;
  }

  function summary(info) {
    const bits = [`${info.events} event${info.events === 1 ? "" : "s"}`];
    if (info.recurring) bits.push(`${info.recurring} repeating`);
    bits.push(info.first === info.last ? human(info.first) : `${human(info.first)} – ${human(info.last)}`);
    return bits.join(" · ");
  }

  function open(file, info, aside) {
    const writable = App.state.calendars.filter((c) => !c.read_only);
    const account = (cal) => App.state.accounts.find((a) => a.id === cal.account_id);
    const name = App.el("input", {
      class: "in", value: info.name || "Imported", maxlength: 300,
      // Typing a name is itself the choice: reaching for the radio first to be
      // allowed to name the thing is a step that exists for no reason.
      oninput: () => { fresh.checked = true; toggle(); },
    });
    const fresh = App.el("input", {
      type: "radio", name: "imp-target", value: "new", checked: true,
      onchange: () => toggle(),
    });
    const error = App.el("div", { class: "modal-error", hidden: true });
    const button = App.el("button", { class: "btn primary", text: "Import" });

    /* Read off the radios themselves rather than through the mounted modal:
       the note under the list is filled in before the card goes on screen, and
       going via `modal` meant reaching through a variable that is still null
       at that point. */
    function chosen() {
      const on = targets.querySelector("input[name=imp-target]:checked");
      return on ? on.value : "new";
    }

    /* The note under the list says what this import will actually do, which
       differs by target: a server calendar means the agent has to write the
       events out before they are anywhere but here. */
    const note = App.el("div", { class: "modal-note" });
    function toggle() {
      name.disabled = chosen() !== "new";
      const cal = chosen() === "new" ? null : App.state.calendar(Number(chosen()));
      const acct = cal && account(cal);
      const lines = [];
      if (acct && acct.kind !== "local") {
        lines.push(`These are written to ${acct.label} on the next sync pass, `
                 + "the same way an edit here is.");
      }
      if (info.outside_horizon) {
        lines.push(`${info.outside_horizon} of them fall outside ${human(info.horizon.from)} – `
                 + `${human(info.horizon.to)} and are stored but not drawn until the `
                 + "horizon reaches them.");
      }
      lines.push("An event already there with the same UID is updated, not added twice.");
      note.textContent = lines.join(" ");
    }

    /* The default first, and the existing calendars under it. With twenty
       calendars the list scrolls, and an option that has to be scrolled *to*
       is a poor thing to have selected on open: the box would look like it
       had nothing chosen at all. */
    const targets = App.el("div", { class: "pick-list import-targets" },
      App.el("label", { class: "pick" },
        fresh,
        App.el("span", { class: "pick-fixed", text: "New calendar" }),
        name,
      ),
      writable.map((cal) => App.el("label", { class: "pick" },
        App.el("input", {
          type: "radio", name: "imp-target", value: String(cal.id), onchange: toggle,
        }),
        App.el("span", { class: "cal-dot", style: `--c:${cal.color}` }),
        App.el("span", { class: "cal-name", text: cal.name }),
        App.el("span", { class: "muted small", text: (account(cal) || {}).label || "" }),
      )),
    );

    async function run() {
      const data = new FormData();
      data.append("file", file, file.name);
      if (chosen() === "new") {
        if (!name.value.trim()) {
          error.textContent = "A calendar needs a name";
          error.hidden = false;
          return;
        }
        data.append("new_calendar", name.value.trim());
      } else {
        data.append("calendar_id", chosen());
      }
      button.disabled = true;
      button.textContent = "Importing…";
      let result;
      try {
        result = await App.api.form("/api/import", data);
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
        button.disabled = false;
        button.textContent = "Import";
        return;
      }
      close();
      // A calendar that was just created has to exist in the sidebar before
      // the repaint, or its events are drawn in nobody's colour.
      await App.load.state();
      App.shell.renderSidebar();
      await App.shell.refresh();
      done(result);
    }
    button.onclick = run;

    toggle();
    mount(card("Import calendar",
      [
        App.el("div", { class: "fld" },
          App.el("span", { class: "import-file", text: file.name }),
          App.el("span", { class: "import-summary", text: summary(info) }),
          info.titles.length
            ? App.el("span", { class: "fld-hint",
                // The trailing ellipsis only when there is actually more: a
                // two-event file listing both and then trailing off is a lie
                // about how much was read.
                text: info.titles.join(", ") + (info.events > info.titles.length ? "…" : "") })
            : null,
          aside ? App.el("span", { class: "fld-hint warn", text: aside }) : null,
        ),
        App.el("div", { class: "fld" },
          App.el("span", { class: "fld-label", text: "Into" }),
          targets,
        ),
        note,
        error,
      ],
      [
        App.el("button", { class: "btn", text: "Cancel", onclick: close }),
        button,
      ]));
  }

  function done(result) {
    const cal = result.calendar;
    const parts = [];
    if (result.created) parts.push(`${result.created} added`);
    if (result.updated) parts.push(`${result.updated} updated`);
    mount(card("Imported",
      [
        App.el("div", { class: "fld inline" },
          App.el("span", { class: "cal-dot", style: `--c:${cal.color}` }),
          App.el("span", { class: "import-file", text: `${parts.join(", ")} in ${cal.name}` }),
        ),
        result.queued
          ? App.el("div", { class: "modal-note",
              text: `${result.queued} queued for the server; the agent writes them on its `
                  + "next pass. Press . to ask for one now." })
          : null,
      ],
      App.el("button", { class: "btn primary", text: "Done", onclick: close })));
  }

  // --- the way in -----------------------------------------------------------

  async function take(file, aside) {
    let info;
    try {
      const data = new FormData();
      data.append("file", file, file.name);
      info = await App.api.form("/api/import/preview", data);
    } catch (err) {
      complain(err.message);
      return;
    }
    open(file, info, aside);
  }

  function drop(files) {
    const ics = icsFiles(files);
    if (!ics.length) {
      complain("That is not a calendar file. Drop an .ics.");
      return;
    }
    // One file, one question about where it goes. Two files usually belong in
    // two different calendars, and a dialog that assumed otherwise would be
    // wrong in the case that matters. Said in the dialog rather than in a box
    // of its own: the answer is to drop the other one next, not to read a
    // warning first.
    take(ics[0], ics.length > 1
      ? `${ics.length - 1} other file${ics.length > 2 ? "s were" : " was"} dropped too; `
        + "drop them again one at a time."
      : "");
  }

  /* The file dialog, for when the file is not somewhere you can drag it from
     -- and because a gesture with nothing on screen to suggest it is a feature
     only the person who wrote it knows about. Reached from the sidebar. */
  function choose() {
    const input = App.el("input", { type: "file", accept: ACCEPT, style: "display:none" });
    input.onchange = () => {
      const file = input.files && input.files[0];
      input.remove();
      if (file) take(file);
    };
    document.body.append(input);
    input.click();
  }

  function init() {
    window.addEventListener("dragenter", (ev) => {
      if (!carriesFiles(ev)) return;
      ev.preventDefault();
      depth += 1;
      overlay(true);
    });
    window.addEventListener("dragover", (ev) => {
      if (!carriesFiles(ev)) return;
      // Without this the browser navigates to the file, which loses whatever
      // was on screen and shows the raw iCalendar text. Both halves are
      // needed: `dragover` is what actually permits the drop.
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
    });
    window.addEventListener("dragleave", (ev) => {
      if (!carriesFiles(ev)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) overlay(false);
    });
    window.addEventListener("drop", (ev) => {
      if (!carriesFiles(ev)) return;
      ev.preventDefault();
      depth = 0;
      overlay(false);
      drop(ev.dataTransfer.files);
    });
    // A drag that ends outside the window never fires `drop`, and the counter
    // would keep the overlay up over a calendar nobody can click.
    window.addEventListener("dragend", () => { depth = 0; overlay(false); });
  }

  return { init, choose, close };
})();
