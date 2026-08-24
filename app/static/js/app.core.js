/* meercal core: the App namespace — API client, time helpers, shared state.

   One rule runs through this file: **the browser does no timezone
   arithmetic.** The server sends wall-clock strings already in the display
   zone ("2026-08-24T09:00:00", no offset), and everything here treats them as
   the numbers they are. A Date is only ever built from those components, so
   the calendar draws the same hours whatever zone the laptop is in. */

window.App = window.App || {};

// --- events between modules ------------------------------------------------
App.bus = {
  handlers: {},
  on(name, fn) { (this.handlers[name] ||= []).push(fn); },
  emit(name, payload) { (this.handlers[name] || []).forEach((fn) => fn(payload)); },
};

// --- API -------------------------------------------------------------------
App.api = {
  async request(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const response = await fetch(path, opts);
    if (response.status === 401) {
      await this.promptLogin();
      return this.request(method, path, body);
    }
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (e) { /* not JSON */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  },
  get(path) { return this.request("GET", path); },
  post(path, body) { return this.request("POST", path, body); },
  patch(path, body) { return this.request("PATCH", path, body); },
  put(path, body) { return this.request("PUT", path, body); },
  del(path) { return this.request("DELETE", path); },

  // One overlay and one promise however many requests hit 401 at once — which
  // is what happens when a month-long session expires mid-use and four panes
  // reload together.
  promptLogin() {
    if (this._login) return this._login;
    const overlay = document.getElementById("login-overlay");
    const form = document.getElementById("login-form");
    const input = document.getElementById("login-password");
    const error = document.getElementById("login-error");
    this._login = new Promise((resolve) => {
      overlay.hidden = false;
      input.value = "";
      error.hidden = true;
      setTimeout(() => input.focus(), 0);
      form.onsubmit = async (e) => {
        e.preventDefault();
        try {
          await fetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: input.value }),
          }).then((r) => { if (!r.ok) throw new Error("Wrong password"); });
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
          input.select();
          return;
        }
        overlay.hidden = true;
        this._login = null;
        resolve();
      };
    });
    return this._login;
  },
};

// --- time ------------------------------------------------------------------
App.time = {
  /* A wall-clock string from the server as a Date carrying those exact
     components. Not `new Date(str)`: that is the same thing in every browser
     that matters, but only by convention, and this is the one place where
     being wrong is invisible until a DST weekend. */
  parse(s) {
    if (!s) return null;
    const [date, time = "00:00:00"] = s.split("T");
    const [y, m, d] = date.split("-").map(Number);
    const [hh, mm, ss] = time.split(":").map(Number);
    return new Date(y, m - 1, d, hh || 0, mm || 0, ss || 0);
  },
  ymd(d) {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  },
  iso(d) { return `${this.ymd(d)}T${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:00`; },
  day(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); },
  addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n, d.getHours(), d.getMinutes()); },
  addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, d.getDate()); },
  /* Whole days between two dates, by date only — the count the span bars are
     laid out from, so it must not be a millisecond division: an hour of DST
     inside the interval would round it to the wrong integer. */
  daysBetween(a, b) {
    const ms = this.day(b) - this.day(a);
    return Math.round(ms / 86400000);
  },
  startOfWeek(d, weekStart) {
    const iso = d.getDay() === 0 ? 7 : d.getDay();   // 1 Mon … 7 Sun
    let back = iso - weekStart;
    if (back < 0) back += 7;
    return this.addDays(this.day(d), -back);
  },
  isoWeek(d) {
    const t = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const dayNum = (t.getDay() + 6) % 7;
    t.setDate(t.getDate() - dayNum + 3);
    const first = new Date(t.getFullYear(), 0, 4);
    const week = 1 + Math.round(((t - first) / 86400000 - 3 + ((first.getDay() + 6) % 7)) / 7);
    return { year: t.getFullYear(), week };
  },
  isToday(d) { return this.ymd(d) === this.ymd(new Date()); },
  time(d) {
    const h = d.getHours(), m = d.getMinutes();
    return m ? `${h}:${String(m).padStart(2, "0")}` : `${h}`;
  },
  weekday(d, long) {
    return d.toLocaleDateString(undefined, { weekday: long ? "long" : "short" });
  },
  monthName(d) { return d.toLocaleDateString(undefined, { month: "long", year: "numeric" }); },
};

// --- shared state ----------------------------------------------------------
App.state = {
  ready: false,
  view: "ribbon",
  cursor: App.time.day(new Date()),   // the date the view is centred on
  filter: "",
  regex: false,
  calendars: [],
  sets: [],
  accounts: [],
  events: [],
  range: null,
  prefs: { density: "comfortable", collapseQuiet: true, showFree: true },
  weekStart: 1,
  dayStart: 8,
  dayEnd: 20,

  calendar(id) { return this.calendars.find((c) => c.id === id); },
  visibleIds() { return this.calendars.filter((c) => c.visible).map((c) => c.id); },
};

// --- loading ---------------------------------------------------------------
App.load = {
  async state() {
    const s = await App.api.get("/api/state");
    Object.assign(App.state, {
      calendars: s.calendars,
      sets: s.sets,
      accounts: s.accounts,
      weekStart: s.week_start,
      dayStart: s.day_start,
      dayEnd: s.day_end,
      timezone: s.timezone,
      version: s.version,
      meerail: s.meerail,
    });
    App.state.prefs = Object.assign(App.state.prefs, s.prefs || {});
    if (!App.state.ready) App.state.view = App.state.prefs.view || s.default_view;
    App.state.ready = true;
    App.bus.emit("state");
    return s;
  },

  /* Everything overlapping [start, end). One request per repaint, whatever the
     number of calendars — the server expands recurrence into rows so that this
     is a range scan and not twenty rule engines. */
  async events(start, end) {
    const params = new URLSearchParams({
      start: App.time.iso(start),
      end: App.time.iso(end),
    });
    if (App.state.filter) params.set("q", App.state.filter);
    if (App.state.regex) params.set("regex", "1");
    const payload = await App.api.get(`/api/events?${params}`);
    App.state.events = payload.events;
    App.state.range = { start, end };
    App.bus.emit("events", payload);
    return payload;
  },

  async prefs() {
    App.state.prefs.view = App.state.view;
    clearTimeout(this._prefTimer);
    // Debounced: density and collapse are toggled with a key that repeats.
    this._prefTimer = setTimeout(
      () => App.api.put("/api/prefs", { value: App.state.prefs }).catch(() => {}),
      600,
    );
  },
};

// --- small helpers ---------------------------------------------------------
App.el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "style") node.setAttribute("style", v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  children.flat().forEach((c) => c && node.append(c.nodeType ? c : document.createTextNode(c)));
  return node;
};

/* A colour that stays readable on the calendar's own background, for text and
   chips drawn *in* a calendar's colour rather than filled with it. */
App.tint = (hex, alpha) => {
  const h = (hex || "#1d6ff2").replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
};
