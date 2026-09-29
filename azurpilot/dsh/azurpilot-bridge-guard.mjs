/**
 * Страж соответствия source state AzurPilot для клиента DeepSeek Harness.
 *
 * Плагин принадлежит репозиторию AzurPilot и подключается только вместе с
 * overlay-файлом `azurpilot-bridge.patch.yml`, который готовит запускатель
 * `azurpilot-harness`. Идентичность источника здесь не вычисляется: страж
 * вызывает владельца `azur dsh verify`, а сам лишь блокирует вызовы инструментов
 * AzurPilot MCP, когда checkout перестал соответствовать генерации сессии.
 * Решение всегда закрытое: неизвестное состояние и недоступный владелец проверки
 * означают запрет вызова.
 */

import { execFile } from 'node:child_process'

export const name = 'azurpilot-bridge-guard'

/** Публичные префиксы инструментов каждого семейства AzurPilot MCP. */
const FAMILY_PREFIXES = [
  ['azurpilot-dev', 'mcp__azurpilot-dev__'],
  ['azurpilot-game', 'mcp__azurpilot-game__'],
]

/** Переменные окружения генерации, которые передаёт запускатель `azurpilot-harness`. */
const GENERATION_ENV_VARS = [
  'AZURPILOT_DSH_CHECKOUT_ROOT',
  'AZURPILOT_DSH_AZUR',
  'AZURPILOT_DSH_SOURCE_REVISION',
  'AZURPILOT_DSH_DEV_SOURCE_IDENTITY',
  'AZURPILOT_DSH_GAME_SOURCE_IDENTITY',
]

/** Верхняя граница ожидания владельца проверки, миллисекунды. */
const VERIFY_TIMEOUT_MS = 30000

/** Ограничение объёма ответа владельца проверки, байты. */
const VERIFY_MAX_OUTPUT_BYTES = 4 * 1024 * 1024

/** Ограничение текста отказа, символы. */
const REASON_MAX_LENGTH = 600

function familyOf(toolName) {
  if (typeof toolName !== 'string') return undefined
  for (const [family, prefix] of FAMILY_PREFIXES) {
    if (toolName.startsWith(prefix)) return family
  }
  return undefined
}

function parseVerdict(stdout) {
  const text = String(stdout ?? '').trim()
  if (text === '') return undefined
  const candidates = [text, ...text.split('\n').reverse()]
  for (const candidate of candidates) {
    const line = candidate.trim()
    if (line === '') continue
    try {
      const parsed = JSON.parse(line)
      if (parsed !== null && typeof parsed === 'object') return parsed
    } catch {
      // Строка не является документом JSON: пробуем следующую.
    }
  }
  return undefined
}

function verify(azurExecutable, checkoutRoot, environment) {
  return new Promise((resolve) => {
    execFile(
      azurExecutable,
      ['dsh', 'verify', '--json'],
      {
        cwd: checkoutRoot === '' ? undefined : checkoutRoot,
        timeout: VERIFY_TIMEOUT_MS,
        maxBuffer: VERIFY_MAX_OUTPUT_BYTES,
        windowsHide: true,
        env: {
          PATH: process.env.PATH ?? '',
          HOME: process.env.HOME ?? '',
          PYTHONUTF8: '1',
          PYTHONIOENCODING: 'utf-8',
          ...environment,
        },
      },
      (error, stdout) => {
        // Владелец проверки печатает один документ JSON и при расхождении, а
        // ненулевой код возврата сам по себе означает запрет. Поэтому разбор
        // выполняется всегда: недоступным состояние считается только тогда,
        // когда документ JSON получить не удалось.
        resolve(parseVerdict(stdout))
      },
    )
  })
}

function notPreparedReason(family, missing) {
  return (
    'AZURPILOT_DSH_GUARD_NOT_PREPARED: клиент AzurPilot MCP подключён без ' +
    `подготовленного окружения запускателя (${missing.join(', ')}). ` +
    `Вызов инструмента ${family} заблокирован.`
  )
}

function unavailableReason(family) {
  return (
    'AZURPILOT_DSH_SOURCE_DRIFT: владелец проверки не подтвердил соответствие ' +
    `checkout генерации сессии AzurPilot Harness (${family}). ` +
    'Вызов инструмента заблокирован; перезапустите azurpilot-harness.'
  )
}

function driftReason(family, state) {
  const detail =
    typeof state?.message === 'string' && state.message !== ''
      ? state.message
      : String(state?.reason_code ?? '')
  return (
    'AZURPILOT_DSH_SOURCE_DRIFT: checkout больше не соответствует генерации сессии ' +
    `AzurPilot Harness (${family}). ${detail} ` +
    'Вызов инструмента заблокирован; перезапустите azurpilot-harness из checkout, ' +
    'который соответствует мосту Windows MCP.'
  ).slice(0, REASON_MAX_LENGTH)
}

export function apply(ctx) {
  const environment = {}
  const missing = []
  for (const variable of GENERATION_ENV_VARS) {
    const value = process.env[variable]
    if (typeof value !== 'string' || value.trim() === '') {
      missing.push(variable)
      continue
    }
    environment[variable] = value
  }
  const azurExecutable = environment.AZURPILOT_DSH_AZUR ?? ''
  const checkoutRoot = environment.AZURPILOT_DSH_CHECKOUT_ROOT ?? ''

  const blocked = new Map()
  let pending

  function announce(message) {
    if (ctx.logger && typeof ctx.logger.info === 'function') ctx.logger.info(message)
    else console.error(message)
  }

  function verdicts() {
    if (pending === undefined) {
      pending = verify(azurExecutable, checkoutRoot, environment).finally(() => {
        pending = undefined
      })
    }
    return pending
  }

  function stateOf(report, family) {
    const families = report?.details?.families
    if (!Array.isArray(families)) return undefined
    return families.find((item) => item?.server_name === family)
  }

  async function denialFor(family) {
    const remembered = blocked.get(family)
    if (remembered !== undefined) return remembered
    if (missing.length > 0) {
      const reason = notPreparedReason(family, missing)
      blocked.set(family, reason)
      return reason
    }
    const report = await verdicts()
    const state = stateOf(report, family)
    if (state === undefined || typeof state !== 'object') {
      const reason = unavailableReason(family)
      blocked.set(family, reason)
      return reason
    }
    if (state.status === 'ready') return undefined
    const reason = driftReason(family, state)
    blocked.set(family, reason)
    return reason
  }

  ctx.on('tools/pre-execute', async (execution, next) => {
    const family = familyOf(execution?.name)
    if (family === undefined) return next()
    const reason = await denialFor(family)
    if (reason === undefined) return next()
    announce(`[azurpilot-bridge-guard] ${reason}`)
    return { kind: 'deny', reason }
  })

  announce(
    `[azurpilot-bridge-guard] активен: checkout=${checkoutRoot} ` +
      `revision=${environment.AZURPILOT_DSH_SOURCE_REVISION ?? ''}`,
  )
}
