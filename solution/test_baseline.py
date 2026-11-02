import datetime as dt
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from baseline import Sale, answer, date, money, parse_table, period, quantity, relational_excel_sales, source_family, status


class BaselineRulesTest(unittest.TestCase):
    def test_money_formats(self):
        self.assertEqual(money("5k"), Decimal(5000))
        self.assertEqual(money("1.600đ"), Decimal(1600))
        self.assertEqual(money("6,719.0000"), Decimal(6719))
        self.assertEqual(money("3,185tr"), Decimal(3185000))
        self.assertEqual(quantity("1 bao 25kg"), Decimal(1))
        self.assertEqual(date("45717"), dt.date(2025, 3, 1))

    def test_returned_orders_do_not_count(self):
        self.assertEqual(status("hàng trả về"), "returned")
        self.assertEqual(status("đổi trả"), "returned")
        sales = [
            Sale("A", dt.date(2026, 5, 1), "valid", Decimal(100), "Bánh mì", Decimal(1), "cái", Decimal(100), None),
            Sale("B", dt.date(2026, 5, 1), "returned", Decimal(500), "Nước ngọt", Decimal(1), "cái", Decimal(500), None),
        ]
        item = {"question": "Doanh thu tháng 5/2026 là bao nhiêu?", "as_of_date": "2026-05-30"}
        self.assertEqual(answer(item, sales), {"type": "number", "answer": 100})

    def test_subtotal_row_is_ignored(self):
        rows = [
            ("Trạng thái xử lý", "Tổng tiền", "ngay_ban", "Số lượng", "Nội dung", "don_gia"),
            ("Đã hoàn thành", "10,000", 45717, "1 chai", "Nước", "10,000"),
            (None, "10,000", None, None, "Tổng ngày 01/03", None),
        ]
        _, sales = parse_table("SoBanHang.xlsx:Sheet1", rows)
        self.assertEqual(len(sales), 1)

    def test_month_and_previous_week(self):
        self.assertEqual(period("tháng 2/2026", dt.date(2026, 3, 4)), (dt.date(2026, 2, 1), dt.date(2026, 2, 28)))
        self.assertEqual(period("tuần rồi", dt.date(2026, 3, 4)), (dt.date(2026, 2, 23), dt.date(2026, 3, 1)))

    def test_monthly_growth_and_top_products(self):
        sales = [
            Sale("A", dt.date(2026, 4, 1), "valid", Decimal(100), "Nước ngọt", Decimal(2), "chai", None, None),
            Sale("B", dt.date(2026, 5, 1), "valid", Decimal(110), "Nước ngọt", Decimal(3), "chai", None, None),
            Sale("C", dt.date(2026, 5, 2), "valid", Decimal(10), "Bánh mì", Decimal(1), "cái", None, None),
        ]
        growth = {"question": "Doanh thu tháng 5/2026 thay đổi bao nhiêu % so với tháng trước?", "as_of_date": "2026-05-30"}
        top = {"question": "Top 2 mặt hàng bán chạy nhất tháng 5/2026", "as_of_date": "2026-05-30"}
        self.assertEqual(answer(growth, sales), {"type": "percent", "answer": 20.0})
        self.assertEqual(answer(top, sales), {"type": "list", "answer": ["Nước ngọt", "Bánh mì"]})

    def test_excel_invoice_join_ignores_unrelated_sheets(self):
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            invoices = Workbook()
            sheet = invoices.active
            sheet.title = "DanhSachHoaDon"
            sheet.append(["Mã hóa đơn", "Thời gian", "Trạng thái"])
            sheet.append(["O1", dt.datetime(2026, 5, 1), "Hoàn thành"])
            next_month = invoices.create_sheet("06.2026")
            next_month.append(["Mã hóa đơn", "Thời gian", "Trạng thái"])
            next_month.append(["O2", dt.datetime(2026, 6, 1), "Hoàn thành"])
            unrelated = invoices.create_sheet("SoQuy")
            unrelated.append(["Mã hóa đơn", "Thời gian", "Giá trị"])
            unrelated.append(["O1", dt.datetime(2026, 5, 2), 10000])
            invoices.save(folder / "DuLieuBanHang.xlsx")
            details = Workbook()
            details.active.append(["Mã hóa đơn", "Tên hàng", "Thành tiền", "Số lượng quy đổi"])
            details.active.append(["O1", "Nước ngọt", 10000, 2])
            details.active.append(["O2", "Bánh mì", 5000, 1])
            details.save(folder / "DanhSachHoaDonChiTiet.xlsx")
            sales = relational_excel_sales(folder)
            self.assertEqual(len(sales), 2)
            self.assertEqual(sales[0].day, dt.date(2026, 5, 1))
            self.assertEqual(sales[0].amount, Decimal(10000))
            self.assertEqual(sales[1].day, dt.date(2026, 6, 1))

    def test_sales_schema_prefers_item_and_goods_amount(self):
        _, items = parse_table("chi_tiet_hoa_don.csv", [
            ["Ngày giao dịch", "Thành tiền", "Mã bill", "Mặt hàng", "Nội dung"],
            ["01/02/2025", "79k", "O1", "Cơm chiên", ""],
        ])
        self.assertEqual(items[0].product, "Cơm chiên")
        _, orders = parse_table("danh_sach_hoa_don.csv", [
            ["Ngày tháng", "Mã hóa đơn", "Giá trị hàng", "TongTien"],
            ["2025-02-01", "O1", "79000", "84000"],
        ])
        self.assertEqual(orders[0].amount, Decimal(79000))

    def test_shipping_line_is_not_revenue(self):
        _, sales = parse_table("BanHang_2026.xlsx:T01-2026", [
            ["Ngày", "HĐ", "Tên hàng", "TT", "SL"],
            ["01/01/2026", "O1", "Sữa bột", "100000", 1],
            [None, "O1", "Phí ship", "15000", None],
        ])
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0].amount, Decimal(100000))

    def test_status_note_does_not_leak_to_next_order(self):
        _, sales = parse_table("BanHang_2026.xlsx:T03-2026", [
            ["Ngày", "HĐ", "Tên hàng", "TT", "Ghi chú"],
            ["01/03/2026", "O1", "Sữa bột", "100000", "Đơn huỷ"],
            [None, "O1", "Bỉm", "50000", None],
            ["02/03/2026", "O2", "Sữa bột", "200000", None],
        ])
        self.assertEqual([s.status for s in sales], ["cancelled", "cancelled", "valid"])

    def test_monthly_export_family(self):
        self.assertEqual(source_family("BanHang_T1_2025.xlsx"), source_family("BanHang_T5_2026.xlsx"))
        self.assertEqual(source_family("BanHang_Thang02_2025.xlsx"), source_family("BanHang_Thang05_2026.xlsx"))

    def test_short_service_name_does_not_filter_generic_revenue(self):
        sales = [
            Sale("A", dt.date(2026, 4, 1), "valid", Decimal(100), "rửa", None, None, None, None),
            Sale("B", dt.date(2026, 4, 2), "valid", Decimal(200), "thay bugi", None, None, None, None),
        ]
        item = {"question": "Quý 2/2026 tổng dthu bnhiu rứa?", "as_of_date": "2026-04-30"}
        self.assertEqual(answer(item, sales), {"type": "number", "answer": 300})

    def test_unresolved_product_abstains(self):
        sales = [Sale("A", dt.date(2026, 4, 1), "valid", Decimal(100), "Sữa bột", Decimal(1), "hộp", None, None)]
        item = {"question": "Tháng 4/2026 bán Găng tay sơ sinh được bao nhiêu tiền?", "as_of_date": "2026-04-30"}
        self.assertEqual(answer(item, sales), {"type": "abstain"})

    def test_average_with_common_spelling(self):
        sales = [
            Sale("A", dt.date(2026, 3, 1), "valid", Decimal(100), None, None, None, None, None),
            Sale("B", dt.date(2026, 3, 1), "valid", Decimal(200), None, None, None, None, None),
        ]
        item = {"question": "Bìn quân 1 đơn tháng 3/2026 bao nhiêu tiền?", "as_of_date": "2026-03-31"}
        self.assertEqual(answer(item, sales), {"type": "number", "answer": 150})
        abbreviated = {"question": "Dthu bình quân 1 đh tháng 3/2026 bao nhiêu tiền?", "as_of_date": "2026-03-31"}
        self.assertEqual(answer(abbreviated, sales), {"type": "number", "answer": 150})


if __name__ == "__main__":
    unittest.main()
