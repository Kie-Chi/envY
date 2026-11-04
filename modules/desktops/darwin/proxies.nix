{ config, lib, ... }:

let
  proxyStatus = config.envy.darwin.services.mihomo.mode;
  proxyConfigured = proxyStatus != "none";
  mihomoEnabled = proxyConfigured
    && !(builtins.elem "mihomo" config.envy.darwin.software.homebrew.formulae.exclude);
  proxychainsEnabled = proxyConfigured
    && !(builtins.elem "proxychains-ng" config.envy.darwin.software.homebrew.formulae.exclude);
in
{
  sops.secrets.proxy-url = {
    sopsFile = ../../../secrets/secrets.yaml;
    key = "proxy/url";
  };

  sops.templates."mihomo-config" = lib.mkIf mihomoEnabled {
    path = "${config.home.homeDirectory}/.config/mihomo/config.yaml.tmpl";
    content = ''
      ${builtins.readFile ../../../files/proxies/mihomo_tailscale.yaml}

      # interface-name: @PHYS_IFACE@
      tun:
        enable: ${if config.envy.darwin.services.mihomo.tun then "true" else "false"}
        stack: mixed
        auto-route: true
        strict-route: false
        auto-detect-interface: true
        dns-hijack:
          - "any:53"
        external-controller-cors:
          allow-private-network: true
          allow-origins:
            - '*'
        route-exclude-address:
          - 100.64.0.0/10
          - 192.168.255.0/24

      proxy-providers:
        my-sub:
          type: http
          url: ${config.sops.placeholder.proxy-url}
          path: ./sub.yaml
          interval: 3600
          health-check:
            enable: true
            url: http://www.gstatic.com/generate_204
            interval: 300
    '';
  };

  xdg.configFile."proxychains/proxychains.conf" = lib.mkIf proxychainsEnabled {
    text = ''
      strict_chain
      proxy_dns
      remote_dns_subnet 224

      [ProxyList]
      socks5 127.0.0.1 20122
    '';
  };

  programs.zsh.shellAliases = lib.mkIf proxychainsEnabled {
    proxy = "proxychains4 -f ${config.envy.user.home}/.config/proxychains/proxychains.conf";
  };
}
