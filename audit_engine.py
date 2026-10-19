# -*- coding: utf-8 -*-
"""
审计核对引擎（基于 PaddleOCR 测试台复用）

职责：
  1) 列角色自动识别（同义词词典 + 配置映射，列名不固定时可用）
  2) 逐行读取 Excel（openpyxl，内存副本，绝不修改原文件）
  3) 按规格依次核对：
       规则① 发票链接（图片/PDF）→ 提取关键字段与表格比对
       规则② 京东物流网页 → 跳过，备注“需人工核验”（预留扩展）
       规则③ 外包装 S/N 照片 → 校验 S/N 码
       规则④ 妥投照片水印地址 → 校验 市/区 与收货地址一致
  4) 回写「审核情况」「审核备注」列，输出内存中的结果 Excel

对外唯一入口：run_audit(xlsx_bytes, mapping, progress) -> summary
OCR 引擎复用 app._run_ocr（PaddleOCR LANGO=ch + 方向校正，已适配 2.x/3.x）。
"""

import io
import re
import time
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------
# 常量：审核结论文案（按需求四）
# ----------------------------------------------------------------------------
RESULT_PASS   = "审核通过-补贴未发放"
RESULT_FAIL   = "审核未通过"
RESULT_MANUAL = "需人工复核"

STATUS_PASS, STATUS_FAIL, STATUS_MANUAL = "pass", "fail", "manual"

# ----------------------------------------------------------------------------
# 列角色 与 同义词词典（需求五：列名不固定，自动识别 + 配置映射）
# 说明：匹配时取“命中的最长关键词”，避免“发票”这类泛词误匹配。
# ----------------------------------------------------------------------------
ROLE_LABELS = {
    "invoice_link":   "①发票链接/图片（PDF或图片）",
    "jd_link":        "②京东物流单链接（暂跳转人工）",
    "sn_image_link":  "③外包装S/N照片链接",
    "wm_image_link":  "④妥投照片（水印地址）链接",
    "invoice_no":     "发票号",
    "invoice_date":   "开票日期",
    "invoice_amount": "开票金额（价税合计）",
    "buyer_name":     "发票购买方名称",
    "seller_name":    "发票销售方名称",
    "sn_code":        "表格S/N码",
    "receive_addr":   "表格收货地址",
}
# 注：关键词统一小写；匹配时对表头做全角->半角 + 小写归一
ROLE_KEYWORDS = {
    "invoice_link":   ["发票链接", "发票图片", "发票地址", "发票电子", "发票文件"],
    "jd_link":        ["物流单链接", "物流链接", "物流跟踪链接", "物流单"],
    "sn_image_link":  ["外包装sn", "sn照片", "sn图片", "序列号照片", "sn码照片"],
    "wm_image_link":  ["妥投照片", "妥投", "签收照片", "水印照片", "收货照片"],
    "invoice_no":     ["发票号码", "发票号", "发票编号"],
    "invoice_date":   ["开票日期", "开票时间"],
    "invoice_amount": ["开票金额", "发票金额", "价税合计"],
    "buyer_name":     ["发票购买方名称", "购买方名称", "购买方"],
    "seller_name":    ["发票销售方名称", "销售方名称", "销售方"],
    "sn_code":        ["s/n码", "sn码", "s/n", "序列号"],
    "receive_addr":   ["收货地址", "收件地址", "收货人地址"],
}

# 空值表现：示例表用字符串 'NULL' / '#N/A' / '' 表示空
_EMPTY_VALUES = {"", "null", "na", "#n/a", "nan", "none", "-", "/"}


