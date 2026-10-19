# -*- coding: utf-8 -*-
"""
================================================================================
 app.py —— PaddleOCR 全栈测试后端 (FastAPI)
================================================================================

功能接口
--------
  * POST /api/ocr-file    接收前端上传的图片文件（multipart），调用 PaddleOCR 离线识别
  * POST /api/ocr-canvas  接收前端 Canvas 生成的 Base64(dataURL) 图片，调用 PaddleOCR 识别
  * GET  /api/health      探活接口，返回后端与 OCR 引擎的就绪状态

启动方式
--------
    python app.py                # 默认监听 0.0.0.0:8000
    python app.py --port 9000    # 自定义端口
    python app.py --warmup       # 仅预热/预下载 PaddleOCR 中文模型后退出（用于离线部署准备）

依赖安装（一次性）
------------------
    pip install -U paddlepaddle paddleocr fastapi "uvicorn[standard]" python-multipart pillow

⚠ 完全离线运行说明
------------------
    PaddleOCR 的中文高精度模型（检测/分类/识别）在【首次运行】时若本机没有缓存，
    会自动联网下载到 ~/.paddleocr 目录。要做到真正的“断网离线”：
      1) 在一台能联网的机器上执行  python app.py --warmup  完成模型下载与加载；
      2) 把该机器用户目录下的 ~/.paddleocr 整个文件夹拷贝到目标离线机器；
      3) 之后 app.py 即可完全离线运行，不会发起任何网络请求。
    默认使用 CPU 推理（可用环境变量 PADDLE_OCR_USE_GPU=1 开启 GPU，需本机装有 CUDA）。

版本兼容
--------
    同时兼容 PaddleOCR 2.x 与 3.x：
      2.x 使用  use_angle_cls=True（角度分类）；识别入口  ocr.ocr(img, cls=True)
      3.x 改为  use_textline_orientation=True（文本行方向）；识别入口  ocr.predict(img)
    代码会自动探测主版本并选择对应参数与调用方式，无需手动改动。
================================================================================
"""

import base64
import io
import json
import os
import re
import sys
import threading
import time
import traceback

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image

# ------------------------------------------------------------------------------
# 环境变量预设（必须在 PaddleOCR 首次导入【之前】生效，故放在导入前置位）
# ------------------------------------------------------------------------------
# ① 默认关闭 oneDNN(MKLDNN)：规避 paddlepaddle 3.x 在 CPU 推理时的已知崩溃
#    —— "(Unimplemented) ConvertPirAttribute2RuntimeAttribute not support ..."
#    该问题会让识别直接抛异常。对审计场景正确性优先于速度；
#    若确认自己的 paddle 版本无此问题且希望加速，可设 PADDLE_OCR_USE_MKLDNN=1。
os.environ.setdefault(
    "PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT",
    "True" if os.environ.get("PADDLE_OCR_USE_MKLDNN") == "1" else "False",
)
# ② 跳过 PaddleX 启动时的模型源连通性探测，缩短启动时间（不影响模型按需下载/离线缓存）
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

# ------------------------------------------------------------------------------
# 全局配置
# ------------------------------------------------------------------------------
# 允许前端提交的图片格式（PaddleOCR 实际用 PIL 解码，这里做前置白名单校验）
ALLOWED_FORMATS = {"jpg", "jpeg", "png", "bmp", "webp", "tiff", "tif", "gif"}
# 单次上传的大小上限：默认 20MB，防止超大图片拖垮 CPU 推理
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 手写画板尺寸需与前端 <canvas> 保持一致（仅用于前端展示提示，后端不强制）
CANVAS_SIZE = (640, 400)

app = FastAPI(
    title="PaddleOCR 识别准确率测试后端",
    description="面向财务审计场景的 PaddleOCR 离线中文识别测试服务",
    version="1.0.0",
)

# ------------------------------------------------------------------------------
# 跨域中间件（CORS）
# 允许任意来源（*）—— 确保前端用“双击 HTML”以 file:// 协议打开时，
# 浏览器发出的 Origin: null 跨域请求也能被正常放行。
# allow_credentials 置为 False：本服务不使用 Cookie/Session，避免通配符与凭证冲突。
# ------------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],          # 允许 GET/POST/OPTIONS 等全部方法
    allow_headers=["*"],          # 允许 Content-Type、Accept 等全部请求头
)

