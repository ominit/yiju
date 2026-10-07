{
  perSystem = {pkgs, ...}: {
    devShells.default = pkgs.mkShell {
      packages = with pkgs; [python312 uv just nushell];
      UV_PYTHON = "${pkgs.python312}/bin/python3.12";
      UV_PYTHON_DOWNLOADS = "never";
    };
  };
}
