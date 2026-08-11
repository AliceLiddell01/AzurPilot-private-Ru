"""Ограниченный анализ ошибок через явно настроенный OpenAI-compatible LLM API.

Модуль не выбирает провайдера автоматически и по умолчанию выключен.
Для запроса пользователь должен явно задать API key, API base и имя модели.
Одинаковые traceback кэшируются, поэтому один и тот же сбой не вызывает повторные
API-запросы в рамках текущего процесса.
"""

import hashlib
import os
import traceback

from module.logger import logger


_analyzed_errors_cache = {}
LLM_CONFIG_WARNING = (
    "Анализ ошибок LLM недоступен. Проверьте, что явно заданы API Key, API Base "
    "и имя модели выбранного OpenAI-compatible провайдера."
)
LLM_EMPTY_RESULT_WARNING = (
    "LLM API вернул пустой результат. Проверьте конфигурацию сервиса и имя модели."
)


def _get_analysis_from_response(response):
    """Извлечь текст анализа из OpenAI-compatible ответа."""
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if content is None:
        return ""
    return content.strip()


def _read_log_tail(path, max_bytes=64 * 1024, max_lines=200):
    """Прочитать ограниченный хвост журнала, не загружая весь файл в память."""
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        start = max(0, size - max_bytes)
        stream.seek(start)
        data = stream.read(max_bytes)

    text = data.decode("utf-8", errors="replace")
    if start:
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :]

    result = "".join(text.splitlines(keepends=True)[-max_lines:])
    encoded = result.encode("utf-8")
    if len(encoded) > max_bytes:
        result = encoded[-max_bytes:].decode("utf-8", errors="ignore")
    return result


def analyze_exception(config, error):
    """Отправить один ограниченный диагностический пакет для анализа исключения."""
    if not getattr(config, "Error_LlmAnalysis", False):
        return

    tb = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    error_hash = hashlib.md5(tb.encode("utf-8")).hexdigest()
    if error_hash in _analyzed_errors_cache:
        cached_result = _analyzed_errors_cache[error_hash]
        model = str(getattr(config, "Error_LlmModel", "") or "настроенная модель")
        logger.hr("[LLM] Анализ ошибки LLM", level=1)
        logger.info(
            "[LLM] Эта ошибка уже анализировалась; используется кэшированный результат без нового API-запроса."
        )
        logger.info(
            f"[LLM] \n[Отчёт анализа LLM ({model}, кэш)]\n{cached_result}\n"
        )
        logger.hr("[LLM] Анализ LLM завершён", level=1)
        return

    api_key = str(getattr(config, "Error_LlmApiKey", "") or "").strip()
    api_base = str(getattr(config, "Error_LlmApiBase", "") or "").strip()
    model = str(getattr(config, "Error_LlmModel", "") or "").strip()

    if not api_key or not api_base or not model:
        missing = []
        if not api_key:
            missing.append("API Key")
        if not api_base:
            missing.append("API Base")
        if not model:
            missing.append("имя модели")
        logger.warning(
            "[LLM] Анализ ошибок включён, но не настроены: " + ", ".join(missing)
        )
        logger.warning(LLM_CONFIG_WARNING)
        return

    _analyzed_errors_cache[error_hash] = "Анализ выполняется..."
    if len(_analyzed_errors_cache) > 50:
        _analyzed_errors_cache.clear()
        _analyzed_errors_cache[error_hash] = "Анализ выполняется..."

    logger.hr("[LLM] Анализ ошибки LLM", level=1)
    logger.info("[LLM] Отправка ограниченного диагностического контекста для анализа...")

    try:
        from openai import OpenAI

        log_context = ""
        try:
            if (
                hasattr(logger, "log_file")
                and logger.log_file
                and os.path.exists(logger.log_file)
            ):
                log_context = _read_log_tail(logger.log_file)
        except Exception:
            pass

        def truncate(text, limit):
            if len(text) > limit:
                return f"... [обрезано] ...\n{text[-limit:]}"
            return text

        tb = truncate(tb, 12000)
        log_context = truncate(log_context, 24000)

        prompt = f"""
Ты анализируешь единичный сбой AzurPilot по уже собранному диагностическому контексту.
У тебя нет доступа к репозиторию, файлам, терминалу или сети кроме текста ниже.
Не придумывай отсутствующие факты и не предлагай автоматически изменять код.

Дай компактный отчёт, который разработчик сможет передать в отдельный интерактивный
сеанс для последующего исправления:
1. Краткое описание сбоя.
2. Наиболее вероятная причина и уровень уверенности.
3. Конкретные строки traceback/лога, на которых основан вывод.
4. Что проверить следующим шагом.
5. Какие данные нужны дополнительно, если причины недостаточно ясны.

Исключение: {type(error).__name__}: {str(error)}

Traceback:
{tb}

Последний релевантный фрагмент журнала:
{log_context}
"""
        client = OpenAI(api_key=api_key, base_url=api_base)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты диагностический анализатор AzurPilot. Только анализируй предоставленный "
                        "контекст; не утверждай, что выполнил команды, просмотрел репозиторий или исправил код."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=1200,
            timeout=60,
        )

        analysis = _get_analysis_from_response(response)
        if not analysis:
            _analyzed_errors_cache.pop(error_hash, None)
            logger.warning(LLM_EMPTY_RESULT_WARNING)
            logger.warning(LLM_CONFIG_WARNING)
            logger.hr("[LLM] Анализ LLM завершён", level=1)
            return

        _analyzed_errors_cache[error_hash] = analysis
        logger.info(f"[LLM] \n[Отчёт анализа LLM ({model})]\n{analysis}\n")
        logger.hr("[LLM] Анализ LLM завершён", level=1)

    except ImportError:
        _analyzed_errors_cache.pop(error_hash, None)
        logger.error(
            "[LLM] Библиотека openai не установлена; OpenAI-compatible анализ недоступен."
        )
    except Exception as exc:
        _analyzed_errors_cache.pop(error_hash, None)
        logger.error(f"[LLM] Вызов анализа LLM завершился ошибкой: {exc}")
        logger.warning(LLM_CONFIG_WARNING)
