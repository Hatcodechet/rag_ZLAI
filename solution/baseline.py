"""Deterministic DataMate baseline. Run with an environment containing openpyxl."""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


def norm(value: object) -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def money(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (float, int, Decimal)):
        return Decimal(str(value))
    raw = str(value).strip().lower().replace(" ", "")
    compact_million = re.fullmatch(r"(\d+)tr(\d{1,3})", raw)
    if compact_million:
        fraction = compact_million[2]
        return Decimal(compact_million[1]) * 1000000 + Decimal(fraction) * 10 ** (6 - len(fraction))
    multiplier = 1000000 if re.search(r"(?:triệu|trieu|tr)$", raw) else 1000 if re.search(r"(?:k|nghìn|nghin|ngàn|ngan)$", raw) else 1
    s = raw
    s = re.sub(r"[^\d,.-]", "", s)
    if not s:
        return None
    if multiplier != 1:
        try:
            return Decimal(s.replace(",", ".")) * multiplier
        except Exception:
            return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", "") if len(s.split(",")[-1]) == 3 else s.replace(",", ".")
    elif "." in s and all(len(part) == 3 for part in s.split(".")[1:]):
        s = s.replace(".", "")
    try:
        return Decimal(s) * multiplier
    except Exception:
        return None


def quantity(value: object) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    raw = str(value or "").strip()
    if re.fullmatch(r"-?\d{1,3}(?:[.,]\d{3})+", raw):
        return money(raw)
    match = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", raw)
    if not match:
        return None
    try:
        return Decimal(match[1].replace(",", "."))
    except Exception:
        return None


def date(value: object, default_year: int | None = None) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and 20000 < value < 100000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
    s = str(value or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{5}(?:\.0)?", s):
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(float(s)))
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})\s*thg\s*(\d{1,2})\s*(\d{4})", norm(s))
    if m:
        try:
            return dt.date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            pass
    if default_year is not None:
        m = re.fullmatch(r"\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*", s)
        if m:
            try:
                return dt.date(default_year, int(m[2]), int(m[1]))
            except ValueError:
                pass
    return None


def aliases(headers: list[str], *names: str) -> int | None:
    mapping = {norm(h): i for i, h in enumerate(headers)}
    for name in names:
        if norm(name) in mapping:
            return mapping[norm(name)]
    return None


def get(row: tuple | list, idx: int | None) -> object:
    return row[idx] if idx is not None and idx < len(row) else None


def rows_for(path: Path):
    if path.suffix.lower() == ".csv":
        for encoding in ("utf-8-sig", "cp1258"):
            try:
                with path.open(encoding=encoding, newline="") as f:
                    yield path.name, list(csv.reader(f))
                return
            except UnicodeDecodeError:
                continue
    elif path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet in book.worksheets:
                yield f"{path.name}:{sheet.title}", list(sheet.values)
        finally:
            book.close()
    elif path.suffix.lower() == ".sqlite":
        with sqlite3.connect(path) as con:
            for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                cur = con.execute(f'SELECT * FROM "{table}"')
                yield f"{path.name}:{table}", [tuple(d[0] for d in cur.description), *cur.fetchall()]


def sqlite_sales(path: Path) -> list[Sale]:
    """Read the common POS schema when line items have no date of their own."""
    result = []
    with sqlite3.connect(path) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"don_hang", "chi_tiet_don", "san_pham"}.issubset(tables):
            return result
        query = """
            SELECT d.MaHoaDon, d.NgayLap, d.TinhTrang, c.TriGia,
                   p.TenGoi, c.SLQuyDoi, c.DonViTinh, c.GiaDonVi
            FROM don_hang AS d
            JOIN chi_tiet_don AS c ON d.MaHoaDon = c.MaHoaDon
            LEFT JOIN san_pham AS p ON c.MaHangHoa = p.MaHangHoa
        """
        for order, raw_date, raw_status, raw_amount, product, raw_quantity, unit, price in con.execute(query):
            day = date(raw_date)
            amount = money(raw_amount)
            if day is None or amount is None:
                continue
            result.append(Sale(str(order), day, status(raw_status), amount, product, quantity(raw_quantity), unit, money(price), None, True))
    return result


