{inputs, ...}: {
  perSystem = {
    lib,
    pkgs,
    self',
    ...
  }: let
    workspace = inputs.uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ../.;};
    pythonSet = (pkgs.callPackage inputs.pyproject-nix.build.packages {python = pkgs.python3;}).overrideScope (lib.composeManyExtensions [
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
    environment = pythonSet.mkVirtualEnv "yiju-checks" workspace.deps.default;
  in {
    checks.package = self'.packages.default;
    packages.default = pythonSet.mkVirtualEnv "yiju" workspace.deps.default;
    apps.default = {
      type = "app";
      program = "${pythonSet.mkVirtualEnv "yiju" workspace.deps.default}/bin/yiju";
      meta.description = "Private Chinese vocabulary and sentence study";
    };
    checks.python =
      pkgs.runCommand "yiju-checks" {
        nativeBuildInputs = [environment pkgs.ruff pkgs.ty];
        PYTHONPATH = pkgs.python3Packages.makePythonPath (with pkgs.python3Packages; [pytest playwright]);
        PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
        PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
        FONTCONFIG_FILE = pkgs.makeFontsConf {fontDirectories = [pkgs.dejavu_fonts];};
      } ''
        cp -r ${../.} source
        chmod -R u+w source
        cd source
        export HOME="$TMPDIR"
        ruff check .
        ty check --python ${environment}/bin/python
        python -m pytest -p no:cacheprovider
        touch "$out"
      '';
  };
}
