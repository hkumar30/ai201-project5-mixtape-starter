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
- **Single write path for notifications.** Anything that wants to notify a user is expected to go through `create_notification()` rather than building a `Notification` object itself.

## Milestone 1: Environment Setup

Confirmed the app boots and serves real requests from seeded data:
- `python seed_data.py` populated 5 users, playlists, and songs successfully.
- `FLASK_APP=app:create_app flask run` served `GET /users/<id>`, `GET /songs/search?q=a`, and `GET /users/<id>/streak`, all returning 200 with expected JSON shapes.
- Working branch `bugfix/mixtape` already exists and is checked out.

## Milestone 1: Issue Triage

All five issue reports (streak reset, stale "listening now" feed, duplicate search results, missing rating notification, missing last playlist song) have been read in full. Based on the affected-service mapping in the README, my working plan is to attempt all five if time allows, prioritizing:

1. Issue #1 (streak) — `streak_service.py`
2. Issue #3 (search duplicates) — `search_service.py`
3. Issue #5 (missing last playlist song) — `playlist_service.py`
4. Issue #4 (missing rating notification) — `notification_service.py` (stretch)
5. Issue #2 (stale listening-now feed) — `feed_service.py` (stretch)

This ordering is provisional and will be finalized in Milestone 2 after reproducing each issue.

---

## Milestone 2: Reproduction

Final set to fix: Issue #1 (streak reset), Issue #3 (search duplicates), Issue #5 (missing last playlist song). Issues #2 and #4 stay on the list as stretch candidates.

A note on environment: pytest wouldn't run in one of the environments I was working in (an unrelated dependency mismatch), so I reproduced each bug first by calling the actual service functions directly and hitting the live seeded app over HTTP. I later ran the real `pytest tests/` suite on my own machine, and that's the output I'd treat as the authoritative check:

```
tests/test_playlists.py FF.                                                                        [ 23%]
tests/test_search.py .....                                                                         [ 61%]
tests/test_streaks.py ....F                                                                        [100%]

FAILED tests/test_playlists.py::test_playlist_returns_all_songs - AssertionError: assert 4 == 5
FAILED tests/test_playlists.py::test_playlist_returns_songs_in_order - AssertionError: assert [...] == [...]  (missing "Track 5")
FAILED tests/test_streaks.py::test_streak_increments_on_sunday - assert 1 == 2
3 failed, 10 passed in 0.52s
```

Issues #1 and #5 fail exactly as expected. Issue #3 is the interesting one: all 5 `test_search.py` tests pass, including `test_search_no_duplicates_multi_tag_song`. That's not a sandbox quirk — it's my own machine, with the real dependencies, saying the bug doesn't show up anywhere right now.

### Issue #1 — Listening streak keeps resetting (streak_service.py)

**How I reproduced it:** called `update_listening_streak()` directly, the same function `record_listening_event()` calls on every `POST /songs/<id>/listen`, with controlled timestamps mirroring kenji's report: a streak of 12, `last_listened_at` set to a Saturday, then a listen the following Sunday.

```
Before Sunday listen -> streak = 12
After Sunday listen  -> streak = 1   (expected 13)
After Monday listen  -> streak = 2   (expected 14 — matches report: "Monday bumped it to 2")
```

Matches kenji's report down to the detail that Monday resumes counting from 1 instead of picking the old streak back up. `tests/test_streaks.py::test_streak_increments_on_sunday` fails on this with `assert 1 == 2`, so it's a good candidate for the regression-test stretch goal.

### Issue #3 — Duplicate songs in search (search_service.py)

**How I reproduced it, with a caveat:** I couldn't get duplicate rows to show up in `search_songs()`'s return value or in a live `GET /songs/search?q=Anthem` call, no matter how I approached it. Three checks, same result each time:

1. Called `search_songs("Anthem")` directly against a song with 3 tags (same setup as `tests/test_search.py`'s `song_multi_tags` fixture) — 1 result, not 3.
2. Hit the live seeded app at `GET /songs/search?q=Anthem` — `{"count":1,...}` for "Crown Heights Anthem," which the real seed data gives 3 tags (rap, hip-hop, boom bap).
3. Took the exact SQL `search_songs()` generates and ran it two ways in the same session: through the ORM's `.all()` (1 result), and as a raw execute of the identical compiled statement (3 rows). So the join really does fan out to 3 rows at the SQL level — the code has the exact defect the issue describes, an unguarded `outerjoin` with no `.distinct()` — but SQLAlchemy 2.0.51 quietly deduplicates identical entities on `Query.all()` before any of that reaches a response.

To rule out something specific to this app, I tried the same pattern on two throwaway, unrelated models with no relationships at all: `session.query(Parent).join(Child).all()` also collapses to 1 row for a parent with 3 matching children. So this is a real property of the installed SQLAlchemy version, not a fluke of this codebase.

I'm fixing it anyway, defensively — the raw SQL proves the flaw is real even though it's currently masked. One thing worth flagging: `tests/test_search.py::test_search_no_duplicates_multi_tag_song` documents the intended behavior ("Should be 1, bug causes it to be 3"), but per the pytest run above it currently passes. Unlike #1 and #5, I can't point to this test as a regression test that "would have caught the bug," because it doesn't catch it today.

### Issue #5 — Last playlist song never shows up (playlist_service.py)

**How I reproduced it:** checked the seeded "Friday Energy" playlist directly. `playlist_entries` has 7 rows for it (positions 1–7), but `GET /playlists/<id>/songs` returns `count: 6` — the song at position 7 is missing every time.

To confirm the "shift" darius described (adding a new song frees the previously-missing one and hides the new one instead), I inserted a new `playlist_entries` row at position 8 directly, bypassing an unrelated crash in `add_to_playlist()` noted below, and called `get_playlist_songs()` again:

```
Before: count=6, titles end in [..., "Crown Heights Anthem"]        (position 7 song missing)
Inserted new song at position 8: "Midnight Drive"
After:  count=7, titles end in [..., "Crown Heights Anthem", "Harlem Renaissance"]
        (position 7's "Harlem Renaissance" now shows up; position 8's "Midnight Drive" is missing instead)
```

Exact match for the report: whatever was added most recently is always the one hidden. `tests/test_playlists.py::test_playlist_returns_all_songs` fails with `assert 4 == 5`, and `test_playlist_returns_songs_in_order` fails too (missing "Track 5") — both good candidates for the regression-test stretch goal.

**Side finding, not one of the 5 issues, not being fixed:** `POST /playlists/<id>/songs` 500s (`IntegrityError: NOT NULL constraint failed: playlist_entries.position`) any time the song being added isn't already in the playlist. `add_to_playlist()` appends to `playlist.songs` through the plain SQLAlchemy `secondary=` relationship, which only populates the two foreign keys — it has no way to fill in the join table's `position` or `added_by` columns, both `NOT NULL` with no default. That's why I reproduced the "add a song" step above with a direct insert instead of the real endpoint. Worth flagging since it's a real, severe bug, just outside the scope of the 5 tracked issues.

---

## Milestone 3: Root Cause Analysis

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** See Milestone 2. Called `update_listening_streak()` directly with a streak of 12 and `last_listened_at` on a Saturday, then updated again with `now` set to the following Sunday. Streak dropped to 1 instead of going to 13.

**How I found the root cause:** Started at the route (`POST /songs/<id>/listen` in `routes/songs.py`), which calls `streak_service.record_listening_event()`. That function does two things: writes a `ListeningEvent`, then calls `update_listening_streak(user, now)` to do the actual streak math. Reading `update_listening_streak()`, its own docstring spells out the rule in plain terms: same day = no change, listened yesterday = increment, more than a day passed = reset to 1. Nothing in that docstring mentions weekdays. Then I read the actual `elif` below it and it didn't match its own docstring:

```python
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1
else:
    user.listening_streak = 1
```

The mismatch between the documented rule ("listened yesterday increments") and the code (which adds an extra condition not mentioned anywhere) is what told me this was the exact line, not just a suspicious area. I confirmed by checking what `datetime.weekday()` returns for Sunday — 6 — which lines up exactly with kenji's report of it happening only on Sundays.

**The root cause:** `days_since_last == 1` correctly detects "listened yesterday," which should always increment the streak. But the condition has an extra `and today.weekday() != 6` clause tacked on. `weekday()` returns 6 specifically for Sunday, so on any Sunday, this extra clause evaluates to `False`, and the whole `elif` becomes `False` even though the gap really was one day. Execution falls through to the `else` branch, which is meant for "more than one day skipped," and the streak gets reset to 1 — identical to what happens on a real gap. The streak logic has no way to tell "one day passed and today happens to be Sunday" apart from "the user skipped a day," because the Sunday check makes those two cases produce the same branch.

**My fix and side-effect check:** Deleted the `and today.weekday() != 6` clause, restoring the condition to just `elif days_since_last == 1:` — one line changed, matching the function's own docstring exactly. Verified with 6 scenarios directly against `update_listening_streak()`: new user starts at 1, a second listen on the same day doesn't change the streak, Friday→Saturday increments, Saturday→Sunday now increments (the fixed case), Sunday→Monday increments, and — the boundary check that mattered most — a genuine 2-day gap that skips Saturday and lands on Sunday still resets to 1 rather than incrementing. That last case rules out the fix overcorrecting into "every Sunday increments no matter what."

Ran the real test suite after the fix:

```
tests/test_playlists.py FF.                                                    [ 23%]
tests/test_search.py .....                                                     [ 61%]
tests/test_streaks.py .....                                                    [100%]
2 failed, 11 passed in 0.51s
```

`test_streaks.py` now passes 5/5, including `test_streak_increments_on_sunday`, which was the one failing before this fix. `test_playlists.py` still fails its same 2 tests and `test_search.py` still passes its same 5 — identical to the Milestone 2 baseline — confirming this change didn't touch playlist or search behavior at all, only the streak boundary it targeted.

**Diff:**
```diff
-    elif days_since_last == 1 and today.weekday() != 6:
+    elif days_since_last == 1:
```

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:** See Milestone 2. The seeded "Friday Energy" playlist has 7 rows in `playlist_entries`, but `GET /playlists/<id>/songs` returns `count: 6`, always missing the highest-position (most recently added) song. Adding another song directly shifted which song was hidden.

**How I found the root cause:** Followed `GET /playlists/<id>/songs` in `routes/playlists.py` to `playlist_service.get_playlist_songs()`. That function queries `Song` joined to `playlist_entries`, filters by playlist, and orders by `position` ascending — all straightforward and correct. Its own docstring even has a line that reads "Note: This function returns all songs in the playlist." Then the return statement didn't match that note:

```python
return [song.to_dict() for song in songs[:-1]]
```

`songs` at that point is already the correctly ordered, correctly filtered list — the query itself has no bug. The `[:-1]` slice on the very last line is the only thing removing a song, and it always drops the last element of an ascending-by-position list, which is always the most recently added song. That's what made me confident this was the exact cause rather than something in the query: the query builds the right list, and then one slice throws away its last entry right before returning.

**The root cause:** `get_playlist_songs()` builds the correct, fully ordered list of songs, but returns `songs[:-1]` instead of `songs`. A `[:-1]` slice always drops the last element of whatever list it's given, regardless of how many songs are in the playlist. Since `songs` is sorted ascending by `position`, the last element is always the song with the highest position, i.e. whichever song was added most recently. So the function doesn't just have an off-by-one bug on the count — it structurally always excludes "whatever was added last," which is exactly why darius saw the missing song change identity every time a new one was added.

**My fix and side-effect check:** Removed the `[:-1]` slice, returning `songs` directly. One line changed. Verified with three scenarios directly against `get_playlist_songs()`: an empty playlist still returns `[]`, a single-song playlist now correctly returns that one song (previously the bug returned `[]` here too — a one-song playlist showed zero songs, which is the same defect at its most visible), and a 5-song playlist returns all 5 in order. I also checked whether anything else calls this function: `notification_service.add_to_playlist()` imports `get_playlist_songs` but never actually calls it anywhere in its body, so this fix has no path into the notification flow at all.

Ran the real test suite after the fix:

```
tests/test_playlists.py ...                                                    [ 23%]
tests/test_search.py .....                                                     [ 61%]
tests/test_streaks.py .....                                                    [100%]
13 passed in 0.44s
```

All 13 tests pass now. `test_playlists.py` went from 2 failing to 3/3, including the ordering test, and `test_streaks.py`/`test_search.py` are unaffected — everything Issue #1's fix already confirmed is still true, and nothing in playlists broke anything in search or streaks either.

**Diff:**
```diff
-    return [song.to_dict() for song in songs[:-1]]
+    return [song.to_dict() for song in songs]
```

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it:** See Milestone 2, including the caveat. I could not get duplicate rows to appear in `search_songs()`'s output or in a live search request on this machine — three separate checks all came back clean. What I could prove is that the underlying SQL genuinely returns duplicate rows; the installed SQLAlchemy version (2.0.51) just happens to collapse them before they reach a response. I'm documenting this fix based on that SQL-level proof rather than a visible symptom.

**How I found the root cause:** Followed `GET /songs/search?q=...` in `routes/songs.py` to `search_service.search_songs()`. The function builds one query: it joins `Song` to `song_tags` with `outerjoin()`, then filters on title/artist, then calls `.all()`. The join is only there so a song can be matched or displayed alongside its tags, but nothing about the query limits it to one row per song — a `LEFT OUTER JOIN` produces one result row per matching `song_tags` row, so a song with 3 tags contributes 3 rows to the result set, a song with 1 tag contributes 1, and a song with 0 tags contributes 1 (via the outer join's `NULL` match). I confirmed this wasn't just theoretical by pulling the exact compiled SQL out of the query object and running it directly against the database, bypassing the ORM's row processing entirely: for a search matching a 3-tag song, the raw query returned 3 rows for that one song. That's the moment I was confident the defect was real and exactly where the issue description says it is (a join without deduplication), independent of whatever the ORM does with those rows afterward.

**The root cause:** `search_songs()` joins `Song` to `song_tags` to support tag-aware search, but never deduplicates the result by song. Since `song_tags` has one row per `(song_id, tag_id)` pair, `outerjoin(song_tags, ...)` multiplies each matching song by however many tags it has — a song with 3 tags produces 3 joined rows, each of which becomes a candidate entry in the results. The query has no `.distinct()` and no `.group_by(Song.id)`, so nothing collapses those 3 rows back down to 1 song. Whether that shows up as visible duplicates in the JSON response depends on what the ORM layer does with duplicate-PK rows afterward — in my environment, SQLAlchemy 2.0.51's `Query.all()` happens to collapse them, but the query itself doesn't guarantee that, and simone's report shows an environment where it didn't.

**My fix and side-effect check:** Added `.distinct()` to the query, right before `.all()`. One line. This makes deduplication explicit at the SQL level rather than relying on the ORM to paper over it, so the fix holds regardless of SQLAlchemy version. Verified directly: searching "Anthem" against a database with a 3-tag song and a separate 0-tag song that also matches "Anthem" returns exactly 2 results, one per song, not conflated into one and not the 4 raw rows the join alone would produce. I confirmed this at the SQL level too — the raw, non-distinct compiled query for that search returns 4 rows; the same query with `DISTINCT` added returns 2. I also re-checked the single-tag and zero-tag cases to make sure the fix doesn't accidentally under- or over-return: both still return exactly 1 result for their respective songs.

Ran the real test suite after the fix:

```
tests/test_playlists.py ...                                                    [ 23%]
tests/test_search.py .....                                                     [ 61%]
tests/test_streaks.py .....                                                    [100%]
13 passed in 0.50s
```

Still 13/13, same as before this fix — expected, since `test_search.py` was passing before for the environment-specific reason explained above, and `.distinct()` doesn't change what it was already asserting. This confirms the fix is a no-op for anything already working (single-tag and zero-tag search, non-tag-related search behavior) while closing the real defect the raw SQL proved.

**Diff:**
```diff
         .filter(
             db.or_(
                 Song.title.ilike(f"%{query}%"),
                 Song.artist.ilike(f"%{query}%"),
             )
         )
+        .distinct()
         .all()
```
