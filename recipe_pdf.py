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


def build_recipe_pdf(menu, sections, document_name="조리 매뉴얼", cover=None):
    body = ParagraphStyle("Body", fontName=recipe_font(), fontSize=10.5, leading=15,
                          wordWrap="CJK", spaceAfter=4)
    title = ParagraphStyle("Title", parent=body, fontSize=23, leading=29, textColor=NAVY)
    meta = ParagraphStyle("Meta", parent=body, fontSize=10, textColor=colors.HexColor("#46536b"))
    heading = ParagraphStyle("Heading", parent=body, fontSize=13, leading=18,
                             backColor=colors.HexColor("#edf1f8"), borderPadding=(4, 8, 4, 8),
                             textColor=NAVY, spaceBefore=13, spaceAfter=9, keepWithNext=True)
    step = ParagraphStyle("Step", parent=body, fontSize=11.5, textColor=NAVY,
                          spaceBefore=6, keepWithNext=True)
    warning = ParagraphStyle("Warning", parent=body, fontSize=10,
                             textColor=colors.HexColor("#a82323"))
    amount = ParagraphStyle("Amount", parent=body, alignment=TA_RIGHT)
    styles = {"meta": meta, "heading": heading, "step": step, "text": body, "warning": warning}

    def paragraph(value, style=body):
        safe_text = escape(str(value)).replace("\n", "<br/>")
        return Paragraph(safe_text, style)

    def picture(source, max_width, max_height):
        source = BytesIO(source) if isinstance(source, bytes) else source
        try:
            with Image.open(source) as image:
                photo = ImageOps.exif_transpose(image).convert("RGB")
                photo.thumbnail((960, 960))
                image_buffer = BytesIO()
                photo.save(image_buffer, format="JPEG", quality=85)
                image_buffer.seek(0)
                scale = min(max_width / photo.width, max_height / photo.height, 1)
                return PdfImage(image_buffer, width=photo.width * scale,
                                height=photo.height * scale, hAlign="LEFT")
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    def ingredients_table(rows, width):
        cells = [[paragraph(name), paragraph(quantity, amount)] for name, quantity in rows]
        table = Table(cells, colWidths=[width - 78, 78], repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0fb")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
            ("LINEBELOW", (0, 0), (-1, -1), .35, colors.HexColor("#d9dfe9")),
            ("LINEAFTER", (0, 0), (0, -1), .35, colors.HexColor("#d9dfe9")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return table

    def footer(canvas, document):
        canvas.setStrokeColor(colors.HexColor("#d9dfe9"))
        canvas.line(38, 31, A4[0] - 38, 31)
        canvas.setFillColor(NAVY)
        canvas.setFont(recipe_font(), 9)
        canvas.drawString(38, 18, f"해탄  |  {document_name}")
        canvas.drawRightString(A4[0] - 38, 18, str(document.page))

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, title=f"{menu} · {document_name}",
                            author="해탄", leftMargin=38, rightMargin=38,
                            topMargin=34, bottomMargin=45)
    width = doc.width - 12
    story = [paragraph(f"해탄  |  {document_name}", meta), paragraph(menu, title)]
    cover_image = picture(cover, 156, 160) if cover else None
    first_table = next((rows for kind, rows in sections if kind == "table"), [])
    place_cover_beside_table = cover_image is not None and 1 < len(first_table) <= 11
    for kind, value in sections:
        if kind == "meta":
            story.append(paragraph(value, meta))
            if cover_image and not place_cover_beside_table:
                story.extend((cover_image, Spacer(1, 6)))
        elif kind == "image":
            image = picture(value, 218, 132)
            if image:
                image.keepWithNext = True
                story.extend((image, Spacer(1, 6)))
        elif kind == "table":
            if place_cover_beside_table and value is first_table:
                top = Table([[cover_image, ingredients_table(value, width - 174)]],
                            colWidths=[168, width - 168], hAlign="LEFT")
                top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                         ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
                story.append(top)
            else:
                story.append(ingredients_table(value, width))
        elif value:
            story.append(paragraph(value, styles[kind]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
