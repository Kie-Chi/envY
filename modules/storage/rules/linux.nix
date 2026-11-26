{ home }:

{
  go-build-cache = {
    action = "quarantine";
    depth = null;
    extensions = [  ];
    minAgeDays = 30;
    minSizeBytes = 0;
    names = [  ];
    path = "${home}/.cache/go-build";
    processes = [  ];
    recursiveAge = false;
    targets = "files";
    optionalRoot = true;
  };
}
