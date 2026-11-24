{
  lib,
  python3Packages,
}:
let
  # Name and version come from pyproject.toml. The dependencies below must
  # match its lists; the build's runtime dependency check fails if a
  # dependency is missing or outside the declared bounds.
  pyproject = (lib.importTOML ./src/pyproject.toml).project;
in
python3Packages.buildPythonPackage {
  pname = pyproject.name;
  inherit (pyproject) version;
  pyproject = true;

  src = ./src;

  build-system = with python3Packages; [ flit-core ];

  dependencies = with python3Packages; [
    aiosqlite
    click
    python-mpd2
  ];

  # The `dev` extra of pyproject.toml, plus the hook that runs pytest.
  nativeCheckInputs = with python3Packages; [
    pytestCheckHook
    pytest-cov
    pytest-mypy
    pytest-ruff
  ];

  pythonImportsCheck = [ "mpd_jukebox" ];

  meta = {
    description = "MPD Jukebox";
    license = lib.licenses.mit;
    mainProgram = "mpd-jukebox";
    platforms = lib.platforms.all;
  };
}
