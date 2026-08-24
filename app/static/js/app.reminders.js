/* Reminders, in the browser: the bell in the event panel, and the in-app channel.

   The panel is the interesting half. A rule is a statement about a *kind* of
   event, and there is always an event that is the wrong kind: a daily "Lunch"
   matches `cal:work is:busy` as squarely as a client meeting does. So the event
   carries the last word, and this is where it is given.

   Each channel is one of three things, and **inherit is not the same as on**:
   inherit follows the rules, on and off do not. That is why the toggle has
   three positions rather than two: a mute has to stay a mute against rules
   that do not exist yet, and a two-position switch cannot say the difference
   between "off because I said so" and "off because nothing matched today".

   The row also shows what the channel resolves to right now and why, so the
   panel answers "what will this event actually do" before anything is touched.
   That makes it the quickest way to find out whether a rule does what you
   meant it to. */

window.App = window.App || {};

App.reminders = (() => {
  const STATES = ["inherit", "on", "off"];
  const LABEL = { inherit: "auto", on: "on", off: "off" };

  /* --- the bell in the event panel --------------------------------------- */

  async function section(eventId) {
    let data;
    try {
      data = await App.api.get(`/api/events/${eventId}/reminders`);
    } catch (e) {
      return null;          // no reminders configured, or the call failed
    }
    if (!data.channels.length) return null;

    // What is stored, per scope. Edits go into the map for the scope you are
    // in, never into the resolved view, which would bake an instance's
    // decision into the series the first time you touched anything.
    const stored = {
      series: { ...(data.series.channels || {}) },
      occurrence: { ...(data.occurrence.channels || {}) },
    };
    let scope = "series";
    const rows = App.el("div", { class: "rem-rows" });
    const status = App.el("span", { class: "rem-status" });

    async function save() {
      status.textContent = "saving…";
      try {
        await App.api.put(`/api/events/${eventId}/reminders`, {
          channels: stored[scope],
          scope,
        });
        status.textContent = "saved";
        setTimeout(() => { status.textContent = ""; }, 1400);
      } catch (err) {
        status.textContent = err.message;
      }
    }

    function paint() {
      rows.replaceChildren(...data.channels.map((ch) => {
        const set = stored[scope][ch.name] || "inherit";
        // What actually happens, which is not the same as what is set here:
        // with nothing set, it is whatever the rules say.
        const effective = set === "on" || (set === "inherit" && ch.effective);
        const why = set === "off" ? "muted here"
          : set === "on" ? "always, set here"
          : ch.effective ? `${ch.why}${ch.leads.length ? ` · ${ch.leads.join(", ")}` : ""}`
          : ch.why;

        const toggle = App.el("button", {
          class: `rem-toggle st-${set}`,
          type: "button",
          title: "auto follows your rules · on always · off never",
          text: LABEL[set],
          onclick: () => {
            stored[scope][ch.name] = STATES[(STATES.indexOf(set) + 1) % 3];
            if (stored[scope][ch.name] === "inherit") delete stored[scope][ch.name];
            paint();
            save();
          },
        });
        return App.el("div", { class: `rem-row${effective ? "" : " off"}` },
          App.el("span", { class: "rem-dot", "aria-hidden": "true" }),
          App.el("span", { class: "rem-name", text: ch.name }),
          App.el("span", { class: "rem-why", text: why }),
          toggle,
        );
      }));
    }
    paint();

    // Only a recurring event has a question to ask here. For anything else
    // there is exactly one thing the scope could mean, so it is not drawn.
    const scopeRow = data.recurring
      ? App.el("div", { class: "rem-scope" },
          ...[["series", "All events"], ["occurrence", "This event only"]].map(([key, text]) =>
            App.el("button", {
              class: `rem-scope-btn${scope === key ? " on" : ""}`,
              type: "button", text,
              onclick: (e) => {
                scope = key;
                e.currentTarget.parentElement
                  .querySelectorAll(".rem-scope-btn")
                  .forEach((b) => b.classList.toggle("on", b.textContent === text));
                paint();
              },
            })))
      : null;

    return App.el("div", { class: "fld rem-block" },
      App.el("span", { class: "fld-label" },
        App.el("span", { html: App.icon("bell", 14) }),
        App.el("span", { text: "Reminders" }),
        status),
      scopeRow,
      rows,
      data.alarms.length
        ? App.el("div", { class: "rem-note", text:
            `The calendar server also set ${data.alarms.length} alarm on this event.` })
        : null,
    );
  }

  /* --- the in-app channel ------------------------------------------------- */

  /* Reminders addressed to a channel of kind "app" are claimed from the same
     queue as every other channel, rather than being a second parallel way for
     a reminder to happen. It is also the only channel that works on a machine
     where the agent is not running.

     Deliberately *not* stood down by App.power: the whole point of this one is
     to fire while the window is behind something else. A browser will throttle
     the timer in a background tab, which is why the interval is short enough
     to survive being quartered. */
  const POLL_MS = 30000;
  let timer = null;
  let enabled = false;

  async function poll() {
    try {
      const { reminders } = await App.api.post("/api/reminders/claim", {});
      reminders.forEach(show);
    } catch (e) { /* the server will still have the row next time */ }
  }

  function show(reminder) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const note = new Notification(reminder.title, {
      body: reminder.body,
      tag: `meercal-${reminder.id}`,
      requireInteraction: true,
    });
    note.onclick = () => { window.focus(); note.close(); };
  }

  async function start() {
    let state;
    try {
      state = await App.api.get("/api/reminders?limit=1");
    } catch (e) { return; }
    enabled = (state.channels || []).some((c) => c.in_app);
    if (!enabled) return;
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (e) { /* denied */ }
    }
    clearInterval(timer);
    timer = setInterval(poll, POLL_MS);
    poll();
  }

  return { section, start };
})();
