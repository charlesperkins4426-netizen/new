"""Create a source-only ZIP from an explicit allow-list (never include runtime data)."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
FILES = ["app.py", "requirements.txt", ".env.example", "README.md"]
DIRECTORIES = {"gateway": {".py"}, "web": {".html", ".css", ".js"},
               "deploy": {".service"}, "docs": {".md"}}


def build(destination: Path):
    paths = [ROOT / name for name in FILES]
    for directory, suffixes in DIRECTORIES.items():
        paths += sorted(path for path in (ROOT / directory).rglob("*")
                        if path.is_file() and path.suffix in suffixes
                        and not {"__pycache__", "tests"}.intersection(path.relative_to(ROOT).parts))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for path in paths:
            if path.is_symlink():
                raise ValueError(f"Refusing symlink: {path.name}")
            archive.write(path, Path("dialx-openai-gateway") / path.relative_to(ROOT))
    print(destination)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    build(parser.parse_args().destination)
