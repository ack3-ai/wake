import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wake.testing.coverage import export_coverage


def test_export_coverage_survives_interrupted_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coverage_path = tmp_path / "wake-coverage.cov"
    coverage_path.write_text('{"complete": true}')

    written = []

    def interrupted_dump(data, file):
        written.append(Path(file.name))
        file.write('{"version"')
        raise KeyboardInterrupt

    monkeypatch.setattr(json, "dump", interrupted_dump)

    with pytest.raises(KeyboardInterrupt):
        export_coverage(SimpleNamespace(source_units={}), {}, {}, {}, {})

    assert json.loads(coverage_path.read_text()) == {"complete": True}
    # the temporary file is per process and matches the .gitignore entry `wake up` adds
    assert written == [Path(f"wake-coverage.cov.{os.getpid()}.tmp")]
    assert list(tmp_path.glob("wake-coverage.cov.*.tmp")) == []
