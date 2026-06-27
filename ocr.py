"""OCR 디스패처 — EasyOCR 우선, Tesseract 폴백.

설정 `ocr_engine` 값:
  - 'auto'     : EasyOCR 가용 시 사용, 아니면 Tesseract
  - 'easyocr'  : EasyOCR 강제 (없으면 unavailable)
  - 'tesseract': Tesseract 강제 (기존 동작)
"""

import re


# ── EasyOCR 모델 캐시 (Reader 인스턴스 로드가 5~10초 걸리므로 모듈 레벨 재사용) ──
_easyocr_reader = None
_easyocr_lang_cache = None


_LANG_MAP = {
    'kor': 'ko', 'eng': 'en', 'jpn': 'ja',
    'chi_sim': 'ch_sim', 'chi_tra': 'ch_tra',
}


def _to_easyocr_langs(lang_str):
    """tesseract 형식 'kor+eng' 또는 'ko,en' → EasyOCR ['ko','en']."""
    parts = re.split(r'[+,]', lang_str or 'kor+eng')
    out = []
    seen = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        code = _LANG_MAP.get(p, p)
        if code not in seen:
            out.append(code)
            seen.add(code)
    return out or ['ko', 'en']


def _probe_easyocr():
    try:
        import easyocr  # noqa: F401
        return True, 'easyocr available'
    except Exception as e:
        return False, f'easyocr not available: {e}'


def _probe_tesseract():
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except Exception as e:
        return False, f'pytesseract not available: {e}'
    try:
        ver = pytesseract.get_tesseract_version()
        return True, f'tesseract {ver}'
    except Exception as e:
        return False, f'tesseract binary missing: {e}'


def probe(engine='auto'):
    """OCR 가용성 점검. (ok: bool, reason: str) 반환.

    engine='auto' 인 경우 EasyOCR을 먼저 시도하고 안 되면 Tesseract.
    """
    if engine == 'easyocr':
        return _probe_easyocr()
    if engine == 'tesseract':
        return _probe_tesseract()
    # auto
    ok, reason = _probe_easyocr()
    if ok:
        return True, reason
    ok2, reason2 = _probe_tesseract()
    if ok2:
        return True, f'{reason2} (easyocr fallback)'
    return False, f'no OCR engine — {reason}; {reason2}'


def _get_easyocr_reader(langs):
    global _easyocr_reader, _easyocr_lang_cache
    langs_key = tuple(langs)
    if _easyocr_reader is None or _easyocr_lang_cache != langs_key:
        import easyocr
        _easyocr_reader = easyocr.Reader(list(langs), gpu=False, verbose=False)
        _easyocr_lang_cache = langs_key
    return _easyocr_reader


def _run_easyocr(image_path, lang_str):
    """EasyOCR 수행. (text, status) 반환.

    텍스트 영역의 bbox 중심 y좌표 기준으로 정렬해 위→아래 자연 순서로 join.
    """
    try:
        langs = _to_easyocr_langs(lang_str)
        reader = _get_easyocr_reader(langs)
        results = reader.readtext(image_path)
        # bbox = [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
        def y_center(item):
            box = item[0]
            return (box[0][1] + box[2][1]) / 2
        results.sort(key=y_center)
        lines = [str(r[1]).strip() for r in results if r and r[1]]
        return '\n'.join(l for l in lines if l), 'ok'
    except Exception as e:
        return f'[EasyOCR failed: {e}]', 'failed'


def _run_tesseract(image_path, lang_str):
    """기존 Tesseract 경로 — kor 누락 시 eng 폴백."""
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return '[OCR unavailable]', 'unavailable'
    try:
        with Image.open(image_path) as img:
            text = pytesseract.image_to_string(img, lang=lang_str)
        return text or '', 'ok'
    except Exception:
        try:
            with Image.open(image_path) as img:
                text = pytesseract.image_to_string(img, lang='eng')
            return text or '', 'partial'
        except Exception as e2:
            return f'[OCR failed: {e2}]', 'failed'


def run_ocr_on_file(image_path, lang='kor+eng', engine='auto'):
    """단일 이미지 OCR 수행. (text, status) 반환.

    status ∈ {'ok', 'partial', 'unavailable', 'failed'}
    """
    # 명시적 강제 모드
    if engine == 'easyocr':
        ok, _ = _probe_easyocr()
        if not ok:
            return '[OCR unavailable]', 'unavailable'
        return _run_easyocr(image_path, lang)
    if engine == 'tesseract':
        return _run_tesseract(image_path, lang)

    # auto: EasyOCR 우선
    ok, _ = _probe_easyocr()
    if ok:
        text, status = _run_easyocr(image_path, lang)
        if status in ('ok', 'partial'):
            return text, status
        # EasyOCR이 'failed'면 Tesseract로 재시도
    return _run_tesseract(image_path, lang)


def aggregate_status(statuses):
    """다중 이미지 OCR 결과를 모은 뒤 최종 상태 계산."""
    if not statuses:
        return 'unavailable'
    s = set(statuses)
    if s == {'ok'}:
        return 'ok'
    if s.issubset({'unavailable'}):
        return 'unavailable'
    if 'failed' in s and not any(x in s for x in ('ok', 'partial')):
        return 'failed'
    if 'partial' in s or 'ok' in s:
        return 'partial'
    return 'failed'
