{
  perSystem = {pkgs, ...}: {
    devShells.default = pkgs.mkShell {
      packages = with pkgs; [python312 uv just nushell stdenv.cc ruff ty];
      UV_PYTHON = "${pkgs.python312}/bin/python3.12";
      UV_PYTHON_DOWNLOADS = "never";
      PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
      PLAYWRIGHT_NODEJS_PATH = "${pkgs.nodejs}/bin/node";
      PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
      LD_LIBRARY_PATH = "${pkgs.stdenv.cc.cc.lib}/lib";
    };
  };
}