def relational_csv_sales(folder: Path) -> list[Sale]:
    """Join order, item and catalog CSV exports by their declared codes."""
    required = [folder / name for name in ("don_hang.csv", "chi_tiet_don.csv", "san_pham.csv")]
    if not all(path.exists() for path in required):
        return []
    tables = []
    for path in required:
        with path.open(encoding="utf-8-sig", newline="") as f:
            tables.append(list(csv.reader(f)))
    orders, details, products = tables
    oh, dh, ph = ([str(x or "") for x in table[0]] for table in tables)
    order_id = aliases(oh, "Mã chứng từ", "Số phiếu", "Mã hóa đơn", "Mã đơn hàng", "Số CT")
    order_date = aliases(oh, "NgayBan", "Ngày", "Ngày bán", "Date")
    order_status = aliases(oh, "Xử lý", "Tình trạng", "Tình trạng đơn", "Trạng thái")
    customer = aliases(oh, "Số KH", "Customer ID", "Mã khách mua", "Tên khách")
    item_order = aliases(dh, "Ma don", "Mã đơn hàng", "Số CT", "Mã bill")
    item_product = aliases(dh, "ID SP", "Mã mặt hàng", "Mã hàng bán", "Mã H.hóa")
    item_amount = aliases(dh, "T.Tiền", "Amount", "Tiền bán", "Cộng")
    item_qty = aliases(dh, "SL quy đổi", "Số lượng lẻ", "Quy ra ĐV lẻ")
    item_unit = aliases(dh, "Đơn vị (tính)", "DVT", "ĐV tính")
    item_price = aliases(dh, "Giá đơn vị", "Đơn giá (đ)", "Giá 1 đv")
    product_id = aliases(ph, "SKU", "Mã sản phẩm bán", "Mã HH", "Mã vạch")
    product_name = aliases(ph, "Sản phẩm", "Tên SP", "Tên sản phẩm", "ten_hang")
    if None in (order_id, order_date, item_order, item_product, item_amount, product_id, product_name):
        return []
    names = {str(get(row, product_id)): str(get(row, product_name)) for row in products[1:] if get(row, product_id) and get(row, product_name)}
    order_info = {}
    for row in orders[1:]:
        key, day = get(row, order_id), date(get(row, order_date))
        if key and day:
            order_info[str(key)] = (day, status(get(row, order_status)), get(row, customer))
    result = []
    for row in details[1:]:
        key = str(get(row, item_order))
        if key not in order_info:
            continue
        day, state, cust = order_info[key]
        amount = money(get(row, item_amount))
        if amount is None:
            continue
        product = names.get(str(get(row, item_product)))
        result.append(Sale(key, day, state, amount, product, quantity(get(row, item_qty)), str(get(row, item_unit)) if get(row, item_unit) else None, money(get(row, item_price)), str(cust) if cust else None, item_qty is not None))
    return result