def detect_columns(headers: List[str]) -> Dict:
    """根据表头自动识别各列角色；未命中返回 None（交给用户在界面手动映射）。

    headers: 表头字符串列表（位置即列序，0-based）。
    返回：{role: {"index": int(0-based), "name": str} | None}
    """
    norm = [half_width(str(h or "")).lower() for h in headers]
    mapping = {}
    for role, kws in ROLE_KEYWORDS.items():
        best_idx, best_name, best_len = None, None, 0
        for idx, h in enumerate(norm):
            for kw in kws:
                if kw in h and len(kw) > best_len:
                    best_idx, best_name, best_len = idx, str(headers[idx]), len(kw)
        if best_idx is not None:
            mapping[role] = {"index": best_idx, "name": best_name, "auto": True}
        else:
            mapping[role] = None
    return mapping

# ----------------------------------------------------------------------------
# 通用归一化工具
# ----------------------------------------------------------------------------
def half_width(s) -> str:
    """全角 -> 半角，全角空格 -> 普通空格。"""
    out = []
    for ch in str(s):
        o = ord(ch)
        if o == 0x3000:
            out.append(" ")
        elif 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def compact(s) -> str:
    """归一化：半角、大写、去掉所有空白，用于字段正则与精确比对。"""
    return re.sub(r"\s+", "", half_width(str(s))).upper()


def digits_only(s) -> str:
    """仅保留数字（用于金额/纯数字比对，忽略 ¥ 元 逗号）。"""
    return re.sub(r"\D", "", half_width(str(s)))


def date_iso(s) -> str:
    """把常见日期写法归一为 YYYY-MM-DD；无法解析返回 ''。"""
    s = half_width(str(s))
    m = re.search(r"(\d{4})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})", s)
    if m:
        try:
            return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return ""
    m = re.search(r"(\d{8})", re.sub(r"\s", "", s))
    if m:
        d = m.group(1)
        return "%s-%s-%s" % (d[0:4], d[4:6], d[6:8])
    return ""


def cell_str(v) -> str:
    """单元格值 -> 字符串；把 NULL/#N/A/NaN 等空表现归一为空串。"""
    if v is None:
        return ""
    if isinstance(v, float) and v != v:  # NaN
        return ""
    s = str(v).strip()
    if s.lower() in _EMPTY_VALUES:
        return ""
    return s


def extract_addr_parts(s: str) -> Tuple[str, str, str]:
    """从中文地址/水印地址中提取 (省, 市, 区县)。

    规则：
      - 出现“X省”则省=X，并从文本中剥离（避免省名混入市级）；
      - 没有“省”字时，若开头是 2 字省级简称（如“江苏南京市”），剥离之；
      - 第一个“X市”为地级市；其后紧跟的“X区/X县/X市(县级市)”为区县；
      - 无法切分时对应字段返回 ""。
    """
    s = re.sub(r"\s+", "", half_width(str(s)))
    province = city = county = ""
    m = re.search(r"([\u4e00-\u9fff]{1,10}?)省", s)
    if m:
        province = m.group(1) + "省"
        s = s[len(province):]
    # 直辖市：北京市/上海市/天津市/重庆市（无省）
    mm = re.match(r"(北京|上海|天津|重庆)市", s)
    if mm:
        city = mm.group(1) + "市"
        tail = s[len(city):]
        mc = re.search(r"([\u4e00-\u9fff]{1,10}?(?:区|县))", tail)
        if mc:
            county = mc.group(1)
        return province, city, county
    # 无“省”字时，剥离 2 字省级简称（如“江苏南京市”→“南京市”），
    # 但要避免把“吉林市”这种本身就是市名的开头剥掉。
    if s[:2] in _PROVINCE_ABBR and len(s) > 2 and s[2] != "市":
        s = s[2:]
    # 找第一个“X市”（地级市）
    m2 = re.search(r"([\u4e00-\u9fff]{1,12}?)市", s)
    if m2:
        city = m2.group(1) + "市"
        tail = s[m2.end():]
        # 区县：区/县，或县级市（“X市”）
        mc = re.search(r"([\u4e00-\u9fff]{1,10}?(?:区|县|市))", tail)
        if mc:
            county = mc.group(1)
    return province, city, county


