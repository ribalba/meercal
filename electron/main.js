/* meercal desktop shell.
 *
 * A thin Electron wrapper around the local meercal server: it loads the web app
 * in a native window, opens outbound links in the system browser, and shows a
 * retry screen if the server isn't running. Its shape is meerail's, because the
 * two sit side by side on one desktop and should behave the same way.
 *
 * Point it at a non-default server with MEERCAL_URL:
 *   MEERCAL_URL=http://localhost:8011 npm start
 *
 * Override the spellchecker languages with MEERCAL_SPELLCHECK_LANGS:
 *   MEERCAL_SPELLCHECK_LANGS=en-GB,fr npm start
 */
const { app, BrowserWindow, shell, Menu, MenuItem } = require("electron");
const path = require("path");

const APP_URL = (process.env.MEERCAL_URL || "http://localhost:8010").replace(/\/+$/, "");
const APP_ORIGIN = new URL(APP_URL).origin;

// Chromium checks every language in this list at once, so an event written in
// German and a note written in English are both checked without a manual
// switch. Titles, locations and notes are the typed surfaces here.
const SPELLCHECK_LANGS = (process.env.MEERCAL_SPELLCHECK_LANGS || "en-US,de-DE")
  .split(",").map((l) => l.trim()).filter(Boolean);

let mainWindow = null;

function isInternal(targetUrl) {
  try {
    return new URL(targetUrl).origin === APP_ORIGIN;
  } catch {
    return false;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 760,
    minHeight: 520,
    backgroundColor: "#ffffff",
    title: "meercal",
    icon: path.join(__dirname, "build", "icon.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      partition: "persist:meercal",
      spellcheck: true,
    },
  });

  setUpSpellCheck(mainWindow);

  // The window's session is persistent, so its HTTP cache outlives a restart:
  // an asset cached before the server started sending Cache-Control stays put
  // until its heuristic freshness runs out, and a shell running half-old js
  // against a half-new server fails in confusing ways. The server is local, so
  // refetching the asset set on launch costs nothing worth keeping.
  const win = mainWindow;
  win.webContents.session.clearCache()
    .finally(() => { if (!win.isDestroyed()) win.loadURL(APP_URL); });

  // Links to other origins open in the system browser, never a child window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (e, url) => {
    if (!isInternal(url)) {
      e.preventDefault();
      shell.openExternal(url);
    }
  });

  // If the server is down, show a retry screen instead of a blank window.
  mainWindow.webContents.on("did-fail-load", (_e, errorCode, _desc, validatedURL) => {
    if (errorCode === -3 || !isInternal(validatedURL || APP_URL)) return;
    mainWindow.loadURL(errorPage());
  });

  trackForeground(mainWindow);

  mainWindow.on("closed", () => { mainWindow = null; });
}

/* Tell the page when the window stops being the one in front, so it can stand
 * its polling down (see app.power.js).
 *
 * The page can already see `visibilitychange`, but that only fires for a window
 * that is minimised or hidden. A window sitting fully visible behind another is
 * still not the app the user is in, which is the ordinary case on a desktop,
 * and a calendar left open behind a browser polls all day for a screen nobody
 * is looking at. Only the shell knows about it, so the shell says so.
 *
 * Sent with executeJavaScript rather than IPC because this window has no
 * preload script: adding one to carry a single boolean would be a larger change
 * to the security surface than the message is worth.
 */
function signalForeground(win, state) {
  if (!win || win.isDestroyed()) return;
  win.webContents
    .executeJavaScript(`window.dispatchEvent(new Event("meercal:${state}"))`)
    // The page may be mid-navigation, or be the error screen, which has no
    // listener and does not need one. Never worth failing a window event over.
    .catch(() => {});
}

