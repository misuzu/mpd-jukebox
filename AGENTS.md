# AGENTS.md

Guidance for coding agents (and people) working on mpd-jukebox: a Python
daemon that keeps MPD's queue filled with random songs or albums, porting
myMPD's jukebox mode. Read this first, then the parts of
[DESIGN.md](./DESIGN.md) that touch your change.

## Where things are

- `DESIGN.md`: the design, and the source of truth for behaviour. §1 is
  myMPD's behaviour (what we port), §2–§6 our design, §7 the deviations
  from myMPD. Keep it in sync with the code: every behaviour change updates
  it in the same commit.
- `README.md`: for users (usage, configuration, NixOS module). It points to
  DESIGN.md for details instead of repeating them.
- `src/mpd_jukebox/`: the package. `src/tests/`: its tests.
  `src/mpd_jukebox/migrations/`: SQL schema migrations.
- `nixos/module.nix`, `nixos/test.nix`: the NixOS module and its VM test
  against a real MPD.
- `flake.nix`, `package.nix`, `npins/`: Nix packaging; nixpkgs is pinned
  with npins.

## Commands

Everything runs inside the dev shell. Python tooling runs from `src/`.

```sh
nix develop                       # or direnv (.envrc)
cd src
pytest -q                         # tests + mypy + ruff (lint and format check) + coverage
ruff format . && ruff check --fix .
nix build                         # the package; its check phase runs the same suite
nix build .#checks.x86_64-linux.nixos-module -L   # the VM test (Linux, needs KVM)
nix flake check -L                # everything, as CI does
```

- `pytest` is configured in `src/pyproject.toml` to also run mypy and ruff
  and to fail under 95 % coverage. A lint or type error is a test failure.
- The flake only sees files tracked by git. `git add` new files before any
  `nix build` or `nix flake check`, or they are silently missing.
- Format Nix files with `nixfmt` (in the dev shell): `nixfmt flake.nix
  package.nix nixos/*.nix`.
- The VM test takes about a minute and is timing-based (12-second tracks).
  Run it after changing the runtime, the module or anything
  timing-related. A successful run is cached, so to repeat it for the same
  inputs (to check for flakiness, say), add `--rebuild`.

## Architecture in one screen

Functional core, imperative shell (DESIGN.md §3, §4).

- **Core (pure):** `model`, `rng`, `tags`, `filters`, `albums`,
  `constraints`, `select`, `jukebox`, `connection`, `migrate`, and
  `config.parse_config`. No I/O, no clock, no global randomness, no
  mutation visible outside a function. Frozen dataclasses
  (`frozen=True, slots=True`), tuples, frozensets, `MappingProxyType`. Time
  comes in through events (`now`), randomness through explicit seeds
  (`rng.py`, SplitMix64).
- **The state machine:** `jukebox.update(cfg, state, event) -> (state,
  effects)` is the core's single entry point. The shell
  (`runtime.Runtime`) feeds it events and carries out the effects.
- **Selection** is a pure fold: `constraints.song_candidate` /
  `album_candidate`, then `select.select_step`, driven by
  `runtime.Refiller` over MPD's result windows.
- **Shell:** `runtime` (event loop, background tasks, album index cache),
  `mpd_io` (pure parsers of MPD results plus the `MpdIO` wrapper), `store`
  (aiosqlite), `cli` (click), `systemd` (`sd_notify`).

### Rules that are easy to break

- **Concurrency flags** (DESIGN.md §4.6): `filling`, `enqueuing`,
  `validating`/`revalidate`, `halted`. Before changing any handler in
  `jukebox.py`, read §4.6 and keep its table true. In particular, nothing
  may take entries from the jukebox queue while a refill runs, because
  `RefillDone` replaces it with a snapshot.
- **One MPD connection.** python-mpd2's asyncio client multiplexes `idle`
  and commands itself. It has **no command lists**, so send separate
  commands.
- **Background tasks** (`StartRefill`, `Enqueue`, `Validate`) report back
  with events (`RefillDone`/`RefillFailed`, `Enqueued`, `Validated`). The
  core's flags allow one of each at a time. On reconnect, `Connected`
  resets them and the shell drops the old events.
- **The album index** is cached per database generation in `Refiller`. Use
  `album_index(io)` for a current index; it shares a build in progress.
  Don't add code that scans the whole library: it is about 20 MB and 10 s
  of MPD time on a 56,000-song library (DESIGN.md §4.5).
