# mpd-jukebox

mpd-jukebox keeps [MPD](https://www.musicpd.org/)'s queue filled with random
songs or whole albums, the way [myMPD](https://github.com/jcorporation/myMPD)'s
jukebox mode does, as a small standalone daemon. It works with any MPD
client: run it next to MPD and the music never stops.

- Adds single songs or whole albums, from the whole database or from one
  stored playlist.
- Follows myMPD's rules for what to pick: no repeated artist (or another
  tag of your choice) across the queue and recent plays, nothing played in
  the last 24 hours, optionally no hated songs, include and exclude filters,
  and minimum and maximum song length.
- Records what was played, and writes myMPD's `playCount` and `lastPlayed`
  stickers if you want, so it can replace myMPD's jukebox or share a library
  with it.
- Picks its songs ahead of time and keeps them across restarts, so adding
  the next song is instant.

How it works, and how it differs from myMPD, is in [DESIGN.md](./DESIGN.md).

## Quick start

Turn on MPD's consume mode first. The jukebox only tops the queue up to a
minimum length; without consume mode, played songs stay in the queue, it
never gets short, and the jukebox stops adding.

```sh
mpc consume on

# Connects to localhost:6600, or to MPD_HOST / MPD_PORT like mpc does.
export MPD_HOST=secret@localhost
mpd-jukebox run
```

With an empty queue, the jukebox adds a song and starts playing. From then
on it adds the next song about 10 seconds before the current one ends, or
as soon as the queue gets shorter than `queue_length`.

Other commands:

```sh
mpd-jukebox select -n 10    # what a refill would pick; changes nothing
mpd-jukebox list            # the songs or albums picked in advance
mpd-jukebox check           # validate the configuration and exit
```

`select --seed N` makes the picks repeatable, which helps when tuning
filters.

### Signals

- `SIGHUP` restarts the jukebox: it forgets the songs it picked in advance
  and picks again. Use it to resume a halted jukebox (below), or to make
  edits to a playlist or tags take effect right away.

Songs removed from the library are dropped from the picks as soon as MPD
finishes its database update. One that can't be added for another reason is
skipped, and the next pick takes its place.
- `SIGINT` and `SIGTERM` stop it.

When a refill can't find enough songs (100 in song mode, 25 albums in album
mode, plus the ones about to be added), the jukebox halts, as myMPD's does:
it logs why and adds nothing until `SIGHUP` or a restart. This usually
means the filters are too strict or the library is too small. Lower
`queue_length_song` / `queue_length_album` (below) for a small library.

## Connecting to MPD

The host, port and password come from, highest priority first:

1. `--host`, `--port`, `--password`
2. the configuration file's `[mpd]` table
3. `MPD_HOST` and `MPD_PORT`
4. `localhost:6600`

`MPD_HOST` and `--host` accept everything mpc accepts: `host`,
`password@host`, `/path/to/socket`, `password@/path/to/socket`, `@abstract`
and `password@@abstract`. A password embedded in one source is never sent
to a host from another source.

The exact rules, and why they are what they are, are in
[DESIGN.md](./DESIGN.md) (§5.5).

`--password` is visible in the process list. Use `MPD_HOST`, or
`password_file` in the configuration file, instead. A wrong password, or a
missing one when MPD requires it, stops the daemon with an error; it does
not retry.

## Configuration file

Everything is optional; these are the defaults:

```toml
# Where picked songs and play history are kept.
# state_file = "/path/to/state.sqlite"  # default: $XDG_STATE_HOME/mpd-jukebox/state.sqlite

[mpd]
# host = "localhost"
# port = 6600
# password_file = "/run/secrets/mpd-password"  # or: password = "…"

[jukebox]
mode = "song"               # or "album"
playlist = "Database"       # or the name of a stored playlist (song mode)
queue_length = 1            # minimum length of MPD's queue, 1–999
uniq_tag = "Artist"         # a tag that must not repeat
last_played = 24            # hours before a song may play again, 0–5000
ignore_hated = false        # skip songs with the sticker like = 0
min_song_duration = 0       # seconds, exclusive; 0 = no limit
max_song_duration = 0       # seconds, exclusive; 0 = no limit
filter_include = ""         # e.g. "(Genre == 'Rock')"
filter_exclude = ""         # e.g. "((Genre == 'Christmas') AND (Artist != 'Mariah'))"

[jukebox.advanced]
queue_length_song = 100     # songs picked in advance, 10–1000
queue_length_song_min = 10  # refill below this, 10–500
queue_length_album = 25     # albums picked in advance, 5–250
queue_length_album_min = 5  # refill below this, 5–125
last_played_count = 2000    # history kept, plus all within last_played; 0 = none
write_stickers = false      # write playCount and lastPlayed like myMPD

[albums]
group_tag = ""              # also split albums by this tag, e.g. "Date"
use_mbid = true             # identify albums by MusicBrainz album ID
unknown = false             # songs without an Album tag form "Unknown Album"
split_by_directory = false  # an album is one folder; copies elsewhere are left out
```

