import argparse

import pytest

from hedgebot.cli import _load, _require_bot_stopped, main


def args(**kw):
    return argparse.Namespace(config="config.example.yaml", **kw)


def test_paper_and_live_use_separate_journals():
    assert _load(args(mode="paper")).journal_path == "data/journal-paper.sqlite"
    assert _load(args(mode="live")).journal_path == "data/journal.sqlite"


def test_flatten_and_selftest_refuse_while_the_bot_holds_the_lock(tmp_path):
    cfg = _load(args(mode="live"))
    cfg.journal_path = str(tmp_path / "journal.sqlite")
    held = _require_bot_stopped(cfg)
    with pytest.raises(SystemExit, match="running"):
        _require_bot_stopped(cfg)
    held.close()
    _require_bot_stopped(cfg).close()


def test_selftest_needs_a_trading_mode():
    with pytest.raises(SystemExit, match="real orders"):
        main(["selftest", "-c", "config.example.yaml"])
