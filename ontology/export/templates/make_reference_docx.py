"""Rebuild reference-zh.docx, the Word template of the DOCX export: pandoc's default reference document with
Chinese academic settings — 宋体 / Times New Roman 小四 body with 1.5 line spacing and a two-character first-line
indent, 黑体 headings in black (三号, 四号, 小四), A4 with 2.54 / 3.17 cm margins. Run from the repository root:

    python ontology/export/templates/make_reference_docx.py
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path

OUT = Path(__file__).with_name("reference-zh.docx")
LATIN = 'w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman"'
BODY_FONT = f'<w:rFonts {LATIN} w:eastAsia="SimSun" />'
HEAD_FONT = f'<w:rFonts {LATIN} w:eastAsia="SimHei" />'
# style id -> (run properties, paragraph spacing or None to keep)
STYLES = {
    "Title": (f"{HEAD_FONT}<w:b /><w:sz w:val=\"44\" /><w:szCs w:val=\"44\" />", None),
    "Subtitle": (f"{HEAD_FONT}<w:sz w:val=\"28\" /><w:szCs w:val=\"28\" />", None),
    "Heading1": (f"{HEAD_FONT}<w:b /><w:color w:val=\"000000\" /><w:sz w:val=\"32\" /><w:szCs w:val=\"32\" />",
                 '<w:spacing w:before="360" w:after="240" />'),
    "Heading2": (f"{HEAD_FONT}<w:b /><w:color w:val=\"000000\" /><w:sz w:val=\"28\" /><w:szCs w:val=\"28\" />",
                 '<w:spacing w:before="240" w:after="120" />'),
    "Heading3": (f"{HEAD_FONT}<w:b /><w:color w:val=\"000000\" /><w:sz w:val=\"24\" /><w:szCs w:val=\"24\" />",
                 '<w:spacing w:before="240" w:after="120" />'),
    "BodyText": ("", '<w:spacing w:before="0" w:after="0" w:line="360" w:lineRule="auto" /><w:ind w:firstLineChars="200" w:firstLine="480" />'),
    "Abstract": ("<w:sz w:val=\"21\" /><w:szCs w:val=\"21\" />", None),
    "Bibliography": ("<w:sz w:val=\"21\" /><w:szCs w:val=\"21\" />", '<w:spacing w:before="0" w:after="60" w:line="300" w:lineRule="auto" />'),
}


def _style(xml: str, sid: str, rpr: str, spacing: str | None) -> str:
    pat = re.compile(rf'<w:style w:type="paragraph"[^>]*w:styleId="{sid}".*?</w:style>', re.S)
    m = pat.search(xml)
    if not m:
        raise SystemExit(f"style {sid} not in pandoc's reference.docx")
    block = m.group(0)
    if rpr:
        block = re.sub(r"<w:rPr>.*?</w:rPr>|<w:rPr />", "", block, flags=re.S)
        block = block.replace("</w:style>", f"<w:rPr>{rpr}</w:rPr></w:style>")
    if spacing:
        if re.search(r"<w:pPr>.*?</w:pPr>", block, re.S):
            block = re.sub(r"<w:pPr>(.*?)</w:pPr>", lambda p: "<w:pPr>" + re.sub(r"<w:spacing [^>]*/>", "", p.group(1))
                           .replace("<w:outlineLvl", spacing + "<w:outlineLvl", 1) + ("" if "<w:outlineLvl" in p.group(1)
                                                                                      else spacing) + "</w:pPr>",
                           block, count=1, flags=re.S)
        else:
            block = re.sub(r"<w:pPr />", "", block).replace("<w:rPr>", f"<w:pPr>{spacing}</w:pPr><w:rPr>", 1) \
                if "<w:rPr>" in block else block.replace("</w:style>", f"<w:pPr>{spacing}</w:pPr></w:style>")
    return xml[:m.start()] + block + xml[m.end():]


def patch_styles(xml: str) -> str:
    xml = re.sub(r"(<w:rPrDefault>\s*<w:rPr>\s*)<w:rFonts [^>]*/>", rf"\1{BODY_FONT}", xml, count=1)
    for sid, (rpr, spacing) in STYLES.items():
        xml = _style(xml, sid, rpr, spacing)
    return xml


def patch_document(xml: str) -> str:
    page = ('<w:pgSz w:w="11906" w:h="16838" /><w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800" '
            'w:header="851" w:footer="992" w:gutter="0" />')
    return re.sub(r"</w:sectPr>", page + "</w:sectPr>", xml, count=1)


def main() -> int:
    default = subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"], capture_output=True,
                             check=True).stdout
    src = zipfile.ZipFile(io.BytesIO(default))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "word/styles.xml":
                data = patch_styles(data.decode("utf-8")).encode("utf-8")
            elif info.filename == "word/document.xml":
                data = patch_document(data.decode("utf-8")).encode("utf-8")
            out.writestr(info, data)
    OUT.write_bytes(buf.getvalue())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
