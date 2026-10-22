"""`jezero eval`: question files, scoring, scopes, comparing runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jezero.cli import main

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "eval.example.toml"


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    path = tmp_path / "jezero.toml"
    path.write_text(f'[storage]\npath = "{tmp_path / "lib.db"}"\n')
    assert main(["-c", str(path), "init"]) == 0
    items = ROOT / "examples" / "items.example.jsonl"
    assert main(["-c", str(path), "import-jsonl", str(items), "--source", "demo"]) == 0
    return path


def test_the_example_file_passes_on_the_demo_items(cfg: Path, capsys) -> None:
    capsys.readouterr()
    assert main(["-c", str(cfg), "eval", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "5/5 found in the top 5" in out
    assert "MISS" not in out


def test_misses_stale_keys_and_fail_under(cfg: Path, tmp_path: Path, capsys) -> None:
    questions = tmp_path / "q.toml"
    questions.write_text(
        """[[case]]
id = "right"
question = "invoices migration rollback"
expect = [1]

[[case]]
id = "wrong-item"
question = "invoices migration rollback"
expect = [3]

[[case]]
id = "gone"
question = "refund test"
expect = [999]
"""
    )
    capsys.readouterr()
    code = main(["-c", str(cfg), "eval", str(questions), "--fail-under", "0.5", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1  # 1 of 3 < 50 %
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["right"]["rank"] == 1 and by_id["right"]["passed"]
    assert not by_id["wrong-item"]["passed"] or by_id["wrong-item"]["rank"] > 1
    assert by_id["gone"]["missing"] == [999]
    assert report["stale"] == 1


def test_sources_scope_hides_other_sources(cfg: Path, tmp_path: Path, capsys) -> None:
    capsys.readouterr()
    assert main(["-c", str(cfg), "eval", str(EXAMPLE), "--sources", "agent-*", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] == 0  # the demo items are in source "demo"


def test_save_and_compare(cfg: Path, tmp_path: Path, capsys) -> None:
    saved = tmp_path / "runs" / "before.json"
    assert main(["-c", str(cfg), "eval", str(EXAMPLE), "--save", str(saved)]) == 0
    before = json.loads(saved.read_text())
    before["results"][0]["passed"] = False  # pretend it used to miss
    before["results"][1]["passed"] = True
    saved.write_text(json.dumps(before))
    capsys.readouterr()
    assert main(["-c", str(cfg), "eval", str(EXAMPLE), "--compare", str(saved)]) == 0
    out = capsys.readouterr().out
    assert "better: rollback" in out and "worse: -" in out


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[[case]]\nid = "a"\nquestion = "q"\n', "expect is required"),
        ('[[case]]\nid = "a"\nquestion = "q"\nexpect = []\n', "non-empty list"),
        ('[[case]]\nid = "a"\nquestion = "q"\nexpect = [1]\nkind = "later"\n', "needs item"),
        ('[[case]]\nid = "a"\nquestion = "q"\nexpect = [1]\nwhy = "x"\n', "unknown keys"),
        ("[defaults]\nk = 5\n", "no [[case]]"),
    ],
)
def test_bad_files_say_why(cfg: Path, tmp_path: Path, capsys, body: str, message: str) -> None:
    questions = tmp_path / "q.toml"
    questions.write_text(body)
    capsys.readouterr()
    assert main(["-c", str(cfg), "eval", str(questions)]) == 2
    assert message in capsys.readouterr().err
