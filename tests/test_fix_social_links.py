import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "fix_social_links", Path(__file__).resolve().parent.parent / "scripts" / "fix_social_links.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_markdown_text_is_rebuilt_including_escaped_form():
    prop = {"rich_text": [{"plain_text": "[Facebook](https://f.example)\n\\[Instagram\\](https://i.example)"}]}
    assert mod.needs_fix(prop) == {"facebook": "https://f.example", "instagram": "https://i.example"}


def test_already_linked_or_empty_pages_are_left_alone():
    assert mod.needs_fix({"rich_text": [{"plain_text": "Facebook", "href": "https://f.example"}]}) == {}
    assert mod.needs_fix({"rich_text": []}) == {}
    assert mod.needs_fix(None) == {}
