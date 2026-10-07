{self, ...}: {
  flake.nixosModules.default = import ./module.nix {inherit self;};
  imports = [
    ./devshell.nix
    ./python-packages.nix
    ./treefmt.nix
  ];
}
