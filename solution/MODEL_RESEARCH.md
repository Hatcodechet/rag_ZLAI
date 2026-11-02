# Nghiên cứu LLM và phương án chạy Docker cho DataMate

Ngày rà soát: 2026-11-02. Đây là thiết kế và tiêu chí thử nghiệm, chưa phải kết quả benchmark model.

## Ràng buộc và mục tiêu

- Chương trình cuối cần chạy offline, đọc `/data`, ghi `/result/submission.jsonl`; theo `PLAN.md`, tổng model inference không quá 15B tham số và private test dưới 6 giờ.
- Điểm là Penalized Accuracy: đúng +1, sai -1, abstain 0. Vì thế cần đo **điểm cuối cùng** sau thực thi, không chọn model chỉ theo khả năng trả lời văn bản.
- `nvidia-smi` trên môi trường hiện tại không kết nối được NVIDIA driver. Chưa biết VRAM và cấu hình GPU máy chấm; mọi ước lượng bộ nhớ/tốc độ phải kiểm chứng trên phần cứng thật.
- Public test không có nhãn; dùng train và validation chia theo **business_id** để chọn model, prompt, và ngưỡng abstain.

## Model nên thử

| Model | Vai trò | Lý do thử | Rủi ro |
|---|---|---|---|
| [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) | Mốc LLM đầu tiên | Text instruct, Apache 2.0, kích thước nhỏ nhất trong shortlist; phù hợp trích xuất intent/filters thành JSON | Có thể hiểu câu hỏi khó kém hơn 8B/9B; phải đo trên tiếng Việt và schema mới |
| [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) | Mốc chất lượng kế tiếp | Text model, Apache 2.0, hỗ trợ thinking và non-thinking; thử non-thinking trước để giảm thời gian | Thinking mặc định cần tắt rõ khi chạy thử nhanh; bộ nhớ lớn hơn 4B |
| [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B) | Ứng viên chất lượng cao | Apache 2.0, dưới giới hạn 15B, có hướng dẫn vLLM và chế độ chỉ text | Model card ghi cần vLLM từ nhánh phát triển cho Qwen3.5; kiểm thử tương thích container/version trước khi chọn |

Không chọn model có tổng tham số trên 15B, kể cả MoE có ít tham số active. Không chạy nhiều model cùng lúc nếu tổng tham số vượt giới hạn. Bộ nhớ trọng số BF16 xấp xỉ 2 byte/tham số: 4B ~8 GB, 8B ~16 GB, 9B ~18 GB, **chưa tính** KV cache, runtime, multimodal encoder hay overhead; đây chỉ là phép tính kích thước, không phải yêu cầu VRAM đủ để chạy.

## Vai trò của model trong pipeline

1. Bộ đọc hiện tại chuẩn hóa CSV/XLSX/SQLite thành dữ liệu nghiệp vụ. Mở rộng tiếp để lấy đủ khách, kho, công nợ, lãi và luật ngành.
2. Parser rule-based trả lời câu mà nó xử lý chắc chắn. Câu còn lại đưa cho LLM với **câu hỏi, `as_of_date`, rulebook liên quan và schema cột**, không nhồi toàn bộ sổ vào prompt.
3. LLM sinh JSON plan có schema cố định, ví dụ `metric`, `period`, `entity`, `filters`, `comparison`, `output_type`. JSON plan phải được validate; chỉ cho phép metric và phép lọc đã implement. Không thực thi SQL/code do model tạo trực tiếp.
4. Python/SQL thực thi plan trên dữ liệu đã chuẩn hóa, áp quy tắc doanh thu, trả hàng, thời gian, đơn vị, làm tròn. Model không tự cộng tiền hoặc đoán số.
5. Kiểm tra dữ liệu nguồn, phạm vi thời gian, định dạng đáp án và consistency. Nếu plan không hợp lệ hoặc thiếu nguồn, trả `abstain`; nếu xác định dữ liệu phát hành không đủ theo rulebook, trả `unanswerable`.

## Serving và Docker

**Chạy nộp bài:** một entrypoint batch trong Docker đọc `/data/questions.jsonl` rồi ghi `/result/submission.jsonl`. Không cần mở cổng HTTP nếu hệ thống chỉ chấm file. Dùng inference batch nội bộ để tránh chi phí khởi động server hoặc mạng trong container.

**Chạy phát triển:** có thể dùng [vLLM OpenAI-compatible server](https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html) qua Docker và gọi API cục bộ để thử prompt. vLLM cũng hỗ trợ [structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs/) và [Docker image chính thức](https://docs.vllm.ai/en/latest/deployment/docker/). Không phụ thuộc API bên ngoài khi chấm.

Thứ tự đóng gói:

1. Đóng gói baseline Python thành Docker batch trước, cố định Python và `openpyxl`, kiểm tra output bằng mount `/data` và `/result`.
2. Tải model/checkpoint **trước khi build hoặc trước khi chấm**, lưu weights trong image/volume được phép; chạy chế độ offline, không tải từ Hub khi container đang chấm. Pin revision của model và version runtime thay vì dùng `latest`.
3. Thử 4B qua vLLM trên GPU, giới hạn context và output tokens theo độ dài câu hỏi; dùng JSON schema constrained decoding và validate ở code. Test batch 100 câu, sau đó toàn train/validation, đo throughput, peak VRAM, thời gian khởi động và tỉ lệ lỗi JSON.
4. So cùng bộ validation với 8B và 9B. Qwen3.5 cần kiểm thử runtime riêng do yêu cầu phiên bản vLLM. Chỉ giữ model có PA tăng so với baseline và qua giới hạn thời gian/bộ nhớ.
5. Chốt image/revision, chạy lại offline hai lần, xác nhận 100% ID và format; không chứa logic theo ID câu hỏi hay ID cửa hàng.

## Quyết định fine-tune

Chưa fine-tune. Trước tiên đo lỗi: parser hiểu sai intent, executor thiếu metric, reader đọc sai schema, hay dữ liệu thật sự thiếu. Nếu lỗi hiểu câu hỏi chiếm đa số và few-shot + JSON schema chưa đủ, thử LoRA trên cặp `question -> plan` được kiểm chứng từ **train**. Không train trực tiếp `question -> answer` vì số tiền phụ thuộc sổ của từng cửa hàng. Giữ riêng cửa hàng validation để tránh rò rỉ cùng schema.

## Gate để chọn model

- PA trên validation theo business **cao hơn** baseline trên cùng tập, với số sai và abstain được báo riêng.
- Tất cả JSON plan hợp schema; output cuối đúng type/format rulebook; các trường không được hỗ trợ không tự tạo đáp án.
- Tổng tham số dưới 15B; chạy offline; private workload ước lượng và chạy thử dưới 6 giờ trên GPU tương đương máy chấm.
- Chỉ số phải được đo thực tế. Hiện chưa có GPU khả dụng và chưa benchmark LLM nên chưa thể khẳng định 4B/8B/9B đáp ứng thời gian hoặc tăng điểm.
