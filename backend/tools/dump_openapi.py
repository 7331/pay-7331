"""Writes the API's OpenAPI document to a file: ``uv run python tools/dump_openapi.py <path>`` from backend/.

The server does not serve it. Importing the app reads only the log level, so this needs no settings, database or
network. Keys are sorted and indented so two dumps diff line by line.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # after the path above, so the script runs from any directory


def dump_openapi_document(output_path: Path) -> None:
    output_path.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    argument_parser = argparse.ArgumentParser(description="Write the API's OpenAPI document as JSON.")
    argument_parser.add_argument("output_path", type=Path, help="the file to write")
    dump_openapi_document(argument_parser.parse_args().output_path)


if __name__ == "__main__":
    main()