def relational_excel_sales(folder: Path) -> list[Sale]:
    """Join invoice and line-item Excel exports through their order code."""
    paths = sorted(folder.glob("*.xlsx"))
    invoice_files = [p for p in paths if norm(p.stem).replace(" ", "").startswith(("banhanghoadon", "danhsachhoadon", "dulieubanhang")) and "chitiet" not in norm(p.stem).replace(" ", "") and "phongban" not in norm(p.stem).replace(" ", "")]
    detail_files = [p for p in paths if any(x in norm(p.stem).replace(" ", "") for x in ("chitiethoadon", "hoadonchitiet")) and "chitietlo" not in norm(p.stem).replace(" ", "")]
    if not invoice_files or not detail_files:
        return []
    orders: dict[str, tuple[dt.date, str, str | None]] = {}
    for path in invoice_files:
        for _, table in rows_for(path):
            if not table:
                continue
            found = None
            for n, raw in enumerate(table[:20]):
                headers = [str(x or "") for x in raw]
                code = aliases(headers, "Order ID", "Mã hóa đơn", "Mã đơn hàng")
                day_col = aliases(headers, "Ngày bán", "Ngày đặt", "Ngày", "Thời gian")
                state_col = aliases(headers, "Trạng thái", "Tình trạng")
                if code is not None and day_col is not None and state_col is not None:
                    found = n, headers, code, day_col
                    break
            if found is None:
                continue
            header_at, headers, code, day_col = found
            state_col = aliases(headers, "Trạng thái", "Tình trạng")
            customer_col = aliases(headers, "Mã KH mua", "Mã KH", "Tên KH")
            for row in table[header_at + 1:]:
                key, day = get(row, code), date(get(row, day_col))
                if key and day:
                    cust = get(row, customer_col)
                    orders[str(key)] = (day, status(get(row, state_col)), str(cust) if cust else None)
    result = []
    for path in detail_files:
        for _, table in rows_for(path):
            if not table:
                continue
            found = None
            for n, raw in enumerate(table[:20]):
                headers = [str(x or "") for x in raw]
                code = aliases(headers, "Mã giao dịch", "Mã hóa đơn", "Mã đơn hàng")
                amount_col = aliases(headers, "Thanh tien", "Thành tiền", "TT dòng")
                product_col = aliases(headers, "Chi tiết hàng", "Tên hàng", "Tên sản phẩm")
                if None not in (code, amount_col, product_col):
                    found = n, headers, code, amount_col, product_col
                    break
            if found is None:
                continue
            header_at, headers, code, amount_col, product_col = found
            qty_col = aliases(headers, "SL cơ bản", "Số lượng cơ bản", "Số lượng quy đổi")
            qty_is_base = qty_col is not None
            if qty_col is None:
                qty_col = aliases(headers, "Slg", "Số lượng")
            unit_col = aliases(headers, "Đ.vị", "Đơn vị", "Đơn vị tính")
            price_col = aliases(headers, "Giá niêm yết", "Đơn giá")
            for row in table[header_at + 1:]:
                key = str(get(row, code))
                if key not in orders:
                    continue
                amount = money(get(row, amount_col))
                if amount is None:
                    continue
                day, state, customer = orders[key]
                product = get(row, product_col)
                unit = get(row, unit_col)
                result.append(Sale(key, day, state, amount, str(product) if product else None,
                                   quantity(get(row, qty_col)), str(unit) if unit else None,
                                   money(get(row, price_col)), customer, qty_is_base))
    return result


def sales_file(name: str) -> bool:
    s = norm(name.split(":")[0])
    return any(token in s for token in ("ban hang", "banhang", "so ban hang", "sobanhang", "don hang", "donhang", "hoa don", "hoadon", "pos sqlite", "du lieu xuat", "dulieuxuat", "so sua xe", "sosuaxe")) and not any(token in s for token in ("nhap", "quy doi", "danh sach hang hoa", "danh sach san pham", "danh sach thuoc", "lo hang", "khach hang", "bao cao ton", "cong no", "thanh toan", "phieu thu"))


def source_family(name: str) -> str:
    """Group monthly and yearly exports of the same sales ledger."""
    stem = Path(name).stem
    stem = re.sub(r"_(?:thang|t)?\d{1,2}_20\d{2}$", "", stem, flags=re.I)
    stem = re.sub(r"_20\d{2}$", "", stem, flags=re.I)
    stem = re.sub(r"_\d+$", "", stem, flags=re.I)
    return norm(stem)


@dataclass(frozen=True)
class Sale:
    order: str
    day: dt.date
    status: str
    amount: Decimal
    product: str | None
    quantity: Decimal | None
    unit: str | None
    price: Decimal | None
    customer: str | None
    quantity_is_base: bool = False


class Sales(list):
    def __init__(self, values=()):
        super().__init__(values)
        self.products = {s.product for s in self if s.product}
        self.first_day = min((s.day for s in self), default=None)
        self.last_day = max((s.day for s in self), default=None)


def status(value: object) -> str:
    s = norm(value)
    if any(x in s for x in ("tra hang", "tra lai", "doi tra", "hoan tra", "hoan hang", "hang tra ve", "returned")):
        return "returned"
    if any(x in s for x in ("huy", "khong giao duoc", "cancel")):
        return "cancelled"
    return "valid"


