# -*- coding: utf-8 -*-
"""Pure-stdlib XLSX (OOXML) writer for the magiah web UI — spec UI_SPEC.md §7A / §11.

Public API
----------
``write_workbook(path, sheets)``
    Pure function, no DB access. Writes a valid ``.xlsx`` workbook using only
    ``zipfile`` + string building (no third-party packages).

    ``sheets`` is a list of dicts::

        {
          "name":    str,                      # sheet name (Hebrew OK; sanitized, <=31 chars, deduped)
          "headers": [str, ...],               # header row (bold, frozen, autoFilter)
          "rows":    iterable of lists,        # each cell: str | int | float | None
          "widths":  [float, ...],             # OPTIONAL per-column widths (Excel width units).
                                               # Default is 18 for every column. Because rows may be
                                               # a one-shot generator (streamed), widths cannot be
                                               # inferred by sampling — pass them explicitly for
                                               # long-text columns (e.g. 50 for a snippet column).
        }

Guarantees (per spec):
  * Every sheet: ``<sheetView rightToLeft="1">``, frozen top row (ySplit=1,
    topLeftCell=A2, state=frozen), bold header row, autoFilter over the header range.
  * Strings are written as inline strings (``<c t="inlineStr"><is><t>``); ints/floats
    as numeric cells; ``None`` -> empty cell. Everything is XML-escaped and
    XML-illegal control chars (\\x00-\\x08, \\x0b, \\x0c, \\x0e-\\x1f) are stripped.
    Leading/trailing whitespace is preserved via ``xml:space="preserve"``.
  * STREAMING: ``rows`` may be a generator of hundreds of thousands of rows; each
    worksheet is written incrementally into the zip member (``ZipFile.open(..., "w")``)
    so memory stays low (no giant string is ever built).
  * If ``path`` is locked/unwritable a ``PermissionError`` naming the path
    propagates (the caller turns it into the Hebrew error message).
"""

from __future__ import annotations

import io
import re
import zipfile

__all__ = ["write_workbook"]

DEFAULT_COL_WIDTH = 18.0

# XML 1.0 illegal control characters (spec: strip \x00-\x08, \x0b, \x0c, \x0e-\x1f).
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Characters Excel forbids in sheet names.
_BAD_SHEETNAME = re.compile(r"[\[\]:*?/\\]")

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


# ---------------------------------------------------------------------------
# escaping helpers
# ---------------------------------------------------------------------------

def _clean(text):
    """Strip XML-illegal control chars from *text* (str)."""
    if _ILLEGAL_XML.search(text):
        text = _ILLEGAL_XML.sub("", text)
    return text


def _esc_text(text):
    """Escape *text* for use inside an XML text node (also strips illegal chars)."""
    text = _clean(text)
    if "&" in text:
        text = text.replace("&", "&amp;")
    if "<" in text:
        text = text.replace("<", "&lt;")
    if ">" in text:
        text = text.replace(">", "&gt;")
    return text


def _esc_attr(text):
    """Escape *text* for use inside a double-quoted XML attribute value."""
    text = _esc_text(text)
    if '"' in text:
        text = text.replace('"', "&quot;")
    if "\n" in text or "\r" in text or "\t" in text:
        text = text.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")
    return text


def _col_letter(idx):
    """0-based column index -> Excel column letters (0 -> 'A', 25 -> 'Z', 26 -> 'AA')."""
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# ---------------------------------------------------------------------------
# sheet-name sanitizing
# ---------------------------------------------------------------------------

def _sanitize_sheet_names(raw_names):
    """Sanitize + dedupe sheet names per Excel rules. Returns list of final names."""
    result = []
    seen = set()  # casefolded (Excel sheet names are case-insensitive)
    for i, raw in enumerate(raw_names):
        name = _BAD_SHEETNAME.sub("", _clean(str(raw or "")))
        name = name.strip().strip("'").strip()
        if not name:
            name = "Sheet%d" % (i + 1)
        name = name[:31]
        base = name
        n = 1
        while name.casefold() in seen:
            n += 1
            suffix = " (%d)" % n
            name = base[: 31 - len(suffix)] + suffix
        seen.add(name.casefold())
        result.append(name)
    return result


# ---------------------------------------------------------------------------
# static package parts
# ---------------------------------------------------------------------------

