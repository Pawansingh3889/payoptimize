"""The CLI's one piece of real logic: which address to bind."""

from __future__ import annotations

import pytest

from payoptimize.cli import _bind_host, build_parser


def test_a_laptop_binds_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binding a dev server to every interface is how a demo ends up reachable
    from the conference wifi."""
    monkeypatch.delenv("PAYOPTIMIZE_HOST", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.setattr("payoptimize.cli.Path.exists", lambda self: False)

    assert _bind_host() == "127.0.0.1"


def test_a_container_binds_every_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYOPTIMIZE_HOST", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.setattr("payoptimize.cli.Path.exists", lambda self: True)

    assert _bind_host() == "0.0.0.0"


@pytest.mark.parametrize("var", ["FLY_APP_NAME", "FLY_MACHINE_ID"])
def test_a_fly_machine_binds_every_interface(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    """A Fly machine is a Firecracker microVM, not a container: it has neither
    /.dockerenv nor /run/.containerenv. The first deploy bound loopback and
    fly-proxy could not reach it."""
    monkeypatch.delenv("PAYOPTIMIZE_HOST", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.setattr("payoptimize.cli.Path.exists", lambda self: False)
    monkeypatch.setenv(var, "payoptimize")

    assert _bind_host() == "0.0.0.0"


def test_an_explicit_host_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch for the next platform whose shape I guess wrong."""
    monkeypatch.setattr("payoptimize.cli.Path.exists", lambda self: True)
    monkeypatch.setenv("PAYOPTIMIZE_HOST", "192.0.2.7")

    assert _bind_host() == "192.0.2.7"


def test_serve_defaults_keep_the_generator_on() -> None:
    args = build_parser().parse_args(["serve"])
    assert args.no_generator is False
    assert args.command == "serve"
