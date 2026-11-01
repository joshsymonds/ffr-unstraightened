{
  description = "FINAL FANTASY RESONANCE dialogue mod and its toolchain";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      py = pkgs.python3Packages;

      # Epic's Oodle runtime. The game's paks are Oodle-compressed, and repak
      # loads this library from the directory holding its own executable.
      oodle = pkgs.stdenv.mkDerivation {
        pname = "oodle-core";
        version = "2.9.10";
        src = pkgs.fetchurl {
          url = "https://github.com/WorkingRobot/OodleUE/raw/5e38cb6c99c588b51cde0cae4a6420d6bc865605/Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/2.9.10/linux/lib/liboo2corelinux64.so.9";
          hash = "sha256-7X6Y9wvhJUqAZE79OuRC/2H4VKL+neuwuXi5UomITpw=";
        };
        dontUnpack = true;
        nativeBuildInputs = [ pkgs.autoPatchelfHook ];
        buildInputs = [ pkgs.stdenv.cc.cc.lib ];
        installPhase = "install -Dm755 $src $out/lib/liboo2corelinux64.so.9";
      };

      # repak resolves /proc/self/exe, so the binary is copied (not linked)
      # into the same bin/ as the Oodle library.
      repak = pkgs.runCommand "repak-oodle-${pkgs.repak.version}" { meta.mainProgram = "repak"; } ''
        install -Dm755 ${pkgs.repak}/bin/repak $out/bin/repak
        ln -s ${oodle}/lib/liboo2corelinux64.so.9 $out/bin/
      '';

      binsl = py.buildPythonPackage rec {
        pname = "binsl";
        version = "0.1.2";
        pyproject = true;
        src = py.fetchPypi {
          inherit pname version;
          hash = "sha256-grqo+E9amBaAH+4NSe0otYltDPbj7XV1H+8q5pDpxUU=";
        };
        build-system = [ py.setuptools ];
        pythonImportsCheck = [ "binsl" ];
      };

      pylocres = py.buildPythonPackage {
        pname = "pylocres";
        version = "0.2-unstable-2026-06-17";
        pyproject = true;
        src = pkgs.fetchFromGitHub {
          owner = "stas96111";
          repo = "pylocres";
          rev = "833cc0ad8c43dc16be380894e0ba4a681ba7a9fc";
          hash = "sha256-HxLAxEguwx7w+P/4pTgoinLOhUdzRvbwKHpFbayVGYA=";
        };
        build-system = [ py.setuptools ];
        dependencies = [
          binsl
          py.click
          py.polib
        ];
        pythonRelaxDeps = [ "binsl" ];
        pythonImportsCheck = [ "pylocres" ];
      };

      ffrd = pkgs.writers.writePython3Bin "ffrd" {
        libraries = [
          pylocres
          py.polib
        ];
        flakeIgnore = [ "E501" ];
      } (builtins.replaceStrings [ "@repak@" ] [ "${repak}/bin/repak" ] (builtins.readFile ./tools/ffrd.py));
    in
    {
      packages.${system} = {
        inherit
          oodle
          repak
          pylocres
          ffrd
          ;
        default = ffrd;
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = [
          ffrd
          repak
          (py.python.withPackages (_: [
            pylocres
            py.polib
          ]))
          pkgs.gettext
          pkgs.poedit
        ];
      };
    };
}