def _content_types_xml(n_sheets):
    parts = [
        _XML_DECL,
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    for i in range(1, n_sheets + 1):
        parts.append(
            '<Override PartName="/xl/worksheets/sheet%d.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i
        )
    parts.append("</Types>")
    return "".join(parts)


def _root_rels_xml():
    return (
        _XML_DECL
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument'
        '/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _workbook_xml(sheet_names):
    parts = [
        _XML_DECL,
        '<workbook xmlns="%s" xmlns:r="%s">' % (_NS_MAIN, _NS_REL_DOC),
        "<sheets>",
    ]
    for i, name in enumerate(sheet_names, start=1):
        parts.append(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_esc_attr(name), i, i)
        )
    parts.append("</sheets></workbook>")
    return "".join(parts)


def _workbook_rels_xml(n_sheets):
    parts = [
        _XML_DECL,
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    for i in range(1, n_sheets + 1):
        parts.append(
            '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument'
            '/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i)
        )
    parts.append(
        '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument'
        '/2006/relationships/styles" Target="styles.xml"/>' % (n_sheets + 1)
    )
    parts.append("</Relationships>")
    return "".join(parts)


def _styles_xml():
    # Style index 0 = normal, 1 = bold (used for the header row).
    return (
        _XML_DECL
        + '<styleSheet xmlns="%s">' % _NS_MAIN
        + '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="2">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        "</fills>"
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


# ---------------------------------------------------------------------------
# worksheet streaming
# ---------------------------------------------------------------------------

def _cell_xml(ref, value, style):
    """XML for one cell; returns '' for None (cell omitted, position kept by r=refs)."""
    if value is None:
        return ""
    s_attr = ' s="1"' if style else ""
    if isinstance(value, bool):  # bool before int (bool is an int subclass)
        return '<c r="%s"%s><v>%d</v></c>' % (ref, s_attr, int(value))
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                # NaN/inf are not representable as numeric cells -> inline string
                value = str(value)
            else:
                num = repr(value)
                return '<c r="%s"%s><v>%s</v></c>' % (ref, s_attr, num)
        else:
            return '<c r="%s"%s><v>%d</v></c>' % (ref, s_attr, value)
    text = value if isinstance(value, str) else str(value)
    esc = _esc_text(text)
    # Preserve significant leading/trailing whitespace (Excel strips it otherwise).
    if text != text.strip() or "\n" in text or "\t" in text:
        t_el = '<t xml:space="preserve">%s</t>' % esc
    else:
        t_el = "<t>%s</t>" % esc
    return '<c r="%s"%s t="inlineStr"><is>%s</is></c>' % (ref, s_attr, t_el)


class _ColLetters:
    """Grow-on-demand cache of column letters."""

    def __init__(self):
        self._letters = [_col_letter(i) for i in range(64)]

    def get(self, idx):
        letters = self._letters
        while idx >= len(letters):
            letters.append(_col_letter(len(letters)))
        return letters[idx]


def _write_worksheet(zf, arcname, headers, rows, widths):
    """Stream one worksheet XML into zip member *arcname* (never builds it whole)."""
    headers = list(headers or [])
    ncols = max(len(headers), len(widths or []))
    cols = _ColLetters()

    with zf.open(arcname, "w") as raw, io.TextIOWrapper(
        raw, encoding="utf-8", newline="", write_through=False
    ) as out:
        out.write(_XML_DECL)
        out.write('<worksheet xmlns="%s">' % _NS_MAIN)
        # sheet view: RTL + frozen header row
        out.write(
            "<sheetViews>"
            '<sheetView rightToLeft="1" workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
            "</sheetView>"
            "</sheetViews>"
        )
        # column widths (default 18, overridable per sheet via "widths")
        if ncols:
            out.write("<cols>")
            for i in range(ncols):
                w = DEFAULT_COL_WIDTH
                if widths and i < len(widths) and widths[i]:
                    try:
                        w = float(widths[i])
                    except (TypeError, ValueError):
                        w = DEFAULT_COL_WIDTH
                out.write(
                    '<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, ("%g" % w))
                )
            out.write("</cols>")
        out.write("<sheetData>")

        rownum = 0
        if headers:
            rownum = 1
            cells = []
            for i, h in enumerate(headers):
                cells.append(_cell_xml(cols.get(i) + "1", "" if h is None else str(h), style=True))
            out.write('<row r="1">%s</row>' % "".join(cells))

        # Stream data rows, flushing to the zip in batches to keep memory low
        # while avoiding one TextIOWrapper.write per row.
        buf = []
        for row in rows or ():
            rownum += 1
            r = str(rownum)
            cells = []
            for i, v in enumerate(row):
                if v is None:
                    continue
                cells.append(_cell_xml(cols.get(i) + r, v, style=False))
            buf.append('<row r="%s">%s</row>' % (r, "".join(cells)))
            if len(buf) >= 512:
                out.write("".join(buf))
                buf.clear()
        if buf:
            out.write("".join(buf))

        out.write("</sheetData>")
        # autoFilter over the header range (must come after sheetData)
        if headers:
            out.write(
                '<autoFilter ref="A1:%s1"/>' % cols.get(len(headers) - 1)
            )
        out.write("</worksheet>")


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def write_workbook(path, sheets):
    """Write an .xlsx workbook to *path*. See module docstring for the contract.

    Pure function: no DB access, stdlib only. Raises PermissionError (message
    includes the path) if the target file is locked, e.g. open in Excel.
    """
    sheets = list(sheets or [])
    if not sheets:
        sheets = [{"name": "Sheet1", "headers": [], "rows": []}]
    names = _sanitize_sheet_names([s.get("name", "") for s in sheets])
    n = len(sheets)

    try:
        # compresslevel=4: near-identical size to the default level for XML,
        # noticeably faster on 100+ MB worksheet streams.
        zf = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4)
    except PermissionError as exc:
        raise PermissionError(
            "cannot write workbook (file locked or unwritable): %s (%s)" % (path, exc)
        ) from exc

    try:
        with zf:
            zf.writestr("[Content_Types].xml", _content_types_xml(n))
            zf.writestr("_rels/.rels", _root_rels_xml())
            zf.writestr("xl/workbook.xml", _workbook_xml(names))
            zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml(n))
            zf.writestr("xl/styles.xml", _styles_xml())
            for i, sheet in enumerate(sheets, start=1):
                _write_worksheet(
                    zf,
                    "xl/worksheets/sheet%d.xml" % i,
                    sheet.get("headers") or [],
                    sheet.get("rows"),
                    sheet.get("widths"),
                )
    except PermissionError as exc:
        raise PermissionError(
            "cannot write workbook (file locked or unwritable): %s (%s)" % (path, exc)
        ) from exc
    return path
