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
    };
}
