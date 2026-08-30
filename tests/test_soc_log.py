"""tools/soc_log.py -- the passive parts and the one opt-in poll.

None of this spawns ``candump`` / ``cansend``: ``subprocess.Popen`` is
stubbed to raise so every probe constructs in its "listener did not start"
state (``_proc is None``, no threads), then the reader loops are driven
by hand with canned ``candump -L`` lines.
"""

import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import soc_log  # noqa: E402


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    """Any probe that tries to spawn a helper gets OSError -> _proc = None."""
    def boom(*_a, **_kw):
        raise OSError("stubbed: no candump/cansend in tests")

    monkeypatch.setattr(soc_log.subprocess, "Popen", boom)
    monkeypatch.setattr(soc_log.subprocess, "run", boom)


def _noop_log(*_a, **_kw):
    pass


# -- _uds_soc_percent ---------------------------------------------------
def test_uds_soc_percent_endpoints_and_midpoint():
    assert soc_log._uds_soc_percent(0) == 0.0
    assert soc_log._uds_soc_percent(255) == 100.0
    assert soc_log._uds_soc_percent(0x9C) == pytest.approx(61.176, abs=1e-3)


def test_uds_soc_percent_is_linear():
    assert soc_log._uds_soc_percent(128) == pytest.approx(50.196, abs=1e-3)


# -- CandidateProbe ---------------------------------------------------
def _cand():
    return soc_log.CandidateProbe("can0", _noop_log)


def test_candidate_snapshot_none_until_frames_land():
    probe = _cand()
    assert probe.snapshot() == [
        ("3E3.0", None), ("3E3.1", None), ("3E3.6", None),
        ("228.2", None), ("186.6", None),
    ]
    assert probe.fmt() == "cand[3E3.0=-- 3E3.1=-- 3E3.6=-- 228.2=-- 186.6=--]"


def test_candidate_reader_decodes_each_id_and_offset():
    probe = _cand()
    probe._proc = types.SimpleNamespace(stdout=[
        "(1693500000.100000) can0 3E3#A1A2A3A4A5A6A7A8\n",
        "(1693500000.200000) can0 228#00014F0000000000\n",
        "(1693500000.300000) can0 186#0000000000003F00\n",
        "(1693500000.400000) can0 3E9#16800000\n",   # not a candidate -> ignored
    ])
    probe._reader()

    snap = dict(probe.snapshot())
    assert snap["3E3.0"] == 0xA1   # byte 0
    assert snap["3E3.1"] == 0xA2   # byte 1
    assert snap["3E3.6"] == 0xA7   # byte 6
    assert snap["228.2"] == 0x4F   # byte 2
    assert snap["186.6"] == 0x3F   # byte 6
    assert probe.fmt() == "cand[3E3.0=161 3E3.1=162 3E3.6=167 228.2=79 186.6=63]"


def test_candidate_reader_skips_malformed_lines():
    probe = _cand()
    probe._proc = types.SimpleNamespace(stdout=[
        "not a frame at all\n",
        "(1693500000.1) can0 3E3#ZZ\n",          # bad hex
        "(1693500000.2) can0 3E3\n",             # no '#'
        "(1693500000.3) can0 3E3#0102\n",        # valid but too short for b6
    ])
    probe._reader()
    snap = dict(probe.snapshot())
    assert snap["3E3.0"] == 0x01
    assert snap["3E3.6"] is None   # frame had only 2 bytes


def test_candidate_probe_close_is_safe_without_a_process():
    _cand().close()   # _proc is None -> no-op, must not raise


# -- DiagSocProbe ---------------------------------------------------
def _diag(period=10.0, req_id=None):
    return soc_log.DiagSocProbe("can0", period, _noop_log, req_id=req_id)


def test_diag_snapshot_and_fmt_before_any_reply():
    probe = _diag()
    assert probe.snapshot() is None
    assert probe.fmt() == "soc22=n/a"


def test_diag_positive_response_decodes_and_locks_onto_ecu():
    probe = _diag(req_id=None)
    probe._proc = types.SimpleNamespace(stdout=[
        # positive: 04 62 00 5B 9C ...  from 0x7EC
        "(1693500000.500000) can0 7EC#0462005B9C000000\n",
    ])
    probe._reader()

    snap = probe.snapshot()
    assert snap is not None
    pct, raw, rx_id, age = snap
    assert raw == 0x9C
    assert pct == pytest.approx(61.176, abs=1e-3)
    assert rx_id == 0x7EC
    assert age is not None and age >= 0.0
    assert probe.replies == 1
    assert probe._req_id == 0x7E4          # locked onto rx - 8
    assert probe.fmt() == "soc22=61.2%(0x9C@7EC)"


def test_diag_negative_response_counts_as_nrc_only():
    probe = _diag()
    probe._proc = types.SimpleNamespace(stdout=[
        "(1693500000.600000) can0 7EC#037F2231AAAAAAAA\n",   # 7F 22 <NRC 31>
    ])
    probe._reader()
    assert probe.nrcs == 1
    assert probe.replies == 0
    assert probe.snapshot() is None


def test_diag_ignores_other_dids_and_short_frames():
    probe = _diag()
    probe._proc = types.SimpleNamespace(stdout=[
        "(1693500000.7) can0 7E8#0462005A40000000\n",   # 62 but DID 005A, not 005B
        "(1693500000.8) can0 7E8#036200\n",             # too short
        "(1693500000.9) can0 7E8#0462005B10000000\n",  # 005B raw 0x10
    ])
    probe._reader()
    snap = probe.snapshot()
    assert snap is not None
    assert snap[1] == 0x10


def test_diag_pinned_req_id_does_not_relock():
    probe = _diag(req_id=0x7E0)
    assert probe._req_id == 0x7E0
    probe._proc = types.SimpleNamespace(stdout=[
        "(1693500000.500000) can0 7EC#0462005B9C000000\n",
    ])
    probe._reader()
    assert probe._req_id == 0x7E0   # stays pinned even though 0x7EC answered


def test_diag_fmt_marks_stale_reading(monkeypatch):
    probe = _diag(period=10.0)
    probe._proc = types.SimpleNamespace(stdout=[
        "(1693500000.500000) can0 7EC#0462005B9C000000\n",
    ])
    probe._reader()
    # force the stored timestamp far into the past: age > period * 3
    probe._at -= 60.0
    assert probe.fmt() == "soc22=61.2%!(0x9C@7EC)"


def test_diag_close_is_safe_without_a_process():
    probe = _diag()
    probe.close()                 # _proc is None
    assert probe._stop.is_set()


def test_build_parser_help_renders():
    """--help must format without raising (a bare '%' in an action help=
    string trips argparse's help-string %-expansion; regression for the
    --diag-soc help text)."""
    parser = soc_log._build_parser()
    text = parser.format_help()            # raised ValueError before the fix
    assert "--diag-soc" in text


def test_build_parser_accepts_diag_soc_flags():
    parser = soc_log._build_parser()
    args = parser.parse_args(
        ["--yes", "--diag-soc", "--diag-soc-every", "5", "--diag-req-id", "0x7E4"])
    assert args.diag_soc is True
    assert args.diag_soc_every == 5.0
    assert args.diag_req_id == "0x7E4"
