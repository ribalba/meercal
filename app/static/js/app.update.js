/* "A newer meercal is out": the strip in the sidebar.

   The server does the actual checking (app/updates.py); this only renders the
   verdict. That split is deliberate: the browser never talks to github, so one
   install makes one request a day however many tabs are open, and turning the
   check off in meercal.toml genuinely turns it off rather than just hiding the
   banner.

   Deliberately quiet. It is a strip under the calendars, not a modal. Nothing
   here is urgent, and an update prompt that interrupts somebody reading their
   week earns itself a permanent dismissal. Dismissing pins the *version* that
   was waved away, so the next release says its piece and this one stays quiet.

   It boots itself rather than being called from the shell: it needs nothing
   from the rest of the app beyond the API client, and a module that has to be
   remembered somewhere else is a module that stops working when that somewhere
   else is rewritten. */

window.App = window.App || {};

App.update = (() => {
  // The version already waved away. Per browser, like every other local
  // preference; "0.4.0" means "stop telling me about 0.4.0".
  const KEY = "meercal.update.dismissed";

  // The page stays open for days at a time, so a check on boot alone would
  // never fire on the machine most likely to have fallen behind. The server
  // caches for a day regardless: this is how often we ask it what it knows,
  // not how often it asks github.
  const RECHECK = 6 * 3600 * 1000;

  let info = null;

  function dismissed() {
    try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }

  function dismiss(version) {
    try { localStorage.setItem(KEY, version); } catch (e) { /* private mode */ }
    render();
  }

  function render() {
    const box = document.getElementById("update-notice");
    if (!box) return;

    const show = Boolean(info && info.update_available && dismissed() !== info.latest);
    box.hidden = !show;
    if (!show) { box.replaceChildren(); return; }

    box.replaceChildren(
      // The link goes to the README's "Updating", not the releases page:
      // whoever clicks this wants the command for their install, not a
      // changelog.
      App.el("a", {
        class: "un-text",
        href: info.update_url,
        target: "_blank",
        rel: "noopener noreferrer",
        title: `meercal ${info.latest} is out; you are running ${info.version}. How to update.`,
      },
        App.el("span", { class: "un-icon", html: App.icon("download", 13) }),
        App.el("span", { class: "un-label", text: `Update available: ${info.latest}` }),
      ),
      App.el("button", {
        class: "un-close",
        type: "button",
        title: "Dismiss until the next release",
        "aria-label": "Dismiss",
        html: App.icon("close", 12),
        onclick: () => dismiss(info.latest),
      }),
    );
  }

  async function refresh() {
    try {
      info = await App.api.get("/api/version");
    } catch (e) {
      // Offline, or a server too old to have the endpoint. Neither is worth a
      // word on screen.
      return;
    }
    render();
  }

  /* "Which version am I running" is the first question of every bug report, so
     it is answerable without an update being available: the line under the
     shortcuts says it whatever the verdict. */
  function line() {
    if (!info) return "";
    if (!info.check_enabled) return `meercal ${info.version}`;
    if (info.update_available) return `meercal ${info.version}, ${info.latest} is out`;
    if (info.latest) return `meercal ${info.version}, up to date`;
    // No `latest` with checks on means the first check has not come back yet,
    // or could not: say nothing rather than claim to be current.
    return `meercal ${info.version}`;
  }

  function init() {
    // Not awaited: the boot path should not wait on this, and there is nothing
    // to show until it comes back anyway.
    refresh();
    setInterval(refresh, RECHECK);
  }

  window.addEventListener("DOMContentLoaded", init);

  return { init, refresh, line, current: () => info };
})();