# 中国 31 个省级行政区简称（2 字），用于“江苏省”写法省略时的剥离
_PROVINCE_ABBR = [
    "北京", "天津", "河北", "山西", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东",
    "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
    "青海", "宁夏", "新疆", "内蒙古",
]


# ----------------------------------------------------------------------------
# 远端资源获取（需求三：按链接下载）
# ----------------------------------------------------------------------------
_DOWNLOAD_TIMEOUT = 25
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_bytes(url: str) -> Optional[bytes]:
    """下载链接内容。注意：必须用 GET（部分签名链接拒绝 HEAD）。
    失败返回 None。"""
    import requests
    for attempt in (1, 2):
        try:
            r = requests.get(url, timeout=_DOWNLOAD_TIMEOUT,
                             headers={"User-Agent": _USER_AGENT,
                                      "Accept": "*/*"})
            if r.status_code == 200 and r.content:
                return r.content
            if r.status_code == 206:  # 分段下载成功也应可用
                return r.content
        except Exception:
            pass
        time.sleep(0.6)
    return None


def _load_pil(raw: bytes) -> Optional["object"]:
    """bytes -> PIL RGB 图；失败返回 None。"""
    try:
        from PIL import Image
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


def is_pdf(raw: bytes) -> bool:
    return raw[:5] == b"%PDF-"


# ----------------------------------------------------------------------------
# 发票（PDF/图片）→ 关键字段
# ----------------------------------------------------------------------------
def pdf_text_layer(raw: bytes) -> str:
    """尽量抽 PDF 文本层全文；异常或无文本返回 ''。"""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(raw)
        parts = []
        for i in range(len(doc)):
            tp = doc[i].get_textpage()
            for meth in ("get_text_bounded", "get_text_range", "extract_text"):
                f = getattr(tp, meth, None)
                if not f:
                    continue
                try:
                    v = f()
                    if isinstance(v, str) and v.strip():
                        parts.append(v.strip())
                        break
                except Exception:
                    continue
        return "\n".join(parts)
    except Exception:
        return ""


def pdf_to_image(raw: bytes):
    """PDF 首页渲染为 PIL RGB 图；非 PDF 直接当图片加载。失败返回 None。"""
    try:
        if is_pdf(raw):
            import pypdfium2 as pdfium
            return pdfium.PdfDocument(raw)[0].render(scale=2.2).to_pil().convert("RGB")
        return _load_pil(raw)
    except Exception:
        return None


def invoice_fields(raw: bytes) -> Tuple[Dict[str, str], str]:
    """提取发票关键字段，返回 (fields, source)。

    策略（自适应两类数电发票）：
      1) 先抽 PDF 文本层：若文本层能把「发票号码」等信息提出来，优先采用（最精确）；
      2) 本类发票文本层是“标签与值分离”排版（提取不出邻接字段），自动回退为
         渲染成图 + OCR —— 该方式会把「发票号码：26327…」排在同一行，提取稳定；
      3) 两者合并：文本层有的用文本层，缺的字段由 OCR 补齐。
    """
    tl_text = pdf_text_layer(raw) if is_pdf(raw) else ""
    fields = {}
    source = []

    if tl_text.strip() and len(tl_text) > 40:
        ft = extract_invoice_fields(tl_text)
        for k, v in ft.items():
            if v:
                fields[k] = v
        if ft.get("invoice_no"):
            source.append("textlayer")

    img = pdf_to_image(raw)
    if img is not None:
        ot = _ocr_text(img)
        if ot.strip():
            fo = extract_invoice_fields(ot)
            for k, v in fo.items():
                if v and not fields.get(k):
                    fields[k] = v
            source.append("ocr")

    return fields, "+".join(dict.fromkeys(source)) or "none"


