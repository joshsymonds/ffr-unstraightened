{
  description = "Voice-model training environment: CPU and CUDA dev shells from one pinned nixpkgs";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/02c087f3fe673c9e317f206a627cb6b910336dbe";

  outputs =
    { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      # The same pinned nixpkgs, re-imported with CUDA enabled. Only the cuda
      # dev shell uses it, so the default shell's torch stays the CPU build.
      pkgsCuda = import pkgs.path {
        inherit system;
        config = {
          allowUnfree = true;
          cudaSupport = true;
        };
      };

      python = pkgs.python3.withPackages (ps: [
        ps.pytest
        ps.torch
        ps.transformers
        ps.peft
        ps.accelerate
        ps.sentencepiece
        ps.protobuf
      ]);

      pythonCuda = pkgsCuda.python3.withPackages (ps: [
        ps.pytest
        ps.torch
        ps.transformers
        ps.peft
        ps.accelerate
        ps.bitsandbytes
        ps.sentencepiece
        ps.protobuf
      ]);

      # llama.cpp ships its GGUF converter only in the source tree, so it runs
      # from there with the python of the shell that carries it. Each shell
      # passes its own python, so the cuda shell does not pull in CPU torch.
      convertHfToGguf =
        py:
        pkgs.writeShellScriptBin "convert_hf_to_gguf" ''
          exec ${py}/bin/python ${pkgs.llama-cpp.src}/convert_hf_to_gguf.py "$@"
        '';
    in
    {
      devShells.${system} = {
        default = pkgs.mkShell {
          packages = [
            python
            pkgs.llama-cpp
            (convertHfToGguf python)
          ];
        };

        # /run/opengl-driver/lib is where NixOS publishes the host NVIDIA
        # libcuda.so; torch needs it on LD_LIBRARY_PATH at runtime.
        cuda = pkgsCuda.mkShell {
          packages = [
            pythonCuda
            pkgsCuda.llama-cpp
            (convertHfToGguf pythonCuda)
          ];
          LD_LIBRARY_PATH = "${pkgs.lib.makeLibraryPath [
            pkgsCuda.stdenv.cc.cc.lib
            pkgsCuda.zlib
          ]}:/run/opengl-driver/lib";
        };
      };
    };
}
