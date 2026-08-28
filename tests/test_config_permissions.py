"""The guard that keeps the agent off a config other users can read.

It holds calendar passwords in plaintext, and the check is the only thing
standing between a mistyped `chmod` and them: worth a test of its own, because
the failure is silent in both directions. A guard that stopped firing would let
the agent sync happily from a world-readable file, and a guard that fired
regardless of MEERCAL_INSECURE_CONFIG would leave a PaaS deployment in a
restart loop with no way out.
"""

from __future__ import annotations

import pytest

from agent.main import check_config_permissions


@pytest.fixture()
def config(tmp_path, monkeypatch):
    """A config file at a mode of the test's choosing, pointed at by the loader."""

    def write(mode: int):
        path = tmp_path / "meercal.toml"
        path.write_text("[server]\n", encoding="utf-8")
        path.chmod(mode)
        monkeypatch.setenv("MEERCAL_CONFIG", str(path))
        monkeypatch.delenv("MEERCAL_INSECURE_CONFIG", raising=False)
        return path
    return write


def test_mode_600_passes(config):
    config(0o600)
    check_config_permissions()


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o666])
def test_any_read_bit_beyond_the_owner_stops_it(config, mode):
    config(mode)
    with pytest.raises(SystemExit) as exc:
        check_config_permissions()
    # The message has to carry the way out, because the place it is read is a
    # container log scrolling past on a restart loop.
    assert "MEERCAL_INSECURE_CONFIG" in str(exc.value)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_insecure_config_demotes_it_to_a_warning(config, monkeypatch, value):
    config(0o644)
    monkeypatch.setenv("MEERCAL_INSECURE_CONFIG", value)
    check_config_permissions()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
def test_anything_else_is_not_consent(config, monkeypatch, value):
    config(0o644)
    monkeypatch.setenv("MEERCAL_INSECURE_CONFIG", value)
    with pytest.raises(SystemExit):
        check_config_permissions()


def test_no_file_is_not_an_error(monkeypatch, tmp_path):
    # The server runs from the environment alone, and so can an agent whose
    # accounts arrive some other way. Only a file that exists has a mode.
    monkeypatch.setenv("MEERCAL_CONFIG", str(tmp_path / "absent.toml"))
    check_config_permissions()
    monkeypatch.setenv("MEERCAL_CONFIG", "")
    check_config_permissions()
