# Mixtape Bug Hunt — Submission

## Milestone 1: Codebase Map

### Main files and their roles

**`app.py`** — Flask application factory (`create_app`). Configures the SQLAlchemy database URI (SQLite by default, overridable via `DATABASE_URL`), initializes the shared `db` object, registers the four blueprints (`songs`, `playlists`, `users`, `feed`) under their URL prefixes, and calls `db.create_all()` on startup. This is the single entry point that wires the rest of the app together — nothing else constructs the Flask app.

**`models.py`** — Defines all SQLAlchemy models and the association (join) tables:
- `User` — has a `listening_streak` counter and `last_listened_at` timestamp used by the streak feature, plus relationships to songs shared, ratings given, listening events, notifications, playlists created, and friends.
- `Song` — a shared track, owned by the user who shared it (`shared_by`), with relationships to ratings, listening events, and tags.
- `Tag` — a many-to-many label on songs via the `song_tags` join table.
- `ListeningEvent` — one row per "user listened to a song" action, timestamped. This is the raw event log that streaks and feeds are derived from.
- `Rating` — a user's 1–5 score for a song. Has a unique constraint on `(user_id, song_id)`, so a user can only have one rating per song (re-rating updates it rather than creating a new row).
- `Playlist` — has a `songs` relationship through `playlist_entries`, a join table that (unlike a plain many-to-many) also stores `position`, `added_by`, and `added_at` — so playlists track *order* and *provenance* of each song, not just membership.
- `Notification` — a per-user record with a `notification_type`, free-text `body`, and `read` flag.
- `friendships` — a many-to-many self-join on `User`. It is symmetric but not automatically bidirectional at the DB level: the seed script inserts both `(A, B)` and `(B, A)` rows explicitly, meaning any code path that adds a friendship is responsible for inserting both directions itself.

**`routes/`** — one Flask blueprint per resource area. Every route function follows the same shape: parse the request (path params, query string, or JSON body), delegate to a function in `services/`, and translate the result (or a raised `ValueError`) into a JSON response with an appropriate status code (400/404 for `ValueError`s, 200/201 for success). No business logic lives in routes.
- `routes/songs.py` — search, song detail, rating, and recording a listen.
- `routes/playlists.py` — create playlist, get playlist detail, list songs in a playlist, add a song to a playlist.
- `routes/users.py` — user profile, streak lookup, notification list, mark-as-read.
- `routes/feed.py` — "friends listening now" and general activity feed.