def parse_table(source: str, table: list[tuple | list]) -> tuple[int, list[Sale]]:
    if not sales_file(source):
        return 0, []
    header_at = None
    fields = None
    year_match = re.search(r"20\d{2}", source)
    sheet_year = int(year_match[0]) if year_match else None
    for n, raw in enumerate(table[:20]):
        headers = [str(x or "") for x in raw]
        d = aliases(headers, "Ngày", "Ngày tháng", "ngay_ban", "Ngày bán", "Ngày mua", "Ngày lập", "Ngày đặt hàng", "Ngày giao dịch", "Ngày HĐ", "Ngày xuất", "Thời gian", "NgayDat", "NgayBan")
        a = aliases(headers, "Thành tiền", "Thành tiền (đ)", "Thanh tien", "thanh_tien", "Trị giá", "Giá trị hàng", "Tổng", "Amount", "Tiền", "Tổng tiền hàng", "Tien hang", "Tiền hàng", "TriGia", "DoanhThu", "Tổng tiền", "TongTien", "TT")
        o = aliases(headers, "Mã đơn hàng", "Mã hóa đơn", "Mã bill", "Mã GD", "Số phiếu", "Mã phiếu", "Ma don", "MaDon", "MaHoaDon", "MaDH", "Đơn số", "Bill", "HĐ")
        p = aliases(headers, "Tên hàng hóa", "Tên sản phẩm", "TenSanPham", "Tên hàng", "Tên SP", "Sản phẩm", "Hàng bán", "Hàng", "Mặt hàng", "Ten hang", "Hàng hóa", "Làm gì", "Nội dung")
        if d is not None and a is not None and (o is not None or p is not None):
            header_at = n
            fields = (d, a, o, p, headers)
            break
    if fields is None:
        return 0, []
    d, a, o, p, headers = fields
    st = aliases(headers, "Kết quả", "Trạng thái", "Trạng thái đơn hàng", "Trạng thái đơn", "Trạng thái xử lý", "Trạng thái giao", "Tiến độ", "TT đơn", "trang_thai", "Status", "Ghi chú TT", "Ghi chú")
    q = aliases(headers, "Quy ra ĐV lẻ", "Số lượng quy đổi", "Số lượng lẻ", "SL lẻ", "SLQuyDoi", "SL (ĐV cơ bản)")
    q_is_base = q is not None
    if q is None:
        q = aliases(headers, "Số lượng", "Số lượng sản phẩm", "Số lượng hàng", "Số lượng bán", "SL bán", "Sluong", "so_luong", "SoLuong", "SLg", "SL", "S.L")
    u = aliases(headers, "Đơn vị tính", "Đơn vị (tính)", "Unit", "DonVi", "dvt", "ĐV")
    price = aliases(headers, "Giá bán", "Giá bán lẻ", "Giá sản phẩm", "Giá lẻ", "Giá 1 đv", "Giá đơn vị", "Đơn giá", "Đơn giá bán", "don_gia", "Price", "ĐG")
    cust = aliases(headers, "Đối tác", "Tên khách hàng", "Khách hàng", "Bên mua", "Khách", "TenKH", "Khách hàng (tên)", "Tên khách")
    result = []
    previous = {}
    for n, row in enumerate(table[header_at + 1 :], header_at + 2):
        day = date(get(row, d), sheet_year)
        order = get(row, o)
        if o is not None and p is None and not str(order or "").strip():
            # Export totals have a date and amount but no order code.
            continue
        if day is None and order is None and (p is None or not get(row, p) or (get(row, q) is None and get(row, price) is None)):
            continue
        if order is None:
            order = previous.get("order") if o is not None else f"{source}:{n}"
        same_order = order is not None and str(order) == previous.get("order")
        if day is None and same_order:
            day = previous.get("day")
        if day is None:
            continue
        amount = money(get(row, a))
        if amount is None:
            continue
        product = get(row, p)
        if p is not None and not product:
            continue
        if product and norm(product) in ("phi ship", "phi giao hang", "voucher"):
            continue
        current_status = get(row, st)
        if current_status is None and same_order:
            current_status = previous.get("status")
        customer = get(row, cust)
        if customer is None and same_order:
            customer = previous.get("customer")
        previous = {"day": day, "order": order, "status": current_status, "customer": customer}
        result.append(Sale(str(order), day, status(current_status), amount, str(product) if product else None, quantity(get(row, q)), str(get(row, u)) if get(row, u) else None, money(get(row, price)), str(customer) if customer else None, q_is_base))
    # Prefer item-level exports over order summaries; score is based on coverage.
    return (2 if p is not None else 1), result


