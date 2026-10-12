import pytest

from text_guard import clean_generated, has_content


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"Hello world"', "Hello world"),
        ("'Hello world'", "Hello world"),
        ("«Привет, мир»", "Привет, мир"),
        ("“Quoted”", "Quoted"),
        ('  "  padded  "  ', "padded"),
        ('""nested""', "nested"),            # repeated wrapping quotes are all stripped
        ("plain text", "plain text"),
    ],
)
def test_wrapping_quotes_are_stripped(raw, expected):
    assert clean_generated(raw) == expected


def test_inner_quotes_are_preserved():
    text = 'He said "no" and left'
    assert clean_generated(text) == text


def test_text_starting_and_ending_with_separate_quotes_is_treated_as_wrapped():
    # Documents current behaviour: the function only checks the first and last character,
    # so two separate quoted fragments lose their outer quotes.
    assert clean_generated('"first" and "second"') == 'first" and "second'


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_input_returns_empty_string(raw):
    assert clean_generated(raw) == ""


def test_has_content_counts_letters_and_digits_only():
    assert has_content("Hello 123", min_alnum=5)
    assert not has_content("!!! ... ???", min_alnum=1)
    assert not has_content("ab", min_alnum=3)


def test_has_content_handles_none():
    assert not has_content(None, min_alnum=1)
