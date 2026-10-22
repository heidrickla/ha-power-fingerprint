"""The offline tools' REST client verifies certificates unless told not to."""

import importlib.util
import pathlib
import ssl

_PATH = pathlib.Path(__file__).resolve().parents[1] / "tools" / "_ha.py"
_spec = importlib.util.spec_from_file_location("pf_tools_ha", _PATH)
_ha = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ha)


def test_certificates_are_verified_by_default(monkeypatch):
    monkeypatch.delenv("HA_INSECURE", raising=False)
    ctx = _ha.tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_a_value_other_than_1_still_verifies(monkeypatch):
    monkeypatch.setenv("HA_INSECURE", "true")
    assert _ha.tls_context().verify_mode == ssl.CERT_REQUIRED


def test_ha_insecure_accepts_a_self_signed_certificate(monkeypatch):
    monkeypatch.setenv("HA_INSECURE", "1")
    ctx = _ha.tls_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
