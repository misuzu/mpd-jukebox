# mpd-jukebox — Design

A standalone Python daemon that does what myMPD's jukebox mode does: it keeps
MPD's queue topped up with random songs or whole albums, following the same
constraints (unique tag, last played, hated songs, include/exclude filters,
song duration). It does not include myMPD's web UI, JSON-RPC API, Lua
scripting (the `script` jukebox mode) or MPD partitions.

The code is built as a **functional core with an imperative shell**. All
decisions live in pure functions over immutable data. A thin asyncio shell
talks to MPD and SQLite, turns what it sees into events for the core, and
carries out the effects the core returns.

This document has three parts: §1 describes myMPD's behaviour (what we
port), §2–§6 describe the Python design, and §7 lists deviations from myMPD
and the decisions behind them.

---

## 1. myMPD jukebox behaviour (reference)

Source paths are relative to `mympd/src`.

### 1.1 Modes and settings

`lib/jukebox.{c,h}`, `lib/config/jukebox_state.{c,h}`, `compile_time.h.in`

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `off` | `off` \| `song` \| `album` \| `script` (we keep only `song` and `album`; see §2) |
| `playlist` | `"Database"` | Song source in song mode: the whole DB or a stored playlist name |
| `queue_length` | `1` | Minimum MPD queue length (1..999) |
| `last_played` | `24` (hours) | Skip songs whose `lastPlayed` sticker falls inside this window (0..5000) |
| `uniq_tag` | `Artist` | A single tag whose value must not repeat across the queue, recent history and new picks |
| `ignore_hated` | `false` | Skip songs that have the sticker `like == 0` |
| `filter_include` / `filter_exclude` | `""` | MPD-style filter expressions; exclude wins over include |
| `min_song_duration` / `max_song_duration` | `0` | Strict bounds, `0` means no limit (`dur > min`, `dur < max`) |
| `autostart` | `true` | Run the jukebox on connect if the MPD queue is empty |

Global internal sizes (`lib/config/config.c`):

| Key | Default | Range |
|---|---|---|
| `jukebox_queue_length_song` | 100 | 10..1000 |
| `jukebox_queue_length_song_min` | 10 | 10..500 |
| `jukebox_queue_length_album` | 25 | 5..250 |
| `jukebox_queue_length_album_min` | 5 | 5..125 |

Constants: `JUKEBOX_ADD_SONG_MAX = 99`, `JUKEBOX_UNIQ_RANGE = 50`,
`JUKEBOX_ADD_SONG_OFFSET = 10` s, `MPD_RESULTS_MAX = 10000`,
`MYMPD_LAST_PLAYED_COUNT = 2000`, `SCROBBLE_TIME_MAX = 240`,
`SCROBBLE_TIME_TOTAL = 480`.

### 1.2 Two queues

myMPD keeps two queues:

1. The **MPD queue**, which is what plays.
2. The **jukebox queue**, an internal list of already-picked candidates. Each
   entry is `(key, lineno, tag_value)`, where `key` is a song URI or an album
   key. In song mode the list holds about 100 entries, in album mode about 25.

The jukebox moves entries from (2) into (1). (2) is refilled in a worker
thread when it gets short.

### 1.3 When the jukebox runs

- **On connect** (`mympd_client/partitions.c`): if `autostart` is set, the mode
  is not `off`, and the MPD queue is empty.
- **On idle `queue`** (`mympd_client/idle.c`): if the mode is not `off` and
  `mpd_queue_length < queue_length`.
- **On the jukebox timer** (`mympd_api/status.c`): every status refresh while
  the player is in `play` sets a one-shot timer to
  `duration - (elapsed + crossfade + 10)` seconds. If that value is ≤ 0, or
  the player is not playing, or the mode is `off`, the timer is cancelled.
- **On restart** (API): clear the jukebox queue, then run.

### 1.4 `jukebox_run` (`mympd_client/jukebox.c`)

```
if filling: return                      # a refill is already in progress
if mode == off: return
if mpd_qlen > queue_length: return
add = (queue_length - mpd_qlen) if queue_length > mpd_qlen else 1
add = min(add, 99)
if add > len(jukebox_queue):
    request worker REFILL_ADD(add)      # fill, then move `add` entries
    return
move `add` entries jukebox_queue -> MPD queue (see 1.5)
if (song mode and len(jq) < song_min) or (album mode and len(jq) < album_min):
    request worker REFILL                # background top-up
```

When the queue is exactly at `queue_length`, the jukebox adds **one** entry.
This is what the timer relies on: about 10 s before the song ends, the queue
length equals the target, so one more entry is added. The current song counts
toward the queue length. With `consume` off, the queue grows past the target
and the jukebox stops adding. myMPD expects consume mode to be used with the
jukebox.

In album mode, `add` counts **albums**, but it is computed from the MPD queue
length in **songs**.

### 1.5 Moving entries to MPD (`jukebox_add_to_queue`)

Shift entries off the jukebox queue until `add` of them have been added
successfully. Entries that fail are dropped and logged.

- Song mode: `add <uri>`.
- Album mode: `searchadd` with the album's search expression.