# ------------------------------------------------------------------------------
# PaddleOCR 单例：延迟到第一次请求才初始化，避免服务启动即报错；全局只加载一次模型
# ------------------------------------------------------------------------------
_OCR_INSTANCE = None          # 已初始化的 PaddleOCR 引擎
_OCR_MAJOR_VERSION = 0        # 主版本号（2 或 3），决定调用方式的差异分支


def _detect_paddleocr_version(package) -> int:
    """从已导入的 paddleocr 包中解析主版本号；解析失败时保守地按 2.x 处理。"""
    try:
        ver = str(package.__version__ or "")
        first = ver.split(".")[0]
        major = int(first)
        return major
    except Exception:
        return 2


def _get_ocr():
    """
    全局单例：初始化 PaddleOCR 并返回 (实例, 主版本号)。
    采用“多组参数逐个尝试”的策略，兼顾 2.x/3.x 及其各小版本之间的参数差异，
    任何一组参数因版本不支持而抛 TypeError 时，自动降级到更保守的参数组合。
    模型首次加载需要时间（含可能的联网下载），耗时通常 5~30 秒。
    """
    global _OCR_INSTANCE, _OCR_MAJOR_VERSION
    if _OCR_INSTANCE is not None:
        return _OCR_INSTANCE, _OCR_MAJOR_VERSION

    # 1) 检查依赖是否安装
    try:
        import paddleocr  # noqa: F401
        from paddleocr import PaddleOCR
    except ImportError as e:
        raise RuntimeError(
            "未检测到 PaddleOCR，请先执行安装命令：\n"
            "    pip install -U paddlepaddle paddleocr\n"
            f"（原始错误：{e}）"
        )

    major = _detect_paddleocr_version(paddleocr)
    _OCR_MAJOR_VERSION = major

    use_gpu = os.environ.get("PADDLE_OCR_USE_GPU", "0") == "1"

    # 2) 按主版本组织候选参数组合（越靠前越完整，越靠后越保守）
    candidates = []
    if major >= 3:
        # ---- PaddleOCR 3.x ----
        # use_textline_orientation=True 等价于旧版的“角度分类校正 use_angle_cls”，
        # 负责把 180° 旋转/倾斜/竖排的文本行纠正到可读方向，对拍照的快递单很关键。
        # 注意：3.x 不再接受 show_log / use_gpu（会抛 ValueError: Unknown argument），
        # 因此这里不传；日志默认按需显示。
        candidates.append(dict(
            lang="ch",
            use_doc_orientation_classify=False,   # 不需要整页文档方向分类，节省开销
            use_doc_unwarping=False,              # 不做文档去畸变
            use_textline_orientation=True,        # 开启文本行方向校正（关键）
        ))
        candidates.append(dict(lang="ch"))       # 最保守兜底
    else:
        # ---- PaddleOCR 2.x ----
        candidates.append(dict(
            lang="ch",
            use_angle_cls=True,      # 开启角度分类（需求明确要求）
            use_gpu=use_gpu,         # 默认 CPU，保证离线环境可用
            show_log=False,          # 关闭推理过程日志刷屏
        ))
        candidates.append(dict(lang="ch", use_gpu=use_gpu))
        candidates.append(dict(lang="ch"))

    # 3) 逐个尝试初始化。
    #    历史上不同小版本对“非法参数”抛出的异常类型不一致（TypeError / ValueError），
    #    只要异常隐含“参数不被支持”，就降级到更保守的组合；其他类型的严重错误直接终止。
    last_error = None
    for kwargs in candidates:
        try:
            ocr = PaddleOCR(**kwargs)
            _OCR_INSTANCE = ocr
            return ocr, major
        except (TypeError, ValueError) as e:
            # 参数兼容性问题 → 记录后尝试下一组更保守的组合，继续重试
            last_error = e
            continue
        except Exception as e:
            # 非参数类错误（如模型下载失败 / 磁盘只读）继续重试没有意义，立即中断
            last_error = e
            break

    raise RuntimeError(
        "PaddleOCR 初始化失败。请确认模型已下载（可先在有网环境执行 python app.py --warmup）"
        "，或检查是否安装完整（pip install -U paddlepaddle paddleocr）。"
        f"最后一次错误：{last_error}"
    )


