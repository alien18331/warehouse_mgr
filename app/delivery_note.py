# 交貨單 PDF 產生器（版面依公司制式「交貨單.pdf」重現）
from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = A4          # 595.27 x 841.89 pt
ML, MR = 40, 40              # 左右邊界
TBL_W = PAGE_W - ML - MR     # 515
ROWS_PER_PAGE = 18
ROW_H = 21
LOGO_PATH = Path(__file__).parent / "static" / "jettech.png"

_fonts_ready = False


def _ensure_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    fdir = Path(r"C:\Windows\Fonts")
    pdfmetrics.registerFont(TTFont("Kai", str(fdir / "kaiu.ttf")))
    pdfmetrics.registerFont(TTFont("MSJH", str(fdir / "msjh.ttc"), subfontIndex=0))
    pdfmetrics.registerFont(TTFont("MSJH-Bold", str(fdir / "msjhbd.ttc"), subfontIndex=0))
    _fonts_ready = True


def _fit(c, text, font, size, max_w, min_size=6.5):
    """回傳可塞進 max_w 的 (font_size, text)，過長時縮字級，再不行截斷。"""
    text = text or ""
    while size > min_size and c.stringWidth(text, font, size) > max_w:
        size -= 0.5
    while text and c.stringWidth(text, font, size) > max_w:
        text = text[:-1]
    return size, text


def _center(c, text, font, size, cx, y):
    c.setFont(font, size)
    c.drawCentredString(cx, y, text)


def _header(c):
    """公司抬頭 + 標題，回傳資訊區起始 y。"""
    # Logo
    try:
        c.drawImage(str(LOGO_PATH), ML, PAGE_H - 92, width=110, height=46,
                    preserveAspectRatio=True, anchor='w', mask='auto')
    except Exception:
        pass
    cx = PAGE_W / 2 + 30
    _center(c, "勁傑系統科技股份有限公司", "Kai", 19, cx, PAGE_H - 48)
    c.setFont("Times-Roman", 13)
    t = "Jet-Tech System Technology Co., Ltd."
    c.drawCentredString(cx, PAGE_H - 63, t)
    w = c.stringWidth(t, "Times-Roman", 13)
    c.line(cx - w / 2, PAGE_H - 65, cx + w / 2, PAGE_H - 65)
    _center(c, "新竹總公司：302054 新竹縣竹北市嘉豐南路二段76號7樓", "Kai", 10.5, cx, PAGE_H - 78)
    _center(c, "Tel: 03-667-6081   Fax: 03-667-6082", "Times-Roman", 10, cx, PAGE_H - 90)
    _center(c, "●台南辦事處：744010 台南市新市區大營里豐榮189-30號", "Kai", 10.5, cx, PAGE_H - 103)
    _center(c, "Tel: 06-512-1618   Fax: 06-583-0080", "Times-Roman", 10, cx, PAGE_H - 115)
    c.setLineWidth(2.2)
    c.line(ML, PAGE_H - 124, PAGE_W - MR, PAGE_H - 124)
    c.setLineWidth(1)
    # 標題
    title_y = PAGE_H - 150
    _center(c, "交　貨　單", "MSJH-Bold", 17, PAGE_W / 2, title_y)
    tw = c.stringWidth("交　貨　單", "MSJH-Bold", 17)
    c.setLineWidth(1.2)
    c.line(PAGE_W / 2 - tw / 2, title_y - 3, PAGE_W / 2 + tw / 2, title_y - 3)
    c.setLineWidth(1)
    return title_y - 25


def _info_block(c, y, head):
    """客戶/工號/日期資訊區，回傳表格頂端 y。"""
    rx = 330  # 右欄 x
    c.setFont("MSJH", 11)
    c.drawString(ML, y, f"客戶名稱 : {head.get('owner') or ''}")
    c.drawString(rx, y, f"受訂工號： {head.get('job_no') or ''}")
    y -= 17
    c.drawString(ML, y, "聯 絡 人 :")
    c.drawString(rx, y, "訂單編號：")
    y -= 17
    c.drawString(ML, y, "聯絡電話 :")
    dy, dm, dd = "", "", ""
    date = head.get("date") or ""
    parts = date.split("-")
    if len(parts) == 3:
        dy, dm, dd = parts[0], str(int(parts[1])), str(int(parts[2]))
    c.drawString(rx, y, f"交貨日期 : {dy} 年 {dm} 月 {dd} 日")
    y -= 17
    c.setFont("MSJH-Bold", 11)
    c.drawString(ML, y, "出貨地址：")
    return y - 8


# 欄位 x 座標（項目/名稱規格/單位/交貨數量/備註）
COL_X = [ML, ML + 30, ML + 280, ML + 320, ML + 415, ML + TBL_W]