def load_business(folder: Path) -> Sales:
    conversions: dict[tuple[str, str], Decimal] = {}
    for path in folder.iterdir():
        filename = norm(path.name).replace(" ", "")
        if path.suffix.lower() != ".xlsx" or not any(x in filename for x in ("quydoi", "danhmuc")):
            continue
        for _, table in rows_for(path):
            for n, raw in enumerate(table[:15]):
                headers = [str(x or "") for x in raw]
                product_col = aliases(headers, "Tên sản phẩm", "Tên hàng hóa", "Tên hàng", "Hàng", "Tên món")
                unit_col = aliases(headers, "Đơn vị quy đổi", "Đơn vị lớn", "ĐV lớn", "ĐV nhập", "Đơn vị tính")
                factor_col = aliases(headers, "Tỉ lệ quy đổi", "Tỷ lệ quy đổi", "Hệ số quy đổi", "Hệ số QĐ", "Hệ số")
                if None in (product_col, unit_col, factor_col):
                    continue
                for row in table[n + 1 :]:
                    product, unit, factor = get(row, product_col), get(row, unit_col), money(get(row, factor_col))
                    if product and unit and factor and factor > 0:
                        conversions[(norm(product), norm(unit))] = factor
                break
    candidates = []
    joined = relational_csv_sales(folder)
    if joined:
        candidates.append((3, "relational_csv", joined))
    joined_excel = relational_excel_sales(folder)
    if joined_excel:
        candidates.append((3, "relational_excel", joined_excel))
    if not (joined or joined_excel):
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in (".csv", ".xlsx", ".sqlite"):
                continue
            if not sales_file(path.name):
                continue
            if path.suffix.lower() == ".sqlite":
                sales = sqlite_sales(path)
                if sales:
                    candidates.append((2, path.name, sales))
                continue
            for source, table in rows_for(path):
                priority, sales = parse_table(source, table)
                if sales:
                    candidates.append((priority, source.split(":")[0], sales))
    if not candidates:
        return Sales()
    top = max(x[0] for x in candidates)
    # Keep all sheets and split files of the most detailed export family.
    chosen = [x for x in candidates if x[0] == top]
    families = Counter(source_family(x[1]) for x in chosen)
    family = max(families, key=families.get)
    selected = [x for x in chosen if source_family(x[1]) == family]
    unique = {}
    for _, _, sales in selected:
        for s in sales:
            factor = conversions.get((norm(s.product), norm(s.unit)))
            if factor and s.quantity is not None and not s.quantity_is_base:
                s = replace(s, quantity=s.quantity * factor, quantity_is_base=True)
            key = (s.order, s.day, s.product, s.amount, s.quantity, s.unit, s.price)
            unique[key] = s
    return Sales(unique.values())


def period(question: str, asof: dt.date) -> tuple[dt.date, dt.date] | None:
    q = norm(question)
    recent_days = re.search(r"\b(\d{1,2})\s+ngay\s+(?:qua|gan nhat)\b", q)
    if recent_days:
        days = int(recent_days[1])
        if 1 <= days <= 60:
            return asof - dt.timedelta(days=days - 1), asof
    m = re.search(r"(?:ngay\s*)?(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(20\d{2})", question)
    if m:
        try:
            d = dt.date(int(m[3]), int(m[2]), int(m[1]))
            return d, d
        except ValueError:
            return None
    if any(x in q for x in ("hom qua", "hqua", "bua qua")):
        d = asof - dt.timedelta(days=1)
        return d, d
    if any(x in q for x in ("hom nay", "hnay", "bua nay", "ngay nay")):
        return asof, asof
    m = re.search(r"(?:thang|thangs|th)\s*(\d{1,2})\s*(?:/|nam|năm|\s)\s*(20\d{2})", q)
    if m:
        year, month = int(m[2]), int(m[1])
        if 1 <= month <= 12:
            return dt.date(year, month, 1), dt.date(year, month, calendar.monthrange(year, month)[1])
    m = re.search(r"quy\s*(\d)\s*(?:/|nam|\s)\s*(20\d{2})", q)
    if m and 1 <= int(m[1]) <= 4:
        year, start_month = int(m[2]), int(m[1]) * 3 - 2
        end_month = start_month + 2
        return dt.date(year, start_month, 1), dt.date(year, end_month, calendar.monthrange(year, end_month)[1])
    if "thang nay" in q or "thang hien tai" in q:
        return dt.date(asof.year, asof.month, 1), asof
    if "tuan nay" in q:
        return asof - dt.timedelta(days=asof.weekday()), asof
    if "tuan truoc" in q or "tuan roi" in q:
        end = asof - dt.timedelta(days=asof.weekday() + 1)
        return end - dt.timedelta(days=6), end
    return None


