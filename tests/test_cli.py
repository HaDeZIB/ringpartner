from __future__ import annotations

import io
import sys

import httpx
import respx
from conftest import API

from ringpartner import build_event, cli
from ringpartner.simulator import encode
from ringpartner.webhooks import sign

KEY = "k"


@respx.mock
def test_devices_table_never_prints_token(monkeypatch, capsys, devices_doc):
    monkeypatch.setenv("RING_ACCESS_TOKEN", "super-secret-token")
    respx.get(f"{API}/v1/devices").mock(return_value=httpx.Response(200, json=devices_doc))
    assert cli.main(["devices"]) == 0
    out = capsys.readouterr()
    assert "Front Door" in out.out and "encrypted" in out.out
    assert "super-secret-token" not in out.out + out.err


def test_missing_token(monkeypatch, capsys):
    monkeypatch.delenv("RING_ACCESS_TOKEN", raising=False)
    assert cli.main(["devices"]) == 2
    assert "RING_ACCESS_TOKEN" in capsys.readouterr().err


def test_verify_command(monkeypatch, capsys):
    body = encode(build_event("button_press", "dev1"))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body)))
    assert cli.main(["verify", "--key", KEY, "--signature", sign(KEY, body)]) == 0
    assert "button_press" in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body + b" ")))
    assert cli.main(["verify", "--key", KEY, "--signature", sign(KEY, body)]) == 1


@respx.mock
def test_simulate_command(capsys):
    route = respx.post("http://localhost:8000/hook").mock(return_value=httpx.Response(204))
    rc = cli.main(
        [
            "simulate",
            "motion_detected",
            "--device",
            "dev1",
            "--url",
            "http://localhost:8000/hook",
            "--key",
            KEY,
            "--sub-type",
            "human",
        ]
    )
    assert rc == 0
    req = route.calls.last.request
    assert req.headers["x-signature"] == sign(KEY, req.content)
    assert b'"sub_type":"human"' in req.content


@respx.mock
def test_capture_fixtures_tolerates_failures(tmp_path, monkeypatch, capsys, devices_doc):
    monkeypatch.setenv("RING_ACCESS_TOKEN", "t")
    respx.get(f"{API}/v1/devices").mock(return_value=httpx.Response(200, json=devices_doc))
    respx.route(host="api.amazonvision.com").mock(
        return_value=httpx.Response(403, json={"errors": [{"status": "403", "title": "Forbidden"}]})
    )
    assert cli.main(["capture-fixtures", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "recorded 1 calls" in out and "FAILED" in out
    assert (tmp_path / "index.json").exists()
