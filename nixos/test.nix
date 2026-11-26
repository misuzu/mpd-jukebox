{ pkgs, module }:
let
  # 30 artists with one 3-track album each; 12 s of silence per track, so
  # songs end (and get scrobbled) while the test runs.
  music =
    pkgs.runCommand "mpd-jukebox-test-music" { nativeBuildInputs = [ pkgs.ffmpeg-headless ]; }
      ''
        for a in $(seq 1 30); do
          mkdir -p "$out/artist$a"
          for t in 1 2 3; do
            ffmpeg -loglevel error -f lavfi -i anullsrc=r=8000:cl=mono -t 12 \
              -c:a flac -metadata artist="Artist $a" -metadata album="Album $a" \
              -metadata title="Track $t" -metadata track="$t" \
              "$out/artist$a/$t.flac"
          done
        done
      '';

  # Test only: a real deployment keeps this file out of the store.
  password = pkgs.writeText "mpd-password" "secret";

  mpdSettings = {
    music_directory = "${music}";
    # Plays in real time without a sound card.
    audio_output = [
      {
        type = "null";
        name = "null";
      }
    ];
  };

  tools = {
    environment.systemPackages = [
      pkgs.mpc
      pkgs.sqlite
    ];
  };
in
pkgs.testers.runNixOSTest {
  name = "mpd-jukebox";

  nodes = {
    # Song mode over TCP, against an MPD that refuses everything without
    # the password. Its music is a writable copy, so songs can be removed
    # under the running jukebox.
    tcp = {
      imports = [
        module
        tools
      ];
      systemd.tmpfiles.rules = [ "C /srv/music - - - - ${music}" ];
      services.mpd = {
        enable = true;
        settings = mpdSettings // {
          music_directory = "/srv/music";
          default_permissions = "";
        };
        credentials = [
          {
            passwordFile = password;
            permissions = [
              "read"
              "add"
              "control"
              "admin"
            ];
          }
        ];
      };
      services.mpd-jukebox = {
        enable = true;
        passwordFile = password;
        settings.jukebox = {
          queue_length = 2;
          last_played = 1;
          advanced = {
            queue_length_song = 20;
            write_stickers = true;
          };
        };
      };
    };

    # Album mode over a Unix socket, without a password.
    socket = {
      imports = [
        module
        tools
      ];
      services.mpd = {
        enable = true;
        settings = mpdSettings // {
          bind_to_address = "/run/mpd/socket";
        };
      };
      services.mpd-jukebox = {
        enable = true;
        settings.jukebox = {
          mode = "album";
          queue_length = 2;
          # Only 30 albums: the default of 25 picked in advance could run
          # out once queued and recently played albums are excluded.
          advanced.queue_length_album = 5;
        };
      };
    };
  };

  testScript = ''
    def mpc(machine, host, command):
        return machine.succeed(f"mpc -h {host} {command}")

    def prepare(machine, host):
        machine.wait_for_unit("mpd.service")
        machine.wait_for_unit("mpd-jukebox.service")
        mpc(machine, host, "update --wait")
        mpc(machine, host, "consume on")
        # A clean start: the database may still have been empty when the
        # jukebox started at boot, which halts it ("found 0 of ...").
        machine.succeed("systemctl stop mpd-jukebox.service")
        mpc(machine, host, "clear")
        machine.succeed("systemctl start mpd-jukebox.service")
        machine.wait_until_succeeds(
            f"mpc -h {host} status | grep -q '\\[playing\\]'", timeout=30
        )

    def playlist(machine, host):
        return mpc(machine, host, "playlist -f %file%").split()

    start_all()

    tcp_host = "secret@localhost"

    with subtest("song mode fills MPD's queue and plays"):
        prepare(tcp, tcp_host)
        tcp.wait_until_succeeds(
            f"test $(mpc -h {tcp_host} playlist | wc -l) -eq 2", timeout=30
        )
        journal = tcp.succeed("journalctl -u mpd-jukebox.service")
        assert "Connected to MPD at 127.0.0.1:6600" in journal, journal
        assert "secret" not in journal, journal
        tcp.fail("grep -r secret /etc/mpd-jukebox/config.toml")

    with subtest("a finished song is replaced"):
        first = mpc(tcp, tcp_host, "current -f %file%").strip()
        tcp.wait_until_succeeds(
            f"test \"$(mpc -h {tcp_host} current -f %file%)\" != '{first}'",
            timeout=30,
        )
        tcp.wait_until_succeeds(
            f"test $(mpc -h {tcp_host} playlist | wc -l) -eq 2", timeout=30
        )
        songs = playlist(tcp, tcp_host)
        artists = {song.split("/")[0] for song in songs}
        assert len(artists) == len(songs), songs

    with subtest("played songs are recorded, with myMPD's stickers"):
        tcp.wait_until_succeeds(
            "test $(sqlite3 /var/lib/mpd-jukebox/state.sqlite "
            "'SELECT COUNT(*) FROM history') -ge 1",
            timeout=30,
        )
        tcp.wait_until_succeeds(
            "test $(sqlite3 /var/lib/mpd/sticker.sql "
            "\"SELECT COUNT(*) FROM sticker WHERE name = 'lastPlayed'\") -ge 1",
            timeout=30,
        )

    with subtest("systemctl reload restarts the jukebox"):
        count = (
            "journalctl -u mpd-jukebox.service | grep -c 'Filling jukebox queue'"
            " || true"
        )
        fills = int(tcp.succeed(count))
        tcp.succeed("systemctl reload mpd-jukebox.service")
        tcp.wait_until_succeeds(
            "journalctl -u mpd-jukebox.service | grep -q 'Restarting the jukebox'",
            timeout=10,
        )
        # The jukebox queue was cleared, so a new list is picked.
        tcp.wait_until_succeeds(f"test $({count}) -gt {fills}", timeout=30)
        tcp.succeed("systemctl is-active mpd-jukebox.service")
        tcp.wait_until_succeeds(
            f"test $(mpc -h {tcp_host} playlist | wc -l) -eq 2", timeout=30
        )

    with subtest("the saved jukebox queue can be listed by hand"):
        output = tcp.succeed(
            "mpd-jukebox --config /etc/mpd-jukebox/config.toml list"
        )
        assert 10 <= len(output.splitlines()) <= 20, output

    def pick(which):
        # `list` needs no password file, even while the service is stopped.
        return tcp.succeed(
            "mpd-jukebox --config /etc/mpd-jukebox/config.toml list "
            f"| {which} -n 1 | cut -f2"
        ).strip()

    with subtest("a song removed from the library is dropped from the picks"):
        # The last pick: the next ones may be added while MPD updates.
        gone = pick("tail")
        tcp.succeed(f"rm '/srv/music/{gone}'")
        mpc(tcp, tcp_host, "update --wait")
        tcp.wait_until_succeeds(
            "! mpd-jukebox --config /etc/mpd-jukebox/config.toml list "
            f"| cut -f2 | grep -qxF '{gone}'",
            timeout=30,
        )
        tcp.succeed(
            "journalctl -u mpd-jukebox.service "
            "| grep -q 'Dropped 1 picked entries'"
        )

    with subtest("a pick that vanished while stopped is skipped, as in myMPD"):
        tcp.succeed("systemctl stop mpd-jukebox.service")
        gone = pick("head")
        tcp.succeed(f"rm '/srv/music/{gone}'")
        mpc(tcp, tcp_host, "update --wait")
        mpc(tcp, tcp_host, "clear")
        tcp.succeed("systemctl start mpd-jukebox.service")
        tcp.wait_until_succeeds(
            f"test $(mpc -h {tcp_host} playlist | wc -l) -eq 2", timeout=30
        )
        tcp.wait_until_succeeds(
            f"mpc -h {tcp_host} status | grep -q '\\[playing\\]'", timeout=30
        )
        journal = tcp.succeed("journalctl -u mpd-jukebox.service")
        assert f"Adding {gone} failed, skipping it" in journal, journal
        assert gone not in playlist(tcp, tcp_host)

    with subtest("a wrong password stops the service without restarts"):
        # Swap the credential for a wrong password with a runtime drop-in.
        tcp.succeed(
            "echo wrong > /run/wrong-password",
            "mkdir -p /run/systemd/system/mpd-jukebox.service.d",
            "printf '[Service]\\nLoadCredential=\\n"
            "LoadCredential=mpd-password:/run/wrong-password\\n' "
            "> /run/systemd/system/mpd-jukebox.service.d/wrong.conf",
            "systemctl daemon-reload",
        )
        # It is ready before it connects (MPD may be away at boot), so the
        # restart itself succeeds and the failure follows.
        tcp.succeed("systemctl restart mpd-jukebox.service")
        tcp.wait_until_succeeds(
            "systemctl is-failed mpd-jukebox.service", timeout=30
        )
        assert "78" in tcp.succeed(
            "systemctl show -p ExecMainStatus --value mpd-jukebox.service"
        )
        # Restart=on-failure would have tried again after 10 s.
        tcp.sleep(12)
        tcp.succeed("systemctl is-failed mpd-jukebox.service")
        assert tcp.succeed(
            "systemctl show -p NRestarts --value mpd-jukebox.service"
        ).strip() == "0"
        assert "incorrect password" in tcp.succeed(
            "journalctl -u mpd-jukebox.service"
        )
        tcp.succeed(
            "rm /run/systemd/system/mpd-jukebox.service.d/wrong.conf",
            "systemctl daemon-reload",
            "systemctl restart mpd-jukebox.service",
        )
        tcp.wait_for_unit("mpd-jukebox.service")

    with subtest("album mode adds whole albums in track order"):
        socket_host = "/run/mpd/socket"
        prepare(socket, socket_host)
        # Two albums of three songs, before the first song (12 s) ends.
        socket.wait_until_succeeds(
            f"test $(mpc -h {socket_host} playlist | wc -l) -eq 6", timeout=10
        )
        songs = playlist(socket, socket_host)
        albums = sorted({song.split("/")[0] for song in songs})
        assert len(albums) == 2, songs
        for album in albums:
            tracks = [s for s in songs if s.startswith(album + "/")]
            assert tracks == [f"{album}/{t}.flac" for t in (1, 2, 3)], songs
        journal = socket.succeed("journalctl -u mpd-jukebox.service")
        assert "Connected to MPD at /run/mpd/socket" in journal, journal
  '';
}