def _json_safe(obj):
    """把 numpy 数组 / numpy 标量 / np.int64 等递归转成 Python 原生类型，保证可 JSON 序列化。"""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _json_safe(obj.tolist())
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def _load_image_from_bytes(raw: bytes, source: str) -> Image.Image:
    """
    把原始图片字节解码为 PIL Image，并做鲁棒的合法性校验。
    source 参数用于在报错信息里说明是“上传文件”还是“画板数据”，便于定位问题。
    返回的图片统一为 RGB 三通道，透明区域已合成为白底。
    """
    if not raw:
        raise HTTPException(status_code=400, detail=f"{source}：未收到任何图片数据。")

    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{source}：图片大小超过上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB，请压缩后重试。",
        )

    # 1) 用 PIL 解码校验：非图片内容、被截断的图片都会在这里抛异常
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # 真正读入像素数据，能发现“扩展名对、内容损坏”的假图片
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"{source}：无法解析为有效图片（格式不正确或文件已损坏）。原始错误：{e}",
        )

    # 2) 格式白名单校验
    fmt = (img.format or "").lower()
    if fmt and fmt not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"{source}：不支持的图片格式“{fmt}”，仅支持 {sorted(ALLOWED_FORMATS)}。",
        )

    # 3) 尺寸合理性校验
    if img.width < 2 or img.height < 2:
        raise HTTPException(
            status_code=400,
            detail=f"{source}：图片尺寸过小（{img.width}×{img.height}），无法识别。",
        )
    if img.width > 8000 or img.height > 8000:
        raise HTTPException(
            status_code=400,
            detail=f"{source}：图片尺寸过大（{img.width}×{img.height}），单边超过 8000px，请先压缩。",
        )

    # 4) 透明背景一律合成到白色底上。
    #    这是手写画板场景的关键坑：Canvas 导出的 PNG 是 RGBA 透明图，
    #    若直接转 RGB，透明像素会被填成黑色，导致“黑字黑底”完全无法识别。
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    else:
        img = img.convert("RGB")

    return img


def _parse_base64_image(payload: str, source: str) -> bytes:
    """
    解析前端 Canvas 传来的 Base64 字符串。
    兼容两种形态：
      * 完整 dataURL：data:image/png;base64,xxxx
      * 纯 Base64 正文：xxxx
    解码失败或内容为空时给出明确报错。
    """
    if not payload or not isinstance(payload, str):
        raise HTTPException(
            status_code=400,
            detail=f"{source}：请求缺少 image_base64 字段，或该字段不是字符串。",
        )

    data = payload.strip()
    if data.startswith("data:"):          # 去掉 data:image/png;base64, 前缀
        _, _, b64 = data.partition(",")
    else:
        b64 = data

    b64 = re.sub(r"\s+", "", b64)         # 去除可能的换行/空格
    if not b64:
        raise HTTPException(status_code=400, detail=f"{source}：image_base64 内容为空。")

    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"{source}：Base64 解码失败，前端生成的 dataURL 可能不完整。原始错误：{e}",
        )
    if not raw:
        raise HTTPException(status_code=400, detail=f"{source}：解码后没有有效字节。")
    return raw


