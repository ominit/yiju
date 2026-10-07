{inputs, ...}: {
  perSystem = {
    lib,
    pkgs,
    self',
    ...
  }: let
    workspace = inputs.uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ../.;};
    pythonSet = (pkgs.callPackage inputs.pyproject-nix.build.packages {python = pkgs.python312;}).overrideScope (lib.composeManyExtensions [
      inputs.pyproject-build-systems.overlays.wheel
      (workspace.mkPyprojectOverlay {sourcePreference = "wheel";})
      (final: prev: {
        jieba = prev.jieba.overrideAttrs (old: {
          nativeBuildInputs = old.nativeBuildInputs ++ final.resolveBuildSystem {setuptools = [];};
        });
        yiju = prev.yiju.overrideAttrs (old: {
          nativeBuildInputs = old.nativeBuildInputs ++ final.resolveBuildSystem {hatchling = [];};
        });
      })
    ]);
    environment = pythonSet.mkVirtualEnv "yiju-checks" workspace.deps.all;
  in {
    checks.package = self'.packages.default;
    packages.default = pythonSet.mkVirtualEnv "yiju" workspace.deps.default;
    apps.default = {
      type = "app";
      program = "${pythonSet.mkVirtualEnv "yiju" workspace.deps.default}/bin/yiju";
      meta.description = "Private Chinese vocabulary and sentence study";
    };
    checks.python = pkgs.runCommand "yiju-checks" {nativeBuildInputs = [environment];} ''
      cp -r ${../.} source
      chmod -R u+w source
      cd source
      export HOME="$TMPDIR"
      ruff check .
      ty check --python ${environment}/bin/python
      pytest -p no:cacheprovider
      touch "$out"
    '';
  };
}
