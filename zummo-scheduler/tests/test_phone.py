import pytest

from app.utils.phone import normalize_phone, phones_match


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+15551234567", "+15551234567"),
        ("5551234567", "+15551234567"),
        ("(555) 123-4567", "+15551234567"),
        ("1-555-123-4567", "+15551234567"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_phone(raw) == expected


def test_normalize_empty_raises():
    with pytest.raises(ValueError):
        normalize_phone("")


def test_phones_match():
    assert phones_match("(555) 123-4567", "+15551234567")
    assert not phones_match("5551234567", "5559999999")
