# DataMate: kế hoạch triển khai

## Mục tiêu và giới hạn

Xây chương trình nhận một split dữ liệu tại `/data`, đọc `questions.jsonl`, `rulebook.md`, `calendar.csv` và các thư mục `data/<business_id>`, rồi ghi `/result/submission.jsonl`. Mỗi câu phải có đúng `id`, `type` và `answer` tương ứng. Chạy hoàn toàn offline, không dùng public/private test để huấn luyện, không viết logic theo ID câu hỏi hoặc ID cửa hàng. Tổng tham số mọi model inference không vượt 15B; tính cả tham số không kích hoạt của MoE. Thời gian chạy toàn private test dưới 6 giờ trên máy tham chiếu.

Chỉ số tối ưu là Penalized Accuracy: đúng +1, sai -1, abstain 0. Mọi thay đổi phải đo trên tập train và trên các cửa hàng train giữ riêng để kiểm tra khả năng tổng quát hóa.

## Kiến trúc

1. **Nạp dữ liệu:** đọc CSV, mọi sheet XLSX, SQLite và README của cửa hàng; nhận diện header, ngày, số tiền, ô gộp/ô trống trong nhóm. Giữ provenance của từng dòng (file, sheet, row).
2. **Chuẩn hóa:** biểu diễn đơn, dòng hàng, sản phẩm, khách, phiếu nhập, thanh toán, tồn kho, lô và bảng giá. Các trường không có trong dữ liệu phải được đánh dấu thiếu, không tự suy ra.
3. **Hiểu câu hỏi:** biến câu tiếng Việt thành một cấu trúc gồm metric, kỳ thời gian, đối tượng, bộ lọc, phép so sánh, kiểu đầu ra. Bắt đầu bằng parser xác định; dùng LLM offline cho câu parser chưa hiểu hoặc còn nhập nhằng.
4. **Tính toán:** thực thi cấu trúc bằng code/SQL, áp rulebook đúng split. Quy tắc chung tách khỏi quy tắc ngành.
5. **Kiểm chứng:** kiểm tra khả năng trả lời, phạm vi ngày dữ liệu, đơn vị, mẫu số, định dạng và tính nhất quán. Không đủ bằng chứng thì `unanswerable` hoặc `abstain` tùy trường hợp.
6. **Xuất kết quả:** ghi JSONL tăng dần, có log lỗi ở đường dẫn riêng; không để một câu lỗi làm hỏng cả bài.

## Thứ tự thực hiện

### M1 — Baseline xác định, đo được

- CLI đọc split và xuất submission; lệnh đánh giá train tính đúng/sai/abstain theo type, business, nhóm lỗi.
- Bộ đọc CSV/XLSX/SQLite và chuẩn hóa đơn hàng cho các dạng sổ phổ biến.
- Parser thời gian: hôm nay, hôm qua, ngày cụ thể, tuần, tháng, quý, năm; chặn ngày tương lai theo `as_of_date` khi phù hợp.
- Metrics đầu tiên: doanh thu, số đơn, số lượng, giá bán, ngày doanh thu cao nhất, mặt hàng bán chạy nhất, tỉ lệ huỷ, doanh thu trung bình đơn.
- Golden tests cho quy tắc trọng yếu: trạng thái trả hàng, giảm giá dòng/đơn, ngày bao gồm hai đầu, làm tròn, quy đổi đơn vị.

**Gate:** chạy hết 3.000 câu train không lỗi format, có báo cáo PA và thời gian. Câu chưa hỗ trợ trả abstain. Mọi câu trả lời phải truy được tới các dòng nguồn.

### M2 — Nghiệp vụ đầy đủ

- Tích hợp công nợ theo dòng thanh toán và ngày trả; tồn kho, nhập hàng, lô/hạn dùng, giá vốn/lãi gộp.
- Luật ngành: cà phê, nhà thuốc, vật liệu xây dựng, mẹ và bé, sửa xe, điện thoại.
- Xếp hạng, list, table và so sánh kỳ; kiểm tra `unanswerable` khi kỳ nằm ngoài dữ liệu hoặc thiếu cột bắt buộc.
- Đánh giá theo cửa hàng giữ riêng, không sửa theo ID câu hỏi.

**Gate:** PA tăng trên cửa hàng giữ riêng, lỗi sai nghiệp vụ được phân loại và sửa bằng quy tắc tổng quát.

### M3 — Model offline và tối ưu

- Thử Qwen3.5-9B ở vai trò tạo cấu trúc câu hỏi, không giao model tự tính tiền. So với baseline và Qwen3-8B trên cùng tập giữ riêng, đo PA, tỷ lệ lỗi format và thời gian.
- Chỉ fine-tune LoRA nếu lỗi hiểu câu hỏi là nút thắt đã đo được. Dữ liệu fine-tune là câu hỏi -> cấu trúc/plans được kiểm chứng từ train, không phải câu hỏi -> đáp án số thuần túy.
- Calibrate ngưỡng abstain trên train validation: chỉ trả lời khi xác suất đúng ước lượng đủ cao cho thang điểm +1/-1.
- Docker offline, kiểm tra số tham số và giấy phép, cố định phiên bản model/runtime, seed và kết quả chạy lại.

