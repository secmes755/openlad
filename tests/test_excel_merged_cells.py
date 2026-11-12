"""Real Excel merge declarations restore only their owned cell rectangles."""
import pytest

from core.ingestion.parser import DocumentParser


def test_xlsx_merged_regions_repeat_only_the_anchor_value(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    pytest.importorskip('pandas')
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = 'Merged'
    sheet.merge_cells('A1:B2')
    sheet['A1'] = 'N/A'
    sheet['A3'] = 'outside'
    sheet.merge_cells('A4:B4')
    sheet['A4'] = 0
    sheet.merge_cells('A5:B5')
    sheet['A5'] = False
    path = tmp_path / 'merged.xlsx'
    workbook.save(path)
    workbook.close()

    text = DocumentParser().parse(str(path)).pages[0].raw_text
    lines = text.splitlines()

    assert lines[1] == '| N/A | N/A |'
    assert lines[3] == '| N/A | N/A |'
    assert '| outside |  |' in lines
    assert '| 0 | 0 |' in lines
    assert '| False | False |' in lines
    assert text.count('N/A') == 4


def test_xls_merged_regions_use_the_real_xls_declarations(tmp_path):
    xlwt = pytest.importorskip('xlwt')
    pytest.importorskip('xlrd')
    pytest.importorskip('pandas')
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet('Merged')
    sheet.write_merge(0, 1, 0, 1, 'N/A')
    sheet.write(2, 0, 'outside')
    sheet.write_merge(3, 3, 0, 1, False)
    path = tmp_path / 'merged.xls'
    workbook.save(str(path))

    text = DocumentParser().parse(str(path)).pages[0].raw_text
    lines = text.splitlines()

    assert lines[1] == '| N/A | N/A |'
    assert lines[3] == '| N/A | N/A |'
    assert '| outside |  |' in lines
    assert '| False | False |' in lines
    assert text.count('N/A') == 4


def test_merge_metadata_failure_preserves_the_sheet_and_warns(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip('openpyxl')
    pytest.importorskip('pandas')
    path = tmp_path / 'plain.xlsx'
    workbook = openpyxl.Workbook()
    workbook.active['A1'] = 'content'
    workbook.save(path)
    workbook.close()
    monkeypatch.setattr(
        'core.ingestion.spreadsheet_merged.read_merged_regions',
        lambda path, sheet: (_ for _ in ()).throw(OSError('synthetic metadata failure')),
    )

    document = DocumentParser().parse(str(path))

    assert 'content' in document.pages[0].raw_text
    assert any('merged-cell metadata unavailable' in w
               for w in document.metadata.get('parse_warnings', []))
