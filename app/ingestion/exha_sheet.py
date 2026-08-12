"""COV_EXHA_EXTRACT.xlsx -- the team's reviewed OCR, one row per tract.

The third of the three readings that exist for every legal description, and the
only one a person has looked at. It is not the referee: on covid 4981's Young
Survey tract it won on closure (1:186,912 against the document OCR's 1:25), but
winning is something a reading earns per tract, not a standing rank.

Three rules about this sheet are load-bearing, and all three have already cost
real time when forgotten:

  A BLANK COV.IDp BELONGS TO THE COVID ABOVE IT. The id is written once and the
  tracts follow, so reading the column literally drops every tract after the
  first of each covenant.

  ROW ORDER IS NOT TRACT ORDER. The sheet's second row for a covid is not the
  document's second tract. Match on CONTENT -- acreage, survey and abstract,
  lot and block -- never on position. See app/ingestion/text_compare.py.

  NOT EVERY ROW IS A TRACT. Some rows are CAD account references or bare lot
  lists, and the same land can appear twice in two notations. `looks_like_tract`
  separates the descriptions from the references, and errs toward keeping a row
  rather than dropping land.

Read with the standard library: this environment has no pandas or openpyxl, and
an .xlsx is a zip of XML.
"""
import os
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHEET_PATH = os.path.join(PROJECT_ROOT, "COV_EXHA_EXTRACT.xlsx")

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_COLUMN_RE = re.compile(r"^([A-Z]+)(\d+)$")

# A row that only points AT land -- a CAD account, a bare lot list -- rather than
# describing it. Kept deliberately narrow: dropping a real tract loses encumbered
# land, which is the expensive direction, so anything with prose survives.
_REFERENCE_ONLY_RE = re.compile(
    r"^\s*(?:(?:CAD\s*)?(?:account|acct|property\s*id|prop\s*id|parcel\s*id)\b"
    r"|[\d\s,;&/-]+$)", re.IGNORECASE)
_DESCRIPTION_CUES = ("acre", "survey", "abstract", "beginning", "thence", "lot",
                     "block", "tract", "addition", "subdivision", "volume", "cabinet")


@dataclass(frozen=True)
class SheetRow:
    covid: int
    row_number: int          # the spreadsheet's own row, for citing a finding
    text: str

    @property
    def is_tract(self) -> bool:
        return looks_like_tract(self.text)


def looks_like_tract(text: str) -> bool:
    """Does this row DESCRIBE land, or merely point at it?"""
    body = (text or "").strip()
    if len(body) < 40:
        return False
    if _REFERENCE_ONLY_RE.match(body):
        return False
    low = body.lower()
    return any(cue in low for cue in _DESCRIPTION_CUES)


def _cell_text(cell, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(_NS + "is")
        return "".join(t.text or "" for t in inline.iter(_NS + "t")) if inline is not None else ""
    value = cell.find(_NS + "v")
    if value is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return ""
    return value.text or ""


def read_sheet(path: str | None = None) -> list[SheetRow]:
    """Every tract row in the sheet, with blank covids filled down.

    Rows whose covid never resolves (text before the first id) are dropped --
    they belong to no covenant and guessing one would invent a link.
    """
    path = path or SHEET_PATH
    if not os.path.exists(path):
        return []
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            for item in ET.fromstring(archive.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in item.iter(_NS + "t")))
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    out: list[SheetRow] = []
    current: int | None = None
    data = sheet.find(_NS + "sheetData")
    for row in (data if data is not None else []):
        cells = {}
        for cell in row:
            match = _COLUMN_RE.match(cell.get("r") or "")
            if match:
                cells[match.group(1)] = _cell_text(cell, shared)
        covid_cell = (cells.get("A") or "").strip()
        legal = (cells.get("B") or "").strip()
        if covid_cell:
            digits = re.sub(r"\D", "", covid_cell)
            if digits:
                current = int(digits)
            elif covid_cell.upper().startswith("COV"):
                continue                       # the header row
        if current is None or not legal:
            continue
        out.append(SheetRow(covid=current, row_number=int(row.get("r") or 0), text=legal))
    return out


def rows_for(covid: int, path: str | None = None) -> list[SheetRow]:
    return [r for r in read_sheet(path) if r.covid == covid]


def covids(path: str | None = None) -> list[int]:
    return sorted({r.covid for r in read_sheet(path)})
