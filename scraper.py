"""하남문화재단 게시판 스크래퍼.

리스트 페이지 / 상세 페이지 / 이미지 다운로드 / 타이틀 정규화 헬퍼.
모든 함수는 mod_basic.ModuleBasic에서 호출됨.
"""

import os
import re
import json
import time
import hashlib
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from lxml import html as lxml_html


BRACKETS = '「」〈〉『』【】[]<>《》'
META_RE = re.compile(
    r'작성자\s*[:：]\s*(.+?)\s+조회\s*[:：]\s*(\d+)\s+작성일\s*[:：]\s*(\d{4}-\d{2}-\d{2})'
)
NTTNO_RE = re.compile(r'nttNo=(\d+)')
SITE_BASE = 'https://www.hnart.or.kr'


def normalize_title(s):
    """NFKC 정규화 + 괄호 종류 통일(공백화) + 공백 정리."""
    if not s:
        return ''
    s = unicodedata.normalize('NFKC', s)
    s = ''.join(' ' if ch in BRACKETS else ch for ch in s)
    return ' '.join(s.split()).strip()


def is_match(title, prefix_setting):
    return normalize_title(title).startswith(normalize_title(prefix_setting))


def http_get(url, timeout=15, user_agent=None, binary=False, retries=2):
    """GET with retry + exponential backoff. binary=True 시 bytes, 아니면 text."""
    headers = {
        'User-Agent': user_agent or 'Mozilla/5.0 (compatible; flaskfarm/stage_hanam)',
        'Accept-Language': 'ko,en;q=0.8',
    }
    last_exc = None
    delays = [0, 1.0, 3.0]
    for attempt in range(retries + 1):
        if delays[attempt]:
            time.sleep(delays[attempt])
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if binary:
                return resp.content
            resp.encoding = resp.encoding or 'utf-8'
            if resp.encoding.lower() not in ('utf-8', 'utf8'):
                resp.encoding = 'utf-8'
            return resp.text
        except Exception as e:
            last_exc = e
    raise last_exc


def build_list_url(base_url, page_unit, page_index):
    sep = '&' if '?' in base_url else '?'
    return f'{base_url}{sep}pageUnit={page_unit}&pageIndex={page_index}'


def parse_list_page(html_text):
    """리스트 페이지에서 (nttNo, raw_title, posted_date) 튜플 리스트 반환.

    상세 링크 href에서 nttNo를 추출한다. 게시판이 내림차순이므로 결과도 그 순서.
    """
    doc = lxml_html.fromstring(html_text)
    rows = []
    seen = set()
    for a in doc.xpath('//a[contains(@href,"selectBbsNttView.do")]'):
        href = a.get('href') or ''
        m = NTTNO_RE.search(href)
        if not m:
            continue
        nttNo = m.group(1)
        if nttNo in seen:
            continue
        seen.add(nttNo)

        # 제목 — a 태그의 텍스트(공백 정리)
        raw_title = ' '.join((a.text_content() or '').split()).strip()
        if not raw_title:
            continue

        # 작성일 — 주변 li 컨테이너의 텍스트에서 YYYY-MM-DD 추출
        posted_date = ''
        container = a
        for _ in range(4):
            container = container.getparent()
            if container is None:
                break
            txt = container.text_content() or ''
            dm = re.search(r'(\d{4}-\d{2}-\d{2})', txt)
            if dm:
                posted_date = dm.group(1)
                break

        rows.append({
            'nttNo': nttNo,
            'raw_title': raw_title,
            'posted_date': posted_date,
            'detail_href': href,
        })
    return rows