**Gate:** chạy public test không lỗi, trong giới hạn 6 giờ trên cấu hình tương đương máy chấm; hai lần chạy cho kết quả ổn định.

## Quy tắc đánh giá

- Chia train theo **business**, không chia ngẫu nhiên từng câu, vì nhiều câu cùng schema và cùng dữ liệu.
- Ghi `correct`, `wrong`, `abstain`, `PA=(correct-wrong)/N` theo business, type, metric.
- Public test chỉ dùng để chạy inference và xem phản hồi leaderboard; không lấy đáp án để train hoặc hard-code.
- Mỗi lần mở rộng metric phải giữ baseline trước đó và chạy lại regression tests liên quan.

## Trạng thái

- [x] Kiểm kê dữ liệu và rulebook.
- [x] Ghi kế hoạch.
- [x] M1: baseline chạy hết 3.000 câu train và có báo cáo PA. Bản đo ngày 2026-10-27: 1.240 đúng, 20 sai, 1.740 abstain; PA = 0,4067; thời gian 191,67 giây trên máy hiện tại. Đã đọc CSV, XLSX, SQLite và nối các bảng CSV đơn hàng/chi tiết/sản phẩm. Phạm vi parser và metrics còn giới hạn; chưa có provenance tới từng dòng trong output.
- [x] Cải thiện ngày 2026-10-28: nối hóa đơn và chi tiết hàng từ Excel nhiều sheet; chọn sheet theo schema để bỏ sheet phụ nhưng giữ các sheet tháng. Chặn 5 kiểu câu parser dễ trả sai. Train: 1.288 đúng, 20 sai, 1.692 abstain; PA = 0,4227, tăng 0,0160 so với bản trước; thời gian 184,79 giây. 6 unit tests đạt.
- [x] Mở rộng alias cho các cách đặt tên cột khác của CSV/Excel, đọc sổ sửa xe và bỏ dòng phí ship/voucher khỏi doanh thu. Đo lại train: PA giữ 0,4227, thời gian 200,20 giây; 8 unit tests đạt. Public: 199/1.000 câu không abstain (bản cũ 102), 801 abstain; đủ ID và đúng cấu trúc. Chưa có điểm public vì không có đáp án công khai.
- [x] Phản hồi leaderboard của bản 199 câu: PA public = -0,0150. Đã phát hiện và sửa ba lỗi tổng quát: file bán hàng tách theo tháng chỉ đọc một file; trạng thái/ghi chú của đơn trước lan sang đơn sau; tên mặt hàng quá ngắn hoặc không khớp vẫn tạo đáp án doanh thu. Siết các intent chưa hỗ trợ và sửa một số cách hỏi bình quân. Train mới: 1.323 đúng, 27 sai, 1.650 abstain; PA = 0,4320; 13 unit tests đạt. Candidate public mới có 179 câu không abstain, 821 abstain, đúng format. Chưa có điểm public của candidate này.
- [x] Smoke test public: xuất đủ 1.000/1.000 ID, không trùng, đúng thứ tự và cấu trúc JSONL; 898 abstain, 102 câu có đáp án hoặc unanswerable. Thời gian 61,99 giây trên máy hiện tại. Public không có đáp án để tính PA.
- [ ] M2 nghiệp vụ đầy đủ.
- [ ] M3 model offline, Docker và kiểm tra thời gian.
- [x] Rà soát model/runtime ngày 2026-11-02: shortlist Qwen3-4B-Instruct-2507, Qwen3-8B, Qwen3.5-9B; đề xuất vLLM cho thử nghiệm GPU và entrypoint batch Docker cho bài nộp. Chi tiết tại `solution/MODEL_RESEARCH.md`. Chưa benchmark vì `nvidia-smi` không kết nối được driver trên môi trường hiện tại.

### Ưu tiên tiếp theo

1. Phân loại 20 câu sai trên train theo nguyên nhân, sửa quy tắc tổng quát và giữ số sai thấp; ưu tiên doanh thu, trạng thái trả/hủy, đơn vị tiền, thời gian.
2. Mở rộng nguồn cho các cửa hàng abstain nhiều, đặc biệt `biz_124`, rồi thêm công nợ, kho, giá vốn/lãi gộp và câu trả lời dạng list/table.
3. Tách validation theo cửa hàng để đánh giá khả năng áp dụng lên schema mới. Mỗi thay đổi chạy lại train và public test, kiểm tra JSONL đủ ID và đúng kiểu.
4. Khi parser xác định đã ổn định, thử Qwen3.5-9B offline để tạo query plan; chỉ giữ model nếu PA và độ phủ tăng trên validation theo cửa hàng. Sau đó đóng gói Docker và đo thời gian trên cấu hình tương đương máy chấm.