def resolve_product(question: str, sales: list[Sale]) -> str | None:
    q = norm(question)
    names = sales.products if isinstance(sales, Sales) else {s.product for s in sales if s.product}
    matches = [name for name in names if len(norm(name)) >= 5 and re.search(r"(?:^|\s)" + re.escape(norm(name)) + r"(?:$|\s)", q)]
    if not matches:
        return None
    return max(matches, key=lambda name: len(norm(name)))


def integer(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def revenue(sales: list[Sale], start: dt.date, end: dt.date, asof: dt.date) -> Decimal:
    return sum((s.amount for s in sales if start <= s.day <= end and s.day <= asof and s.status == "valid"), Decimal())


def previous_month(start: dt.date, end: dt.date, same_days: bool) -> tuple[dt.date, dt.date]:
    previous_end = start - dt.timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    if same_days:
        previous_end = previous_end.replace(day=min(end.day, calendar.monthrange(previous_end.year, previous_end.month)[1]))
    return previous_start, previous_end


def answer(item: dict, sales: list[Sale]) -> dict:
    q = norm(item["question"])
    asof = date(item["as_of_date"])
    span = period(item["question"], asof)
    if not span or not sales:
        return {"type": "abstain"}
    start, end = span
    available_start = sales.first_day if isinstance(sales, Sales) else min(s.day for s in sales)
    available_end = sales.last_day if isinstance(sales, Sales) else max(s.day for s in sales)
    if end < available_start or start > available_end:
        return {"type": "unanswerable"}
    # These questions need a different source, a distinct filter, or a list output.
    if any(x in q for x in (
        "tu dau", "nha cung cap", "tien tuoi", "bao nhieu noi", "cat hang", "danh hang",
        "ban chiu", "phai tra", "tra gop", "dot giao", "da giao", "hoa hong", "hue hong",
        "phi ship", "voucher", "diem thanh vien", "nguoi ban", "nhan vien",
        "tra bang", "thanh toan bang", "kenh ban", "qua zalo", "qua dien thoai",
        "san thuong mai", "tro len", "tro lne", "it nhat", "tien hang dat",
        "bao nhieu ngay", "bn ngay", "bao nhieu bua", "dem so don", "phieu co thu tien", "phieu thu tien",
    )) or re.search(r"\b(?:nv\d*|tho)\b", q):
        return {"type": "abstain"}
    if len(re.findall(r"\bthang\s*\d{1,2}\s*(?:nam\s*)?20\d{2}\b", q)) > 1:
        return {"type": "abstain"}
    valid = [s for s in sales if start <= s.day <= end and s.day <= asof and s.status == "valid"]
    all_orders = {s.order: s for s in sales if start <= s.day <= end and s.day <= asof}
    prod = resolve_product(item["question"], sales)
    if not prod:
        named_sale = re.search(r"\bban\s+(?!duoc\b|dc\b|ra\b|hang\b|cua\b|tong\b)(?:\w+\s+){1,12}(?:duoc|dc|bao nhieu)\b", q)
        named_revenue = re.search(r"\bdoanh thu cua\s+(?!cua hang\b|quan\b|tiem\b|shop\b|thang\b|nam\b)", q)
        if named_sale or named_revenue:
            return {"type": "abstain"}
    if prod:
        valid = [s for s in valid if norm(s.product) == norm(prod)]
    is_order = any(x in q for x in ("so don", "may don", "bao nhieu don", "so bill", "may bill", "nhieu don hang"))
    is_cancel = "huy" in q and "don" in q
    is_average = any(x in q for x in ("trung binh", "binh quan", "bin quan")) and (any(x in q for x in ("don", "phieu")) or re.search(r"\b(?:dh|bill|order)\b", q))
    is_percent = any(x in q for x in ("phan tram", "ti le", "ty le", "ti trong", "tang truong")) or "%" in item["question"]
    if is_cancel and is_percent:
        total = len(all_orders)
        if total == 0:
            return {"type": "abstain"}
        cancelled = sum(s.status == "cancelled" for s in all_orders.values())
        return {"type": "percent", "answer": float((Decimal(cancelled) * 100 / total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))}
    if is_cancel and is_order:
        return {"type": "number", "answer": sum(s.status == "cancelled" for s in all_orders.values())}
    if is_percent and any(x in q for x in ("so voi", "so vs", "so vs", "thay doi", "tang hay giam", "hon hay kem")) and any(x in q for x in ("doanh thu", "dthu", "revenue")):
        if any(x in q for x in ("cung thang nam truoc", "cung ky nam ngoai", "thang nam truoc")):
            try:
                compare = (start.replace(year=start.year - 1), end.replace(year=end.year - 1))
            except ValueError:
                compare = None
        elif any(x in q for x in ("thang truoc", "thang lien truoc", "thang trc", "th truoc do", "cung ky thang truoc")) and start.day == 1:
            compare = previous_month(start, end, "thang nay" in q or "tu dau thang" in q)
        else:
            compare = None
        if compare and compare[0] >= available_start and compare[1] <= available_end:
            current = revenue(sales, start, end, asof)
            previous = revenue(sales, compare[0], compare[1], asof)
            if previous:
                value = (current - previous) * 100 / previous
                return {"type": "percent", "answer": float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))}
    rank = re.search(r"(?:top\s*|(?:cho|lay|liet ke|xem|hoi|muon|\b)\s*)(\d+)\s*(?:mat hang|mon|hang|san pham|sp)\b", q)
    if rank and 1 <= int(rank[1]) <= 10 and any(x in q for x in ("chay", "nhieu nhat", "hot")) and "nhom" not in q:
        qty = defaultdict(Decimal)
        for s in valid:
            if s.product and s.quantity is not None and norm(s.product) != "phi phuc vu":
                qty[s.product] += s.quantity
        if qty:
            products = sorted(qty, key=lambda p: (-qty[p], norm(p)))[: int(rank[1])]
            return {"type": "list", "answer": products}
    # These intents need separate denominator, comparison, join, or output logic.
    if is_percent or any(x in q for x in ("so voi", "so vs", "hon hay kem", "thay doi", "chenh lech", "tang giam", "tang hay giam", "tang bao nhieu", "giam bao nhieu", "nhap", "ton kho", "trong kho", "con no", "tra no", "thu no", "loi", "lai gop", "lai rong", "nhom ", "nhoms ", "khach", "top ", "liet ke", "size ", "bao hanh", "moi order", "tu dau nam", "nam nay", "3 thang", "thang nao", "so ngay", "may ngay", "nhieu ngay", "ngay dat dthu", "ngay doanh thu dat", "so luot", "ca sang", "ca chieu", "hoat chat", "nhan vien", "qua zalo", "tai quay", "qua dien thoai", "moi thang", "tung thang", "lan luot", "bao nhieu don co", "may don co", "don co doanh thu", "don co tien", "bill co", "tu ngay 1 1", "ke don", "can don", "chuyen khoan", "tien mat", " cod", "cuoi ngay")):
        return {"type": "abstain"}
    if re.search(r"\b\d+\s+(?:mat hang|mon|hang)\b", q) and any(x in q for x in ("nhat", "hot", "chay")):
        return {"type": "abstain"}
    if any(x in q for x in ("don gia", "gia ban", "gia bao nhieu")) and not prod:
        return {"type": "abstain"}
    if any(x in q for x in ("don co dthu", "don hang co", "co tien hang tu", "tang lien tuc", "thang sau", "nguoi ban", "ngayf nao", "nhaats", "binh quaan", "con bao nhieu")):
        return {"type": "abstain"}
    if not prod and any(x in q for x in ("mat hang", "hang nao", "ban duoc bao nhieu ly", "ban duoc bao nhieu cai", "ban dc bao nhieu ly", "ban dc bao nhieu cai")) and not any(x in q for x in ("chay nhat", "hot nhat", "nhieu nhat")):
        return {"type": "abstain"}
    if any(x in q for x in ("ngay nao", "ngay nao", "ngay ban", "ngay doanh thu")) and any(x in q for x in ("nhieu tien nhat", "cao nhat", "dat nhat", "nhieu tien")):
        by_day = defaultdict(Decimal)
        for s in valid:
            by_day[s.day] += s.amount
        if by_day:
            day = max(by_day, key=lambda d: (by_day[d], -d.toordinal()))
            return {"type": "date", "answer": day.isoformat()}
    if any(x in q for x in ("ngay nao", "hom nao")):
        return {"type": "abstain"}
    if any(x in q for x in ("ban chay nhat", "chay nhat", "hot nhat", "ban nhieu nhat")) and not prod:
        qty = defaultdict(Decimal)
        for s in valid:
            if s.product and s.quantity is not None:
                qty[s.product] += s.quantity
        if qty:
            product = max(qty, key=lambda p: (qty[p], norm(p)))
            return {"type": "string", "answer": product}
    if is_average:
        orders = {s.order for s in valid}
        if orders:
            return {"type": "number", "answer": integer(sum((s.amount for s in valid), Decimal()) / len(orders))}
    if is_order and not prod:
        return {"type": "number", "answer": len({s.order for s in valid})}
    if prod and any(x in q for x in ("don gia", "gia ban", "gia bao nhieu", "gia nhiu")):
        prices = {s.price for s in valid if s.price is not None}
        if len(prices) == 1:
            return {"type": "number", "answer": integer(next(iter(prices)))}
    if prod and any(x in q for x in ("bao nhieu kg", "may kg", "so kg", "bao nhieu ly", "may ly", "bao nhieu cai", "bao nhieu cay", "bao nhieu goi", "bao nhieu chai", "bao nhieu hop")) and not any(x in q for x in ("tien", "doanh thu", "dthu")):
        if valid and all(s.quantity is not None for s in valid):
            return {"type": "number", "answer": integer(sum((s.quantity for s in valid), Decimal()))}
    if not is_percent and not is_order and any(x in q for x in ("doanh thu", "dthu", "revenue", "bao nhieu tien", "bn tien", "tong tien", "thu ve", "ban duoc bao nhieu tien", "ban dc bao nhieu tien")):
        return {"type": "number", "answer": integer(sum((s.amount for s in valid), Decimal()))}
    return {"type": "abstain"}


