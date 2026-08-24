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
  /* Always H:MM, never a bare hour. "10" beside a title reads as a number in
     the title — a count, an issue, a room — and only "10:00" reads as a time.
     The two characters are worth it. */
  time(d) {
    return `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
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
      places: s.places || [],
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


/* The mouse wheel, as a way of moving through time.

   In the grid views there is nothing below the fold worth a wheel of its own —
   a month is a month — so the wheel moves to the next one. The week and day
   grids *are* scrollable, though: the hours have to stay reachable. So the rule
   is "scroll first, then step": the wheel scrolls the hour grid until it runs
   out, and only a wheel at the edge changes the date. That is the same
   overscroll gesture a phone uses to page, and it means neither behaviour costs
   the other.

   The Ribbon never gets this: it is one continuous scroll by design, and there
   is no "next period" to move to. */
App.wheel = {
  // One notch on a mouse is ~100; trackpads send a stream of small deltas. The
  // threshold plus the cooldown is what stops a flick from crossing a year.
  THRESHOLD: 80,
  COOLDOWN: 420,

  /* Attach once, for the life of the page. The accumulator and the cooldown
     live in the closure, and a step re-renders the view — so re-attaching per
     render handed every wheel event a fresh cooldown of zero, and one flick
     walked through six months. */
  attach(el, onStep) {
    let acc = 0;
    let until = 0;
    el.onwheel = (e) => {
      if (e.ctrlKey || e.metaKey) return;                 // pinch-zoom
      const dir = Math.sign(e.deltaY);
      if (!dir) return;
      if (this.scrollable(e.target, dir, el)) { acc = 0; return; }
      e.preventDefault();
      const now = performance.now();
      if (now < until) return;
      acc += e.deltaY;
      if (Math.abs(acc) < this.THRESHOLD) return;
      acc = 0;
      until = now + this.COOLDOWN;
      onStep(dir);
    };
  },

  /* Something between the pointer and the view that can still move this way.
     "Still" is the operative word: a grid scrolled to its bottom is not
     scrollable downwards, which is exactly when the wheel should page. */
  scrollable(node, dir, stop) {
    while (node && node !== document.body) {
      const style = getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) {
        const atTop = node.scrollTop <= 0;
        const atBottom = node.scrollTop + node.clientHeight >= node.scrollHeight - 1;
        if (dir < 0 ? !atTop : !atBottom) return node;
      }
      if (node === stop) break;
      node = node.parentElement;
    }
    return null;
  },
};


/* Icons, as inline SVG.

   The chrome used typographic stand-ins for a while — ⌕ for search, ↻ for
   refresh — and they are a poor deal: the glyph is whatever the user's font
   decides it is, it sits on the text baseline rather than in the middle of its
   button, and ⌕ in particular renders as an unrecognisable blob in most UI
   fonts. These are drawn instead, in one weight, and they take `currentColor`
   so they follow whatever the button is already doing about hover and theme. */
App.icons = {
  search: '<circle cx="11" cy="11" r="7"/><line x1="16.6" y1="16.6" x2="21" y2="21"/>',
  refresh: '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><polyline points="20.5 3.5 20.5 9 15 9"/>',
  // A disc lit from one side: light, dark, or whatever the system says.
  theme: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 0 0 17z" fill="currentColor" stroke="none"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><line x1="12" y1="11" x2="12" y2="16.5"/>'
      + '<circle cx="12" cy="7.7" r="1" fill="currentColor" stroke="none"/>',
  close: '<line x1="6.5" y1="6.5" x2="17.5" y2="17.5"/><line x1="17.5" y1="6.5" x2="6.5" y2="17.5"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  left: '<polyline points="14.5 5 8 12 14.5 19"/>',
  right: '<polyline points="9.5 5 16 12 9.5 19"/>',
  pencil: '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z"/><line x1="14.5" y1="7.5" x2="17.5" y2="10.5"/>',
  solo: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/>',
  menu: '<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/>'
      + '<line x1="4" y1="17" x2="20" y2="17"/>',
};

App.icon = (name, size = 17) => {
  const body = App.icons[name];
  if (!body) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
};

/* Fill everything carrying `data-icon`. Called once at boot, and again by
   anything that builds buttons of its own after it. */
App.paintIcons = (root = document) => {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = App.icon(el.dataset.icon, Number(el.dataset.iconSize) || 17);
  });
};
