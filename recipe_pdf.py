from io import BytesIO
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


NAVY = colors.HexColor("#263d88")


@lru_cache(maxsize=1)
def recipe_font():
    for path in (Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
                 Path("C:/Windows/Fonts/malgun.ttf")):
        if path.is_file():
            pdfmetrics.registerFont(TTFont("RecipeKorean", str(path)))
            return "RecipeKorean"
    font = "HYSMyeongJo-Medium"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    return font


def build_recipe_pdf(menu, sections):
    body = ParagraphStyle("Body", fontName=recipe_font(), fontSize=11, leading=17,
                          wordWrap="CJK", spaceAfter=5)
    title = ParagraphStyle("Title", parent=body, fontSize=23, leading=29, textColor=NAVY)
    heading = ParagraphStyle("Heading", parent=body, fontSize=14, leading=20,
                             textColor=NAVY, spaceBefore=12, keepWithNext=True)
    warning = ParagraphStyle("Warning", parent=body, fontSize=10,
                             textColor=colors.HexColor("#a82323"))
    amount = ParagraphStyle("Amount", parent=body, alignment=TA_RIGHT)
    styles = {"title": title, "heading": heading, "text": body, "warning": warning}

    def paragraph(value, style=body):
        safe_text = escape(str(value)).replace("\n", "<br/>")
        return Paragraph(safe_text, style)

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, title=f"{menu} · 조리 매뉴얼",
                            author="해탄", leftMargin=38, rightMargin=38,
                            topMargin=36, bottomMargin=36)
    story = [paragraph("해탄 조리 매뉴얼"), paragraph(menu, title)]
    for kind, value in sections:
        if kind == "image":
            source = BytesIO(value) if isinstance(value, bytes) else value
            try:
                with Image.open(source) as image:
                    photo = ImageOps.exif_transpose(image).convert("RGB")
                    photo.thumbnail((960, 960))
                    image_buffer = BytesIO()
                    photo.save(image_buffer, format="JPEG", quality=85)
                    image_buffer.seek(0)
                    scale = min(280 / photo.width, 190 / photo.height, 1)
                    picture = PdfImage(image_buffer, width=photo.width * scale,
                                       height=photo.height * scale, hAlign="LEFT")
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
            story.extend((picture, Spacer(1, 7)))
        elif kind == "table":
            cells = [[paragraph(name), paragraph(quantity, amount)] for name, quantity in value]
            table = Table(cells, colWidths=[doc.width - 94, 82], repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0fb")),
                ("LINEBELOW", (0, 0), (-1, -1), .4, colors.HexColor("#d9dfe9")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(table)
        elif value:
            story.append(paragraph(value, styles[kind]))
    doc.build(story)
    return output.getvalue()
