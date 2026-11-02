# Baseline DataMate

Baseline xác định để đo độ đúng trước khi tích hợp model. Hiện xử lý một số câu về đơn bán, doanh thu, giá, số lượng, ngày bán cao nhất, top mặt hàng và phần trăm thay đổi doanh thu theo tháng. Các câu chưa hỗ trợ trả `abstain`. Đây chưa phải lời giải hoàn chỉnh cho công nợ, kho, lãi, luật ngành, bảng hoặc mọi dạng so sánh kỳ.

## Chạy

Cần Python 3.10+ và `openpyxl` để đọc Excel (`pip install openpyxl`). Không cần GPU hay mạng khi chạy.

```bash
python solution/baseline.py train /tmp/datamate_train_baseline.jsonl --evaluate
python solution/baseline.py public_test submission.jsonl
python -m unittest discover -s solution -p 'test_*.py'
```

Chạy các lệnh từ thư mục chứa `train/` và `public_test/`. Trên workspace hiện tại, môi trường `/home/tripnv/miniconda3/envs/detap/bin/python` đã có `openpyxl`.

`--evaluate` chỉ dùng với train vì public test không có đáp án. File kết quả được ghi từng dòng theo đúng thứ tự câu hỏi.

File `submission.jsonl` tạo từ `public_test` là file để upload lên public leaderboard. Định dạng yêu cầu là JSON Lines: mỗi dòng là một object JSON chứa `id`, `type` và `answer` khi type cần giá trị. Để tạo lại trên workspace này, thay `python` trong lệnh trên bằng `/home/tripnv/miniconda3/envs/detap/bin/python`.

Lần đo train gần nhất: PA 0,4320 (1.323 đúng, 27 sai, 1.650 abstain) trên 3.000 câu. Bản public trước, dù đạt PA 0,4227 trên train, chỉ được -0,0150 trên leaderboard. Bản `submission.jsonl` hiện tại đã sửa lỗi ghép file tháng, lỗi kế thừa trạng thái huỷ sang đơn mới và siết điều kiện trả lời; có 179 câu không abstain, đủ 1.000 ID và đúng format. Điểm public của bản này chỉ biết sau khi upload.

Bộ đọc hiện hỗ trợ file CSV phẳng, Excel nhiều sheet, Excel/CSV tách hóa đơn và chi tiết hàng, cùng POS SQLite. Dùng README/rulebook của từng split để kiểm tra lại cách hiểu các cột trước khi mở rộng metric.

## Phạm vi và rủi ro hiện tại

- Parser dựa trên alias cột và mẫu câu. Cần mở rộng để hiểu schema mới, ghi chú trạng thái, quy đổi đơn vị và luật ngành.
- Một số nguồn chỉ có bảng đơn mà không có chi tiết hàng; câu hỏi theo sản phẩm phải abstain.
- Chưa có model, Docker hoặc cơ chế confidence calibration. File hiện tại phù hợp để nộp thử public leaderboard; điểm public cần được kiểm tra sau khi upload. Docker vẫn cần cho vòng nộp lời giải cuối.