def _ocr_text(img) -> str:
    """复用 app._run_ocr（PaddleOCR 中文高精度）。"""
    import app as _app
    try:
        data = _app._run_ocr(img)
        lines = []
        for it in data.get("items", []):
            lines.append(it.get("text", ""))
        return "\n".join(lines)
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# 发票关键字段提取（号码/日期/金额走“去空白文本”正则；名称按“行”匹配）
# ----------------------------------------------------------------------------
def extract_invoice_fields(text: str) -> Dict[str, str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    c = compact(text)  # 去掉空白便于正则
    fields = {}
    # 发票号码
    m = re.search(r"发票号码[:：]?(\d{8,30})", c)
    if m:
        fields["invoice_no"] = m.group(1)
    # 开票日期
    m = re.search(r"开票日期[:：]?(\d{4}年\d{1,2}月\d{1,2}日?)", c)
    if m:
        fields["invoice_date"] = date_iso(m.group(1))
    else:
        m = re.search(r"开票日期[:：]?(\d{4}[-.]\d{1,2}[-.]\d{1,2})", c)
        if m:
            fields["invoice_date"] = date_iso(m.group(1))
    # 价税合计（小写）金额
    m = re.search(r"小写[)）]?[¥￥]?([\d,]+\.\d{2})", c)
    if m:
        fields["invoice_amount"] = m.group(1)
    else:
        m = re.search(r"价税合计[（(]小写[)）][¥￥]?([\d,]+\.\d{2})", c)
        if m:
            fields["invoice_amount"] = m.group(1)

    # 名称：按“行”提取 名称：X 与 纳税人识别号：Y，顺序配对；
    # 用“纯18位数字=身份证号=>购买方”判断买/卖，与行序无关（OCR行序常交叉）。
    names, ids = [], []
    for ln in lines:
        m2 = re.match(r"名称\s*[:：]?\s*(\S+)$", ln)
        if m2 and len(m2.group(1)) >= 2:
            nm = m2.group(1).split("统一社会信用代码")[0].strip()[:40]
            if 1 <= len(nm) <= 40 and not nm.startswith(("购买方", "销售方")):
                names.append(nm)
        m3 = re.search(r"纳税人识别号\s*[:：]?\s*([0-9A-Za-z]{18})", ln)
        if m3:
            ids.append(m3.group(1))
    buyer_name = seller_name = ""
    if len(names) == len(ids) and len(names) == 2:
        for nm, idv in zip(names, ids):
            if idv.isdigit() and len(idv) == 18:
                buyer_name = nm
            else:
                seller_name = nm
    elif len(names) == 2 and not ids:
        buyer_name, seller_name = names  # 兜底：第一个=购买方
    if buyer_name:
        fields["buyer_name"] = buyer_name
    if seller_name:
        fields["seller_name"] = seller_name
    return fields


# ----------------------------------------------------------------------------
# 规则③ S/N 码
# ----------------------------------------------------------------------------
def sn_candidates(img) -> List[str]:
    """从图片 OCR 结果中收集 S/N 码候选（8 位以上字母数字串，去重保序）。"""
    import app as _app
    try:
        data = _app._run_ocr(img)
    except Exception:
        return []
    cands = []
    for it in data.get("items", []):
        t = compact(it.get("text", ""))
        for m in re.finditer(r"(?<![A-Za-z0-9])[A-Za-z0-9]{8,}(?![A-Za-z0-9])", t):
            cands.append(m.group(0))
    seen = set()
    out = []
    for c_ in cands:
        if c_ not in seen:
            seen.add(c_)
            out.append(c_)
    return out


# ----------------------------------------------------------------------------
# 规则④ 水印地址
# ----------------------------------------------------------------------------
def watermark_addr(img) -> Optional[Tuple[str, str]]:
    """从妥投照片水印提取 (市, 区县)。无法识别返回 None。"""
    import app as _app
    try:
        data = _app._run_ocr(img)
    except Exception:
        return None
    lines = [compact(it.get("text", "")) for it in data.get("items", [])]
    # 优先找“拍摄地点：”水印行；其次找同时含 省+市 的地址行
    target = ""
    for ln in lines:
        if ln.startswith("拍摄地点"):
            target = ln
            break
    if not target:
        for ln in lines:
            if "省" in ln and "市" in ln:
                target = ln
                break
    if not target:
        # 保底：以整段识别文本当作地址去切分
        target = "".join(lines)
    # 去掉“拍摄地点：”这类标签前缀，避免标签混入省名
    target = re.split(r"[：:]", target, 1)[-1].strip()
    _, city, county = extract_addr_parts(target)
    if not city or len(city) > 12:  # 解析不到市、或解析结果明显异常
        return None
    return city, county


# ----------------------------------------------------------------------------
# 单元格访问：同时兼容“普通文本链接”与“单元格超链接对象”
# ----------------------------------------------------------------------------
def cell_url(ws, row, col):
    if col is None:
        return ""
    v = ws.cell(row=row, column=col).value
    try:
        h = ws.cell(row=row, column=col).hyperlink
        tgt = h.target if h is not None else None
    except Exception:
        tgt = None
    s = cell_str(v)
    if s:
        return s
    if tgt:
        return str(tgt)
    return s


# ----------------------------------------------------------------------------
# 主入口：运行整表审计
# ----------------------------------------------------------------------------
def run_audit(xlsx_bytes: bytes,
              mapping: Dict,
              progress=None) -> Dict:
    """执行审计，返回摘要；不写盘，全部在内存副本上进行。

    mapping 结构（由前端 + detect_columns 生成，1-based 列号，None=该行不启用）：
      {
        "header_row": 1,
        "invoice_link": 43, "jd_link": 44, "sn_image_link": 48, "wm_image_link": 49,
        "invoice_no": 38, "invoice_date": 39, "invoice_amount": 42,
        "buyer_name": 40, "seller_name": 41, "sn_code": 24, "receive_addr": 14,
      }
    """
    from openpyxl import load_workbook

    total = done = 0
    counts = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_MANUAL: 0}
    rows_out = []

    wb = load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb.worksheets[0]

    header_row = int(mapping.get("header_row", 1) or 1)
    first_data = max(header_row + 1, 1)
    last_row = ws.max_row

    # 确保有「审核情况」「审核备注」列（优先复用已有，否则追加）
    col_status = _ensure_columns(ws, header_row)

    total = max(0, last_row - first_data + 1)
    if progress:
        try:
            progress(0, total, None)
        except Exception:
            pass

    for row_idx in range(first_data, last_row + 1):
        status, note = audit_one_row(ws, row_idx, mapping)
        ws.cell(row=row_idx, column=col_status["status"], value=status)
        ws.cell(row=row_idx, column=col_status["remark"], value=note)
        # 统计
        key = STATUS_MANUAL if status == RESULT_MANUAL else (
            STATUS_FAIL if status == RESULT_FAIL else STATUS_PASS)
        counts[key] = counts.get(key, 0) + 1
        done += 1
        rows_out.append({"row": row_idx, "status": status,
                         "remark": (note or "")[:500], "snapshot": _row_snapshot(ws, row_idx, header_row)})
        if progress:
            try:
                progress(done, total, row_idx)
            except Exception:
                pass

    # 生成结果 xlsx 字节（内存）
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    return {
        "ok": True,
        "total": total,
        "counts": counts,
        "rows": rows_out,
        "xlsx": xlsx_bytes,
    }


