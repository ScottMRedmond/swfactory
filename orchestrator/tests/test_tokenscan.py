"""The token scan: finds and redacts token-like strings in prose and log lines."""

import pytest

from factory import tokenscan

TOKENS = [
    "sk-ant-oat01-AbCdEf0123456789_-xyz",
    "sk-ant-api03-q1w2e3r4t5y6u7i8o9p0",
    "ghs_16C7e42F292c6912E7710c838347Ae178B4a",
    "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxMjMifQ.c2lnbmF0dXJlLXBhcnQ",
]


@pytest.mark.parametrize("token", TOKENS)
def test_finds_and_redacts_each_kind_of_token(token):
    text = f"the value was {token} today"

    assert tokenscan.find(text)
    assert tokenscan.redact(text) == "the value was [REDACTED] today"


def test_redacts_a_private_key_block():
    text = "key: -----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY----- done"

    assert tokenscan.find(text)
    assert tokenscan.redact(text) == "key: [REDACTED] done"


def test_redacts_a_private_key_cut_off_before_its_end_marker():
    text = "-----BEGIN PRIVATE KEY-----\nMIIEow"
    assert tokenscan.redact(text) == "[REDACTED]"


def test_leaves_ordinary_text_alone():
    text = "sk-ant is the prefix; ghs_ and a.b.c are not tokens; run 412 failed check_failed"

    assert tokenscan.find(text) == []
    assert tokenscan.redact(text) == text
