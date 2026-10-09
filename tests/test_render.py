import re

from bginfo.bot.render import callback_data, keyboard, split_html
from bginfo.engine.dialog import Button

TAG = re.compile(r"<(/?)([a-z]+)[^>]*>")


def balanced(chunk: str) -> bool:
    stack = []
    for closing, name in TAG.findall(chunk):
        if not closing:
            stack.append(name)
        elif stack and stack[-1] == name:
            stack.pop()
        else:
            return False
    return not stack


def text_of(chunks: list[str]) -> str:
    return re.sub(r"\s+", "", TAG.sub("", "".join(chunks)))


def test_short_text_untouched():
    assert split_html("<b>hi</b>", 4000) == ["<b>hi</b>"]


def test_splits_on_paragraphs():
    text = "\n\n".join(["а" * 30] * 10)
    chunks = split_html(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert len(chunks) > 1
    assert text_of(chunks) == text_of([text])
    # режем по границе абзаца, а не посреди слова
    assert all(c.endswith("а" * 30) for c in chunks)


def test_splits_on_newline_then_space_then_hard():
    assert all(len(c) <= 50 for c in split_html("\n".join(["слово"] * 40), 50))
    assert all(len(c) <= 50 for c in split_html(" ".join(["слово"] * 40), 50))
    hard = split_html("x" * 205, 50)
    assert all(len(c) <= 50 for c in hard) and "".join(hard) == "x" * 205


def test_open_tags_closed_and_reopened():
    text = "<b>" + " ".join(["жирный"] * 60) + "</b> конец <a href=\"https://e.org/?a=1&amp;b=2\">" + \
           " ".join(["ссылка"] * 60) + "</a>"
    chunks = split_html(text, 120)
    assert len(chunks) > 3
    for c in chunks:
        assert len(c) <= 120
        assert balanced(c), c
    assert any(c.startswith("<a href=") for c in chunks)
    assert text_of(chunks) == text_of([text])


def test_nested_tags():
    text = "<b><i>" + " ".join(["вложено"] * 80) + "</i></b>"
    chunks = split_html(text, 100)
    assert all(len(c) <= 100 and balanced(c) for c in chunks)
    assert chunks[1].startswith("<b><i>")


def test_entities_not_split():
    text = "&amp;" * 100
    chunks = split_html(text, 23)
    assert all(len(c) <= 23 for c in chunks)
    assert "".join(chunks) == text
    assert all(c.count("&") == c.count(";") for c in chunks)


def test_tag_not_broken_when_cut_at_limit():
    text = "x" * 98 + "<b>bold</b>" + "y" * 50
    chunks = split_html(text, 100)
    assert all(len(c) <= 100 and balanced(c) for c in chunks)
    assert text_of(chunks) == text_of([text])


def test_callback_data_limit_and_keyboard():
    assert callback_data(1, 2, "ans", "yes") == "1:2:ans:yes"
    long = callback_data(123456, 99, "tog", "я" * 100)
    assert len(long.encode()) <= 64
    kb = keyboard([[Button("т" * 100, "ans", "x" * 200)], []], 5, 3)
    assert len(kb.inline_keyboard) == 1
    b = kb.inline_keyboard[0][0]
    assert len(b.text) == 64 and len(b.callback_data.encode()) <= 64
    assert b.callback_data.startswith("5:3:ans:")