def _ensure_columns(ws, header_row):
    """返回 {'status': c1, 'remark': c2} 不存在则追加到右侧。"""
    status_col = remark_col = None
    for c in range(1, ws.max_column + 1):
        t = cell_str(ws.cell(row=header_row, column=c).value)
        if t == "审核情况":
            status_col = c
        elif t == "审核备注":
            remark_col = c
    if status_col is None:
        status_col = ws.max_column + 1
        ws.cell(row=header_row, column=status_col, value="审核情况")
    if remark_col is None:
        remark_col = ws.max_column + 1
        ws.cell(row=header_row, column=remark_col, value="审核备注")
    return {"status": status_col, "remark": remark_col}


def _row_snapshot(ws, row, header_row):
    """提取可见关键数据（调试/审计留痕用），只取映射列未必有，故取全部非空前几列。"""
    try:
        vals = []
        for c in range(1, min(ws.max_column, 10) + 1):
            v = cell_str(ws.cell(row=row, column=c).value)
            if v:
                vals.append(v if len(v) < 40 else v[:37] + "...")
        return vals
    except Exception:
        return []


# ----------------------------------------------------------------------------
# 单行审计：按需求三的四个规则依次处理
# ----------------------------------------------------------------------------
def audit_one_row(ws, row, mapping) -> Tuple[str, str]:
    notes = []
    have_fail = have_manual = False

    def _mapped_hint(role):
        """链接列读不到值时，说明是“没映射”还是“映射了但为空”，便于定位原因。"""
        col = _m(mapping, role)
        if not col:
            return f"（未映射「{ROLE_LABELS.get(role, role)}」列→下拉框里没选）"
        return f"（已映射第{col}列，但该单元格为空）"

    def note_fail(msg):
        nonlocal have_fail
        have_fail = True
        notes.append(f"【未通过】{msg}")

    def note_manual(msg):
        nonlocal have_manual
        have_manual = True
        notes.append(f"【需人工】{msg}")

    def note_info(msg):
        notes.append(f"[提示] {msg}")

    # ---------- 规则② 京东物流（按规格：暂不自动核验，仅备注） ----------
    jd_url = cell_url(ws, row, _m(mapping, "jd_link"))
    if jd_url:
        note_info("京东物流需人工核验（当前版本暂不自动登录抓取，已在规则中预留扩展接口）")
    else:
        note_info(f"缺少京东物流链接{_mapped_hint('jd_link')}，跳过")

    # ---------- 规则① 发票 ----------
    inv_url = cell_url(ws, row, _m(mapping, "invoice_link"))
    if inv_url:
        raw = fetch_bytes(inv_url)
        if raw is None:
            note_fail("发票链接无法访问（下载失败/超时）")
        else:
            try:
                fields, fsrc = invoice_fields(raw)
                if not fields:
                    note_manual("发票未能自动提取任何关键字段，需人工复核")
                else:
                    _check_invoice_fields(mapping, ws, row, fields,
                                          note_fail, note_manual, note_info)
                    note_info(f"发票提取成功，字段来源：{fsrc}；识别字段={str(fields)}")
            except Exception as e:
                note_fail(f"发票处理异常：{type(e).__name__}: {e}")
    else:
        note_manual(f"缺少发票链接/图片{_mapped_hint('invoice_link')}，无法自动核对")

    # ---------- 规则③ S/N 码 ----------
    sn_url = cell_url(ws, row, _m(mapping, "sn_image_link"))
    sn_code_table = cell_str(ws.cell(row=row, column=_m(mapping, "sn_code")).value) if _m(mapping, "sn_code") else ""
    if sn_url:
        raw = fetch_bytes(sn_url)
        if raw is None:
            note_fail("外包装S/N照片无法访问（下载失败/超时）")
        else:
            img = _load_pil(raw)
            if img is None:
                note_manual("外包装S/N照片无法解析为图片")
            else:
                cands = sn_candidates(img)
                if not sn_code_table:
                    note_manual("表格S/N码为空，无法比对")
                elif not cands:
                    note_info("图片中未识别到 S/N 码（不判定错误，请人工确认外包装照片）")
                else:
                    sn_norm = compact(sn_code_table)
                    if sn_norm in set(cands):
                        note_info(f"S/N码一致：{sn_norm}")
                    else:
                        note_fail(f"S/N码不一致：表格={sn_norm}，图片识别={','.join(cands[:6])}")
    else:
        note_info(f"缺少外包装S/N照片链接{_mapped_hint('sn_image_link')}，跳过S/N核验")

    # ---------- 规则④ 水印地址 ----------
    wm_url = cell_url(ws, row, _m(mapping, "wm_image_link"))
    addr_table = cell_str(ws.cell(row=row, column=_m(mapping, "receive_addr")).value) if _m(mapping, "receive_addr") else ""
    if wm_url:
        raw = fetch_bytes(wm_url)
        if raw is None:
            note_fail("妥投照片无法访问（下载失败/超时）")
        else:
            img = _load_pil(raw)
            if img is None:
                note_manual("妥投照片无法解析为图片")
            else:
                wm_rc = watermark_addr(img)
                if wm_rc is None:
                    note_manual("水印地址无法识别（未提取到 市/区 信息），需人工复核")
                else:
                    wm_city, wm_county = wm_rc
                    _, t_city, t_county = extract_addr_parts(addr_table) if addr_table else ("", "", "")
                    if not t_city:
                        note_manual("表格收货地址无法解析出市，需人工核对")
                    else:
                        extra = "，请人工确认" if not wm_county else ""
                        if wm_city == t_city:
                            note_info(f"水印市级一致：{t_city}")
                        else:
                            note_fail(f"水印市不一致：水印={wm_city}，表格={t_city}{extra}")
                        if wm_county and t_county:
                            if wm_county == t_county:
                                note_info(f"水印区县一致：{t_county}")
                            else:
                                note_fail(f"水印区县不一致：水印={wm_county}，表格={t_county}")
                        elif not wm_county:
                            note_manual("水印未体现区级地址（或区级与市并列），区级一致性需人工确认")
    else:
        note_info(f"缺少妥投照片链接{_mapped_hint('wm_image_link')}，跳过水印地址核验")

    # ---------- 汇总 ----------
    if have_fail:
        status = RESULT_FAIL
    elif have_manual:
        status = RESULT_MANUAL
    else:
        status = RESULT_PASS
    return status, "；".join(notes) if notes else ""


