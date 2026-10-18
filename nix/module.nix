{self}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.yiju;
in {
  options.services.yiju = {
    enable = lib.mkEnableOption "Yiju vocabulary study";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
    };
    host = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 8000;
    };
    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
    };
  };

  config = lib.mkIf cfg.enable {
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [cfg.port];
    systemd.services.yiju = {
      description = "Yiju vocabulary study";
      wantedBy = ["multi-user.target"];
      wants = ["network-online.target"];
      after = ["network-online.target"];
      environment.YIJU_DATA_DIR = "/var/lib/yiju";
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/yiju serve --host ${lib.escapeShellArg cfg.host} --port ${toString cfg.port}";
        DynamicUser = true;
        StateDirectory = "yiju";
        StateDirectoryMode = "0700";
        WorkingDirectory = "/var/lib/yiju";
        Restart = "on-failure";
        RestartSec = 5;
        UMask = "0077";
        PrivateTmp = true;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
      };
    };
  };
}
