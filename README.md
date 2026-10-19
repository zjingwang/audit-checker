# 🧾 消费明细审计核对工具（PaddleOCR 驱动）

一个**完全本地离线**的网页工具，把「消费明细 / 补贴申领 Excel」逐行审核一遍，按你的规则自动核对**发票、京东物流、S/N 码、妥投照片水印**，最后输出一份 `原文件名_审核结果.xlsx`——**不改动任何原始文件**。

它同时保留了底层的 **PaddleOCR 中文识别准确率测试台**（形近字 / 连笔手写评估），前端一个页面两个标签页，开箱即用。

- 后端：Python + FastAPI（PaddleOCR `lang="ch"` 中文高精度 + 文本行方向校正）
- 前端：单个 HTML 文件，双击即可打开
- 兼容 PaddleOCR 2.x 与 3.x 两代 API，自动适配，无需改代码
- 审计引擎**全程内存运行**：读原表副本 → 逐行核对 → 输出结果表，不写盘、不动原文件

---

## ✨ 审计功能做了什么

对 Excel 里**每一行**（一行 = 一笔消费/一件商品）依次核对：

| 步骤 | 核对内容 | 判定 |
|---|---|---|
| ① 发票 | 顺着「发票链接/图片」取到发票（PDF 或图片），识别出 发票号 / 开票日期 / 开票金额 / 购买方名称 / 销售方名称，与表格对应列比对 | 任一不一致 → **审核未通过**；无法提取 → 需人工复核 |
| ② 京东物流 | 规格明确：当前版本**不自动登录抓取**京东物流，只做提示 | 不改变审核结论 |
| ③ S/N 码 | 打开「家电外包装 SN 照片」识别 S/N，与表格 S/N 码列比对 | 不一致 → 审核未通过 |
| ④ 妥投照片水印 | 识别照片水印的 **市 / 区县**，与表格收货地址所在地比对 | 不一致 → 审核未通过 |

每一行最终有三种结论之一，写入结果表的「审核情况」列：

- `审核通过-补贴未发放`
- `审核未通过`
- `需人工复核`

「审核备注」列会写清**每一条核对依据**（一致/不一致/缺什么），方便复核人追溯。只要有任何一项不过即判「审核未通过」；有「需人工」项但无不过项，则判「需人工复核」。

> 💡 想照自己的字段核对？列都是**下拉可配**的：上传后自动识别常见列名，识别不对的手动指一下即可（见下方「使用步骤」）。

---

## 📁 目录结构

```
audit-checker/
├── app.py            # 后端服务（FastAPI + PaddleOCR + 审计任务管理）
├── audit_engine.py   # 审计核对引擎（列识别/发票/S·N/水印/结果输出，纯内存）
├── index.html        # 前端网页（单文件，双击即用；含 OCR 测试台 + 审计两个标签页）
├── requirements.txt  # Python 依赖清单
├── tests.py          # 后端 OCR 接口自测（无需真实模型）
├── audit_test.py     # 审计引擎离线回归测试（不打网、不加载模型，31 项断言）
├── smoke_test.py     # 真实模型冒烟测试（需要已下载模型）
├── start.sh          # Linux/macOS 一键启动
└── LICENSE           # Apache-2.0 开源协议
```

---

## 🚀 快速开始（第一次使用请看这里）

### 0️⃣ 前置要求

- **Python 3.10 或更高版本**（官网下载：https://www.python.org/downloads/）
- 能联网（装依赖；PaddleOCR 模型首次运行联网下载，之后永久留档）

> Windows 用户把命令里的 `python` 换成 `py` 或 `python` 即可（在 cmd / PowerShell 里执行）。

### 1️⃣ 安装依赖

```bash
python -m pip install -r requirements.txt
```