def _run_ocr(img: Image.Image) -> dict:
    """
    核心识别流程：调用 PaddleOCR，返回统一结构的字典。
    返回结构：
        {
          "count": 识别出的文本行数量,
          "elapsed_ms": 纯推理耗时(毫秒),
          "avg_score": 平均置信度,
          "engine": "paddleocr x.y.z",
          "items": [ {"text": "...", "score": 0.99, "box": [[x,y],...]}, ... ]
        }
    """
    ocr, major = _get_ocr()
    array = np.array(img)

    import paddleocr as _padd

    start = time.perf_counter()
    items = []

    if major >= 3:
        # ---------- PaddleOCR 3.x 调用方式 ----------
        # predict 返回 OCRResult 对象列表；result.json 是结构化结果字典，
        # 其中 rec_texts / rec_scores / rec_polys 分别对应 文本 / 置信度 / 坐标框。
        try:
            results = ocr.predict(array)
        except Exception as e:
            raise RuntimeError(f"PaddleOCR 推理阶段报错（3.x）：{e}")

        for res in results:
            data = getattr(res, "json", None)
            # 有的小版本 json 直接是 dict，有的是 JSON 字符串，也可能是 {"res": {...}} 包装
            if isinstance(data, str):
                import json
                try:
                    data = json.loads(data)
                except Exception:
                    continue
            if isinstance(data, dict) and "res" in data and isinstance(data["res"], dict):
                data = data["res"]
            if not isinstance(data, dict):
                continue

            texts = data.get("rec_texts")
            scores = data.get("rec_scores")
            polys = data.get("rec_polys")
            if polys is None:
                polys = data.get("rec_boxes")
            if texts is None:
                texts = []
            if scores is None:
                scores = []
            if polys is None:
                polys = []
            for i, t in enumerate(texts):
                items.append({
                    "text": str(t),
                    "score": round(float(scores[i]), 4) if i < len(scores) and scores[i] is not None else None,
                    "box": _json_safe(polys[i]) if i < len(polys) else None,
                })
    else:
        # ---------- PaddleOCR 2.x 调用方式 ----------
        # ocr.ocr(img, cls=True) 中 cls=True 表示对每一行做角度分类校正；
        # 返回 [ [ [box, (text, score)], ... ], ... ]，最外层是“页”的列表（单图只有一页）。
        try:
            result = ocr.ocr(array, cls=True)
        except Exception as e:
            raise RuntimeError(f"PaddleOCR 推理阶段报错（2.x）：{e}")

        for page in result:
            if not page:               # 该页没有检测到任何文本时，返回的是 None/空，需跳过
                continue
            for box, (text, score) in page:
                items.append({
                    "text": str(text),
                    "score": round(float(score), 4) if score is not None else None,
                    "box": _json_safe(box),
                })

    elapsed_ms = (time.perf_counter() - start) * 1000.0

    scored = [it["score"] for it in items if it["score"] is not None]
    avg = round(sum(scored) / len(scored), 4) if scored else 0.0

    engine = f"paddleocr {getattr(_padd, '__version__', '?')}"
    return {
        "count": len(items),
        "elapsed_ms": round(elapsed_ms, 1),
        "avg_score": avg,
        "engine": engine,
        "items": items,
    }


def _build_response(success: bool, **extra) -> dict:
    """统一返回包一层 success 标记，前端只需检查一个字段即可。"""
    out = {"success": success}
    out.update(extra)
    return out


# ------------------------------------------------------------------------------
# API 接口
# ------------------------------------------------------------------------------

# 根路径直接返回前端页面：浏览器访问 http://127.0.0.1:8000 即可打开测试台，
# 无需手动去文件管理器里找 index.html。同时保留“双击 index.html”的 file:// 方式。
# 路径用 __file__ 解析，保证从任何目录启动都定位到本文件旁的 index.html。
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/", include_in_schema=False)
def index():
    """返回前端单页应用。"""
    return FileResponse(os.path.join(_BASE_DIR, "index.html"))


@app.get("/api/health")
def api_health():
    """探活接口：不加载模型，仅报告服务与依赖就绪状态，供前端页面加载时探测后端地址。"""
    try:
        import paddleocr
        engine = f"paddleocr {paddleocr.__version__}"
        module_ok = True
        detail = "依赖已安装；模型将在首次识别请求时自动加载（离线模式下需预先 warmup）。"
    except Exception as e:
        engine = "未安装"
        module_ok = False
        detail = f"尚未安装 paddleocr：{e}。请先执行 pip install -U paddlepaddle paddleocr。"
    return {
        "status": "ok" if module_ok else "degraded",
        "engine": engine,
        "detail": detail,
    }


@app.post("/api/ocr-file")
def api_ocr_file(file: UploadFile = File(...)):
    """
    接口一：接收前端上传的图片文件（multipart/form-data，字段名为 file）。
    """
    try:
        # 1) 读取并校验原始字节
        raw = file.file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"上传文件读取失败：{e}")

    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空（0 字节）。")

    # 文件名只用来在日志/报错里提示，不参与任何路径拼接，避免路径穿越风险
    filename = (file.filename or "未命名").strip()

    try:
        img = _load_image_from_bytes(raw, source=f"上传文件「{filename}」")
    except HTTPException:
        raise  # 已是面向用户的友好错误，直接透传
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"图片预处理发生未知错误：{e}")

    return _build_response(True, **_run_ocr(img))


@app.post("/api/ocr-canvas")
def api_ocr_canvas(body: dict):
    """
    接口二：接收前端 Canvas 生成的 Base64 图片。
    请求体示例：{"image_base64": "data:image/png;base64,iVBORw0KGgo..."}
    body 用 dict 直接接收，并做类型兜底，避免字段名/类型细节差异造成 500。
    """
    payload = body.get("image_base64") if isinstance(body, dict) else None
    try:
        raw = _parse_base64_image(payload, source="画板数据")
        img = _load_image_from_bytes(raw, source="画板数据")
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"画板数据预处理发生未知错误：{e}")

    return _build_response(True, **_run_ocr(img))


