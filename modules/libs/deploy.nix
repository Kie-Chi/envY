# Render declarations to files and install them as root, or merge them into
# application-owned files that must stay writable.
{ lib, pkgs, cmds, task }:

rec {
  renderers = {
    ini = attrs: lib.generators.toINI {} attrs;
    yaml = attrs: lib.generators.toYAML {} attrs;
    json = attrs: builtins.toJSON attrs;
    toml = attrs: lib.generators.toTOML {} attrs;
    plain = text: text;
    lines = values: lib.concatStringsSep "\n" values + "\n";
    kv = attrs: renderers.lines (lib.mapAttrsToList (k: v: "${k} ${toString v}") attrs);
    kvEq = attrs: renderers.lines (lib.mapAttrsToList (k: v: "${k} = ${toString v}") attrs);
  };

  renderedText = { format, data }:
    if format == "concat" then
      lib.concatMapStrings (
        item:
          if builtins.isAttrs item && item ? format && item ? data
          then renderedText item
          else throw "Invalid concat item: expected { format, data; }"
      ) data
    else if format == "ini" then renderers.ini data
    else if format == "yaml" then renderers.yaml data
    else if format == "json" then renderers.json data
    else if format == "toml" then renderers.toml data
    else if format == "plain" then data
    else if format == "lines" then renderers.lines data
    else if format == "kv" then renderers.kv data
    else if format == "kvEq" then renderers.kvEq data
    else throw "Unsupported render format: ${format}";

  renderedFile = { name, format, data }:
    pkgs.writeText name (renderedText { inherit format data; });

  deployScript = {
    source,
    target,
    owner ? "root",
    group ? "root",
    mode ? "0644",
    useSudo ? true,
    postDeploy ? ""
  }:
  let
    mkdir = if useSudo then "esudo ${cmds.mkdir}" else cmds.mkdir;
    install = if useSudo then "esudo ${cmds.install}" else cmds.install;
  in ''
    TMP_FILE="$(${cmds.mktemp})"
    cp "${source}" "$TMP_FILE"
    if [ ! -f "${target}" ] || ! ${cmds.cmp} -s "$TMP_FILE" "${target}"; then
      ${mkdir} -p "$(dirname "${target}")"
      ${install} -m ${mode} -o ${owner} -g ${group} "$TMP_FILE" "${target}"
      ${postDeploy}
    fi
    ${cmds.rm} -f "$TMP_FILE"
  '';

  deploy = {
    name,
    format,
    data,
    target,
    owner ? "root",
    group ? "root",
    mode ? "0644",
    post ? ""
  }:
    let src = renderedFile { inherit name format data; };
    in deployScript {
      source = src;
      postDeploy = post;
      inherit target owner group mode;
    };

  # Merge declarations into application-owned files after Home Manager has
  # generated its links, leaving a writable user-owned result.
  mergeWritable = { name, format ? "json", data, target }:
    let
      source = renderedFile { inherit name data; format = "json"; };
      python = pkgs.python3.withPackages (ps: [ ps.pyyaml ps.tomli-w ]);
    in lib.hm.dag.entryAfter [ "linkGeneration" ] ''
      _LOG_CTX="${name}"
      if [ -z "$DRY_RUN_CMD" ]; then
        PYTHONPATH=${../../resources/scripts} ${python}/bin/python -m envy.structured_config \
          --format ${lib.escapeShellArg format} \
          --source ${lib.escapeShellArg (toString source)} \
          --target "$HOME"/${lib.escapeShellArg target}
      fi
    '';

  activation = {
    after ? [ "userBoundary" ],
    pre ? "",
    name,
    format,
    data,
    target,
    owner ? "root",
    group ? "root",
    mode ? "0644",
    post ? "",
    message ? null
  }:
    task.activation {
      inherit after name;
      script = ''
        ${lib.optionalString (message != null) "echo \"${message}\""}
        ${pre}
        ${deploy {
          inherit name format data target owner group mode post;
        }}
      '';
    };
}
