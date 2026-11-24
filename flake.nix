{
  outputs =
    inputs:
    let
      sources = import ./npins;

      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];

      forAllSystems =
        f:
        builtins.listToAttrs (
          map (system: {
            name = system;
            value = f system;
          }) systems
        );

      pkgsFor =
        system:
        import sources.nixpkgs {
          inherit system;
          overlays = [ inputs.self.overlays.default ];
        };
    in
    {
      overlays.default = final: prev: {
        mpd-jukebox = final.callPackage ./package.nix { };
      };

      # A development shell with the package's dependencies, including the
      # ones for running its tests.
      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShell {
            inputsFrom = [ pkgs.mpd-jukebox ];
            packages = [ pkgs.npins ];
          };
        }
      );

      packages = forAllSystems (system: {
        default = (pkgsFor system).mpd-jukebox;
      });

      # The module alone expects `pkgs.mpd-jukebox`; `default` also adds the
      # overlay that provides it.
      nixosModules = {
        mpd-jukebox = ./nixos/module.nix;
        default = {
          imports = [ inputs.self.nixosModules.mpd-jukebox ];
          nixpkgs.overlays = [ inputs.self.overlays.default ];
        };
      };

      # NixOS VM tests run on Linux only.
      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          nixos-module = import ./nixos/test.nix {
            inherit pkgs;
            module = inputs.self.nixosModules.mpd-jukebox;
          };
        }
      );
    };
}
