{ lib }:

let
  inherit (lib) mkOption types;
  absoluteDirectory = types.addCheck types.str (value: lib.hasPrefix "/" value);
in
{
  path = mkOption {
    type = absoluteDirectory;
    description = ''
      Absolute directory scanned by this rule. Shell-style wildcards
      are expanded by envY before organizing; organize itself does not
      expand them.
    '';
  };
  action = mkOption {
    type = types.enum [ "quarantine" "archive" ];
    description = "Action for matching entries.";
  };
  targets = mkOption {
    type = types.enum [ "files" "dirs" ];
    default = "files";
    description = "Whether the rule selects regular files or directories.";
  };
  depth = mkOption {
    type = types.nullOr types.ints.positive;
    default = null;
    description = ''
      Maximum scan depth below each root, where 1 selects direct
      children. Null scans the whole subtree.
    '';
  };
  names = mkOption {
    type = types.listOf types.str;
    default = [ ];
    description = "Exact entry names to match; empty matches every name.";
  };
  recursiveAge = mkOption {
    type = types.bool;
    default = false;
    description = ''
      Require the entry and every descendant to be older than
      minAgeDays. Only applies to directory rules.
    '';
  };
  processes = mkOption {
    type = types.listOf types.str;
    default = [ ];
    description = ''
      Process names that must not run while the rule is planned or
      executed. A pattern containing a slash is matched against the
      full command line.
    '';
  };
  extensions = mkOption {
    type = types.listOf types.str;
    default = [ ];
    description = "File extensions to match; empty matches all extensions.";
  };
  minAgeDays = mkOption {
    type = types.ints.positive;
    default = 30;
    description = "Minimum entry age in days.";
  };
  minSizeBytes = mkOption {
    type = types.ints.unsigned;
    default = 0;
    description = "Minimum entry size in bytes.";
  };
  optionalRoot = mkOption {
    type = types.bool;
    default = false;
    description = "Skip absent optional application caches; filesystem errors remain errors.";
  };
  protectedOverrides = mkOption {
    type = types.listOf absoluteDirectory;
    default = [ ];
    description = "Specific protected application cache directories authorized by this rule.";
  };
}