# ------------------------------------------------------------------------------
# 审计核对（Excel）模块：列识别 / 启动 / 进度 / 结果下载
# ------------------------------------------------------------------------------
# 任务存内存字典，结果 xlsx 以 base64 存在内存中，全程不写盘、不碰原始文件。
AUDIT_JOBS = {}                     # job_id -> job 状态字典
_AUDIT_LOCK = threading.Lock()      # 同一时间只跑一个审计（PaddleOCR 实例非线程安全）
_AUDIT_JOB_TIMEOUT = 60 * 40        # 40 分钟后的任务自动清理


def _audit_job_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def _audit_prune():
    """清理早已完成/过期的任务，避免内存无限增长。"""
    now = time.time()
    for jid in list(AUDIT_JOBS.keys()):
        j = AUDIT_JOBS[jid]
        if j.get("status") in ("done", "error") and now - j.get("started", 0) > _AUDIT_JOB_TIMEOUT:
            AUDIT_JOBS.pop(jid, None)


@app.post("/api/audit/columns")
def api_audit_columns(file: UploadFile = File(...)):
    """读取上传 Excel 的表头，自动识别列角色，返回可编辑的列映射。

    前端可据此展示映射下拉框，让用户确认/手动调整后再提交运行。
    """
    from openpyxl import load_workbook
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        wb = load_workbook(io.BytesIO(raw))
    except Exception as e:
        return _build_response(False, detail=f"无法解析 Excel 文件：{e}")

    ws = wb.worksheets[0]  # 默认处理第一个工作表

    # 自动定位表头行：找第一行“非空单元格数 >= 3”的行，否则退化为第 1 行
    header_row = 1
    for r in range(1, min(ws.max_row, 8) + 1):
        non_empty = sum(1 for c in range(1, ws.max_column + 1)
                        if (ws.cell(row=r, column=c).value not in (None, "")))
        if non_empty >= 3:
            header_row = r
            break

    headers = [str(ws.cell(row=header_row, column=c).value or "").strip()
               for c in range(1, ws.max_column + 1)]

    import audit_engine as ae
    detected = ae.detect_columns(headers)   # 0-based
    mapping = {}
    for role, v in detected.items():
        if v is None:
            mapping[role] = None
        else:
            mapping[role] = {"col": v["index"] + 1, "name": v["name"], "auto": True}

    roles = [{"key": r, "label": ae.ROLE_LABELS[r]} for r in ae.ROLE_LABELS]
    sample_rows = []
    for r in range(header_row + 1, min(header_row + 4, ws.max_row + 1)):
        sample_rows.append([str(ws.cell(row=r, column=c).value or "")
                            for c in range(1, ws.max_column + 1)])

    return _build_response(True, headers=headers, mapping=mapping,
                           roles=roles, header_row=header_row,
                           sample_rows=sample_rows, max_row=ws.max_row)


@app.post("/api/audit/start")
def api_audit_start(file: UploadFile = File(...), mapping: str = Form("{}")):
    """启动审计：上传 xlsx（原文件只读入副本）+ 列映射 JSON。"""
    import audit_engine as ae
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        mapping_cfg = json.loads(mapping) if isinstance(mapping, str) else mapping
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"列映射 JSON 解析失败：{e}")

    if not isinstance(mapping_cfg, dict):
        raise HTTPException(status_code=400, detail="列映射必须是 JSON 对象")

    # 兜底校验：发票/S·N/妥投三个必需链接列若一个都没映射，直接拒绝，
    # 避免“整表结果全是缺少链接”这种无法使用的输出。
    required_links = [("invoice_link", "发票链接/图片"),
                      ("sn_image_link", "S/N照片链接"),
                      ("wm_image_link", "妥投照片链接")]
    miss_links = [name for role, name in required_links if not mapping_cfg.get(role)]
    if len(miss_links) == len(required_links):
        return _build_response(
            False,
            detail=("列映射缺少全部必需链接列（" + "、".join(miss_links) +
                    "）。请返回上一步，在「核对项」下拉框里为发票/S·N照片/妥投照片"
                    "指定对应列后，再点「开始审计」。"))

    if not _AUDIT_LOCK.acquire(blocking=False):
        return _build_response(False, detail="已有审计任务在运行，请等待其完成后再提交")

    _audit_prune()

    job_id = _audit_job_id()
    job = {"id": job_id, "status": "running", "total": 0, "done": 0,
           "current": None, "error": None, "result": None,
           "xlsx_b64": None, "started": time.time()}
    AUDIT_JOBS[job_id] = job

    def _progress(done, total, cur):
        job["done"] = done
        job["total"] = total
        job["current"] = cur

    def _worker():
        try:
            summary = ae.run_audit(raw, mapping_cfg, progress=_progress)
            job["status"] = "done"
            job["total"] = summary["total"]
            job["result"] = {
                "counts": summary["counts"],
                "total": summary["total"],
                "rows": summary["rows"],   # 结果预览（每行状态+备注）
            }
            job["xlsx_b64"] = base64.b64encode(summary["xlsx"]).decode()
        except Exception as e:
            traceback.print_exc()
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            _AUDIT_LOCK.release()

    threading.Thread(target=_worker, daemon=True).start()
    return _build_response(True, job_id=job_id)


