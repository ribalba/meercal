# meercal.com

The marketing site. A static page and a stylesheet, served by nginx. Nothing to
build.

```bash
cd website
docker compose up --build -d      # http://127.0.0.1:8081
```

The landing styles are the meerail site's, which are in turn meerato's, so the
three read as one family. Keep the shared part of `public/css/site.css`
identical between them and put anything specific under the
`===== meercal additions =====` heading at the foot of the file, so a fix to one
site should be copyable to the others without a diff to read.

## Screenshots

They are captured from a *running* meercal, not mocked, so a shot that renders
here is one the app actually produces:

```bash
make -C .. up          # the app
make -C .. seed        # the demo calendars
../.venv/bin/pip install playwright && ../.venv/bin/playwright install chromium
../.venv/bin/python screenshots/shoot.py            # all of them
../.venv/bin/python screenshots/shoot.py --only ribbon --dark
```

Output lands in `public/img/screenshots/` at 2x device scale: the page serves
2880x1800 files and displays them at half that, so they stay sharp on retina
panels. `--dark` writes the `-dark` variants the page offers through
`<picture><source media="(prefers-color-scheme: dark)">`.

The demo data is placed relative to *today*, so re-shooting months later gives
the same picture with current dates.

## Deploying

The image is plain nginx with `public/` copied in, so anything that runs a
container will do:

```bash
docker build -t meercal-website .
docker run -p 80:80 meercal-website
```

Put TLS in front of it. Nothing here is dynamic and nothing is stored, so the
container is disposable.
