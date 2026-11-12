from app.tools.notion import markdown_to_blocks


def types(md):
    return [b["type"] for b in markdown_to_blocks(md)]


def text_of(block):
    inner = block[block["type"]]
    return inner["rich_text"][0]["text"]["content"]


def test_paragraphs_and_blank_lines():
    blocks = markdown_to_blocks("first line\n\nsecond line\n   \n")
    assert [b["type"] for b in blocks] == ["paragraph", "paragraph"]
    assert text_of(blocks[0]) == "first line"


def test_headings():
    blocks = markdown_to_blocks("# Title\n## Sub\n### Deep")
    assert [b["type"] for b in blocks] == ["heading_1", "heading_2", "heading_3"]
    assert text_of(blocks[1]) == "Sub"


def test_bullets_and_star_bullets():
    blocks = markdown_to_blocks("- mic gain low\n* test outdoors")
    assert [b["type"] for b in blocks] == ["bulleted_list_item"] * 2
    assert text_of(blocks[1]) == "test outdoors"


def test_todos_checked_and_unchecked():
    blocks = markdown_to_blocks("- [ ] order parts\n- [x] email supplier")
    assert [b["type"] for b in blocks] == ["to_do", "to_do"]
    assert blocks[0]["to_do"]["checked"] is False
    assert blocks[1]["to_do"]["checked"] is True
    assert text_of(blocks[0]) == "order parts"


def test_empty_markdown_gives_no_blocks():
    assert markdown_to_blocks("") == []
    assert markdown_to_blocks("\n\n") == []


def test_mixed_document_order_preserved():
    md = "# Field notes\nobserved today:\n- [ ] check antenna\n- gain too low"
    assert types(md) == ["heading_1", "paragraph", "to_do", "bulleted_list_item"]