def parse_detail_page(html_text, detail_url):
    """상세 페이지 파싱. dict 반환:
        {title, author, view_count, posted_date, image_urls, raw_html_snippet}
    title은 원본 그대로. image_urls는 절대 URL.
    """
    doc = lxml_html.fromstring(html_text)
    full_text = doc.text_content() or ''
    full_text_compact = ' '.join(full_text.split())

    author = ''
    view_count = 0
    posted_date = ''
    m = META_RE.search(full_text_compact)
    if m:
        author = m.group(1).strip()
        try:
            view_count = int(m.group(2))
        except Exception:
            view_count = 0
        posted_date = m.group(3)

    # 제목 — h3/h4/strong 우선, 없으면 페이지 타이틀
    title = ''
    for xp in (
        '//*[contains(@class,"board_view")]//h3',
        '//*[contains(@class,"board_view")]//h4',
        '//*[contains(@class,"view_title")]',
        '//*[contains(@class,"bbs_title")]',
        '//h3', '//h4',
    ):
        nodes = doc.xpath(xp)
        if nodes:
            cand = ' '.join((nodes[0].text_content() or '').split()).strip()
            if cand:
                title = cand
                break
    if not title:
        # 마지막 폴백 — 페이지 <title> 태그. '축제게시판 상세보기'처럼
        # 게시판 공통 페이지 타이틀은 본문 제목으로 부적합하므로 걸러냄.
        title_nodes = doc.xpath('//title')
        if title_nodes:
            cand = ' '.join((title_nodes[0].text_content() or '').split()).strip()
            if cand and '상세보기' not in cand and '게시판' not in cand:
                title = cand

    # 본문 포스터 이미지 — /DATA/bbs/44/ 경로이면서 썸네일(/thumb/) 제외
    image_urls = []
    for img in doc.xpath('//img[contains(@src,"/DATA/bbs/44/")]'):
        src = img.get('src') or ''
        if not src or '/thumb/' in src:
            continue
        absolute = urljoin(SITE_BASE, src)
        if absolute not in image_urls:
            image_urls.append(absolute)

    # 원본 HTML 일부 보관 (디버깅용, 앞 2KB)
    raw_html_snippet = html_text[:2048] if html_text else ''

    return {
        'title': title,
        'author': author,
        'view_count': view_count,
        'posted_date': posted_date,
        'image_urls': image_urls,
        'raw_html_snippet': raw_html_snippet,
        'detail_url': detail_url,
    }


def safe_filename(url):
    """URL에서 안전한 파일명 추출. 기본은 basename."""
    path = urlparse(url).path
    base = os.path.basename(path) or 'image.jpg'
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)
    return base


def download_image(url, dest_dir, timeout=15, user_agent=None):
    """이미지를 dest_dir에 저장하고 (local_path, sha256) 반환."""
    os.makedirs(dest_dir, exist_ok=True)
    content = http_get(url, timeout=timeout, user_agent=user_agent, binary=True)
    sha = hashlib.sha256(content).hexdigest()
    fname = safe_filename(url)
    local_path = os.path.join(dest_dir, fname)
    with open(local_path, 'wb') as f:
        f.write(content)
    return local_path, sha


def build_detail_url(detail_url_tmpl, nttNo):
    return detail_url_tmpl.format(nttNo=nttNo)


def find_venue_excerpts(ocr_text, venue_keywords, window=240, max_excerpts=4):
    """OCR 텍스트에서 공연장 키워드 주변 문맥만 골라 발췌.

    Args:
        ocr_text: 다중 포스터 OCR 합본 문자열.
        venue_keywords: ['미사호수공원', '미사역', ...] — 검색 키워드 리스트.
        window: 키워드 위치 기준 좌·우 합산 글자 수.
        max_excerpts: 반환할 최대 발췌 개수.

    Returns:
        [{'venues': ['미사호수공원', ...], 'excerpt': '...'}, ...]
        매칭 없을 시 빈 리스트. 인접한 매칭은 겹치는 윈도우끼리 자동 병합.
    """
    if not ocr_text or not venue_keywords:
        return []

    keywords = [vk.strip() for vk in venue_keywords if vk and vk.strip()]
    if not keywords:
        return []

    # 모든 키워드 매칭 위치 수집
    raw_matches = []
    for vk in keywords:
        for m in re.finditer(re.escape(vk), ocr_text):
            raw_matches.append((m.start(), vk))
    if not raw_matches:
        return []
    raw_matches.sort()

    # 겹치는 윈도우 병합
    half = max(window // 2, 30)
    merged = []
    for pos, vk in raw_matches:
        start = max(0, pos - half)
        end = min(len(ocr_text), pos + half)
        if merged and start <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], end)
            if vk not in merged[-1]['venues']:
                merged[-1]['venues'].append(vk)
        else:
            merged.append({'start': start, 'end': end, 'venues': [vk]})

    result = []
    for ex in merged[:max_excerpts]:
        chunk = ocr_text[ex['start']:ex['end']]
        # 빈 줄 제거 + 행 내부 공백 정리, 줄바꿈은 유지
        lines = [' '.join(l.split()) for l in chunk.splitlines()]
        cleaned = '\n'.join(l for l in lines if l)
        if ex['start'] > 0:
            cleaned = '… ' + cleaned
        if ex['end'] < len(ocr_text):
            cleaned = cleaned + ' …'
        result.append({'venues': ex['venues'], 'excerpt': cleaned})
    return result