def _m(mapping, key):
    """取 1-based 列号；0/空 -> None。"""
    v = mapping.get(key)
    if v in (None, "", 0):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _check_invoice_fields(mapping, ws, row, fields, note_fail, note_manual, note_info) -> bool:
    """逐一比对发票提取字段 vs 表格字段；返回是否全部通过（无失败）。"""
    ok = True

    def cmp_one(label, extract_key, table_val, kind="text"):
        nonlocal ok
        ext = fields.get(extract_key, "")
        t = cell_str(table_val)
        if not t:
            note_info(f"发票{label}：表格未提供该列值，跳过比对")
            return
        if not ext:
            note_manual(f"发票未识别到「{label}」，需人工复核提取")
            ok = False
            return
        if kind == "digits":
            match = (digits_only(ext) == digits_only(t) and digits_only(t) != "")
        elif kind == "date":
            match = (date_iso(ext) != "" and date_iso(ext) == date_iso(t))
        else:
            match = (compact(ext) == compact(t))
        if match:
            note_info(f"发票{label}一致：{t}")
        else:
            if compact(t):
                note_fail(f"发票{label}不一致：发票={ext}，表格={t}")
            ok = False

    col = lambda key: _m(mapping, key)

    if col("invoice_no"):
        cmp_one("发票号", "invoice_no",
                ws.cell(row=row, column=col("invoice_no")).value, kind="digits")
    if col("invoice_date"):
        cmp_one("开票日期", "invoice_date",
                ws.cell(row=row, column=col("invoice_date")).value, kind="date")
    if col("invoice_amount"):
        cmp_one("开票金额", "invoice_amount",
                ws.cell(row=row, column=col("invoice_amount")).value, kind="digits")
    if col("buyer_name"):
        cmp_one("购买方名称", "buyer_name",
                ws.cell(row=row, column=col("buyer_name")).value)
    if col("seller_name"):
        cmp_one("销售方名称", "seller_name",
                ws.cell(row=row, column=col("seller_name")).value)
    return ok
