"""Константы определения платформы окружения. Определяет флаги операционных систем
IS_WINDOWS, IS_MACINTOSH, IS_LINUX для использования модулями уровня устройства."""

import sys

IS_WINDOWS = sys.platform == 'win32'
IS_MACINTOSH = sys.platform == 'darwin'
IS_LINUX = sys.platform == 'linux'