```sh
mpd-jukebox --config jukebox.toml run    # or MPD_JUKEBOX_CONFIG=jukebox.toml
```

Unknown keys and invalid values are errors. Changing what may be picked
(the `[jukebox]` keys other than `queue_length`, or the `[albums]` table)
discards the songs picked in advance at the next start, as myMPD does.

- **Recent plays.** The "last played" check uses MPD's `lastPlayed`
  stickers when MPD has a sticker database, as myMPD does, and the
  jukebox's own play history otherwise. Turn on `write_stickers` if nothing
  else (such as myMPD) writes them. Leave it off when myMPD runs too, or
  plays are counted twice.
- **Album mode** adds whole albums in disc and track order, and counts
  albums: with `queue_length = 2` and an empty queue, it adds two albums.
  It ignores `playlist`, the song durations and `ignore_hated`. Like
  myMPD, it groups songs into albums by tags (MusicBrainz album ID, or
  AlbumArtist and Album), so stray copies of an album's songs elsewhere in
  the library are added with it. With `split_by_directory`, an album is
  one folder, with its disc and bonus-disc subfolders; of the folders
  holding an album's songs, the one with the most is used, and stray
  copies and other editions are left out. Folders count as parts of one
  album when they share no song title (DESIGN.md §7.1).
- **Filters** use myMPD's subset of MPD's filter syntax: clauses joined
  with `AND`, each `(TAG OP 'value')` with `==`, `!=`, `contains`,
  `starts_with`, `=~` or `!~` (regular expressions). `any`, `file ==`,
  `base`, `modified-since`, `added-since` and `prio >=` also work.
  Comparisons ignore case and accents. A filter that doesn't parse is
  ignored with a warning, as in myMPD. Fuzzy matching (`~~`) is not
  supported and is an error.

## NixOS module

```nix
# in your flake
inputs.mpd-jukebox.url = "github:misuzu/mpd-jukebox/v0.1";

# in your NixOS configuration
imports = [
  # the module, and the overlay providing pkgs.mpd-jukebox
  inputs.mpd-jukebox.nixosModules.default
];
```

`nixosModules.mpd-jukebox` is the module alone, for when you provide
`pkgs.mpd-jukebox` yourself.

```nix
services.mpd-jukebox = {
  enable = true;
  passwordFile = "/run/secrets/mpd-password";  # if MPD needs one
  settings.jukebox = {
    mode = "song";
    queue_length = 2;
    uniq_tag = "Album";
    filter_exclude = "(Genre == 'Christmas')";
  };
};
```

- `settings` is the configuration file above, as a freeform TOML
  attribute set. The generated file is checked with `mpd-jukebox check`
  when the system is built, so a typo fails `nixos-rebuild`, not the
  service.
- With `services.mpd` enabled on the same machine, the jukebox connects to
  it (TCP or socket, whichever MPD listens on) and starts after it.
  Otherwise set `settings.mpd.host` and `settings.mpd.port`.
- `passwordFile` reaches the service as a systemd credential; the password
  never enters the Nix store. `settings.mpd.password` is rejected for that
  reason.
- The service runs as a dynamic user with systemd sandboxing, and keeps its
  state in `/var/lib/mpd-jukebox`. `systemctl reload mpd-jukebox` sends
  `SIGHUP`. A wrong password or configuration leaves the service failed
  (exit status 78) instead of restarting it every 10 seconds; the journal
  says why.
- The configuration is also at `/etc/mpd-jukebox/config.toml`, for
  `sudo mpd-jukebox --config /etc/mpd-jukebox/config.toml list`.

The module doesn't turn on MPD's consume mode; do that once with
`mpc consume on` (MPD keeps it in its state file).

## Development

```sh
nix develop
cd src && python -m pytest    # tests, mypy, ruff and coverage
nix build                     # the package (runs the tests too)
nix flake check               # also the NixOS module's VM test, on Linux
```

The VM test runs the module against a real MPD. nixpkgs is pinned with
npins; `npins update` (in the dev shell) moves it forward.

## License

This project is released under the MIT License. See [LICENSE](./LICENSE).
