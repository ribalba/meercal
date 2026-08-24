/* Calendar sets: creating them, and changing them afterwards.

   A set is the answer to having twenty calendars: you do not think in
   calendars, you think in situations, and a set is one of those with a number
   key on it. Which means a set is something you *keep*, and anything you keep
   has to be editable: renaming it, moving its key, adding the calendar you
   only started using last week.

   Clicking a set in the sidebar applies it. Everything else lives behind the
   pencil, so that the common action stays one click and the rare one is still
   there. */

window.App = window.App || {};

App.sets = (() => {
  let modal = null;

  function close() {
    if (modal) modal.remove();
    modal = null;
  }

  function mount(card) {
    close();
    modal = App.el("div", { class: "modal-backdrop", onclick: close }, card);
    document.body.append(modal);
    const first = modal.querySelector("input");
    if (first) setTimeout(() => { first.focus(); first.select(); }, 0);
  }

  /* `set` is null for a new one, which starts from whatever is on screen:
     making a set is nearly always "these, the ones I am looking at". */
  function open(set) {
    const editing = Boolean(set);
    const chosen = new Set(editing ? set.calendars : App.state.visibleIds());
    const taken = new Map(
      App.state.sets.filter((s) => s.hotkey !== null && (!editing || s.id !== set.id))
        .map((s) => [s.hotkey, s.name]),
    );

    const name = App.el("input", {
      class: "in", value: editing ? set.name : "", placeholder: "Work, Family, the release…",
    });
    const error = App.el("div", { class: "modal-error", hidden: true });

    const keys = App.el("div", { class: "key-row" },
      [null, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map((key) => {
        const label = key === null ? "–" : String(key);
        const owner = key !== null && taken.get(key);
        return App.el("button", {
          class: "key-btn" + ((editing ? set.hotkey : null) === key ? " on" : ""),
          type: "button",
          dataset: { key: key === null ? "" : String(key) },
          title: owner ? `Currently ${owner}'s; this takes it`
                       : key === null ? "No key"
                       : key === 0 ? "0: the key that always means everything"
                       : `Press ${key}`,
          onclick: (ev) => {
            modal.querySelectorAll(".key-btn").forEach((b) => b.classList.remove("on"));
            ev.currentTarget.classList.add("on");
          },
        }, App.el("span", { text: label }),
           owner ? App.el("span", { class: "key-owner", text: owner }) : null);
      }),
    );

    const list = App.el("div", { class: "pick-list" },
      App.state.calendars.map((cal) => App.el("label", { class: "pick" },
        App.el("input", {
          type: "checkbox", checked: chosen.has(cal.id),
          onchange: (ev) => (ev.currentTarget.checked ? chosen.add(cal.id) : chosen.delete(cal.id)),
        }),
        App.el("span", { class: "cal-dot", style: `--c:${cal.color}` }),
        App.el("span", { class: "cal-name", text: cal.name }),
      )),
    );

    async function save() {
      const chosenKey = modal.querySelector(".key-btn.on");
      // `dataset.key` is "" for no key and "0" for the zero key, and 0 is
      // falsy, so this has to test the string rather than the number.
      const hotkey = chosenKey && chosenKey.dataset.key !== "" ? Number(chosenKey.dataset.key) : null;
      const body = { name: name.value.trim(), hotkey, calendars: [...chosen] };
      if (!body.name) {
        error.textContent = "A set needs a name";
        error.hidden = false;
        return;
      }
      try {
        if (editing) {
          await App.api.patch(`/api/sets/${set.id}`, { ...body, clear_hotkey: hotkey === null });
        } else {
          await App.api.post("/api/sets", body);
        }
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
        return;
      }
      close();
      await App.load.state();
      App.shell.renderSidebar();
    }

    async function remove() {
      if (!confirm(`Delete the set “${set.name}”? The calendars stay.`)) return;
      await App.api.del(`/api/sets/${set.id}`);
      close();
      await App.load.state();
      App.shell.renderSidebar();
    }

    mount(App.el("div", { class: "modal-card", onclick: (e) => e.stopPropagation() },
      App.el("div", { class: "modal-head" },
        App.el("span", { class: "modal-title", text: editing ? "Edit set" : "New set" }),
        App.el("button", { class: "icon-btn", html: App.icon("close"), onclick: close }),
      ),
      App.el("div", { class: "modal-body" },
        App.el("label", { class: "fld" }, App.el("span", { class: "fld-label", text: "Name" }), name),
        App.el("div", { class: "fld" },
          App.el("span", { class: "fld-label", text: "Key" }),
          keys,
          App.el("span", { class: "fld-hint", text: "Press it anywhere in the app to switch to this set." }),
        ),
        App.el("div", { class: "fld" },
          App.el("span", { class: "fld-label", text: `Calendars (${App.state.calendars.length})` }),
          list,
        ),
        error,
      ),
      App.el("div", { class: "modal-foot" },
        editing ? App.el("button", { class: "btn danger", text: "Delete", onclick: remove }) : null,
        App.el("span", { class: "grow" }),
        App.el("button", { class: "btn", text: "Cancel", onclick: close }),
        App.el("button", { class: "btn primary", text: editing ? "Save" : "Create", onclick: save }),
      ),
    ));
  }

  return { open, close };
})();
