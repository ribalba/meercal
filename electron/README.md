# meercal desktop

A thin Electron wrapper that opens the meercal web app in a native window. It
needs the **server running** (`make up` in the repo root) and, for anything to
appear in it, the **agent** syncing your calendars.

Same shape as [meerail's](https://github.com/ribalba/meerail) desktop shell on
purpose: the two live on one desktop and should behave the same way.

## Run in development

```bash
cd electron
npm install
npm start                                  # loads http://localhost:8010
MEERCAL_URL=http://host:8010 npm start     # or a remote server
```

## Build installers

```bash
npm run dist        # -> dist/  (macOS .dmg/.zip, Linux .AppImage/.deb, Windows .exe)
```

## Install on Linux (KDE / GNOME)

```bash
make distinstall    # build, then register with the desktop
make distuninstall  # remove it again
```

`distinstall` builds the AppImage and installs it for the current user (no root):

| what | where |
| --- | --- |
| AppImage | `~/.local/share/meercal/meercal.AppImage` |
| CLI symlink | `~/.local/bin/meercal` |
| launcher | `~/.local/share/applications/meercal.desktop` |
| icon | `~/.local/share/icons/hicolor/512x512/apps/meercal.png` |

It then refreshes the desktop/icon caches (`update-desktop-database`,
`gtk-update-icon-cache`, `kbuildsycoca6`), so the app shows up in the KDE and
GNOME menus right away. The launcher pins the server URL, so pass it in if it
isn't the default: `make distinstall MEERCAL_URL=http://meercal.local:8010`.
`StartupWMClass=meercal` keeps the window grouped with the launcher in the task
bar/dock.

## What the shell adds over a browser tab

- **A window of its own**, with the app's own icon in the dock and the task bar.
- **Foreground signalling.** It dispatches `meercal:focus` / `meercal:blur` at
  the page, which is the one thing a browser cannot tell it: a window sitting
  behind another is not hidden, but it is not the app you are in either. The
  page stands its polling down while it is back there and refreshes when it
  comes forward; see `app/static/js/app.power.js`.
- **Spell checking** in the event panel's title, location and notes fields, with
  a context menu of suggestions (`MEERCAL_SPELLCHECK_LANGS=en-GB,de-DE`).
- **Outbound links** open in the system browser rather than in a window with no
  address bar.
- **A retry screen** when the server is not up, instead of a blank window.

`electron-builder` targets are configured in `package.json`. The app icon is
`build/icon.png` (1024×1024, which is what macOS wants). Code signing and
notarization are not configured; add your certificates for distributable macOS
builds.