> 依赖含深度学习框架 paddlepaddle，约几百 MB，请耐心等待。
> 下载慢可换镜像：`python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

### 2️⃣ 启动后端

```bash
python app.py
```

Linux / macOS 也可用一键脚本（自动复用模型缓存、避免重复下载）：

```bash
./start.sh            # 默认 8000 端口
./start.sh 9000       # 指定端口 9000
```

看到下图提示即成功：

```
========================================================
  PaddleOCR 识别准确率测试后端已启动
  👉 请用浏览器访问:  http://127.0.0.1:8000
     （后端已自动托管前端页面，打开即用；也可直接双击 index.html）
========================================================
```

> **首次启动较慢（5~30 秒）**：后端加载/下载中文模型，之后每次都快。

### 3️⃣ 打开页面（两个标签页）

浏览器访问 `http://127.0.0.1:8000`。页面顶部有 **① OCR 识别测试** 和 **② 审计核对（Excel）** 两个标签。

### 4️⃣ 使用「审计核对」（核心）

1. 切到 **② 审计核对** 标签，把 `.xlsx`（如「消费明细表.xlsx」）**拖进**虚线框（或点击选择）；
2. 后端自动解析表头并**自动识别每一列的角色**（发票链接、物流链接、S/N 照片、妥投照片、发票号、开票日期、开票金额、买卖方名称、S/N 码、收货地址等）；
3. 检查自动识别结果，没认出的列在**下拉框手动指定**（尤其是四个“链接类”列：发票 / 物流 / S·N照片 / 妥投照片，缺了无法核对该项）；
4. 点 **「▶ 开始审计」**，页面显示逐行进度；
5. 完成后看到统计（通过 / 未通过 / 需人工复核）与逐行结果预览；
6. 点 **「⬇ 下载审核结果」**，得到 `消费明细表_审核结果.xlsx`：**原始文件保持原样**，结果表在最右侧追加「审核情况」「审核备注」两列（若原表已有这两列则直接复用）。

> 🎯 想快速试跑？任何一张按上面规则列好列名的 Excel 都行；列名对不上也能在第三步手动指定后运行。
> 若整张表**一列都没识别出来**，多半是表头行不是第 1 行——把「第 _ 行是表头」的数字改对再点「重新识别列」。

### 5️⃣ 使用「OCR 识别测试」（保留了原测试台）

- 方式 A：把发票/快递单/手写单据图片**拖进**「① 上传图片文件」，自动识别并展示每行文本与置信度；
- 方式 B：在「② 手写识别」画板用鼠标/手指书写（或点下方的**形近字样例**一键写入），点「识别画板文字」；
- 右侧结果按置信度着色：🟢 ≥90% 可信；🟡 75%~90% 一般；🔴 <75% 建议人工复核。

---

## 🛡️ 完全离线部署（审计内网必读）

模型只在**本机无缓存**时联网下载。断网可用：

1. 在联网机器上预热一次：`python app.py --warmup`；
2. 把模型缓存目录拷到内网机器（Windows：`C:\Users\你\.paddleocr` 与 `.paddlex`；Linux/macOS：`~/.paddleocr` 与 `~/.paddlex`）；
3. 内网 `python app.py` 即可，全程不发外部网络请求（审计时只请求你表格里的发票/图片链接）。

> 审计中下载的发票 PDF、SN 照片、妥投照片 URL 均由**你的表格提供**，工具只按链接抓取后识别，不上传任何数据到第三方。

---

## 📡 API 接口

### OCR 测试

| 接口 | 方法 | 请求 | 返回 |
|---|---|---|---|
| `/api/ocr-file` | POST | `multipart`，字段 `file` | `{success,count,avg_score,elapsed_ms,engine,items:[{text,score,box}]}` |
| `/api/ocr-canvas` | POST | `{"image_base64":"data:image/png;base64,..."}` | 同上 |
| `/api/health` | GET | 无 | `{status,engine,detail}` |

### 审计核对

