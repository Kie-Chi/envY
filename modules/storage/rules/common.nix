{ home }:

{
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