@app.get("/api/audit/status/{job_id}")
def api_audit_status(job_id: str):
    """查询任务进度与结果预览（前端轮询）。"""
    j = AUDIT_JOBS.get(job_id)
    if not j:
        return _build_response(False, detail="任务不存在或已过期")
    return _build_response(True, status=j["status"], total=j["total"],
                           done=j["done"], current=j.get("current"),
                           error=j.get("error"), result=j.get("result"))


@app.get("/api/audit/download/{job_id}")
def api_audit_download(job_id: str):
    """下载审核结果 xlsx（内存中生成，不改动原文件）。"""
    from urllib.parse import quote
    j = AUDIT_JOBS.get(job_id)
    if not j or j.get("status") != "done" or not j.get("xlsx_b64"):
        raise HTTPException(status_code=404, detail="审核结果尚未生成或任务已过期")
    data = base64.b64decode(j["xlsx_b64"])
    # HTTP 头仅支持 ASCII：文件名同时给 ASCII 与 RFC5987(UTF-8) 两种，中文名在浏览器端可正常显示
    fn_ascii = f"audit_result_{job_id}.xlsx"
    fn_utf = quote(f"审核结果_{job_id}.xlsx")
    return Response(
        content=data,
        media_type=("application/vnd.openxmlformats-"
                    "officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition":
                     f"attachment; filename=\"{fn_ascii}\"; filename*=UTF-8''{fn_utf}",
                 "Cache-Control": "no-store"},
    )

    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content=_build_response(
            False,
            detail=f"服务器内部错误：{exc}",
            path=request.url.path,
        ),
    )


# ------------------------------------------------------------------------------
# 全局兜底异常处理器：把任何未捕获异常转成 JSON，返回清晰的错误文案而不是空白 500
# ------------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content=_build_response(
            False,
            detail=f"服务器内部错误：{exc}",
            path=request.url.path,
        ),
    )


# ------------------------------------------------------------------------------
# 模型预热：联网机器上执行 python app.py --warmup 即可预下载全部中文模型
# ------------------------------------------------------------------------------
def warmup():
    """加载模型并用一张 64x64 白图做一次空跑，确保模型文件完整就位。"""
    print("[warmup] 正在初始化 PaddleOCR 并加载中文模型（首次可能需要联网下载）...")
    start = time.perf_counter()
    ocr, major = _get_ocr()
    dummy = Image.new("RGB", (64, 64), (255, 255, 255))
    _run_ocr(dummy)
    print(f"[warmup] 完成！PaddleOCR 主版本={major}（当前文件夹无需再下载模型），"
          f"总耗时 {time.perf_counter() - start:.1f} 秒。")
    print("[warmup] 现在可以把本机 ~/.paddleocr 目录拷贝到离线机器实现完全离线运行。")


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="PaddleOCR 测试后端")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--warmup", action="store_true", help="仅预热模型后退出")
    args = parser.parse_args()

    if args.warmup:
        try:
            warmup()
            sys.exit(0)
        except Exception as e:
            print(f"[warmup] 失败：{e}", file=sys.stderr)
            sys.exit(1)

    print("=" * 64)
    print("  PaddleOCR 识别准确率测试后端已启动")
    print(f"  👉 请用浏览器访问:  http://127.0.0.1:{args.port}")
    print(f"     （后端已自动托管前端页面，打开即用；也可直接双击 index.html）")
    print(f"  接口: POST /api/ocr-file | POST /api/ocr-canvas | GET /api/health")
    print("=" * 64)
    uvicorn.run(app, host=args.host, port=args.port)