| 接口 | 方法 | 请求 | 返回 |
|---|---|---|---|
| `/api/audit/columns` | POST | 上传 xlsx | `{success,headers,roles,mapping,header_row,max_row}`，`mapping[role]={col,name,auto}`（1-based） |
| `/api/audit/start` | POST | `multipart`：`file` + `mapping`(JSON) | `{success, job_id}` |
| `/api/audit/status/{job_id}` | GET | 无 | `{success,status,total,done,current,error,result}`，`result.counts={pass,fail,manual}`、`result.rows=[{row,status,remark,snapshot}]` |
| `/api/audit/download/{job_id}` | GET | 无 | 审核结果 xlsx 字节（`Content-Disposition` 带中文文件名） |

`mapping` 结构示例（1-based 列号，空列传 `null`）：
```json
{
  "header_row": 1,
  "invoice_link": 43, "jd_link": 44, "sn_image_link": 48, "wm_image_link": 49,
  "invoice_no": 38, "invoice_date": 39, "invoice_amount": 42,
  "buyer_name": 40, "seller_name": 41, "sn_code": 24, "receive_addr": 14
}
```

错误统一返回 `{success:false, detail:"中文原因"}`。

想要更高一级自动化？直接把 `audit_engine.run_audit(xlsx_bytes, mapping)` 拿进自己的 Python 脚本：
```python
from audit_engine import run_audit, detect_columns
raw = open("消费明细.xlsx", "rb").read()
mapping = detect_columns(...)     # 或用前端给你确认过的 mapping
summary = run_audit(raw, mapping) # 全程内存，返回 {ok,total,counts,rows,xlsx: bytes}
open("消费明细_审核结果.xlsx", "wb").write(summary["xlsx"])  # 原文件从未被打开写入
```

---

## 🧪 自测

```bash
python tests.py          # OCR 接口 37 项自测（无需真实模型）
python audit_test.py     # 审计引擎 31 项离线回归（不打网、不加载模型）
python smoke_test.py     # 真实模型冒烟（需已下载模型）
```

---

## ❓ 常见问题（FAQ）

**Q1：审计时一直停在某一行进度不动？**
该行正在下载/识别发票或照片，首行前还要加载模型。行数多时总时长 = 行数 × 单行数秒到数十秒（发票渲染+OCR 最慢）。别关页面，耐心等待；结束后可下载结果表。

**Q2：为什么某一行判成了「需人工复核」而不是自动通过？**
四种可能：发票字段提取不全（PDF/图片太花）、该行缺链接、S/N 或水印识别不到、收货地址解析不出市。备注列会写明具体缺哪一项。

**Q3：会不会改坏我的原始 Excel？**
不会。引擎把原表读成**内存副本**处理，结果另存新文件；代码里没有任何写回原文件的路径（可用 `audit_test.py` 里的“原文件未动”断言佐证）。

**Q4：浏览器访问 8000 打不开或提示后端未连接？**
后端没启动或换了端口：执行 `python app.py` / `./start.sh`；换端口后访问对应网址，双击 `index.html`（file://）方式则需改文件顶部 `const API_BASE` 的端口。

**Q5：发票是 PDF，能识别吗？**
能。优先读 PDF 文本层；对“标签与值分离”排版的数电发票会**自动回退为渲染成图 + OCR**，两种都能稳定提起发票号/日期/金额/买卖方名称。

**Q6：这些数据会不会上传到服务器？**
不会。推理全部在本机；只会向本机后端发请求。审计下载的发票/照片也是由你的表格提供链接、直接从源站抓取识别。

**Q7：报错 `ConvertPirAttribute2RuntimeAttribute`？**
paddlepaddle 3.x CPU 开 oneDNN 的已知问题，本项目默认已规避；如无此问题想提速可设 `PADDLE_OCR_USE_MKLDNN=1`。

---

## 📜 协议

本项目基于 [Apache License 2.0](LICENSE) 开源，可自由使用、修改、商用。
