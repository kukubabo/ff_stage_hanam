import os
import json
import time
import traceback
from datetime import datetime

# M-QA-5: 명시적 import. star import 제거.
import requests

from plugin import F, PluginModuleBase, jsonify, render_template

from .setup import P
from .model import ModelHanamPost, ModelJobResult
from . import scraper
from . import ocr as ocr_mod


LIST_URL_DEFAULT = 'https://www.hnart.or.kr/artcenter/selectBbsNttList.do?bbsNo=44&key=208'
DETAIL_URL_TMPL_DEFAULT = (
    'https://www.hnart.or.kr/artcenter/selectBbsNttView.do'
    '?key=208&bbsNo=44&nttNo={nttNo}'
)


class ModuleBasic(PluginModuleBase):
    """하남문화재단 「스테이지 하남」 공지 자동 수집·OCR·알림."""

    def __init__(self, P):
        super(ModuleBasic, self).__init__(
            P, name='basic',
            first_menu='setting',
            scheduler_desc='스테이지 하남 공지 수집'
        )
        self.db_default = {
            'db_version': '1.0',
            # 자동 실행 설정
            f'{self.name}_auto_start': 'False',
            f'{self.name}_interval': '0 7 * * *',
            # DB 관리 설정
            f'{self.name}_db_delete_day': '180',
            f'{self.name}_db_auto_delete': 'False',
            # 알림 설정
            'use_notify_on_success': 'True',
            'use_notify_on_failure': 'False',
            # 스크래핑 설정
            'list_url': LIST_URL_DEFAULT,
            'detail_url_tmpl': DETAIL_URL_TMPL_DEFAULT,
            'fetch_pages': '1',
            'page_unit': '12',
            'request_timeout': '15',
            'request_delay_sec': '1.5',
            'user_agent': 'Mozilla/5.0 (compatible; flaskfarm/stage_hanam)',
            'title_prefix': '스테이지 하남',
            # OCR / 저장 설정
            'image_dir': '/home/kukubabo/git/flaskfarm/data/download/stage_hanam',
            # OCR 설정 — 기본 OFF. 포스터 이미지를 텔레그램에 그대로 전송하는 방식이 기본.
            'ocr_enable': 'False',
            'ocr_engine': 'auto',
            'ocr_lang': 'kor+eng',
            'ocr_snippet_len': '300',
            # 알림 — 포스터 이미지를 직접 sendPhoto로 전송 (권장)
            'notify_send_images': 'True',
            'notify_send_delay_sec': '1.0',
            # OCR 활성화 시 추가 발췌 옵션 (OCR off면 미사용)
            'notify_venue_keywords': '미사호수공원,미사역,계단광장,시계탑',
            'notify_excerpt_window': '240',
            'notify_max_excerpts': '4',
            # 마지막 목록 옵션 (UI 상태 유지용)
            f'{P.package_name}_item_last_list_option': '',
        }
        self.web_list_model = ModelJobResult

    # ── 메뉴 / AJAX ──────────────────────────────────────────────

    def process_menu(self, sub, req):
        # 페이지에 따라 web_list_model 스왑
        if sub == 'posts':
            self.web_list_model = ModelHanamPost
        elif sub == 'list':
            self.web_list_model = ModelJobResult

        arg = P.ModelSetting.to_dict()
        if sub == 'setting':
            arg['is_include'] = F.scheduler.is_include(self.get_scheduler_name())
            arg['is_running'] = F.scheduler.is_running(self.get_scheduler_name())
            ocr_engine_cur = (P.ModelSetting.get('ocr_engine') or 'auto').strip().lower()
            ok, reason = ocr_mod.probe(engine=ocr_engine_cur)
            arg['ocr_available'] = 'Y' if ok else 'N'
            arg['ocr_probe_reason'] = reason
        return render_template(
            f'{P.package_name}_{self.name}_{sub}.html', arg=arg)

    def process_command(self, command, arg1, arg2, arg3, req):
        ret = {'ret': 'success'}
        try:
            if command == 'manual_execute':
                self.scheduler_function()
                ret['msg'] = '수동 실행이 완료되었습니다.'
            elif command == 'reocr':
                nttNo = arg1
                if not nttNo:
                    ret['ret'] = 'failed'
                    ret['msg'] = 'nttNo 가 필요합니다.'
                else:
                    n = self._reocr_post(nttNo)
                    ret['msg'] = f'재OCR 완료: {n}장 처리'
            elif command == 'ocr_probe':
                cur_engine = (P.ModelSetting.get('ocr_engine') or 'auto').strip().lower()
                ok, reason = ocr_mod.probe(engine=cur_engine)
                ret['msg'] = f'OCR {"사용 가능" if ok else "불가"} ({cur_engine}) — {reason}'
            elif command == 'retry_notify':
                n_tried, n_sent = self._retry_failed_notifications()
                ret['msg'] = f'미알림 {n_tried}건 재시도, {n_sent}건 성공'
            elif command == 'reset_posts':
                count, err = ModelHanamPost.delete_all()
                if err is not None:
                    ret['ret'] = 'danger'
                    ret['msg'] = f'공지 추적 초기화 실패: {err}'
                else:
                    ret['msg'] = (
                        f'공지 추적 {count}건 삭제 완료. '
                        f'다음 실행부터 동일 nttNo 도 신규로 인식됩니다. '
                        f'(주의: 디스크의 포스터 이미지는 그대로 — 다음 다운로드 시 동일 파일명으로 덮어씁니다.)'
                    )
            else:
                ret['ret'] = 'failed'
                ret['msg'] = f'알 수 없는 명령: {command}'
        except Exception as e:
            P.logger.error(f'process_command Exception: {str(e)}')
            P.logger.error(traceback.format_exc())
            ret['ret'] = 'failed'
            ret['msg'] = f'예외 발생: {str(e)}'
        return jsonify(ret)

    # ── 스케줄러 본 로직 ────────────────────────────────────────────

    def scheduler_function(self):
        job_key = f'run_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        try:
            P.logger.info('======== Stage Hanam 수집 시작 ========')

            list_url = P.ModelSetting.get('list_url') or LIST_URL_DEFAULT
            detail_url_tmpl = (
                P.ModelSetting.get('detail_url_tmpl') or DETAIL_URL_TMPL_DEFAULT
            )
            fetch_pages = int(P.ModelSetting.get('fetch_pages') or '1')
            page_unit = int(P.ModelSetting.get('page_unit') or '12')
            timeout = int(P.ModelSetting.get('request_timeout') or '15')
            delay = float(P.ModelSetting.get('request_delay_sec') or '1.5')
            user_agent = (P.ModelSetting.get('user_agent') or
                          'Mozilla/5.0 (compatible; flaskfarm/stage_hanam)')
            title_prefix = P.ModelSetting.get('title_prefix') or '스테이지 하남'
            image_dir = (P.ModelSetting.get('image_dir') or
                         '/home/kukubabo/git/flaskfarm/data/download/stage_hanam')
            ocr_enable = P.ModelSetting.get_bool('ocr_enable')
            ocr_engine = (P.ModelSetting.get('ocr_engine') or 'auto').strip().lower()
            ocr_lang = P.ModelSetting.get('ocr_lang') or 'kor+eng'

            # 1) 리스트 페이지 순회 — nttNo + 제목 매칭
            new_posts = []
            total_checked = 0
            total_matched = 0
            early_exit = False

            for page_index in range(1, fetch_pages + 1):
                if early_exit:
                    break
                url = scraper.build_list_url(list_url, page_unit, page_index)
                P.logger.info(f'리스트 페이지 조회: {url}')
                try:
                    html_text = scraper.http_get(
                        url, timeout=timeout, user_agent=user_agent)
                except Exception as e:
                    P.logger.error(f'리스트 페이지 실패 (page={page_index}): {e}')
                    raise

                rows = scraper.parse_list_page(html_text)
                P.logger.info(f'페이지 {page_index}: {len(rows)} 행 파싱')

                for row in rows:
                    total_checked += 1
                    if not scraper.is_match(row['raw_title'], title_prefix):
                        continue
                    total_matched += 1
                    if ModelHanamPost.exists(row['nttNo']):
                        # 게시판은 내림차순 → 이후 글은 모두 기존
                        P.logger.info(
                            f'기존 nttNo={row["nttNo"]} 발견, 조기 종료')
                        early_exit = True
                        break
                    new_posts.append(row)

                if not early_exit and delay > 0 and page_index < fetch_pages:
                    time.sleep(delay)

            P.logger.info(
                f'스캔 완료 — 조회 {total_checked}건, 매칭 {total_matched}건, '
                f'신규 {len(new_posts)}건'
            )

            # 2) 신규 글 상세 처리
            ocr_ok, ocr_reason = ocr_mod.probe(engine=ocr_engine)
            if ocr_enable and not ocr_ok:
                P.logger.warning(f'OCR 사용 불가 — {ocr_reason}')
            elif ocr_enable:
                P.logger.info(f'OCR 엔진: {ocr_reason}')

            notified_count = 0
            created_count = 0
            errored = []
            created_titles = []
            new_nttNo_list = []

            for row in new_posts:
                nttNo = row['nttNo']
                detail_url = scraper.build_detail_url(detail_url_tmpl, nttNo)
                try:
                    if delay > 0:
                        time.sleep(delay)
                    detail_html = scraper.http_get(
                        detail_url, timeout=timeout, user_agent=user_agent)
                    detail = scraper.parse_detail_page(detail_html, detail_url)
                except Exception as e:
                    P.logger.error(f'상세 조회 실패 nttNo={nttNo}: {e}')
                    errored.append({'nttNo': nttNo, 'error': str(e)})
                    continue

                # 리스트 페이지 제목이 권위 있는 출처 (prefix 매칭도 이걸로 수행).
                # 하남문화재단 상세 페이지는 h3/h4 제목 노드가 없어서 detail.title은
                # 보통 비어 있거나 generic page title이라 폴백으로만 사용.
                title = row['raw_title'] or detail.get('title') or ''
                title_normalized = scraper.normalize_title(title)
                posted_date = detail.get('posted_date') or row.get('posted_date') or ''

                # 이미지 다운로드
                post_dir = os.path.join(image_dir, str(nttNo))
                local_paths = []
                hashes = []
                for img_url in detail.get('image_urls') or []:
                    try:
                        if delay > 0:
                            time.sleep(min(delay, 1.0))
                        local_path, sha = scraper.download_image(
                            img_url, post_dir, timeout=timeout, user_agent=user_agent)
                        local_paths.append(local_path)
                        hashes.append(sha)
                    except Exception as e:
                        P.logger.error(
                            f'이미지 다운로드 실패 nttNo={nttNo} url={img_url}: {e}')

                # OCR — 캐시(동일 해시) 우선, 없으면 수행
                ocr_texts = []
                ocr_statuses = []
                if not ocr_enable:
                    ocr_text_combined = '[OCR disabled]'
                    ocr_status_final = 'unavailable'
                elif not ocr_ok:
                    ocr_text_combined = '[OCR unavailable]'
                    ocr_status_final = 'unavailable'
                else:
                    for local_path, sha in zip(local_paths, hashes):
                        cached = ModelHanamPost.find_ocr_by_hash(sha)
                        if cached:
                            ocr_texts.append(cached)
                            ocr_statuses.append('ok')
                            continue
                        text, st = ocr_mod.run_ocr_on_file(
                            local_path, lang=ocr_lang, engine=ocr_engine)
                        ocr_texts.append(text)
                        ocr_statuses.append(st)
                    ocr_text_combined = ('\n\n----\n\n'.join(
                        t for t in ocr_texts if t) if ocr_texts else '')
                    ocr_status_final = ocr_mod.aggregate_status(ocr_statuses)

                # DB 저장
                saved = ModelHanamPost.create(
                    nttNo=nttNo,
                    title=title,
                    title_normalized=title_normalized,
                    author=detail.get('author') or '',
                    posted_date=posted_date,
                    view_count=detail.get('view_count') or 0,
                    detail_url=detail_url,
                    image_urls=json.dumps(detail.get('image_urls') or [],
                                          ensure_ascii=False),
                    local_image_paths=json.dumps(local_paths,
                                                 ensure_ascii=False),
                    image_hashes=json.dumps(hashes, ensure_ascii=False),
                    ocr_text=ocr_text_combined,
                    ocr_status=ocr_status_final,
                    raw_html_snippet=detail.get('raw_html_snippet') or '',
                )
                if saved is None:
                    errored.append({'nttNo': nttNo, 'error': 'DB 저장 실패'})
                    continue

                created_count += 1
                created_titles.append(title)
                new_nttNo_list.append(nttNo)

                # 알림 — 포스터 이미지를 직접 전송 (기본) 또는 텍스트만
                if P.ModelSetting.get_bool('use_notify_on_success'):
                    sent_n = self._notify_post(
                        title=title,
                        posted_date=posted_date,
                        view_count=detail.get('view_count') or 0,
                        image_urls=detail.get('image_urls') or [],
                        image_paths=local_paths,
                        ocr_text=ocr_text_combined,
                        ocr_status=ocr_status_final,
                        detail_url=detail_url,
                    )
                    if sent_n > 0:
                        ModelHanamPost.mark_notified(nttNo)
                        notified_count += 1

            # 3) 실행 이력 기록
            if errored and created_count > 0:
                status = 'partial'
            elif errored and created_count == 0 and len(new_posts) > 0:
                status = 'failure'
            else:
                status = 'success'

            if created_count == 0:
                summary = f'신규 공지 없음 (조회 {total_checked}건, 매칭 {total_matched}건)'
            else:
                summary = (
                    f'신규 {created_count}건 (조회 {total_checked}건, 알림 {notified_count}건)'
                )

            result_data = json.dumps({
                'total_checked': total_checked,
                'total_matched': total_matched,
                'new_found': len(new_posts),
                'created': created_count,
                'notified': notified_count,
                'errors': errored,
                'titles': created_titles,
                'nttNo_list': new_nttNo_list,
                'ocr_available': ocr_ok,
                'ocr_probe_reason': ocr_reason,
            }, ensure_ascii=False)

            ModelJobResult.create(
                job_key=job_key,
                status=status,
                message=summary,
                new_posts_count=created_count,
                total_checked=total_checked,
                result_data=result_data,
            )

            if status == 'failure' and P.ModelSetting.get_bool('use_notify_on_failure'):
                self._send_notify(f'[{P.package_name}] 실패: {summary}')

            P.logger.info('======== Stage Hanam 수집 종료 ========')

        except Exception as e:
            P.logger.error(f'scheduler_function Exception: {str(e)}')
            P.logger.error(traceback.format_exc())
            try:
                ModelJobResult.create(
                    job_key=job_key,
                    status='failure',
                    message=f'실행 중 예외 발생: {str(e)}',
                    new_posts_count=0,
                    total_checked=0,
                    result_data=json.dumps(
                        {'exception': str(e)}, ensure_ascii=False),
                )
            except Exception:
                pass
            if P.ModelSetting.get_bool('use_notify_on_failure'):
                try:
                    self._send_notify(
                        f'[{P.package_name}] 스케줄러 예외: {str(e)}')
                except Exception:
                    pass

    # ── 헬퍼 ────────────────────────────────────────────────────

    def _reocr_post(self, nttNo):
        """저장된 로컬 이미지로 재OCR 수행."""
        ocr_enable = P.ModelSetting.get_bool('ocr_enable')
        ocr_engine = (P.ModelSetting.get('ocr_engine') or 'auto').strip().lower()
        ocr_lang = P.ModelSetting.get('ocr_lang') or 'kor+eng'
        ok, _ = ocr_mod.probe(engine=ocr_engine)
        if not ocr_enable or not ok:
            return 0

        post = ModelHanamPost.get_by_nttNo(nttNo)
        if post is None:
            return 0
        try:
            local_paths = json.loads(post.local_image_paths or '[]')
        except Exception:
            local_paths = []
        texts = []
        statuses = []
        for p in local_paths:
            if not p or not os.path.exists(p):
                continue
            text, st = ocr_mod.run_ocr_on_file(p, lang=ocr_lang, engine=ocr_engine)
            texts.append(text)
            statuses.append(st)
        if not texts:
            return 0
        combined = '\n\n----\n\n'.join(t for t in texts if t)
        final = ocr_mod.aggregate_status(statuses)
        ModelHanamPost.update_ocr(nttNo, combined, final)
        return len(texts)

    @staticmethod
    def _make_ocr_snippet(text, snippet_len):
        if not text:
            return '(OCR 텍스트 없음)'
        compact = ' '.join(text.split())
        if len(compact) > snippet_len:
            return compact[:snippet_len] + '…'
        return compact

    def _notify_post(self, title, posted_date, view_count, image_urls,
                     image_paths, ocr_text, ocr_status, detail_url):
        """공지 1건에 대한 알림 전송. 전송 성공 메시지 수 반환.

        전송 전략 (우선순위):
          1) 로컬 다운로드 파일이 있으면 Telegram API에 직접 멀티파트 업로드
             (sendPhoto + caption 으로 한 번에). Telegram이 hnart.or.kr URL을
             가져오면 anti-hotlink 등으로 'wrong type of the web page content'
             400 에러가 발생하므로 직접 업로드가 안정적.
          2) Telegram 설정이 없거나 직접 업로드 실패 시 ToolNotify 로 폴백
             — image_url 첨부는 같은 이유로 실패하므로 텍스트만 전송.
        """
        send_images = P.ModelSetting.get_bool('notify_send_images')
        delay = float(P.ModelSetting.get('notify_send_delay_sec') or '1.0')
        image_urls = list(image_urls or [])
        image_paths = list(image_paths or [])

        ocr_section = self._build_ocr_section(ocr_text, ocr_status)

        header_lines = [
            '[스테이지 하남] 신규 공지',
            f'제목: {title}',
            f'작성일: {posted_date or "-"} · 조회: {view_count}',
            f'포스터: {len(image_urls)}장',
            f'링크: {detail_url}',
        ]
        first_msg = '\n'.join(header_lines)
        if ocr_section:
            first_msg += '\n\n' + ocr_section

        # 이미지 전송 비활성 또는 이미지 없음 → 텍스트만 1건 전송
        if not send_images or not image_paths:
            if not send_images:
                return 1 if self._send_one(first_msg) else 0
            # 이미지 다운로드 실패한 경우 — 텍스트만이라도 전송
            P.logger.warning('포스터 로컬 파일이 없어 텍스트 전용 알림으로 폴백')
            return 1 if self._send_one(first_msg) else 0

        # Telegram 사용 여부 — 직접 업로드 가능한지 결정
        tg_token = (F.SystemModelSetting.get('notify_telegram_token') or '').strip()
        tg_chat = (F.SystemModelSetting.get('notify_telegram_chat_id') or '').strip()
        tg_enabled = bool(tg_token and tg_chat and F.SystemModelSetting.get_bool('notify_telegram_use'))

        sent = 0
        total = len(image_paths)
        for i, path in enumerate(image_paths):
            if i == 0:
                caption = first_msg
            else:
                caption = f'(↑포스터 {i + 1}/{total})'

            ok = False
            if tg_enabled and os.path.exists(path):
                ok, reason = self._send_telegram_photo_file(path, caption=caption)
                if not ok:
                    P.logger.warning(f'Telegram 직접 업로드 실패 ({reason}) — ToolNotify 폴백')

            if not ok:
                # ToolNotify 폴백 — 텍스트만 (image_url 경로는 이미 검증된 실패 케이스)
                if self._send_one(caption):
                    ok = True

            if ok:
                sent += 1

            if delay > 0 and i < total - 1:
                time.sleep(delay)
        return sent

    def _retry_failed_notifications(self):
        """notified=False 인 행을 모두 재알림 시도. (시도건수, 성공건수) 반환."""
        rows = []
        try:
            with F.app.app_context():
                pending = F.db.session.query(ModelHanamPost).filter(
                    ModelHanamPost.notified == False  # noqa: E712
                ).order_by(ModelHanamPost.first_seen_at).all()
                for p in pending:
                    try:
                        rows.append({
                            'nttNo': p.nttNo,
                            'title': p.title,
                            'posted_date': p.posted_date,
                            'view_count': p.view_count or 0,
                            'image_urls': json.loads(p.image_urls or '[]'),
                            'image_paths': json.loads(p.local_image_paths or '[]'),
                            'ocr_text': p.ocr_text or '',
                            'ocr_status': p.ocr_status or 'unavailable',
                            'detail_url': p.detail_url,
                        })
                    except Exception as e:
                        P.logger.error(f'retry_notify row 변환 실패 nttNo={p.nttNo}: {e}')
        except Exception as e:
            P.logger.error(f'_retry_failed_notifications query Exception: {e}')

        delay = float(P.ModelSetting.get('notify_send_delay_sec') or '1.0')
        n_sent = 0
        for r in rows:
            n = self._notify_post(**r)
            if n > 0:
                ModelHanamPost.mark_notified(r['nttNo'])
                n_sent += 1
            # 글 사이에도 약간의 딜레이 (rate limit)
            if delay > 0:
                time.sleep(delay)
        return len(rows), n_sent

    # Telegram sendPhoto 한도: 가로+세로 합 ≤ 10000, 비율 ≤ 20:1, 파일 ≤ 10MB.
    # sendDocument: 차원 제한 없음, 파일 ≤ 50MB.
    _TG_PHOTO_SUM_LIMIT = 9500          # 여유분 두고 9500으로 설정
    _TG_PHOTO_RATIO_LIMIT = 19          # 비율 한도 (20:1 미만)
    _TG_PHOTO_BYTES_LIMIT = 10 * 1024 * 1024  # 10MB

    def _send_telegram_photo_file(self, local_path, caption=''):
        """Telegram에 포스터 전송. (ok, reason) 반환.

        전송 우선순위:
          1) 차원이 한도 내면 sendPhoto 그대로
          2) 한도 초과면 Pillow로 한도에 맞게 리사이즈 후 sendPhoto
          3) 그래도 실패 (PHOTO_INVALID_DIMENSIONS 등) 시 sendDocument 폴백
        sendDocument는 차원 제한 없음. 채팅창에 파일 첨부 형태로 표시되지만
        클릭 시 동일한 이미지 미리보기가 열림.
        """
        token = (F.SystemModelSetting.get('notify_telegram_token') or '').strip()
        chat_id = (F.SystemModelSetting.get('notify_telegram_chat_id') or '').strip()
        if not token or not chat_id:
            return False, 'telegram not configured'

        # 1) Photo로 보낼 수 있는지 검사 + 필요 시 리사이즈
        send_path, is_tmp = self._prepare_for_sendphoto(local_path)
        try:
            ok, reason = self._tg_api_upload(
                token, chat_id, 'sendPhoto', send_path,
                file_field='photo', caption=caption)
            if ok:
                return True, 'ok'
            # 2) PHOTO_INVALID_DIMENSIONS 같은 케이스 → sendDocument 폴백
            if 'PHOTO_INVALID' in reason or 'DIMENSIONS' in reason or '400' in reason:
                P.logger.info(f'sendPhoto 실패 — sendDocument로 폴백 시도: {reason}')
                ok2, reason2 = self._tg_api_upload(
                    token, chat_id, 'sendDocument', local_path,
                    file_field='document', caption=caption)
                if ok2:
                    return True, 'ok (sent as document)'
                return False, f'photo:{reason} | document:{reason2}'
            return False, reason
        finally:
            if is_tmp:
                try:
                    os.unlink(send_path)
                except Exception:
                    pass

    @staticmethod
    def _tg_api_upload(token, chat_id, method, local_path, file_field, caption=''):
        """Telegram Bot API 멀티파트 업로드 공통 헬퍼. (ok, reason)."""
        api_url = f'https://api.telegram.org/bot{token}/{method}'
        try:
            with open(local_path, 'rb') as f:
                files = {
                    file_field: (
                        os.path.basename(local_path), f, 'image/jpeg'
                    ),
                }
                data = {'chat_id': chat_id}
                if caption:
                    data['caption'] = caption[:1024]
                r = requests.post(api_url, data=data, files=files, timeout=60)
            try:
                resp = r.json()
            except Exception:
                resp = {}
            if r.status_code != 200 or not resp.get('ok'):
                return False, f'http={r.status_code} resp={resp}'
            return True, 'ok'
        except Exception as e:
            return False, str(e)

    @classmethod
    def _prepare_for_sendphoto(cls, local_path):
        """sendPhoto 한도 검사 + 초과 시 임시 리사이즈 JPEG 생성.

        Returns: (send_path, is_temp). is_temp=True 시 호출자가 정리해야 함.
        Pillow 미설치·읽기 실패 시 원본 그대로 반환 — sendDocument 폴백이 받아냄.
        """
        try:
            from PIL import Image
        except Exception:
            return local_path, False
        try:
            file_size = os.path.getsize(local_path)
            with Image.open(local_path) as img:
                w, h = img.size
                ratio = max(w, h) / max(1, min(w, h))
                within_dims = (w + h) <= cls._TG_PHOTO_SUM_LIMIT and ratio <= cls._TG_PHOTO_RATIO_LIMIT
                within_size = file_size <= cls._TG_PHOTO_BYTES_LIMIT
                if within_dims and within_size:
                    return local_path, False
                # 비율 한도 초과면 photo로는 불가 — 원본 반환해 sendDocument 폴백 유도.
                if ratio > cls._TG_PHOTO_RATIO_LIMIT:
                    P.logger.info(
                        f'이미지 비율 {ratio:.1f}:1 — sendPhoto 부적합, sendDocument로 진행')
                    return local_path, False
                # 합산·파일크기 한도 — 리사이즈.
                scale = min(1.0, cls._TG_PHOTO_SUM_LIMIT / (w + h))
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                P.logger.info(
                    f'리사이즈 적용: ({w}x{h}, {file_size // 1024}KB) '
                    f'→ ({new_w}x{new_h}) for Telegram sendPhoto 한도')
                resized = img.convert('RGB') if img.mode not in ('RGB', 'L') else img.copy()
                resized.thumbnail((new_w, new_h), Image.LANCZOS)
                tmp_path = local_path + '.tg.jpg'
                resized.save(tmp_path, 'JPEG', quality=85, optimize=True)
                return tmp_path, True
        except Exception as e:
            P.logger.warning(f'이미지 전처리 실패 — 원본 사용: {e}')
            return local_path, False

    @staticmethod
    def _build_ocr_section(ocr_text, ocr_status):
        """OCR 결과가 의미 있을 때만 공연장 키워드 발췌 섹션 생성."""
        if not ocr_text or ocr_status not in ('ok', 'partial'):
            return ''
        venue_kw_str = (P.ModelSetting.get('notify_venue_keywords') or '').strip()
        if not venue_kw_str:
            return ''
        venues = [v.strip() for v in venue_kw_str.split(',') if v.strip()]
        if not venues:
            return ''
        window = int(P.ModelSetting.get('notify_excerpt_window') or '240')
        max_excerpts = int(P.ModelSetting.get('notify_max_excerpts') or '4')
        excerpts = scraper.find_venue_excerpts(
            ocr_text, venues, window=window, max_excerpts=max_excerpts)
        if not excerpts:
            return ''
        lines = ['─ 공연 정보 (OCR) ─']
        for ex in excerpts:
            lines.append(f'▶ {" / ".join(ex["venues"])}')
            lines.append(ex['excerpt'])
            lines.append('')
        return '\n'.join(lines).rstrip()

    def _send_one(self, message, image_url=None):
        """1건 알림 전송 — 텍스트 또는 텍스트+이미지."""
        try:
            from tool import ToolNotify
            ToolNotify.send_message(
                message,
                message_id=f'bot_{P.package_name}',
                image_url=image_url,
            )
            return True
        except Exception as e:
            P.logger.error(f'_send_one Exception: {str(e)}')
            return False

    def _send_notify(self, message):
        """간단 텍스트 전용 알림 (실패 알림 등)."""
        return self._send_one(message)
