import sys
import typing as t

from deploy.utils import DEPLOY_TEMPLATE, poor_yaml_read, poor_yaml_write

"""
通过命令行设置 config/deploy.yaml，用法：
python -m deploy.set PythonExecutable=/usr/bin/python3
"""


def get_args() -> t.Dict[str, str]:
    args = {}
    for arg in sys.argv[1:]:
        if '=' not in arg:
            continue
        key, value = arg.split('=', maxsplit=1)
        key, value = key.strip(), value.strip()
        args[key] = value
    return args


def config_set(output='./config/deploy.yaml'):
    data = poor_yaml_read(DEPLOY_TEMPLATE)
    data.update(poor_yaml_read(output))
    updates = get_args()

    for key, value in updates.items():
        if key in data:
            print(f'{key} set')
            data[key] = value
        else:
            print(f'{key} not exist')

    poor_yaml_write(
        data,
        file=output,
        preserve_existing=True,
        keys=set(updates),
    )


if __name__ == '__main__':
    config_set()
