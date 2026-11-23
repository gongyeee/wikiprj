"""Isolated MarkItDown converter; called only by api.document_conversion."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.document_conversion import _convert_direct, conversion_error_details


if __name__ == "__main__":
    output = Path(sys.argv[2])
    try:
        output.write_text(_convert_direct(sys.argv[1]), encoding="utf-8")
    except Exception as exc:
        cause = exc.__cause__
        output.with_suffix(".error.json").write_text(json.dumps({"errorType": type(exc).__name__,
            "causeType": type(cause).__name__ if cause else None,
            "missingModule": getattr(cause, "name", None), "causes": conversion_error_details(exc)}), encoding="utf-8")
        sys.exit(1)