def _item_table(c, y_top, rows, start_no=1):
    """明細表（表頭 + 固定 18 列），回傳表格底端 y。"""
    hh = 20
    y_bot = y_top - hh - ROWS_PER_PAGE * ROW_H
    # 外框加粗、內線細
    c.setLineWidth(1.2)
    c.rect(ML, y_bot, TBL_W, y_top - y_bot)
    c.setLineWidth(0.6)
    for x in COL_X[1:-1]:
        c.line(x, y_bot, x, y_top)
    c.line(ML, y_top - hh, ML + TBL_W, y_top - hh)
    # 表頭
    heads = ["項目", "名　稱　　規　格", "單位", "交貨數量", "備　註"]
    c.setFont("MSJH-Bold", 10.5)
    for i, h in enumerate(heads):
        c.drawCentredString((COL_X[i] + COL_X[i + 1]) / 2, y_top - hh + 6, h)
    # 資料列
    for r in range(ROWS_PER_PAGE):
        ry_top = y_top - hh - r * ROW_H
        ry = ry_top - ROW_H + 6.5
        c.line(ML, ry_top - ROW_H, ML + TBL_W, ry_top - ROW_H)
        row = rows[r] if r < len(rows) else None
        c.setFont("MSJH", 10)
        c.drawCentredString((COL_X[0] + COL_X[1]) / 2, ry, str(start_no + r))
        if not row:
            continue
        if row.get("blank"):
            c.drawString(COL_X[1] + 4, ry, "以下空白")
            continue
        s, t = _fit(c, row["name"], "MSJH", 10, COL_X[2] - COL_X[1] - 8)
        c.setFont("MSJH", s)
        c.drawString(COL_X[1] + 4, ry, t)
        c.setFont("MSJH", 10)
        c.drawCentredString((COL_X[2] + COL_X[3]) / 2, ry, row["unit"])
        c.drawCentredString((COL_X[3] + COL_X[4]) / 2, ry, row["qty"])
        s, t = _fit(c, row["note"], "MSJH", 9.5, COL_X[5] - COL_X[4] - 8)
        c.setFont("MSJH", s)
        c.drawString(COL_X[4] + 4, ry, t)
    return y_bot


def _footer(c, y_top, head):
    """簽收區 + 註記。"""
    hh, bh = 20, 58
    y_bot = y_top - hh - bh
    xs = [ML, ML + 129, ML + 258, ML + 387, ML + TBL_W]
    c.setLineWidth(1.2)
    c.rect(ML, y_bot, TBL_W, y_top - y_bot)
    c.setLineWidth(0.6)
    for x in xs[1:-1]:
        c.line(x, y_bot, x, y_top)
    c.line(ML, y_top - hh, ML + TBL_W, y_top - hh)
    c.setFont("MSJH-Bold", 10.5)
    for i, h in enumerate(["客戶簽收", "簽收日期", "交貨經辦", "發票號碼"]):
        c.drawCentredString((xs[i] + xs[i + 1]) / 2, y_top - hh + 6, h)
    if head.get("signer"):
        c.setFont("MSJH", 11)
        c.drawCentredString((xs[2] + xs[3]) / 2, y_bot + bh / 2 - 4, head["signer"])
    # 註記
    c.setFont("MSJH", 9.5)
    ny = y_bot - 14
    c.drawString(ML, ny, '註:  1.請確認後簽蓋公司章並回傳至Fax:(06)583-0080或回傳"mail:ninja@jet-tech.com.tw"以利作業!')
    c.drawString(ML + 20, ny - 12, "2. 以上帳款未結清前，上列貨品所有權仍屬勁傑系統科技股份有限公司")
    c.drawString(ML + 20, ny - 24, "3. 未於收貨二日內回傳者，視同收貨無誤，請特別留意。")


def build_delivery_note(head, lines) -> bytes:
    """head: 出貨單 header dict（含 owner/job_no/date/signer）
    lines: 明細 dict list（brand/model/description/qty/unit/note）"""
    _ensure_fonts()
    rows = []
    for idx, l in enumerate(lines, 1):
        name = " ".join(x for x in (l.get("brand"), l.get("model"), l.get("description")) if x)
        qty = l.get("qty")
        if isinstance(qty, float) and qty.is_integer():
            qty = int(qty)
        rows.append({"no": idx, "name": name, "unit": l.get("unit") or "",
                     "qty": "" if qty is None else str(qty), "note": l.get("note") or ""})
    rows.append({"no": len(rows) + 1, "blank": True})

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for start in range(0, len(rows), ROWS_PER_PAGE):
        page_rows = rows[start:start + ROWS_PER_PAGE]
        y = _header(c)
        y = _info_block(c, y, head)
        y_bot = _item_table(c, y, page_rows, start_no=start + 1)
        _footer(c, y_bot, head)
        c.showPage()
    c.save()
    return buf.getvalue()
