{
  description = "Joyce local reader development environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      forAllSystems = nixpkgs.lib.genAttrs systems;
    in {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
            ] ++ nixpkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
              pkgs.xdg-utils
            ];

            shellHook = ''
              echo "Joyce development shell: $(python3 --version)"
            '';
          };
        });
    };
}
