{
  perSystem = {pkgs, ...}: {
    devShells.default = pkgs.mkShell {
      packages = with pkgs; [python3 uv just nushell stdenv.cc ruff ty];
      UV_PYTHON = "${pkgs.python3}/bin/python3";
      UV_PYTHON_DOWNLOADS = "never";
      PYTHONPATH = pkgs.python3Packages.makePythonPath (with pkgs.python3Packages; [pytest playwright]);
      PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
      PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
      LD_LIBRARY_PATH = "${pkgs.stdenv.cc.cc.lib}/lib";
    };
  };
}
