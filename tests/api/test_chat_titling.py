"""Unit tests for chat titling heuristics and cleaners in src.api.ui."""

from src.api.ui import _clean_title, _fallback_title


def test_fallback_title_strips_question_prefixes():
    cases = [
        ("What is the unit name of product CAP03", "Unit Name Of Product CAP03"),
        ("give me warehouse details of finished goods", "Warehouse Details Of Finished Goods"),
        ("total qty adjusted for product SHP08", "Total Qty Adjusted For Product SHP08"),
        ("what products does Apple sell", "Apple Sell"),
        ("how many stock adjustments happened today", "Stock Adjustments Happened Today"),
        ("How many parties are in the database", "Parties Are In The Database"),
        ("What specific OCR engines are used in rag", "OCR Engines Are Used In Rag"),
        ("can you tell me about revenue growth in 2024", "Revenue Growth In 2024"),
        ("show me all items in warehouse 1", "Items In Warehouse 1"),
        ("list distinct vendors in the system", "Vendors In The System"),
    ]
    for prompt, expected in cases:
        result = _fallback_title(prompt)
        assert result == expected, f"Prompt '{prompt}' gave '{result}', expected '{expected}'"


def test_fallback_title_bounds_and_empty():
    assert _fallback_title("") == "New Chat"
    assert _fallback_title("   ") == "New Chat"
    assert _fallback_title("hello") == "Hello"
    assert _fallback_title("hello there friend") == "Hello There Friend"
    assert len(_fallback_title("alpha beta gamma delta epsilon zeta eta theta").split()) <= 6


def test_clean_title_normalizes_llm_output():
    assert _clean_title("Title: Warehouse Inventory Status Report") == "Warehouse Inventory Status Report"
    assert _clean_title("Chat Title - Fiscal Year 2025 Summary") == "Fiscal Year 2025 Summary"
    assert _clean_title('"Apple Tax Rate"') == ""
    # titles are 4-6 words: up to six are kept; longer ones are cut, and dangling stop words dropped
    assert _clean_title("Overview of Global Operations for 2025") == "Overview of Global Operations for 2025"
    assert _clean_title("Overview of Global Operations for the 2025 Fiscal Year") == "Overview of Global Operations"
    assert _clean_title("") == ""
