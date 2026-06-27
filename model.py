# M-QA-5: 명시적 import. setup 에서 노출되던 star import 제거.
import json
import traceback
from datetime import datetime

from sqlalchemy import desc

from plugin import F, ModelBase, db

from .setup import P


class ModelHanamPost(ModelBase):
    """하남문화재단 「스테이지 하남」 공지 카탈로그. 1행 = 1 nttNo."""
    P = P
    __tablename__ = f'{P.package_name}_post'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    nttNo = db.Column(db.String(20), unique=True, nullable=False, index=True)
    title = db.Column(db.String(500))
    title_normalized = db.Column(db.String(500))
    author = db.Column(db.String(200))
    posted_date = db.Column(db.String(10))
    view_count = db.Column(db.Integer, default=0)
    detail_url = db.Column(db.String(500))
    image_urls = db.Column(db.Text)
    local_image_paths = db.Column(db.Text)
    image_hashes = db.Column(db.Text)
    ocr_text = db.Column(db.Text)
    ocr_status = db.Column(db.String(30))
    first_seen_at = db.Column(db.DateTime)
    raw_html_snippet = db.Column(db.Text)
    notified = db.Column(db.Boolean, default=False)

    def __init__(self):
        self.created_time = datetime.now()
        self.first_seen_at = self.created_time
        self.notified = False
        self.view_count = 0
        self.ocr_status = 'pending'

    @classmethod
    def exists(cls, nttNo):
        try:
            with F.app.app_context():
                return F.db.session.query(cls).filter(
                    cls.nttNo == str(nttNo)).first() is not None
        except Exception as e:
            P.logger.error(f'ModelHanamPost.exists Exception: {str(e)}')
            return False

    @classmethod
    def get_by_nttNo(cls, nttNo):
        try:
            with F.app.app_context():
                return F.db.session.query(cls).filter(
                    cls.nttNo == str(nttNo)).first()
        except Exception as e:
            P.logger.error(f'ModelHanamPost.get_by_nttNo Exception: {str(e)}')
            return None

    @classmethod
    def delete_all(cls):
        """추적 테이블 전체 비우기. (count, error) 반환.

        같은 nttNo 공지를 다시 감지·재처리하고 싶을 때 사용.
        디스크의 포스터 이미지(`data/download/stage_hanam/<nttNo>/`)는 그대로 남음.
        """
        try:
            with F.app.app_context():
                count = F.db.session.query(cls).delete()
                F.db.session.commit()
                return count, None
        except Exception as e:
            P.logger.error(f'ModelHanamPost.delete_all Exception: {str(e)}')
            try:
                F.db.session.rollback()
            except Exception:
                pass
            return 0, str(e)

    @classmethod
    def create(cls, nttNo, title, title_normalized, author, posted_date,
               view_count, detail_url, image_urls, local_image_paths,
               image_hashes, ocr_text, ocr_status, raw_html_snippet):
        try:
            with F.app.app_context():
                item = cls()
                item.nttNo = str(nttNo)
                item.title = title
                item.title_normalized = title_normalized
                item.author = author
                item.posted_date = posted_date
                item.view_count = view_count or 0
                item.detail_url = detail_url
                item.image_urls = image_urls
                item.local_image_paths = local_image_paths
                item.image_hashes = image_hashes
                item.ocr_text = ocr_text
                item.ocr_status = ocr_status
                item.raw_html_snippet = raw_html_snippet
                item.notified = False
                item.save()
                return item
        except Exception as e:
            P.logger.error(f'ModelHanamPost.create Exception: {str(e)}')
            P.logger.error(traceback.format_exc())
            try:
                F.db.session.rollback()
            except Exception:
                pass
            return None

    @classmethod
    def mark_notified(cls, nttNo):
        try:
            with F.app.app_context():
                item = F.db.session.query(cls).filter(
                    cls.nttNo == str(nttNo)).first()
                if item is not None:
                    item.notified = True
                    F.db.session.commit()
        except Exception as e:
            P.logger.error(f'ModelHanamPost.mark_notified Exception: {str(e)}')

    @classmethod
    def update_ocr(cls, nttNo, ocr_text, ocr_status):
        try:
            with F.app.app_context():
                item = F.db.session.query(cls).filter(
                    cls.nttNo == str(nttNo)).first()
                if item is not None:
                    item.ocr_text = ocr_text
                    item.ocr_status = ocr_status
                    F.db.session.commit()
                    return True
                return False
        except Exception as e:
            P.logger.error(f'ModelHanamPost.update_ocr Exception: {str(e)}')
            return False

    @classmethod
    def find_ocr_by_hash(cls, image_hash):
        """동일 sha256 해시를 가진 행 중 ocr_status='ok'인 첫 행의 ocr_text 반환."""
        try:
            with F.app.app_context():
                rows = F.db.session.query(cls).filter(
                    cls.ocr_status == 'ok').all()
                for row in rows:
                    try:
                        hashes = json.loads(row.image_hashes or '[]')
                        if image_hash in hashes:
                            return row.ocr_text
                    except Exception:
                        continue
        except Exception as e:
            P.logger.error(f'ModelHanamPost.find_ocr_by_hash Exception: {str(e)}')
        return None

    @classmethod
    def get_list(cls):
        return super().get_list(by_dict=True)

    @classmethod
    def make_query(cls, req, order='desc', search='', option1='all', option2='all'):
        with F.app.app_context():
            query = cls.make_query_search(
                F.db.session.query(cls), search, cls.title)

            if option1 != 'all':
                query = query.filter(cls.ocr_status == option1)

            if order == 'desc':
                query = query.order_by(desc(cls.first_seen_at))
            else:
                query = query.order_by(cls.first_seen_at)

            return query


class ModelJobResult(ModelBase):
    """배치 수행 결과 기록 모델."""
    P = P
    __tablename__ = f'{P.package_name}'
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    job_key = db.Column(db.String(100))
    status = db.Column(db.String(50))
    new_posts_count = db.Column(db.Integer, default=0)
    total_checked = db.Column(db.Integer, default=0)
    message = db.Column(db.String(500))
    result_data = db.Column(db.Text)

    def __init__(self):
        self.created_time = datetime.now()
        self.status = 'pending'
        self.new_posts_count = 0
        self.total_checked = 0

    @classmethod
    def create(cls, job_key, status, message,
               new_posts_count=0, total_checked=0, result_data=None):
        try:
            with F.app.app_context():
                item = cls()
                item.job_key = job_key
                item.status = status
                item.message = message
                item.new_posts_count = new_posts_count or 0
                item.total_checked = total_checked or 0
                item.result_data = result_data
                item.save()
                return item
        except Exception as e:
            cls.P.logger.error(f'ModelJobResult.create Exception:{str(e)}')
            cls.P.logger.error(traceback.format_exc())
            return None

    @classmethod
    def get_list(cls):
        return super().get_list(by_dict=True)

    @classmethod
    def make_query(cls, req, order='desc', search='', option1='all', option2='all'):
        with F.app.app_context():
            query = cls.make_query_search(
                F.db.session.query(cls), search, cls.message)

            if option1 != 'all':
                query = query.filter(cls.status == option1)

            if order == 'desc':
                query = query.order_by(desc(cls.created_time))
            else:
                query = query.order_by(cls.created_time)

            return query
