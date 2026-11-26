{ home }:

{
  fontconfig-cache = {
    path = "${home}/.cache";
    names = [ "fontconfig" ];
    targets = "dirs";
    depth = 1;
    recursiveAge = true;
    action = "quarantine";
    prune = true;
    minAgeDays = 7;
    optionalRoot = true;
  };
  nvim-lua-cache = {
    path = "${home}/.cache/nvim";
    names = [ "luac" ];
    targets = "dirs";
    depth = 1;
    recursiveAge = true;
    action = "quarantine";
    prune = true;
    minAgeDays = 7;
    optionalRoot = true;
    processes = [ "nvim" ];
  };
  envy-python-cache = {
    path = "${home}/.cache/envy";
    names = [ "pycache" ];
    targets = "dirs";
    depth = 1;
    recursiveAge = true;
    action = "quarantine";
    prune = true;
    minAgeDays = 7;
    optionalRoot = true;
  };
  envy-build-cache = {
    path = "${home}/.cache/envy";
    names = [ "cargo-target" "envy-tui-target" ];
    targets = "dirs";
    depth = 1;
    recursiveAge = true;
    action = "quarantine";
    prune = true;
    minAgeDays = 7;
    optionalRoot = true;
    processes = [ "cargo" "rustc" ];
  };
  old-installers = {
    action = "quarantine";
    depth = null;
    extensions = [ "dmg" "pkg" "iso" ];
    minAgeDays = 30;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/Downloads";
    processes = [  ];
    recursiveAge = false;
    targets = "files";
    optionalRoot = true;
  };
}
