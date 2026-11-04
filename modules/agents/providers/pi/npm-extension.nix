{ pkgs }:

# Unpack a reviewed npm tarball as a Pi package directory. Pi follows its
# package.json "pi.extensions" manifest and supplies the host Pi modules, so
# packages without runtime dependencies need no npm install step. postUnpack
# runs inside the unpacked package for reviewed, drift-checked adaptations.
{ pname, version, url, hash, postUnpack ? "" }:

pkgs.runCommand "${pname}-${version}" {
  src = pkgs.fetchurl { inherit url hash; };
} ''
  mkdir -p "$out"
  tar -xzf "$src" -C "$out" --strip-components=1
  cd "$out"
  ${postUnpack}
''
