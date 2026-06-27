"""stage_hanam 플러그인 런타임 의존성 보장.

베이스 Docker 이미지는 프레임워크 의존성만 담는 미니멀 설계라, 플러그인 전용
패키지는 import 전에 런타임 설치한다. FLASKFARM_PIP_TARGET 가 설정돼 있으면
(build_pip_install) /data/deps 에 설치돼 컨테이너 재생성에도 살아남는다.

lxml 은 최상위 import 라 반드시 필요. OCR 계열(easyocr/pytesseract/Pillow)은
함수 내부 lazy import 라 OCR 기능을 쓸 때만 필요하므로 여기서 강제 설치하지 않는다.
"""
import importlib
import subprocess
import sys


def _ensure(packages):
    for import_name, pip_name in packages:
        try:
            __import__(import_name)
        except ImportError:
            try:
                from pip_target import build_pip_install
                cmd = build_pip_install(pip_name)
            except Exception:
                cmd = [sys.executable, "-m", "pip", "install", pip_name]
            subprocess.run(cmd, check=False)
            importlib.invalidate_caches()


_ensure((
    ("lxml", "lxml"),
))
