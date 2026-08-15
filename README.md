# Day Ring — Productive Day Calculator

A daily habit tracker that scores your day as a single productivity percentage,
built from weighted habits across six categories: Mind, Body, Rest, Nutrition,
Discipline, and Social. Designed for students who want a whole-life view of
productivity, not just study hours.

## Setup

```bash
cd productive-day
pip install -r requirements.txt
python app.py
```

Then open `http://localhost:5050` in your browser. The SQLite database
(`productive_day.db`) is created automatically on first run.

## How scoring works

Each habit has a weight (they sum to 100 across the default set). Your daily
score is the sum of weights for completed habits, out of the total possible.
The habit list lives in the `habits` table — edit `DEFAULT_HABITS` in `app.py`
before first run to change categories, habits, or weights.

## What's here (v1)

- Daily checklist grouped by category
- "Day ring" visualization — concentric progress rings, one per category, with
  overall score in the center
- 14-day history view
- Daily reflection text field (stored, not yet analyzed)
- **Habit editor** (`/habits`) — add, rename, reweight, archive, or delete
  habits from the browser, no code edits needed. Categories are created
  freely; new ones get an automatically assigned color.
- **Insights page** (`/insights`) — correlates each category's daily
  completion % against your overall score (Pearson correlation), plus each
  category's running average. Needs at least 5 logged days before it shows
  real numbers; below that it tells you how many more days it needs.
- **Any-day logging** — use the &larr;/&rarr; arrows on the dashboard (or a
  history bar) to view or edit any past day, not just today. Handy for
  backfilling a day you forgot to log.
- **Streak badge** — shows your current run of consecutive days scoring 50%
  or higher.
- **Baseline progress bar** — tracks logged days toward a 15-day target,
  since that's roughly when the correlation/predictive ML features have
  enough history to say something real. Shown right on the dashboard so the
  payoff for logging consistently stays visible.
- **Encouragement message** — small reactive text under today's ring based
  on how the day's going so far.
- **Contribution heatmap** — History is now a GitHub-style grid instead of a
  bar chart, so a run of logged days is visually satisfying to build and easy
  to spot gaps in.
- **Reflection sentiment + tagging** — each saved reflection gets a
  sentiment score (via VADER) and is scanned for recurring themes (Fatigue,
  Distraction, Procrastination, Stress, Health, Social, Academics, Focus,
  Motivation). The Insights page then compares your score on days a theme
  came up versus days it didn't — e.g. "Fatigue: 17% on those days vs 92%
  otherwise." Works from the very first reflection; gets more reliable as
  themes repeat.
- **No-reload habit toggling** — checking a habit updates the ring, category
  bars, and habit state instantly via JavaScript (`static/app.js`), and saves
  in the background. No full page reload, so your scroll position never
  jumps while you're working down the list.
- **Suggested weights** (`/insights`) — turns the category correlations into
  an action instead of just a chart. Categories that move with your best
  days get more weight; ones that don't get less. One button applies the
  new weights to your habits. Needs the same 5-day minimum as the
  correlation feature, and comes with an honest caveat in the UI: a
  category's score is part of the overall score by construction, so this is
  a directional nudge, not proof.
- **Trend chart** (`/insights`) — an SVG line chart of your overall score
  over the last 30 logged days, so patterns are visible at a glance instead
  of buried in numbers.
- **Data export** (`/insights`) — download all your habits, daily logs, and
  reflections as CSV or JSON, scoped to your own account only.
- **Predictive model** (`/insights` + dashboard) — a real per-user linear
  regression (via numpy), trained on your reflection *sentiment* rather than
  the overall score itself, since the score is defined by the category
  weights and would make a model circular. Sentiment comes from your own
  words, so this is a genuinely independent read on which categories predict
  a positive-toned day. Needs 7 reflections with sentiment before it
  trains; until then it shows how many more are needed. Once trained, the
  dashboard shows a live "predicted reflection tone" for today.
- **Anomaly detection** — flags when today's score is statistically unusual
  compared to your own history (using a simple z-score, needs 7+ prior
  logged days), with a gentle, non-alarmist message either way — not a
  guilt trip on a low day, just a description.
- **Password reset + login safety** — registration now includes a security
  question, used to reset a forgotten password without needing email/SMTP
  setup. Login locks for 15 minutes after 5 failed attempts on an account.
  A logged-in user can change their password from `/account` (linked from
  their name in the top bar).
- **Weekly summary** — a narrated recap at the top of `/insights`: this
  week's average, how it compares to last week, and your strongest/weakest
  category, all generated from data the app already has.
- **Installable as a PWA** — `static/manifest.json` + `static/service-worker.js`
  (served from `/service-worker.js` so its scope covers the whole app, which
  Chrome's install prompt requires) make the site installable to a phone's
  home screen with its own icon and window, no app store needed. The service
  worker deliberately only caches static assets (CSS/JS/icons) — HTML pages
  with personal data are never cached, so nothing stale or another user's
  data can ever be served on a shared device.
- **Dark mode** — a toggle (🌙/☀️) in the top bar switches themes instantly,
  remembers your choice (`localStorage`), and defaults to your system's
  light/dark setting if you've never toggled it manually. This was a full
  color-token rewrite of the stylesheet (not just flipping the background),
  since the original design had many one-off hex colors for chips, buttons,
  and the heatmap that needed their own light/dark pairs to look right.

## Deploying for free (Render + Turso)

Running this on your own laptop only you can reach it. To share it with
other students at zero cost, host the app on Render's free tier and point it
at a free Turso database instead of the local SQLite file — Render's free
web services can't keep a persistent disk, so the database has to live
somewhere else that does.

1. **Create a free Turso database.** Sign up at turso.tech (no credit card),
   create a database, and get its connection URL and an auth token from the
   dashboard (or the `turso` CLI).
2. **Push this project to GitHub.**
3. **Create a Render web service** from that GitHub repo. Set:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
4. **Set three environment variables in Render's dashboard** (never commit
   these to git):
   - `SECRET_KEY` — any long random string
   - `TURSO_DATABASE_URL` — from step 1
   - `TURSO_AUTH_TOKEN` — from step 1
5. Deploy. The app detects those two Turso variables automatically and
   switches its database backend — no code changes needed. Locally, without
   those variables set, it keeps using the local SQLite file exactly as
   before.

Note: this was tested locally against the same compatibility layer that
talks to Turso (via libSQL's local file mode), but not against a live Turso
account, since that requires real credentials. The first deploy is the real
test — if something doesn't work, share the exact error and we'll fix it.



To put this under Git and push it to GitHub:

```bash
git init
git add .
git commit -m "Initial Day Ring app"
```

Then create an empty repo on GitHub and follow its "push an existing
repository" instructions (roughly `git remote add origin <url>` followed by
`git push -u origin main`). `.gitignore` already excludes the SQLite database,
`__pycache__`, and virtual environment folders, so your personal habit data
never gets committed.

## Roadmap (in build order)

1. **Rule-based baseline** — done (this is v1).
2. **Pattern/correlation discovery** — done. See `/insights`.
3. **Predictive personalized score** — done. See "Predictive model" above —
   trained on reflection sentiment, not the score itself, to avoid circularity.
4. **Anomaly/streak-break detection** — done. See "Anomaly detection" above.
5. **NLP on the reflection field** — done. See the "Reflection sentiment +
   tagging" feature above and the second section of `/insights`.

Every roadmap item is live. What's left is really just accumulating more
logged days and reflections — every ML feature here gets more reliable the
more it has to learn from, not because more code is needed.
