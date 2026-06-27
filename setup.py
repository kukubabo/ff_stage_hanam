# M-QA-5: 명시적 import — 어떤 심볼이 노출되는지 한눈에. star import 제거.
import traceback

from plugin import create_plugin_instance

setting = {
    'filepath': __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': None,
    'menu': {
        'uri': __package__,
        'name': '스테이지 하남',
        'list': [
            {
                'uri': 'basic/setting',
                'name': '설정'
            },
            {
                'uri': 'basic/posts',
                'name': '공지 목록'
            },
            {
                'uri': 'basic/list',
                'name': '실행 이력'
            },
            {
                'uri': 'log',
                'name': '로그'
            }
        ]
    },
    'setting_menu': None,
    'default_route': 'normal'
}

P = create_plugin_instance(setting)

try:
    from .mod_basic import ModuleBasic
    P.set_module_list([ModuleBasic])
except Exception as e:
    P.logger.error(f'Exception:{str(e)}')
    P.logger.error(traceback.format_exc())