def run(split: Path, output: Path) -> None:
    cache: dict[str, list[Sale]] = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    with (split / "questions.jsonl").open(encoding="utf-8") as fin, output.open("w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            item = json.loads(line)
            business = item["business_id"]
            if business not in cache:
                try:
                    cache[business] = load_business(split / "data" / business)
                except Exception as exc:
                    print(f"load {business}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    cache[business] = Sales()
            try:
                result = answer(item, cache[business])
            except Exception as exc:
                print(f"answer {item['id']}: {type(exc).__name__}: {exc}", file=sys.stderr)
                result = {"type": "abstain"}
            fout.write(json.dumps({"id": item["id"], **result}, ensure_ascii=False) + "\n")


def evaluate(split: Path, output: Path) -> None:
    gold = {a["id"]: a for a in (json.loads(line) for line in (split / "answers.jsonl").open(encoding="utf-8"))}
    pred = {a["id"]: a for a in (json.loads(line) for line in output.open(encoding="utf-8"))}
    businesses = {q["id"]: q["business_id"] for q in (json.loads(line) for line in (split / "questions.jsonl").open(encoding="utf-8"))}
    counts = Counter()
    per_type: dict[str, Counter] = defaultdict(Counter)
    per_business: dict[str, Counter] = defaultdict(Counter)
    for id_, expected in gold.items():
        got = pred.get(id_, {"type": "abstain"})
        if got["type"] == "abstain":
            outcome = "abstain"
        elif got["type"] == expected["type"] and got.get("answer") == expected.get("answer"):
            outcome = "correct"
        else:
            outcome = "wrong"
        counts[outcome] += 1
        per_type[expected["type"]][outcome] += 1
        per_business[businesses[id_]][outcome] += 1
    n = len(gold)
    print(json.dumps({"total": n, **counts, "PA": round((counts["correct"] - counts["wrong"]) / n, 4), "by_reference_type": per_type, "by_business": dict(sorted(per_business.items()))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("split", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--evaluate", action="store_true")
    args = parser.parse_args()
    run(args.split, args.output)
    if args.evaluate:
        evaluate(args.split, args.output)
