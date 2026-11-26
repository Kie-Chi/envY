{
  outputs = { self }: {
    sourceProbe = {
      tracked = builtins.readFile ./tracked.txt;
      deletedExists = builtins.pathExists ./deleted.txt;
      newImport = import ./new.nix;
      ignored = builtins.readFile ./ignored-sentinel;
      files = builtins.attrNames (builtins.readDir ./.);
      sourcePath = self.outPath;
    };
  };
}