**`services/`** — all business logic. Routes never touch the database directly (except a couple of simple `db.session.get(User, ...)` lookups in `routes/users.py`); everything else goes through a service function.
- `streak_service.py` — `record_listening_event()` writes a `ListeningEvent` and then calls `update_listening_streak()`, which compares the calendar date of the new listen to `user.last_listened_at` to decide whether to leave the streak alone (already listened today), increment it (listened yesterday), or reset it to 1 (gap of more than a day, or no prior listen).
- `feed_service.py` — `get_friends_listening_now()` looks up the current user's friends, pulls `ListeningEvent`s for those friends within a trailing time window, and collapses them to one (most recent) entry per friend. `get_activity_feed()` is a simpler, non-time-filtered "last N events across all friends" feed.
- `search_service.py` — `search_songs()` matches a query string against song title/artist (case-insensitive) and returns full song dicts including tags; `get_song()` fetches a single song by ID.
- `notification_service.py` — the only place `Notification` rows are created, via a shared `create_notification(user_id, type, body)` helper. This module contains two higher-level actions with the same shape (look up entities, validate, mutate, commit): `add_to_playlist()` (adds a song to a playlist, and calls `create_notification()` to notify the original sharer) and `rate_song()` (saves or updates a user's `Rating` for a song). This module also owns notification retrieval (`get_notifications`) and marking as read (`mark_as_read`).
- `playlist_service.py` — `create_playlist()`, `get_playlist()` (metadata only), `get_playlist_songs()` (songs in playlist order, joined through `playlist_entries` and sorted by `position`), and `get_user_playlists()`.

**`seed_data.py`** — populates a fresh database with 5 users, friendships, tags, songs (with varying tag counts), playlists, listening events spanning roughly two weeks, and some pre-existing streaks/notifications. This is the fixture data all manual testing and reproduction is done against — useful to read when trying to reproduce an issue, since it tells you which users/songs already have the preconditions a bug needs (e.g., a user with an existing streak, a song with 3+ tags).

**`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`. Existing automated coverage for three of the five affected areas.

### Data flow: adding a shared song to a playlist → notification

This is the clearest end-to-end flow in the app and a good model for how the rest of the notification system is meant to work.

1. Client sends `POST /playlists/<playlist_id>/songs` with a JSON body of `{song_id, added_by}`.
2. `routes/playlists.py::add_song()` parses the body, checks that both fields are present, and calls `notification_service.add_to_playlist(playlist_id, song_id, added_by)`. Note that this route imports from `notification_service`, not `playlist_service` — adding a song to a playlist is treated as a notification-producing action first, and playlist mutation happens as a side effect inside that same service call.
3. `add_to_playlist()` loads the `Song`, the adding `User`, and the `Playlist`, raising `ValueError` (→ 400 in the route) if any are missing.
4. If the song isn't already in `playlist.songs`, it's appended via the SQLAlchemy relationship (which writes a row into the `playlist_entries` join table) and committed.
5. If the person who added the song is not the same person who originally shared it (`song.shared_by != added_by_user_id`), `create_notification()` is called with `notification_type="song_added_to_playlist"` and a formatted body naming the adder, the song, and the playlist. This writes a new `Notification` row for `song.shared_by`.
6. The recipient later reads it via `GET /users/<user_id>/notifications`, which goes through `routes/users.py::notifications()` → `notification_service.get_notifications()`, returning all (or only unread) notifications ordered newest-first.

`create_notification()` is the single write path for the `Notification` table — any action meant to notify a user is expected to route through it, the way `add_to_playlist()` does.

### Patterns noticed

- **Thin routes, fat services.** Every route's job is limited to request parsing, calling exactly one service function, and shaping the response. All validation and persistence logic lives in `services/`.
- **`ValueError` as the cross-layer error contract.** Services raise plain `ValueError` for "not found" or "invalid input" conditions; routes catch `ValueError` uniformly and convert it to a 400 or 404 JSON response. There's no custom exception hierarchy.
- **UUID primary keys everywhere.** Every model uses a string UUID (`generate_uuid()`) as its primary key rather than an auto-incrementing integer.
- **Association tables carry extra columns when the relationship needs metadata.** `song_tags` and `friendships` are plain many-to-many tables, but `playlist_entries` adds `position`, `added_by`, and `added_at` because playlist membership needs ordering and provenance that a bare join table can't express.
- **Timestamps are timezone-aware UTC via `lambda: datetime.now(timezone.utc)`** as the column default, consistently across models.
- **One shared notification constructor.** `create_notification()` is the single write path for the `Notification` table; any code that wants to notify a user is expected to funnel through it rather than constructing a `Notification` directly.

## Milestone 1: Environment Setup

Confirmed the app boots and serves real requests from seeded data:
- `python seed_data.py` populated 5 users, playlists, and songs successfully.
- `FLASK_APP=app:create_app flask run` served `GET /users/<id>`, `GET /songs/search?q=a`, and `GET /users/<id>/streak`, all returning 200 with expected JSON shapes.
- Working branch `bugfix/mixtape` already exists and is checked out.

## Milestone 1: Issue Triage (Pre-Milestone 2 Plan)

All five issue reports (streak reset, stale "listening now" feed, duplicate search results, missing rating notification, missing last playlist song) have been read in full. Based on the affected-service mapping in the README, my working plan is to attempt all five if time allows, prioritizing:

1. Issue #1 (streak) — `streak_service.py`
2. Issue #3 (search duplicates) — `search_service.py`
3. Issue #5 (missing last playlist song) — `playlist_service.py`
4. Issue #4 (missing rating notification) — `notification_service.py` (stretch)
5. Issue #2 (stale listening-now feed) — `feed_service.py` (stretch)

This ordering is provisional and will be finalized in Milestone 2 after reproducing each issue.
