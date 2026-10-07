{inputs, ...}: {
  imports = [inputs.treefmt-nix.flakeModule];
  perSystem = {pkgs, ...}: {
    treefmt = {
      projectRootFile = "flake.nix";
      settings.excludes = ["uv.lock"];
      programs.alejandra.enable = true;
      programs.deadnix.enable = true;
      programs.just.enable = true;
      programs.ruff-format.enable = true;
      programs.prettier = {
        enable = true;
        includes = ["*.css" "*.js"];
      };
      settings.formatter.djlint = {
        command = "${pkgs.writeShellScript "format-jinja" ''
          ${pkgs.djlint}/bin/djlint "$@" || test "$?" -eq 1
        ''}";
        options = ["--reformat" "--quiet" "--profile" "jinja" "--indent" "2"];
        includes = ["*.html"];
      };
      programs.taplo = {
        enable = true;
        settings.formatting = {
          reorder_arrays = false;
          reorder_keys = true;
        };
      };
    };
  };
}