function trackForeground(win) {
  win.on("focus", () => signalForeground(win, "focus"));
  win.on("blur", () => signalForeground(win, "blur"));
  // Minimise and hide raise `visibilitychange` in the page by themselves on
  // most platforms, but not all, and a restore does not reliably raise `focus`.
  // Saying it a second time is free: App.power ignores a resume it is not
  // suspended for, and re-suspending an already-suspended app is a no-op.
  win.on("show", () => signalForeground(win, "focus"));
  win.on("restore", () => signalForeground(win, "focus"));
  win.on("hide", () => signalForeground(win, "blur"));
  win.on("minimize", () => signalForeground(win, "blur"));
  // A reload takes the page's listeners with it, and the shell's events do not
  // fire again just because the document changed. Re-state the current answer,
  // or a window reloaded while in the background would come back polling.
  win.webContents.on("did-finish-load",
    () => signalForeground(win, win.isFocused() ? "focus" : "blur"));
}

/* Spell checking for the event panel.
 *
 * Chromium fetches the Hunspell dictionary for each language on first use and
 * caches it under the user data dir, so the first run needs network access; a
 * language whose dictionary hasn't arrived yet simply isn't checked. macOS uses
 * the OS spellchecker instead, which manages its own languages: setting the
 * list there is a no-op, so we skip it.
 *
 * Chromium marks misspellings but leaves the correction UI to the app, hence
 * the context menu below. Replacing the default menu means the ordinary
 * clipboard items have to be re-stated, which is what the tail of it is.
 */
function setUpSpellCheck(win) {
  const session = win.webContents.session;

  if (process.platform !== "darwin") {
    const available = session.availableSpellCheckerLanguages;
    const langs = SPELLCHECK_LANGS.filter((l) => available.includes(l));
    const unknown = SPELLCHECK_LANGS.filter((l) => !available.includes(l));
    if (unknown.length) {
      console.warn(`spellcheck: ignoring unsupported language(s) ${unknown.join(", ")}`);
    }
    if (langs.length) session.setSpellCheckerLanguages(langs);
  }

  win.webContents.on("context-menu", (_e, params) => {
    const menu = new Menu();

    for (const suggestion of params.dictionarySuggestions) {
      menu.append(new MenuItem({
        label: suggestion,
        click: () => win.webContents.replaceMisspelling(suggestion),
      }));
    }
    if (params.misspelledWord) {
      if (params.dictionarySuggestions.length) menu.append(new MenuItem({ type: "separator" }));
      menu.append(new MenuItem({
        label: "Add to Dictionary",
        click: () => session.addWordToSpellCheckerDictionary(params.misspelledWord),
      }));
      menu.append(new MenuItem({ type: "separator" }));
    }

    menu.append(new MenuItem({ role: "cut", enabled: params.editFlags.canCut }));
    menu.append(new MenuItem({ role: "copy", enabled: params.editFlags.canCopy }));
    menu.append(new MenuItem({ role: "paste", enabled: params.editFlags.canPaste }));
    menu.append(new MenuItem({ type: "separator" }));
    menu.append(new MenuItem({ role: "selectAll" }));

    menu.popup({ window: win });
  });
}

function errorPage() {
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8" />
    <style>
      body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f7;color:#1d1d1f}
      .card{text-align:center;max-width:440px;padding:2rem}
      h1{font-size:1.3rem;margin:0 0 .5rem}p{color:#57606a;line-height:1.5}
      button{font:inherit;font-weight:600;cursor:pointer;border:none;border-radius:8px;
        padding:.7rem 1.3rem;background:#1d6ff2;color:#fff;margin-top:1rem}
      code{background:#e6e8eb;padding:.15rem .4rem;border-radius:5px}
    </style></head><body><div class="card">
      <h1>Can't reach the meercal server</h1>
      <p>Couldn't connect to <code>${APP_ORIGIN}</code>. Start it with
      <code>make up</code> in the repo and try again.</p>
      <button onclick="location.href='${APP_URL}'">Retry</button>
    </div></body></html>`;
  return "data:text/html;charset=utf-8," + encodeURIComponent(html);
}

function buildMenu() {
  const isMac = process.platform === "darwin";
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(isMac ? [{ role: "appMenu" }] : []),
    { role: "fileMenu" },
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        { label: "Home", accelerator: "CmdOrCtrl+Shift+H",
          click: () => mainWindow && mainWindow.loadURL(APP_URL) },
        { role: "reload" }, { role: "forceReload" }, { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { type: "separator" },
        { role: "togglefullscreen" }, { role: "toggleDevTools" },
      ],
    },
    { role: "windowMenu" },
  ]));
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    buildMenu();
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
