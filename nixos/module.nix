{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.mpd-jukebox;
  mpd = config.services.mpd;
  format = pkgs.formats.toml { };

  # Where systemd puts the password file given by `passwordFile`.
  credential = "/run/credentials/mpd-jukebox.service/mpd-password";

  # The MPD of this machine, if it runs one.
  localMpdHost =
    if !mpd.enable then
      null
    else if
      builtins.elem mpd.settings.bind_to_address [
        "any"
        "0.0.0.0"
        "::"
      ]
    then
      "localhost"
    else
      mpd.settings.bind_to_address;
  localMpdIsSocket = localMpdHost != null && lib.hasPrefix "/" localMpdHost;

  # TOML has no null: unset options are left out of the file.
  settings = lib.filterAttrsRecursive (_: v: v != null) cfg.settings;

  # mpd-jukebox rejects unknown keys and invalid values, so check the
  # generated file at build time: a typo fails `nixos-rebuild`, not the
  # service. `check` connects to nothing and reads no password file.
  configFile =
    pkgs.runCommand "mpd-jukebox.toml"
      {
        nativeBuildInputs = [ cfg.package ];
        config = format.generate "mpd-jukebox.toml" settings;
      }
      ''
        mpd-jukebox --config "$config" check
        cp "$config" "$out"
      '';

  # A TOML table with some declared keys; other keys pass through and are
  # checked by `mpd-jukebox check`.
  table =
    options:
    lib.mkOption {
      type = lib.types.submodule {
        freeformType = format.type;
        inherit options;
      };
      default = { };
      description = "See the options below; other keys are passed through.";
    };

  sandboxing = {
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    PrivateDevices = true;
    NoNewPrivileges = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectClock = true;
    ProtectHostname = true;
    ProtectProc = "invisible";
    RestrictAddressFamilies = [
      "AF_INET"
      "AF_INET6"
      "AF_UNIX"
    ];
    RestrictNamespaces = true;
    RestrictRealtime = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    SystemCallArchitectures = "native";
    SystemCallFilter = [ "@system-service" ];
    CapabilityBoundingSet = "";
    UMask = "0077";
  };
in
{
  options.services.mpd-jukebox = {
    enable = lib.mkEnableOption "mpd-jukebox, which keeps MPD's queue filled with random songs or albums";

    package = lib.mkPackageOption pkgs "mpd-jukebox" { };

    passwordFile = lib.mkOption {
      type = with lib.types; nullOr path;
      default = null;
      example = "/run/secrets/mpd-password";
      description = ''
        File containing the MPD password. It is passed to the service as a
        systemd credential, so it can be readable by root only, and it never
        enters the Nix store.
      '';
    };

    settings = lib.mkOption {
      type = lib.types.submodule {
        freeformType = format.type;
        options = {
          state_file = lib.mkOption {
            type = lib.types.str;
            default = "/var/lib/mpd-jukebox/state.sqlite";
            readOnly = true;
            description = ''
              The state database. It lives in the service's state directory.
            '';
          };
          mpd = table {
            host = lib.mkOption {
              type = with lib.types; nullOr str;
              default = localMpdHost;
              defaultText = lib.literalMD ''
                the address of {option}`services.mpd` if it is enabled
                (`localhost` if it listens on all addresses), otherwise
                `localhost`
              '';
              example = "/run/mpd/socket";
              description = ''
                MPD host name, IP address, or socket path (`/…` or
                `@abstract`). Use {option}`services.mpd-jukebox.passwordFile`
                for the password, not `password@host`.
              '';
            };
            port = lib.mkOption {
              type = with lib.types; nullOr port;
              default = if mpd.enable && !localMpdIsSocket then mpd.settings.port else null;
              defaultText = lib.literalMD ''
                the port of {option}`services.mpd` if it is enabled, otherwise
                6600
              '';
              description = "MPD port; ignored for sockets.";
            };
          };
          jukebox = table {
            mode = lib.mkOption {
              type = lib.types.enum [
                "song"
                "album"
              ];
              default = "song";
              description = "Add single songs or whole albums.";
            };
            queue_length = lib.mkOption {
              type = lib.types.ints.between 1 999;
              default = 1;
              description = ''
                Minimum length of MPD's queue. MPD's consume mode should be
                on, or the queue grows past this and the jukebox stops adding.
              '';
            };
          };
        };
      };
      default = { };
      example = lib.literalExpression ''
        {
          jukebox = {
            mode = "song";
            queue_length = 2;
            last_played = 48;
            uniq_tag = "Album";
            filter_exclude = "(Genre == 'Christmas')";
          };
        }
      '';
      description = ''
        mpd-jukebox's configuration file, as described in its DESIGN.md
        ("Configuration"). It is checked when the system is built.
        `[mpd].password` and `[mpd].password_file` are set from
        {option}`services.mpd-jukebox.passwordFile`; do not set them here.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !(cfg.settings.mpd ? password);
        message = ''
          services.mpd-jukebox.settings.mpd.password would put the password
          in the Nix store; use services.mpd-jukebox.passwordFile.
        '';
      }
    ];

    services.mpd-jukebox.settings.mpd.password_file = lib.mkIf (cfg.passwordFile != null) credential;

    # For running mpd-jukebox by hand:
    #   sudo mpd-jukebox --config /etc/mpd-jukebox/config.toml list
    environment.systemPackages = [ cfg.package ];
    environment.etc."mpd-jukebox/config.toml".source = configFile;

    systemd.services.mpd-jukebox = {
      description = "MPD jukebox";
      wantedBy = [ "multi-user.target" ];
      wants = lib.optional mpd.enable (if mpd.startWhenNeeded then "mpd.socket" else "mpd.service");
      after = [
        "network.target"
      ]
      ++ lib.optional mpd.enable (if mpd.startWhenNeeded then "mpd.socket" else "mpd.service");
      serviceConfig = sandboxing // {
        # Ready once the signal handlers are in place, so an early
        # `systemctl reload` cannot kill it.
        Type = "notify";
        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/mpd-jukebox"
          "--config"
          configFile
          "run"
        ];
        # SIGHUP restarts the jukebox: it clears the jukebox queue and picks
        # again. This also resumes a jukebox halted by a failed refill.
        ExecReload = "${pkgs.coreutils}/bin/kill -HUP $MAINPID";
        DynamicUser = true;
        StateDirectory = "mpd-jukebox";
        LoadCredential = lib.optional (cfg.passwordFile != null) "mpd-password:${cfg.passwordFile}";
        # Lost MPD connections are retried inside the daemon; this covers
        # unexpected crashes. Exit status 78 (EX_CONFIG) means a wrong
        # password, permission or configuration, which restarting cannot
        # fix: the unit stays failed and the journal says why.
        Restart = "on-failure";
        RestartSec = "10s";
        RestartPreventExitStatus = 78;
      };
    };
  };
}
