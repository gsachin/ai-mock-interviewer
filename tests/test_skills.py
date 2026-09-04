"""interviewer.skills: question-bank discovery + upload validation (pure
functions over tmp dirs — never the real question_banks folder)."""
import pytest

from interviewer.skills import (
    bank_doc_id,
    discover_local_banks,
    normalize_skill_name,
    parse_markdown_shape,
    unusable_bank_files,
    validate_upload,
)

GOOD = """# HTML Question Bank

## Semantic HTML

Explain semantic HTML. Expected points: meaning of elements, accessibility
benefits, SEO implications, and when a div is still appropriate.
"""


# ── name normalization ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("html.md", "html"),
    ("HTML.MD", "html"),
    ("UPPER-CASE.md", "upper-case"),   # lowercased like the extension handling
    ("system-design.md", "system-design"),
    ("javascript.md", "javascript"),
    ("  css.md  ", "css"),
    ("dsa", "dsa"),                    # plain slug (no extension) is fine too
])
def test_normalize_skill_name_accepts(raw, expected):
    assert normalize_skill_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "..\\evil.md", "../evil.md", "a/b.md", "a\\b.md",
    ".hidden.md", "web.md.exe", "not a skill.md",
    "leading-.md", "bank-系统.md", "x" * 65 + ".md", "x.md.md",
])
def test_normalize_skill_name_rejects(raw):
    with pytest.raises(ValueError):
        normalize_skill_name(raw)


def test_bank_doc_id_shape():
    assert bank_doc_id("html") == "bank-html"
    assert bank_doc_id("system-design") == "bank-system-design"


# ── shape parser (mirrors the RAG splitter) ─────────────────────────────────

def test_parse_markdown_shape_counts_sections_and_drops_front_matter():
    text = "# Title\n\nfront matter\n\n## Q one\n\nbody a. Expected points: x.\n\n" \
           "## Q two\n\nbody b. Expected points: y.\n"
    headings, errors = parse_markdown_shape(text)
    assert errors == []
    assert headings == ["Q one", "Q two"]


def test_parse_markdown_shape_flags_problems():
    # empty headings, empty bodies, and no sections at all
    headings, errors = parse_markdown_shape("# Title only\n")
    assert headings == [] and errors
    text = "## \n\nbody\n\n## No body\n"
    headings, errors = parse_markdown_shape(text)
    assert headings == []
    assert any("empty heading" in e for e in errors)
    assert any("no body" in e for e in errors)
    text = "## Broken\n\n## Also broken\n"
    headings, errors = parse_markdown_shape(text)
    assert headings == [] and len(errors) == 2


# ── upload validation ───────────────────────────────────────────────────────

def test_validate_upload_accepts_bank_shaped_corpus():
    validate_upload(GOOD, "html")          # no exception


def test_validate_upload_rejects_bad_name_or_corpus():
    with pytest.raises(ValueError, match="invalid skill name"):
        validate_upload(GOOD, "Web Dev")
    with pytest.raises(ValueError, match="Expected points"):
        validate_upload("# T\n\n## Q\n\nanswer the question please", "html")
    with pytest.raises(ValueError, match="no '## ' question sections"):
        validate_upload("just prose, no sections", "html")
    with pytest.raises(ValueError, match="empty heading"):
        validate_upload("## \n\nbody only", "html")


# ── folder discovery ────────────────────────────────────────────────────────

def test_discover_local_banks_over_tmp_dir(tmp_path):
    (tmp_path / "html.md").write_text(GOOD, encoding="utf-8")
    (tmp_path / "javascript.md").write_text(GOOD, encoding="utf-8")
    (tmp_path / "dsa.md").write_text(GOOD, encoding="utf-8")
    (tmp_path / "Bad Name.md").write_text(GOOD, encoding="utf-8")  # not a slug
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")     # not md

    banks = discover_local_banks(tmp_path)
    assert [b.name for b in banks] == ["dsa", "html", "javascript"]
    assert banks[1].doc_id == "bank-html"
    assert banks[1].path == tmp_path / "html.md"

    unusable = unusable_bank_files(tmp_path)
    assert [p.name for p in unusable] == ["Bad Name.md"]


def test_discover_local_banks_matches_real_folder_shape():
    """The real question_banks folder must contain at least the original four
    domains (the regression floor for every consumer that unions this list)."""
    names = {b.name for b in discover_local_banks()}
    assert {"system-design", "ios", "dsa", "devops"} <= names