- **Tags:** the connection asks MPD for only the tags in use (`tagtypes`,
  `constraints.needed_tags`). If you make the code read a new tag, add it
  to `needed_tags`, or MPD will not send it. The fake MPD enforces this.
- **myMPD fidelity:** behaviour follows myMPD unless DESIGN.md §7.1 lists a
  deviation. A new deviation gets an entry there, with the reason.

## Common changes

- **New config key:**
  - `config.py`: allowed keys, validation and range; `ConfigError` on bad
    values. Unknown keys are errors.
  - `config.fingerprint`, if the key changes what may be picked: a saved
    jukebox queue is then discarded when it changes.
  - `model.Config`.
  - `needed_tags`, if it involves tags.
  - The README config block, DESIGN.md §5.4, and the NixOS module if it
    needs a typed option.
- **New event or effect:** define it in `jukebox.py` and add it to the
  `Event`/`Effect` unions and to `update`'s dispatch. Interpret it in
  `Runtime._apply`. List it in DESIGN.md §4.2. Unit-test it in
  `test_jukebox.py` with plain data; no mocks are needed there.
- **New MPD command:** wrap it in `MpdIO`. Call `check_fatal(e)` on
  `CommandError`, so password and permission errors stay fatal. Add the
  command to `tests/fake_mpd.py`; the fake should behave like MPD,
  including `tagtypes` filtering via `_out`.
- **Schema change:** add `src/mpd_jukebox/migrations/NNNN_description.sql`.
  Never edit a released migration. No `BEGIN`, `COMMIT` or
  `PRAGMA user_version` in the file: `store.upgrade` owns the transaction
  and the version (DESIGN.md §5.3.1).

## Testing conventions

- `unittest` test cases run by pytest; `IsolatedAsyncioTestCase` for async
  code. Use the helpers: `tests/helpers.song`, `tests/fake_mpd.FakeMPD`
  (`track`, `error`, `notify`, `finish_song`, `lose_connection`,
  `fail_add`, `commands` for asserting what was sent).
- Runtime tests run a real `Runtime` against `FakeMPD` and poll with
  `wait_for(...)`. Stop it and await its task before reusing its store.
  Register temp-dir cleanup with `addCleanup` in `setUp`, so it runs after
  the async cleanups, not in `tearDown`.
- Never touch real file descriptors (`os.dup2` on stdout and the like)
  inside tests: pytest uses them for capturing. Mock them instead.
- Platform-specific behaviour (abstract Unix sockets are Linux-only) needs
  `unittest.skipUnless`. The package is also built on macOS.
- Fixed seeds make selection deterministic; assert properties
  (uniqueness, untouched entries) rather than exact picks where you can.

## Behaviour worth knowing before you debug

- **Exit status 78 (`EX_CONFIG`)** means only a configuration change can
  help: invalid configuration, wrong password or permission, or a database
  from a newer version. The NixOS unit does not restart on it
  (`RestartPreventExitStatus=78`).
- **Readiness:** `run` notifies systemd once its signal handlers are
  installed (`Type=notify`), before connecting to MPD, so an MPD that is
  not up yet does not fail the unit.
- **`list` and `check` never read the password file.** It is a systemd
  credential that exists only while the service runs.
- **Halting:** a refill that finds too few songs halts the jukebox, as in
  myMPD; so do endless failed adds. SIGHUP (`systemctl reload`) restarts
  it.
- **Picks are checked against MPD's database** on every database change and
  connect. Failed adds are replaced by the next picks, as in myMPD.

## Working with the repository

- Commit messages: an imperative subject of at most about 72 characters,
  then a body explaining what changed and why. Keep DESIGN.md (and the
  README, if users notice) in the same commit as the code.
- Don't put names of private machines, hosts or people's libraries into
  commits, code, tests or docs. Describe situations generically ("a
  56,000-song library", "a CUE image split into tracks").
- When investigating a live MPD, use read-only commands only (`status`,
  `search`, `find`, `lsinfo`, `sticker find`, `playlistinfo`). Never print
  or log passwords. `Connection.__repr__` masks them; keep it that way.
- The myMPD sources are the reference for ported behaviour: commit
  `aa6f5936306693314bf0ab786c9e9a7aeaf5483d` (2026-11-23, `v26.1.3-4`) of
  https://github.com/jcorporation/myMPD. DESIGN.md §1 describes that
  commit, with file paths relative to its `src/`. When porting more
  behaviour, or comparing with a newer myMPD, say which commit you read.
