{ lib, pkgs }:

let
  python = pkgs.python3Packages;
  simplematch = python.buildPythonPackage {
    pname = "simplematch";
    version = "1.4";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "simplematch";
      version = "1.4";
      sha256 = "55a77278b3d0686cb38e3ffe5a326a5f59c2995f1ba1fa1a4f68872c17caf4cb";
    };
    build-system = [ python.poetry-core ];
    pythonImportsCheck = [ "simplematch" ];
  };
  exifread = python.buildPythonPackage {
    pname = "ExifRead";
    version = "2.3.2";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "ExifRead";
      version = "2.3.2";
      sha256 = "a0f74af5040168d3883bbc980efe26d06c89f026dc86ba28eb34107662d51766";
    };
    build-system = [ python.setuptools ];
    pythonImportsCheck = [ "exifread" ];
  };
  mdfind = python.buildPythonPackage {
    pname = "mdfind-wrapper";
    version = "0.1.3";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "mdfind-wrapper";
      version = "0.1.3";
      sha256 = "084ca010c9932ce7f1ec5351b669d2b347818828fdc3f3e0bbb7eb663a1697c6";
    };
    build-system = [ python.setuptools ];
    pythonImportsCheck = [ "mdfind" ];
  };
  macosTags = python.buildPythonPackage {
    pname = "macos-tags";
    version = "1.5.1";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "macos-tags";
      version = "1.5.1";
      sha256 = "f144c5bc05d01573966d8aca2483cb345b20b76a5b32e9967786e086a38712e7";
    };
    # Upstream still names the pre-poetry-core build backend.
    postPatch = ''
      substituteInPlace pyproject.toml \
        --replace-fail 'poetry.masonry.api' 'poetry.core.masonry.api' \
        --replace-fail 'poetry>=0.12' 'poetry-core'
    '';
    build-system = [ python.poetry-core ];
    dependencies = [ python.xattr mdfind ];
    pythonRelaxDeps = [ "xattr" ];
    pythonImportsCheck = [ "macos_tags" ];
  };
  organize = python.buildPythonApplication {
    pname = "organize-tool";
    version = "3.3.0";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "organize_tool";
      version = "3.3.0";
      sha256 = "034fdcf9ffeb23d21b495e038665278e589fa04dc7c0c0a01a4a3b30a06c539f";
    };
    build-system = [ python.poetry-core ];
    dependencies = (with python; [
      arrow docopt-ng docx2txt jinja2 natsort pdfminer-six platformdirs
      pydantic pyyaml rich send2trash
    ]) ++ [ simplematch exifread ] ++ lib.optional pkgs.stdenv.hostPlatform.isDarwin macosTags;
    # Keep ExifRead pinned as required upstream; allow the newer nixpkgs UI/text libraries.
    pythonRelaxDeps = [ "docx2txt" "rich" "send2trash" ];
    pythonImportsCheck = [ "organize" "organize.cli" ];
    meta = {
      description = "File management automation tool";
      homepage = "https://github.com/tfeldmann/organize";
      license = lib.licenses.mit;
      mainProgram = "organize";
    };
  };
in
organize
