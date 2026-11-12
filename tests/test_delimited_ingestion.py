"""Delimited-document fidelity and non-empty chunk preservation."""
from datetime import datetime

import pytest

from core.ingestion.builder import DocumentIndexBuilder
from core.ingestion.parser import DocumentParser


def test_table_rows_drop_only_real_blanks_and_escape_markdown():
    rows = [
        ['Title', '', ''],
        ['Field', 'Value', ''],
        ['Literal', 'N/A', ''],
        ['Escaped', 'a|b', 'line 1\nline 2'],
        ['', '', ''],
    ]

    text = DocumentParser._rows_to_page_text(rows, '=== Sheet: Synthetic ===')

    assert 'Unnamed:' not in text
    assert 'N/A' in text
    assert '| Field | Value |' in text
    assert '| --- | --- |' in text
    assert 'a\\|b' in text
    assert 'line 1<br>line 2' in text
    assert not any(line.startswith('nan') for line in text.splitlines())


def test_excel_preserves_literal_na_and_normalizes_cells(tmp_path):
    pytest.importorskip('pandas')
    openpyxl = pytest.importorskip('openpyxl')
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = 'Synthetic'
    sheet.append(['Workbook title', None])
    sheet.append(['Field', 'Value'])
    sheet.append(['Literal missing marker', 'N/A'])
    sheet.append(['Integer', 1])
    sheet.append(['Date', datetime(2026, 1, 2, 0, 0, 0)])
    path = tmp_path / 'synthetic.xlsx'
    workbook.save(path)

    document = DocumentParser().parse(str(path))
    text = document.pages[0].raw_text

    assert 'N/A' in text
    assert 'Unnamed:' not in text
    assert 'nan' not in text
    assert '| 1 |' in text
    assert '1.0' not in text
    assert '2026-01-02' in text
    assert '00:00:00' not in text


def test_csv_is_supported_and_preserves_literal_na(tmp_path):
    pytest.importorskip('pandas')
    path = tmp_path / 'synthetic.csv'
    path.write_bytes('\ufeffField;Value\r\nLiteral;N/A\r\nInteger;1\r\n'.encode('utf-8'))

    document = DocumentParser().parse(str(path))
    text = document.pages[0].raw_text

    assert len(document.pages) == 1
    assert '| Field | Value |' in text
    assert 'N/A' in text
    assert '| Integer | 1 |' in text


def test_empty_csv_fails_instead_of_creating_an_empty_document(tmp_path):
    pytest.importorskip('pandas')
    path = tmp_path / 'empty.csv'
    path.write_text('', encoding='utf-8')

    with pytest.raises(RuntimeError, match='CSV parsing produced no content'):
        DocumentParser().parse(str(path))


def test_markdown_table_chunks_repeat_the_real_header():
    rows = [['Field', 'Value']] + [[f'Row {i}', str(i)] for i in range(40)]
    text = DocumentParser._rows_to_page_text(rows)

    chunks = DocumentParser()._chunk_by_structure(text, max_chunk_size=100)

    assert len(chunks) > 1
    assert all('| Field | Value |' in chunk for chunk in chunks if '| Row ' in chunk)


def test_excel_preserves_good_sheets_and_reports_failed_sheets(monkeypatch, tmp_path):
    pd = pytest.importorskip('pandas')

    class PartialWorkbook:
        sheet_names = ['good', 'bad']

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def parse(self, sheet_name, **kwargs):
            if sheet_name == 'bad':
                raise ValueError('broken sheet')
            return pd.DataFrame([['Field', 'Value'], ['Literal', 'N/A']])

    monkeypatch.setattr('core.ingestion.parser.pd.ExcelFile', lambda path: PartialWorkbook())
    path = tmp_path / 'partial.xlsx'
    path.write_bytes(b'synthetic')

    document = DocumentParser()._parse_excel(path)

    assert len(document.pages) == 1
    assert 'N/A' in document.pages[0].raw_text
    assert any('broken sheet' in warning for warning in document.metadata['parse_warnings'])


def test_short_nonempty_chunks_are_not_discarded():
    builder = DocumentIndexBuilder.__new__(DocumentIndexBuilder)
    builder.parser = DocumentParser()
    items = []
    text = 'x' * 30 + '\na'

    builder._process_section_chunks(
        [(1, 1, text)], 'section', 'Section', 20, 0, items,
    )

    assert items
    assert 'a' in ''.join(item[5] for item in items)
    assert all(item[5].strip() for item in items)
