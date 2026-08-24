/* Keyboard. The program is for people who would rather not reach for a mouse,
   so every view-level thing has a key, and the cheat sheet in the sidebar is
   generated from the same table that binds them — a shortcut cannot drift out
   of step with its own documentation. */

window.App = window.App || {};

App.keys = (() => {
  // Order is the cheat sheet's order, and the cheat sheet shows the first nine
  // until it is opened — so the ones you reach for most come first. Today
  // leads: it is the only key you press from anywhere in the calendar.
  const BINDINGS = [
    { key: "t", label: "Today", run: () => App.shell.today() },
    { key: "r", label: "Ribbon", run: () => App.shell.setView("ribbon") },
    { key: "w", label: "Week", run: () => App.shell.setView("week") },
    { key: "m", label: "Month", run: () => App.shell.setView("month") },
    { key: "d", label: "Day", run: () => App.shell.setView("day") },
    { key: "y", label: "Year", run: () => App.shell.setView("year") },
    { key: "←/→", label: "Back / forward", match: (e) => e.key === "ArrowLeft" || e.key === "ArrowRight",
      run: (e) => App.shell.step(e.key === "ArrowLeft" ? -1 : 1) },
    // `c`, which is what every calendar binds it to; `n` keeps working for
    // anyone who learned it here first.
    { key: "c", label: "New event", match: (e) => e.key === "c" || e.key === "n",
      run: () => App.editor.create() },
    { key: "/", label: "Filter", run: () => document.getElementById("filter-input").focus() },
    { key: "0–9", label: "Calendar set", match: (e) => /^[0-9]$/.test(e.key),
      run: (e) => {
        const set = App.state.sets.find((s) => s.hotkey === Number(e.key));
        if (set) App.shell.applySet(set.id);
        // 0 means everything even when no set has claimed it — the one key
        // that should always get you back to seeing the lot.
        else if (e.key === "0") App.shell.setVisible(App.state.calendars.map((c) => c.id));
      } },
    { key: "q", label: "Quiet days", run: () => {
      App.state.prefs.collapseQuiet = !App.state.prefs.collapseQuiet;
      App.load.prefs();
      App.shell.refresh();
    } },
    { key: "g 1–12", label: "Jump to a month", match: () => false },
    { key: ".", label: "Sync now", run: () => document.getElementById("btn-refresh").click() },
    { key: "?", label: "Shortcuts", run: () => document.getElementById("shortcut-box").classList.toggle("open") },
  ];

  /* `g` then a month number.

     Two-digit months are the whole difficulty: after `g 1` the user may mean
     January, or may be halfway through October. So a leading 1 waits a moment
     for a second digit and any other digit resolves at once — which makes
     `g 9` instant and `g 12` cost one short pause, rather than making every
     jump wait. */
  const JUMP_WINDOW = 2000;    // how long `g` stays armed
  const SECOND_DIGIT = 650;    // how long a leading 1 waits for its partner
  let jump = null;             // { buffer, timer, armedAt }

  /* What the sequence looks like while it is being typed. A mode with nothing
     on screen is a mode that feels like the keyboard has stopped working —
     especially this one, where the next keypress means something different
     from usual. */
  function paintHint(text) {
    const hint = document.getElementById("key-hint");
    if (!hint) return;
    hint.hidden = !text;
    if (text) hint.innerHTML = text;
  }

  function cancelJump() {
    if (!jump) return;
    clearTimeout(jump.timer);
    jump = null;
    paintHint("");
  }

  function monthName(month) {
    return new Date(2000, month - 1, 1).toLocaleDateString(undefined, { month: "long" });
  }

  function goToMonth(month) {
    cancelJump();
    if (month < 1 || month > 12) return;
    const target = new Date(App.state.cursor.getFullYear(), month - 1, 1);
    // The year view already shows every month at once, so jumping within it
    // would do nothing visible; that is the one case worth changing view for.
    App.shell.goTo(target, App.state.view === "year" ? "month" : undefined);
  }

  /* Jump, but leave the confirmation up long enough to be read. The move
     itself is immediate; only the hint lingers. */
  function goToMonthSoon(month) {
    const target = new Date(App.state.cursor.getFullYear(), month - 1, 1);
    App.shell.goTo(target, App.state.view === "year" ? "month" : undefined);
    clearTimeout(jump.timer);
    jump.timer = setTimeout(cancelJump, 700);
  }

  function jumpKey(e) {
    if (!jump) {
      if (e.key !== "g") return false;
      jump = { buffer: "", timer: setTimeout(cancelJump, JUMP_WINDOW) };
      paintHint('<kbd>g</kbd><span>Go to month — type <b>1</b>–<b>12</b></span>');
      return true;
    }
    if (!/^[0-9]$/.test(e.key)) { cancelJump(); return false; }
    clearTimeout(jump.timer);
    jump.buffer += e.key;
    const month = Number(jump.buffer);
    if (jump.buffer === "1") {
      // Ambiguous for a moment: 1, or the start of 10/11/12. Say so, rather
      // than looking like nothing happened.
      paintHint('<kbd>g 1</kbd><span>January — or keep typing for <b>10</b>, <b>11</b>, <b>12</b></span>');
      jump.timer = setTimeout(() => goToMonth(1), SECOND_DIGIT);
      return true;
    }
    if (month < 1 || month > 12) {
      paintHint(`<kbd>g ${jump.buffer}</kbd><span>No such month</span>`);
      jump.timer = setTimeout(cancelJump, 900);
      return true;
    }
    paintHint(`<kbd>g ${jump.buffer}</kbd><span>${monthName(month)}</span>`);
    goToMonthSoon(month);
    return true;
  }

  function typing(target) {
    return target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" ||
                      target.tagName === "SELECT" || target.isContentEditable);
  }

  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape") {
      cancelJump();
      App.shell.closeDrawer();
      App.editor.close();
      const filter = document.getElementById("filter-input");
      if (document.activeElement === filter) filter.blur();
      return;
    }
    if (typing(e.target)) return;
    // The `g` sequence gets first refusal: while it is armed a digit is a month
    // number, not a calendar set.
    if (jumpKey(e)) { e.preventDefault(); return; }
    for (const binding of BINDINGS) {
      const hit = binding.match ? binding.match(e) : e.key === binding.key;
      if (hit) { e.preventDefault(); binding.run(e); return; }
    }
  }

  function cheatSheet() {
    const box = document.getElementById("shortcut-box");
    if (!box) return;
    const toggle = () => {
      box.classList.toggle("open");
      more.textContent = box.classList.contains("open") ? "less" : `${BINDINGS.length - 9} more`;
    };
    // The rest are a click as well as a keypress: `?` is only discoverable to
    // somebody who already knows to look for it.
    const more = App.el("button", { class: "shortcut-more", text: `${BINDINGS.length - 9} more`,
                                    onclick: toggle });
    box.replaceChildren(
      App.el("div", { class: "shortcut-title", text: "Shortcuts" }),
      ...BINDINGS.map((b) => App.el("div", { class: "shortcut-row" },
        App.el("kbd", { text: b.key }),
        App.el("span", { text: b.label }),
      )),
      BINDINGS.length > 9 ? more : null,
    );
  }

  function init() {
    document.addEventListener("keydown", onKey);
    cheatSheet();
  }

  return { init, BINDINGS };
})();