If none were added, report an error ("Adding songs from jukebox to queue
failed"). Otherwise, if the player is not in `play`, send `play`.

### 1.6 Uniqueness context (`jukebox_get_last_played`)

Before a refill, build `queue_list`:

- one entry per song in the MPD queue, and
- if that gives fewer than 50 entries, up to 50 of the most recent entries
  from myMPD's own **last-played list**. That list holds the URIs that reached
  scrobble time, newest first, capped at 2000.

Each entry is `(key, tag_value)`. In song mode `key` is the URI, in album mode
it is the album key. If `uniq_tag == Title`, `tag_value` is set to `""`.

### 1.7 Refill: random selection (`mympd_worker/jukebox.c`, `mympd_client/random_select.c`)

The worker gets a **copy** of the current jukebox queue (`add_list`), with
`expected = queue_length_{song|album} + add`. It fills `add_list` up to
`expected` by **reservoir sampling** over the source:

```
initial = len(add_list); need = expected - initial; lineno = 1
for candidate in source:
    if passes_constraints(candidate) and uniq(candidate) == IS_UNIQ:
        if randrange(0, lineno) < need:
            if len(add_list) < expected: append(candidate, lineno)
            else: replace at initial + randrange(0, need)   # never touch pre-existing entries
        lineno += 1
```

- **Song source.** With `playlist == "Database"`: `search` with
  `filter_include` (or an empty `file` constraint), in windows of 10 000.
  With a stored playlist: `searchplaylist` with the include filter on MPD
  0.24, otherwise `listplaylistinfo`, windowed. Windowing continues while
  `lineno + skipno > start`.
- **Song constraints, in order:** min duration, max duration,
  `lastPlayed < now - last_played*3600` (sticker; songs with no sticker pass),
  not hated (`like` sticker is `"0"`; only when `ignore_hated`), exclude/include
  expression, then uniqueness.
- **Album source:** the album cache, with one entry per album key. The
  last-played check uses the album's first song URI. The expression check runs
  on album tags. Album mode ignores `playlist` and the duration and hated
  constraints.
- **Uniqueness (`check_uniq_tag`).** A candidate is rejected if its key or its
  `tag_value` equals the key or `tag_value` of any entry in `queue_list` or in
  `add_list`. Empty tag values compare equal, so two songs without an Artist
  tag collide.
- **Result.** If `len(add_list) < expected`, the refill **fails**: the jukebox
  sets `mode = off` at runtime and stores `last_error`. Otherwise the new list
  **replaces** the jukebox queue. In both cases `filling` is cleared. For
  `REFILL_ADD`, the worker then performs §1.5 with `add`.

### 1.8 Album key (`lib/cache/cache_rax_album.c`)

- In advanced mode, if the song has a `MUSICBRAINZ_ALBUMID` tag that is
  exactly 36 characters long, that value is the key.
- Otherwise the key is `sha1(AlbumArtist or Artist + "::" + Album [+ "::" + group_tag])`.
- If both AlbumArtist and Artist are empty, there is no key. If Album is
  empty, the album is either skipped or named "Unknown Album", depending on
  configuration.

### 1.9 Filter expressions (`lib/search/search.c`)

myMPD evaluates filters locally with a small subset of MPD's filter syntax:

- clauses are split on `") AND ("` only;
- each clause has the form `tag op 'value'`;
- operators are `==`, `!=`, `contains`, `starts_with`, `=~`, `!~`
  (PCRE), and `~~` (fuzzy);
- special tags are `any`, `file`, `base`, `prio >=`, `modified-since`,
  `added-since`, and `AudioFormat` (ignored);
- values are compared after Unicode normalisation and case folding;
- with multi-value tags, a positive operator matches if **any** value
  matches, and a negative operator matches only if **all** values match;
- a missing tag matches only `!=` and `!~`.

**An expression that fails to parse is treated as "no filter".**

### 1.10 Side effects outside the jukebox

- **Scrobble.** Scrobble time is `min(duration/2, 240)` s into playback (240
  for songs longer than 480 s). At that point the URI is pushed onto the
  last-played list, and the MPD stickers `playCount += 1` and
  `lastPlayed = song_start_time` are written.
- **Settings.** Changing any constraint (mode, playlist, uniq tag,
  last_played, ignore_hated, filters, durations) clears the jukebox queue.
  Changing `queue_length` or `autostart` does not.
- **Persistence.** On shutdown, the jukebox queue is saved to
  `jukebox_list.mpack` along with the mode. On load, the saved queue is
  ignored if the mode differs. The last-played list is persisted the same way.

---

## 2. Scope

**In scope:** modes `song` and `album`; every constraint in §1.1 except
`autostart`; the trigger rules in §1.3–§1.5 (with the connect rule changed,
see §7); reservoir selection (§1.7); album keys (§1.8); a
local filter evaluator (§1.9); scrobble tracking and last-played history
(§1.10); persistence. The daemon always uses MPD's default partition and
never sends the `partition` command.

**Out of scope:** the `off` mode and the `autostart` setting (running the
daemon means the jukebox is on), Lua scripting (the `script` mode and
triggers), MPD partitions, presets, the web UI and JSON-RPC API, myMPD's "simple" album
mode, and fuzzy `~~` matching
(a filter that uses it is a config error; see §7).

---

## 3. Architecture

```
            ┌──────────────────── imperative shell (asyncio) ───────────────────┐
 MPD idle ─►│ events.py: MPD/timer/worker  ──Event──►  core.update(state, ev)   │
 timers  ─►│                                          │                        │
 refill  ─►│                         (state', [Effect])◄                        │
            │ effects.py: interpret Effect → MPD cmds / SQLite / timers / tasks │
            └───────────────────────────────────────────────────────────────────┘
                     functional core: pure, no I/O, no clock, no global RNG
```

The core follows the Elm/TEA pattern:

```python
update(cfg: Config, state: State, event: Event) -> tuple[State, tuple[Effect, ...]]
```

The shell loops: wait for the next event → `update` → run the effects → feed
the results back as new events. The current time and random seeds always come
**in** through events, so `update` is deterministic and can be tested without
MPD.

Random selection (§1.7) has to stream thousands of songs from MPD, so it
cannot be one pure call over a list. It is a **pure fold**:

```python
select_step(ctx: SelectContext, acc: Reservoir, candidate: Candidate) -> Reservoir
```

The shell drives that fold over an async stream of MPD result windows. The
fold carries its own PRNG state (§4.4), so given the same seed and inputs it
always produces the same result.

### 3.1 Package layout (`src/mpd_jukebox/`)

| Module | Kind | Contents |
|---|---|---|
| `model.py` | core | Frozen dataclasses and enums: `Song`, `Album`, `Entry`, `Config`, `Status`, `ConfigError` |
| `rng.py` | core | Pure SplitMix64: `next(state) -> (value, state)`, `randrange` |
| `filters.py` | core | Parser and evaluator for filter expressions (§1.9) → `Predicate` |
| `tags.py` | core | Tag value extraction, joining, normalisation, `album_key(song, cfg)` |
| `albums.py` | core | `build_albums(songs) -> Mapping[key, Album]`, as a fold over a stream |
| `constraints.py` | core | Small composable predicates: duration, last played, hated, expression, uniqueness |
| `select.py` | core | `Reservoir`, `select_step`, `select_init`, `select_result` (§1.7) |
| `jukebox.py` | core | `State`, events, effects and `update()`: trigger rules, `jukebox_run`, moving entries, refill bookkeeping, timer and scrobble scheduling |
| `connection.py` | core | `parse_mpd_host`, `resolve_connection`: MPD address and password from CLI, config and environment (§5.5) |
| `config.py` | shell-edge | TOML → `Config` (pure `parse_config(dict)`; reading the file stays in the shell) |
| `mpd_io.py` | shell | python-mpd2 asyncio wrapper: status, queue, windowed search and listplaylist, stickers, add, searchadd, play |
| `migrate.py` | core | Pure migration planning: `parse_name`, `split_statements`, `plan`, `pending` (§5.3.1) |
| `migrations/` | data | Schema migrations as `NNNN_description.sql` files, shipped as package data |
| `store.py` | shell | aiosqlite: applies migrations; reads and writes the jukebox queue, last-played history and meta (constraint fingerprint) |
| `runtime.py` | shell | The event loop: idle listener, timers, refill task, effect interpreter |
| `cli.py` | shell | click entry points |

`jukebox.py` currently holds a from-documentation sketch. It will be replaced
by the modules above. Any ideas worth keeping from it (predicate combinators,
frozen data) carry over.

---

## 4. Functional core

### 4.1 Data (all `@dataclass(frozen=True, slots=True)`, tuples and `frozenset`s, no lists or dicts)

```python
Tags = Mapping[str, tuple[str, ...]]      # MappingProxyType; tag name → values (multi-value)

Song(uri: str, tags: Tags, duration: float, prio: int, last_modified: int, added: int)
Album(key: str, first_uri: str, uris: tuple[str, ...], tags: Tags)  # tags merged across songs
Entry(key: str, lineno: int, tag_value: str)   # one jukebox-queue item (URI or album key)

class Mode(Enum): SONG, ALBUM          # no OFF: the daemon only runs to be a jukebox

Config(mode, playlist, queue_length, last_played_hours, uniq_tag, ignore_hated,
       filter_include, filter_exclude, min_song_duration, max_song_duration,
       queue_length_song=100, queue_length_song_min=10,
       queue_length_album=25, queue_length_album_min=5, add_max=99,
       uniq_range=50, add_offset=10, last_played_count=2000,
       album_group_tag=None, album_use_mbid=True, album_unknown=False,
       write_stickers=False)

Status(state: 'play'|'pause'|'stop', queue_length: int, song_id: int|None,
       song_uri: str|None, elapsed: float, duration: float, crossfade: int)

State(halted: bool,                     # set by a failed refill (§1.7) or endless failed adds; cleared only by Restart
      jukebox_queue: tuple[Entry, ...],
      filling: bool,                    # a refill is running
      enqueuing: bool,                  # an Enqueue is being carried out; no other run takes entries
      add_failures: int,                # failed adds since the last successful one
      validating: bool, revalidate: bool,   # a database check is running / is due afterwards
      last_error: str,
      status: Status | None)                  # last status seen; scrobble data comes from it
```

The play history is not part of `State`. It lives only in the store, and the
shell reads it when it gathers the inputs for a refill.

### 4.2 Events (inputs) and effects (outputs)

```python
# Events: everything carries `now: int`
Connected(status, now)
QueueChanged(status, now)            # idle "playlist"
PlayerChanged(status, now)           # idle "player"
TimerFired(name: 'jukebox'|'scrobble', status, now)   # the shell fetches a fresh status
RefillDone(entries: tuple[Entry, ...], add: int, now)
RefillFailed(error: str, now)
Enqueued(attempted, added, wanted, status, now)   # result of an Enqueue effect
DatabaseChanged(now)                 # idle "database"
Validated(missing: frozenset[str], now)   # result of a Validate effect
Restart(now)                         # SIGHUP: clear `halted`, `last_error` and the jukebox queue, then run

# Effects: plain data that the shell interprets
StartRefill(mode: Mode, existing: tuple[Entry, ...], expected: int, add: int)
Enqueue(mode: Mode, entries: tuple[Entry, ...], wanted: int)   # song: add URI; album: add album URIs
Validate(mode: Mode, entries: tuple[Entry, ...])  # which entries left MPD's database?
Play()
SetTimer(name, seconds) / CancelTimer(name)
RecordPlay(uri, played_at, started_at)  # history row, plus stickers if write_stickers
Persist(jukebox_queue)               # write-through to SQLite
Log(level, message)
```

`StartRefill` needs the uniqueness context (§1.6), which includes the songs in
the MPD queue. The core does not hold the MPD queue's contents, so the shell
fetches them and builds `queue_list` with the pure function
`uniq_context(mode, uniq_tag, songs, album_settings)`. It then calls
`select_init`. The core decides *when* to refill and *how many* entries to
pick. The shell gathers the inputs and runs the fold.

### 4.3 Core functions (selection)

```python
# jukebox.py
should_run(event, state) -> bool                # Connected/QueueChanged: qlen < queue_length; TimerFired: always; never while halted or filling
add_count(cfg, mpd_qlen) -> int | None          # None = nothing to do; otherwise min(max(target-qlen, 1), add_max)
needs_topup(cfg, jq_len) -> bool
jukebox_timer(cfg, state, status) -> Effect     # SetTimer or CancelTimer (§1.3); CancelTimer while halted
scrobble_timer(status, current) -> Effect
take(jq, n) -> tuple[tuple[Entry,...], tuple[Entry,...]]
run(cfg, state, now) -> (State, effects)        # §1.4
update(cfg, state, event) -> (State, effects)   # dispatch on event type

# constraints.py: each returns Predicate[Candidate]
duration_between(min_s, max_s); played_before(stickers, since); not_hated(likes, enabled)
matches(include: Filter | None, exclude: Filter | None)
uniq_against(queue_list, add_list) -> UniqResult   # IN_QUEUE | position | IS_UNIQ

# select.py
select_init(existing, expected, seed) -> Reservoir
select_step(ctx, acc, cand) -> Reservoir         # one step of reservoir sampling, exactly as in §1.7
select_result(acc) -> Filled(entries) | Short(found, expected)   # Short is a failure, as in myMPD

# filters.py
parse(expr: str) -> Filter | Invalid | Unsupported
    # Invalid: syntax error; treated as "no filter", as in myMPD
    # Unsupported: valid syntax using `~~`; parse_config turns this into a ConfigError
evaluate(filter, tags_of: Callable[[str], tuple[str,...]], special) -> bool
```

### 4.4 Randomness

myMPD calls `randrange(0, n)` on a global RNG. We use a pure **SplitMix64**
instead: `randrange(state, n) -> (k, state')`. The PRNG state is stored in
`Reservoir`, and the initial seed is an argument. The shell draws seeds from
`secrets.randbits(64)`. Tests pass fixed seeds and assert exact results.

### 4.5 Performance

- The reservoir is small (≤ about 1100 entries). It is held as a tuple and
  replaced with a new tuple on each change, which costs O(k) per accepted
  candidate.
- Uniqueness keys from `queue_list` are precomputed once into frozensets, so
  checking them is O(1). The `add_list` check is a scan, as in myMPD.
- Library songs are streamed and never all held in memory, except in album
  mode, where the album index is built per refill as an immutable mapping.
  Keeping that index in memory between refills and invalidating it on idle
  `database` is a possible later optimisation.

---

## 5. Imperative shell

### 5.1 Runtime (`runtime.py`)

- One python-mpd2 `mpd.asyncio.MPDClient` connection. A background task
  iterates `idle(['playlist', 'player', 'database'])`. python-mpd2's asyncio client sends
  `noidle` by itself whenever another command is issued, and resumes idling
  afterwards, so a second connection is not needed.
- An `asyncio.Queue[Event]` collects events from the idle listener, the timers
  (`loop.call_later` handles keyed by name; `SetTimer` replaces an existing
  timer) and the refill task.
- At most one refill task runs at a time. The core enforces this with
  `filling`, and the shell asserts it as well.
- The main loop is `state, effs = update(cfg, state, ev)`, then each effect is
  interpreted in order. `Enqueue` produces `Enqueued`. Failures of individual
  `add` commands are logged as warnings, and the counts are reported.
- **Failed adds** (§1.5). As in myMPD, entries that cannot be added are
  replaced by the next ones until `wanted` entries are in MPD's queue. The
  core takes the replacements from the jukebox queue in further `Enqueue`
  rounds; if the jukebox queue runs out, it refills with the number still
  missing. If adds fail `queue_length_song` (or `queue_length_album`) times
  in a row, the jukebox halts instead of churning through picks forever.
- **Database changes** (§7). On idle `database`, and on every connect, the
  core asks the shell to `Validate` the jukebox queue. A background task
  checks each entry: `find file <uri>` in song mode, or membership in a
  rebuilt album index in album mode. The core then drops the missing
  entries, persists the rest and tops up if needed. A change during a check
  or a refill leads to another check once it finishes, so picks made from a
  database that was being updated are checked too.
- The connection authenticates with `password` first if one is configured
  (§5.5).
- A failure in a background task (idle listener, timer, refill) is forwarded
  to the main loop. Connection errors, and MPD errors other than password or
  permission errors, end the session and lead to a reconnect.
- Reconnect uses exponential backoff. On every connect and reconnect a
  `Connected` event is emitted, and the core treats it like a queue change:
  the jukebox runs if MPD's queue is shorter than `queue_length`. A refill
  that was running is cancelled and `filling` is reset.
- On SIGHUP: emit `Restart`. This is how to resume after a failed refill
  halted the jukebox (§7), without restarting the process.
- On SIGINT/SIGTERM: cancel tasks, disconnect and exit with status 0. Every
  store write is its own transaction, so there is nothing to flush.

### 5.2 MPD I/O (`mpd_io.py`)

- `status()` → `Status`. `currentsong()` provides the URI.
  `playlistinfo()` returns the MPD queue songs for §1.6.
- Song source:
  - `Database`: `search` with the include filter, if it is non-empty and
    parses, otherwise `search file ""` (which matches every song, as in
    myMPD), plus `window` `start:end` in steps of 10 000.
  - Stored playlist: `listplaylistinfo name start:end` when MPD ≥ 0.24,
    otherwise one unwindowed call.
  - Include and exclude are **always** also evaluated locally, as in myMPD.
- Album source: read every song with windowed `search file ""` calls, then
  group them with the pure `albums.build_albums`. The index is cached until
  the next album refill. If a saved album key is missing from it (after a
  daemon restart, for example), it is rebuilt once.
- Stickers: `sticker find song "" lastPlayed` and `sticker find song "" like`
  become immutable `Mapping[uri, str]`. If stickers are unavailable (MPD
  answers "unknown command" or there is no sticker DB), these are empty and
  the sticker constraints pass. In that case the last-played check falls back
  to the local history table, so the "last played" constraint still works
  without stickers.
- `Enqueue` for albums: the shell looks up the album and runs `add` for each
  of its URIs, sorted by (disc, track, uri). python-mpd2's asyncio client
  has no command lists, so these are separate `add` commands. An album counts
  as added if at least one of its songs was added. myMPD uses `searchadd`; see
  §7.

### 5.3 Persistence (`store.py`, aiosqlite)

One SQLite database. The default path is
`$XDG_STATE_HOME/mpd-jukebox/state.sqlite`.

The initial schema (`migrations/0001_initial.sql`):

```sql
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);  -- 'mode', 'constraint_fingerprint'

CREATE TABLE jukebox_queue (
    pos       INTEGER PRIMARY KEY,
    key       TEXT NOT NULL,      -- song URI or album key
    lineno    INTEGER NOT NULL,
    tag_value TEXT NOT NULL
);

CREATE TABLE history (
    id  INTEGER PRIMARY KEY,      -- newest = highest id
    uri TEXT NOT NULL,
    ts  INTEGER NOT NULL
);  -- capped at last_played_count
```

- `constraint_fingerprint` is a pure hash of the config fields from §1.10. On
  startup, a saved jukebox queue is discarded if the fingerprint or the mode
  differs. This combines myMPD's mode check on load with its clear-on-change
  rule.
- `Persist` rewrites `jukebox_queue` in one transaction. The queue holds at
  most about 1100 rows, so a full rewrite is cheap.
- The schema version is not kept in `meta`. It is SQLite's built-in
  `PRAGMA user_version`, managed only by the migration code (§5.3.1).

#### 5.3.1 Migrations

Migrations are forward-only and numbered. Each one is a separate SQL file
shipped inside the package:

```
src/mpd_jukebox/migrations/
    0001_initial.sql
    0002_<description>.sql
```

**File rules**

- The filename is `NNNN_description.sql`. The four-digit prefix is the version.
  Versions are unique, contiguous, and start at 1.
- A file contains only schema and data statements. It never contains
  `BEGIN`, `COMMIT` or `PRAGMA user_version`. The code that applies the
  migrations owns the transaction and the version number, so a file cannot
  get either wrong.
- A released migration is never edited. Fixes go in a new migration.
- SQLite's `ALTER TABLE` is limited. Anything beyond adding or renaming a
  column uses the standard rebuild: create `t_new`, copy the rows with
  `INSERT … SELECT`, drop `t`, then rename `t_new` to `t`. All of it is in the
  same file, so it runs inside one transaction.
- The jukebox queue can always be regenerated, so a migration may simply
  clear it. The play history is the only data that must be carried across
  carefully.

**Packaging**

flit includes every file inside the package directory, so the `.sql` files
end up in the wheel and the Nix build with no extra configuration. They are
read with `importlib.resources.files('mpd_jukebox') / 'migrations'`, which
works both when installed and when run from the source tree.

**Pure part (`migrate.py`)**

```python
Migration(version: int, name: str, statements: tuple[str, ...])

parse_name(filename: str) -> int                  # raises MigrationError
split_statements(sql: str) -> tuple[str, ...]     # raises MigrationError
plan(files: Mapping[str, str]) -> tuple[Migration, ...]   # raises MigrationError
    # filename -> file text; checks names, unique and contiguous versions
    # starting at 1, and that every file has at least one statement
pending(current: int, migrations) -> tuple[Migration, ...] | UnsupportedDatabase
    # UnsupportedDatabase when current > latest (database written by a newer version)
```

`split_statements` accumulates lines and cuts a statement whenever
`sqlite3.complete_statement()` returns true. That function uses SQLite's own
tokenizer, so it handles semicolons inside string literals and comments, and
trigger bodies (`CREATE TRIGGER … BEGIN …; END;`). It is needed because the
only stdlib way to run a whole script, `executescript()`, first commits any
open transaction, which would break the locking described below.

**Shell part (`store.upgrade`)**

1. Open the connection with `isolation_level=None`, so Python's `sqlite3`
   does not start or commit transactions on its own. All transactions are
   explicit.
2. Read and plan the migration files. A `MigrationError` here is a packaging
   bug, and the daemon refuses to start.
3. `BEGIN IMMEDIATE`. This takes the write lock before the version is read,
   so two processes cannot upgrade at the same time.
4. Read `PRAGMA user_version` and call `pending()`. If the database is newer
   than the code, roll back and exit with an error instead of risking the
   data.
5. For each pending migration, run its statements one at a time with
   `execute()`, then `PRAGMA user_version = N`.
6. `COMMIT`. On any error, `ROLLBACK`. SQLite DDL is transactional, so a
   failure leaves both the schema and `user_version` exactly as before.

All pending migrations are applied in a single transaction. Either the
database ends up at the latest version, or it is left unchanged.

**Readers**

`mpd-jukebox list` opens the database read-only (`file:…?mode=ro`) and never
migrates. If `user_version` differs from the latest version, it reports that
and suggests running the daemon once, which performs the upgrade.

**Possible later extension**

A migration that needs Python logic, not just SQL, does not fit this scheme.
If one is ever needed, `NNNN_description.py` files with an `upgrade(conn)`
function can sit alongside the SQL files and share the numbering. This is not
built until a migration actually needs it.

### 5.4 Configuration (`config.py`)

The config file is TOML, read with the stdlib `tomllib`. CLI flags override
it. For the MPD address and password, the environment variables `MPD_HOST`
and `MPD_PORT` are also accepted (§5.5).

```toml
[mpd]                      # every key is optional; see §5.5 for precedence
# host = "localhost"       # hostname, IP, /path/to/socket, @abstract, or "password@host"
# port = 6600
# password = "secret"      # alternative to "password@host"; may contain '@'
# password_file = "/run/credentials/mpd-jukebox.service/mpd-password"

[jukebox]
mode = "song"              # song | album
playlist = "Database"
queue_length = 1
last_played = 24           # hours
uniq_tag = "Artist"
ignore_hated = false
filter_include = ""
filter_exclude = ""
min_song_duration = 0
max_song_duration = 0

[jukebox.advanced]         # myMPD globals and constants; rarely changed
queue_length_song = 100
queue_length_song_min = 10
queue_length_album = 25
queue_length_album_min = 5
last_played_count = 2000
write_stickers = false     # write playCount/lastPlayed like myMPD (turn off if myMPD also runs)

[albums]
group_tag = ""             # e.g. "Date"
use_mbid = true
unknown = false
```

`parse_config(raw: Mapping) -> Settings` is pure and raises `ConfigError`.
`Settings` holds the `Config`, the parsed filters, the `[mpd]` table, the
state file path and any warnings. It validates
the value ranges from §1.1 and the tag names. It also runs `filters.parse` on
`filter_include` and `filter_exclude`: a filter that uses fuzzy `~~` is a
`ConfigError`, and the daemon refuses to start. A filter with a syntax error
only logs a warning and is ignored, as in myMPD.

### 5.5 MPD address and password (`connection.py`)

The daemon accepts `MPD_HOST` and `MPD_PORT` the way `mpc` and other
libmpdclient clients do, including a password in `MPD_HOST`. python-mpd2 does
not read these variables itself, so we parse them. Its `connect()` already
treats a host containing `/` as a Unix socket path and a host starting with
`@` as an abstract socket.

**`parse_mpd_host(value: str) -> (host, password | None)`** (pure; raises
`ConfigError`). It follows
libmpdclient's `mpd_parse_host_password` (`dist/libmpdclient/src/settings.c`):

| `MPD_HOST` | host | password |
|---|---|---|
| `localhost` | `localhost` | none |
| `secret@localhost` | `localhost` | `secret` |
| `secret@192.168.1.5` | `192.168.1.5` | `secret` |
| `secret@::1` | `::1` | `secret` |
| `secret@/run/mpd/socket` | `/run/mpd/socket` (Unix socket) | `secret` |
| `@mpd` | `@mpd` (abstract socket) | none |
| `secret@@mpd` | `@mpd` (abstract socket) | `secret` |
| `secret@` | — | ConfigError: empty host |
| `""` (empty) | treated as unset | |

Rules:

- If the value starts with `@`, it is an abstract socket with no password.
- Otherwise the value is split at the **first** `@`. Everything before it is
  the password, and everything after it is the host.
- So a password given in `MPD_HOST` cannot contain `@`. This is a limitation
  of the format itself, and libmpdclient has it too. Such passwords go in the
  config file's `password` key or the `--password` option instead.

`MPD_PORT` must be an integer from 1 to 65535, otherwise it is a
`ConfigError`. The port is ignored for socket hosts. `MPD_TIMEOUT` is not
supported.

**`resolve_connection(cli, config, env) -> Connection`** (pure; raises
`ConfigError`;
the shell passes in `os.environ` as a plain mapping).

- **Host and password** come from the highest-priority source that sets a
  host: `--host`, then `[mpd].host`, then `MPD_HOST`, then the default
  `localhost`. Any of these may use the `password@host` form.
  - The password is taken **from the same source as the host**, or from an
    explicit `--password` / `[mpd].password`.
  - A password from one source is never combined with a host from another.
    Otherwise a global `MPD_HOST=secret@home-server` would send `secret` to
    a different host set in the config file.
  - Setting a password both in `password@host` form and with an explicit
    `password` in the same source is a `ConfigError`.
  - `[mpd].password_file` names a file holding the config file's password,
    for example a systemd credential. It cannot be combined with
    `[mpd].password`. The shell reads the file (trailing newline removed)
    and the pure `with_password_file` puts its contents in place of the key
    before `resolve_connection` runs.
- **Port:** `--port`, then `[mpd].port`, then `MPD_PORT`, then 6600.
- The config file ranks above the environment because it is specific to this
  daemon, while `MPD_HOST` is often set globally for every MPD client. CLI
  flags rank above both.

`Connection(host, port, password)` has a `__repr__` that masks the password,
and the password is never logged. Log lines show the host and port only.

**Shell.** Right after connecting, and before any other command, the
connection sends `password <secret>` if a password is set. The same happens
on every reconnect.

- A wrong password (`ACK [3@0] … incorrect password`) is a fatal error. The
  daemon exits with a non-zero status instead of retrying, because retrying
  cannot fix a configuration problem.
- A permission error (`ACK [4@0]`) on a later command is treated the same
  way. This happens, for example, when MPD requires a password for `add` and
  none was given.

### 5.6 CLI (`cli.py`, click)

- `mpd-jukebox run`: the daemon.
- `mpd-jukebox list`: print the saved jukebox queue (read-only, from SQLite).
- `mpd-jukebox check`: validate the configuration (including the
  connection settings) and exit. It connects to nothing and does not read
  the password file, so it can run while the system is built.
- `mpd-jukebox select [-n N] [--seed S]`: run one selection against the live
  MPD and print the picks without changing anything. It is always a dry run.
  It reads the play history from the state database if one exists. This is
  useful for debugging filters and constraints.

Global options: `--config`, `--debug` (debug output from this program and
python-mpd2 only), `--state` (the state database; also the top-level
`state_file` key in the config file), `--host`, `--port` and `--password`
(§5.5). On the command line,
`--password` is visible in the process list, so `MPD_HOST` or the config
file is the better place for a password.

---

### 5.7 systemd and NixOS (`nixos/module.nix`)

`run` sends `READY=1` to `$NOTIFY_SOCKET` (`systemd.py`, no libsystemd)
once its signal handlers are installed. The unit is `Type=notify`, so a
`systemctl reload` (SIGHUP) sent during startup waits instead of killing the
process with the default signal action.

The NixOS module `services.mpd-jukebox`:

- `settings` is the TOML config file, with typed defaults for `mpd.host`,
  `mpd.port` (taken from `services.mpd` when it is enabled on the same
  machine), `jukebox.mode` and `jukebox.queue_length`. Other keys pass
  through. The generated file is checked with `mpd-jukebox check` at build
  time and installed as `/etc/mpd-jukebox/config.toml` for running `list` by
  hand.
- `passwordFile` is passed in with `LoadCredential`, and the config file
  points `[mpd].password_file` at the credential. Setting
  `settings.mpd.password` is rejected by an assertion, because it would put
  the password in the Nix store.
- The service runs with `DynamicUser`, `StateDirectory=mpd-jukebox` (the state
  database is `/var/lib/mpd-jukebox/state.sqlite`) and systemd hardening:
  `ProtectSystem=strict`, no capabilities, only `AF_UNIX`/`AF_INET`/`AF_INET6`
  sockets and the `@system-service` system calls. `ExecReload` sends SIGHUP. It is ordered
  after `mpd.service` (or `mpd.socket`) when the local MPD is enabled, and it
  restarts on failure after 10 s.

The VM test (`nixos/test.nix`, flake output `checks.<system>.nixos-module`)
runs two machines with a real MPD, a null audio output and 90 generated
12-second tracks:

- `tcp`: song mode over TCP, against an MPD that refuses everything without
  a password. It checks playback, replacement of finished songs, the
  history and `lastPlayed`/`playCount` stickers, `systemctl reload`, and
  `list`. It also checks that the password appears in neither the journal
  nor the config file.
- `socket`: album mode over a Unix socket; whole albums are queued in track
  order.

## 6. Testing

- The test style follows the existing setup: `unittest` run by pytest, with
  `--cov-fail-under=95`, mypy, and ruff (format and lint).
- **Core.** Plain unit tests over the data, with no mocks. Examples:
  - `select_step` with a fixed seed against hand-computed reservoirs;
  - a property-style check that, over many seeds, picks never break
    uniqueness and never touch pre-existing entries;
  - the trigger table from §1.4 (`qlen > target`, `== target` adds one,
    `< target`, the 99 cap);
  - the timer arithmetic from §1.3;
  - filter parse and evaluate cases taken from myMPD semantics;
  - album keys with and without MBID;
  - the `update()` event sequences: connect→refill→enqueue→play; connect
    with a queue that is already long enough does nothing; a refill failure
    halts the jukebox and `Restart` resumes it; `filling` blocks re-entry.
- **Shell.** A fake MPD client (an in-memory object with the same async
  methods) and a temporary SQLite file. The runtime is tested with scripted
  event sequences.
- **MPD address and password.**
  - Every row of the `parse_mpd_host` table in §5.5, plus a bad `MPD_PORT`.
  - Precedence in `resolve_connection`.
  - A password in `MPD_HOST` is not used when the host comes from the config
    file.
  - A conflicting password in the same source is an error.
  - `repr(Connection)` does not contain the password.
  - With the fake MPD client: `password` is the first command on both
    connections and again after a reconnect; a wrong password makes the run
    exit with an error instead of reconnecting.
- **Migrations.**
  - Every shipped file has a valid name and splits into at least one
    statement.
  - `split_statements` handles a semicolon inside a string literal, comments,
    and a trigger body.
  - `plan()` rejects gaps, duplicate versions, bad names and empty files.
  - `pending()` handles a fresh database, a partially upgraded one, an
    up-to-date one and a too-new one.
  - Upgrading twice does nothing the second time.
  - **Schema equivalence:** for every N, a database created at version N and
    then upgraded ends with the same schema (`sqlite_master`) as a fresh
    database at the latest version.
  - A migration that fails part-way leaves `user_version` and the tables
    unchanged.
  - The read-only reader refuses a database whose version doesn't match.

---

## 7. Deviations from myMPD and decisions

1. **Album enqueue** uses the URIs collected in our album index instead of
   `searchadd` with an album expression. The result is the same unless the
   database changes between refill and enqueue. In that case a missing URI
   fails and is logged, as a failed `add` is in song mode.
2. **Fuzzy `~~`** is not implemented. A filter that uses it is a config
   error, and the daemon refuses to start (*decided*). myMPD would run the
   fuzzy match. Other syntax errors keep myMPD's behaviour: the filter is
   ignored and a warning is logged.
3. **Last-played fallback.** If MPD has no sticker DB, our own history table
   provides the last-played timestamps. myMPD would skip the constraint in
   this case.
4. **Refill shortfall halts the jukebox**, as in myMPD. With strict filters
   or a small library, this happens as soon as fewer than
   `queue_length_song + add` candidates match. *Decided:* match myMPD. myMPD
   sets `mode = off`. We have no `off` mode, so the core sets `halted` and
   stores `last_error`. The daemon keeps running and keeps recording play
   history, but adds nothing until SIGHUP (`Restart`) or a process restart.
5. **Quirks kept on purpose:** empty uniq-tag values collide with each other;
   with `uniq_tag == Title`, the context entries carry `""`; album mode
   computes `add` from the song count; duration bounds are strict.
6. **Lua scripting (`script` mode), MPD partitions, presets and the API**
   are not supported (§2).
7. **No `off` mode and no `autostart`** (*decided*). Running the daemon means
   the jukebox is on; to turn it off, stop the daemon. On connect, myMPD runs
   the jukebox only if `autostart` is set and the queue is empty. We run it
   whenever the queue is shorter than `queue_length`, the same rule as a
   queue change. One consequence: if the daemon starts (or MPD restarts)
   with a short queue, it adds songs and starts playback straight away.
8. **Filter details.** `file` takes MPD's `(file == 'uri')` syntax; myMPD's
   parser cannot parse that form. Dates in `modified-since` and `added-since`
   are read as UTC, where myMPD uses local time. Unicode normalisation
   approximates utf8proc with NFKD, case folding and removal of combining
   marks.
9. **Album tags** are merged across the album's songs: each tag keeps its
   distinct values in order of first appearance. `last_modified` is the
   newest and `added` the oldest of its songs, as in myMPD.
10. **Picks are checked against database changes.** myMPD keeps its jukebox
    queue until it is used up, so songs removed or renamed in the library
    (for example a CUE image split into tracks) fail when their turn comes
    and are skipped then. We also skip them, as myMPD does, but in addition
    drop them as soon as MPD reports a database change, or on connect, when
    the database may have changed while we were away (§5.1).
