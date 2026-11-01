{ pkgs, lib, ... }:
let
  # The same pinned nixpkgs, re-imported with CUDA enabled. Only the cuda
  # profile uses it, so the default shell's torch stays the CPU build.
  pkgsCuda = import pkgs.path {
    inherit (pkgs.stdenv.hostPlatform) system;
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
  # from there with the python of the profile that carries it. Each profile
  # passes its own python, so the cuda profile does not pull in CPU torch.
  convertHfToGguf =
    py:
    pkgs.writeShellScriptBin "convert_hf_to_gguf" ''
      exec ${py}/bin/python ${pkgs.llama-cpp.src}/convert_hf_to_gguf.py "$@"
    '';
in
{
  packages = [
    python
    pkgs.llama-cpp
    (convertHfToGguf python)
  ];

  profiles.cuda.module = {
    packages = lib.mkForce [
      pythonCuda
      pkgsCuda.llama-cpp
      (convertHfToGguf pythonCuda)
    ];
    # /run/opengl-driver/lib is where NixOS publishes the host NVIDIA
    # libcuda.so; torch needs it on LD_LIBRARY_PATH at runtime.
    env.LD_LIBRARY_PATH = "${lib.makeLibraryPath [
      pkgsCuda.stdenv.cc.cc.lib
      pkgsCuda.zlib
    ]}:/run/opengl-driver/lib";
  };
}
