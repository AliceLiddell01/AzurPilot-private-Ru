/**
 * Страж границы AzurPilot для клиента DeepSeek Harness.
 *
 * Плагин принадлежит репозиторию AzurPilot и подключается только вместе с
 * overlay-файлом `azurpilot-bridge.patch.yml`, который готовит запускатель
 * `azurpilot-harness`. Идентичность источника здесь не вычисляется: вердикт
 * выдаёт владелец проверки `azur dsh verify`.
 *
 * Запрет расхождения источника — это инвариант fail-closed, а не обычная
 * политика расширяемого `waterfall`. Поэтому он установлен в `ctx.tools.guard()`:
 * монотонный guard DeepSeek Harness исполняется после всех listeners
 * `tools/pre-execute` и не может быть отменён другим policy-плагином
 * (`dsh-tools`: `no guard can force-allow a call another guard denies`).
 * Синхронный контракт guard соблюдается за счёт подготовки вердикта владельцем
 * проверки вне пути вызова: решение принимается по уже принятому состоянию, а
 * запуск проверки при устаревшем состоянии не блокирует вызов и остаётся
 * фоновым (единственный запрос, ограниченный по времени).
 *
 * Проверка живёт по времени жизни плагина: единственный запрос к владельцу
 * ограничен по времени, не накладывается на себя и снимается при остановке
 * сессии. Поэтому отмена вызова инструмента не может оставить висящий процесс
 * проверки — она вообще не привязана к вызову.
 *
 * Подтверждение запуска. У точного pin DeepSeek Harness `0.1.7-rc.2` список
 * обязательных entry — глобальная константа `requiredStartupEntryIds` в
 * `dsh-app-boot`, и расширить её из профиля нельзя: неактивный entry вне этого
 * списка даёт только `dsh: warning: ... did not activate`, а запуск продолжается.
 * Поэтому запуск продукта закрыт со стороны запускателя: страж, подтвердив
 * собственную активацию, активацию обоих клиентов AzurPilot MCP и вердикт
 * владельца проверки, пишет аттестацию готовности по каналу, который создаёт
 * `azurpilot-harness`. Запускатель не считает запуск успешным без неё.
 */

import { spawn } from 'node:child_process'
import { renameSync, rmSync, writeFileSync } from 'node:fs'

export const name = 'azurpilot-bridge-guard'

/** Служба каталога инструментов, из которой страж берёт готовность клиентов. */
export const inject = ['tools']

/** Публичные имена семейств и префиксы их инструментов. */
const FAMILIES = [
  { server: 'azurpilot-dev', prefix: 'mcp__azurpilot-dev__' },
  { server: 'azurpilot-game', prefix: 'mcp__azurpilot-game__' },
]

/** Переменные окружения генерации, которые передаёт запускатель `azurpilot-harness`. */
const GENERATION_ENV_VARS = [
  'AZURPILOT_DSH_CHECKOUT_ROOT',
  'AZURPILOT_DSH_AZUR',
  'AZURPILOT_DSH_SOURCE_REVISION',
  'AZURPILOT_DSH_DEV_SOURCE_IDENTITY',
  'AZURPILOT_DSH_GAME_SOURCE_IDENTITY',
]

/** Канал подтверждения готовности, который создаёт запускатель. */
export const READINESS_FILE_ENV_VAR = 'AZURPILOT_DSH_READINESS_FILE'
export const READINESS_NONCE_ENV_VAR = 'AZURPILOT_DSH_READINESS_NONCE'
export const READINESS_TIMEOUT_ENV_VAR = 'AZURPILOT_DSH_READINESS_TIMEOUT_MS'

/** Версия документа аттестации готовности; владелец — READINESS_SCHEMA_VERSION. */
const ATTESTATION_SCHEMA_VERSION = 1

/** Верхняя граница одного ожидания владельца проверки, миллисекунды. */
const VERIFY_TIMEOUT_MS = 20000

/** Ограничение объёма ответа владельца проверки, байты. */
const VERIFY_MAX_OUTPUT_BYTES = 4 * 1024 * 1024

/**
 * Границы ожидания регистрации обоих клиентов AzurPilot MCP: срок задаёт
 * запускатель, чтобы отказ пришёл раньше его собственного срока ожидания.
 */
const READINESS_TIMEOUT_MS = 45000
const READINESS_TIMEOUT_MIN_MS = 1000
const READINESS_TIMEOUT_MAX_MS = 300000

/** Интервал опроса каталога инструментов при подтверждении активации. */
const READINESS_POLL_INTERVAL_MS = 200

/** Интервал фоновой проверки владельцем, миллисекунды. */
const VERIFY_INTERVAL_MS = 5000

/** Число попыток подтвердить вердикт владельца перед аттестацией готовности. */
const ATTEST_ATTEMPTS = 3

/** Пауза между попытками подтвердить вердикт владельца, миллисекунды. */
const ATTEST_RETRY_DELAY_MS = 500

/** Срок, после которого принятый вердикт считается устаревшим, миллисекунды. */
const VERDICT_MAX_AGE_MS = 20000

/** Ограничение текста отказа, символы. */
const REASON_MAX_LENGTH = 600

/** Строка, которой владелец проверки помечает готовность семейства. */
const READY_STATUS = 'ready'

/** Строка расхождения семейства. */
const DRIFT_STATUS = 'drift'

/** Код отказа: запускатель не подготовил окружение генерации. */
const NOT_PREPARED_CODE = 'AZURPILOT_DSH_GUARD_NOT_PREPARED'

/** Код отказа: обязательные клиенты AzurPilot MCP не активировались. */
const CLIENT_UNAVAILABLE_CODE = 'AZURPILOT_DSH_MCP_CLIENT_UNAVAILABLE'

/** Код отказа: владелец проверки не подтвердил соответствие генерации. */
const SOURCE_DRIFT_CODE = 'AZURPILOT_DSH_SOURCE_DRIFT'

class GuardError extends Error {
  constructor(code, message) {
    super(`${code}: ${message}`)
    this.name = 'GuardError'
    this.code = code
  }
}

function readReadinessTimeout() {
  const raw = Number.parseInt(process.env[READINESS_TIMEOUT_ENV_VAR] ?? '', 10)
  if (!Number.isFinite(raw)) return READINESS_TIMEOUT_MS
  if (raw < READINESS_TIMEOUT_MIN_MS || raw > READINESS_TIMEOUT_MAX_MS) {
    return READINESS_TIMEOUT_MS
  }
  return raw
}

function familyOf(toolName) {
  if (typeof toolName !== 'string') return undefined
  for (const family of FAMILIES) {
    if (toolName.startsWith(family.prefix)) return family.server
  }
  return undefined
}

function announce(ctx, message) {
  if (ctx?.logger && typeof ctx.logger.info === 'function') ctx.logger.info(message)
  else process.stderr.write(`${message}\n`)
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

class Generation {
  constructor(ctx) {
    const values = {}
    const missing = []
    for (const variable of GENERATION_ENV_VARS) {
      const value = process.env[variable]
      if (typeof value !== 'string' || value.trim() === '') {
        missing.push(variable)
        continue
      }
      values[variable] = value
    }
    this.ctx = ctx
    this.missing = missing
    this.azurExecutable = values.AZURPILOT_DSH_AZUR ?? ''
    this.checkoutRoot = values.AZURPILOT_DSH_CHECKOUT_ROOT ?? ''
    this.revision = values.AZURPILOT_DSH_SOURCE_REVISION ?? ''
    this.environment = Object.freeze(values)
    this.readinessFile = process.env[READINESS_FILE_ENV_VAR] || undefined
    this.readinessNonce = process.env[READINESS_NONCE_ENV_VAR] || undefined
    this.readinessTimeout = readReadinessTimeout()
    this.ready = false
    this.verdict = { families: new Map(), at: 0 }
    this.pending = undefined
    this.watchdog = undefined
    this.child = undefined
    this.stopped = false
  }

  /**
   * Подтвердить, что оба клиента AzurPilot MCP действительно активировались.
   *
   * Молчаливая деградация до обычной сессии без инструментов AzurPilot здесь
   * закрыта: незавершённое подтверждение считается отказом, а не готовностью.
   */
  async confirmActivation() {
    if (this.missing.length > 0) {
      throw new GuardError(
        NOT_PREPARED_CODE,
        'клиент AzurPilot MCP подключён без подготовленного окружения ' +
          `запускателя (${this.missing.join(', ')}).`,
      )
    }
    if (this.readinessFile !== undefined && this.readinessNonce === undefined) {
      throw new GuardError(
        NOT_PREPARED_CODE,
        'канал подтверждения готовности запускателя создан без одноразового ' +
          'значения; повторный запуск не подтверждается.',
      )
    }
    const deadline = Date.now() + this.readinessTimeout
    for (;;) {
      if (this.stopped) return
      const missing = this.unregisteredFamilies()
      if (missing.length === 0) {
        this.ready = true
        announce(
          this.ctx,
          `[azurpilot-bridge-guard] активен: checkout=${this.checkoutRoot} ` +
            `revision=${this.revision}`,
        )
        return
      }
      if (Date.now() >= deadline) {
        throw new GuardError(
          CLIENT_UNAVAILABLE_CODE,
          'обязательные клиенты AzurPilot MCP не активировались ' +
            `(${missing.join(', ')}); запуск Harness остановлен.`,
        )
      }
      await new Promise((resolve) =>
        setTimeout(resolve, READINESS_POLL_INTERVAL_MS),
      )
    }
  }

  /**
   * Семейства, инструменты которых опубликованы в каталоге текущей сессии.
   *
   * Каталог — независимое свидетельство регистрации: аттестация клиента несёт
   * именно наблюдение, а не константу требований, поэтому запускатель сверяет
   * подтверждение с собственным ожиданием, а не повторяет заявление стража.
   */
  observedFamilies() {
    const registry = this.ctx?.tools
    if (registry === undefined || typeof registry.schemas !== 'function') {
      return []
    }
    let names
    try {
      const schemas = registry.schemas()
      names = Array.isArray(schemas)
        ? schemas.map((schema) => String(schema?.name ?? ''))
        : []
    } catch {
      return []
    }
    return FAMILIES.filter((family) =>
      names.some((candidate) => candidate.startsWith(family.prefix)),
    ).map((family) => family.server)
  }

  /** Семейства, инструменты которых не опубликованы в каталоге текущей сессии. */
  unregisteredFamilies() {
    const observed = new Set(this.observedFamilies())
    return FAMILIES.filter((family) => !observed.has(family.server)).map(
      (family) => family.server,
    )
  }

  /** Состояние одного семейства по последнему принятому вердикту владельца. */
  stateOf(server) {
    return this.verdict.families.get(server)
  }

  /** Принят ли вердикт, который ещё можно считать действующим. */
  isCurrent() {
    return (
      this.verdict.families.size > 0 &&
      Date.now() - this.verdict.at < VERDICT_MAX_AGE_MS
    )
  }

  /** Итог последнего принятого вердикта: `ready`, `drift` или `unavailable`. */
  verdictState() {
    if (!this.isCurrent()) return 'unavailable'
    const states = FAMILIES.map((family) => this.stateOf(family.server))
    if (states.some((state) => state === undefined)) return 'unavailable'
    if (states.some((state) => state.status === DRIFT_STATUS)) return 'drift'
    if (states.every((state) => state.status === READY_STATUS)) return 'ready'
    return 'unavailable'
  }

  /** Запустить фоновую проверку и поддерживать её без наложения запросов. */
  startWatchdog() {
    if (this.stopped || this.watchdog !== undefined) return
    this.watchdog = setInterval(() => void this.refresh(), VERIFY_INTERVAL_MS)
    if (typeof this.watchdog.unref === 'function') this.watchdog.unref()
  }

  /**
   * Запросить проверку у владельца. Запрос идёт вне пути вызова инструмента и
   * ограничен по времени, поэтому отмена вызова не может оставить висящую
   * проверку, а остановка плагина снимает уже запущенный процесс.
   */
  refresh() {
    if (this.stopped || this.pending !== undefined) return this.pending
    this.pending = this.verifyVerdict()
      .then((families) => {
        if (this.stopped) return
        this.verdict = { families, at: Date.now() }
      })
      .catch(() => {
        if (this.stopped) return
        this.verdict = { families: new Map(), at: Date.now() }
      })
      .finally(() => {
        this.pending = undefined
      })
    return this.pending
  }

  verifyVerdict() {
    return new Promise((resolve) => {
      let settled = false
      const finish = (families) => {
        if (settled) return
        settled = true
        resolve(families ?? new Map())
      }
      let child
      try {
        child = spawn(this.azurExecutable, ['dsh', 'verify', '--json'], {
          cwd: this.checkoutRoot === '' ? undefined : this.checkoutRoot,
          windowsHide: true,
          env: {
            PATH: process.env.PATH ?? '',
            HOME: process.env.HOME ?? '',
            PYTHONUTF8: '1',
            PYTHONIOENCODING: 'utf-8',
            ...this.environment,
          },
        })
      } catch {
        finish(new Map())
        return
      }
      this.child = child
      let stdout = ''
      let size = 0
      const timer = setTimeout(() => {
        child.kill()
        finish(new Map())
      }, VERIFY_TIMEOUT_MS)
      const collect = (chunk) => {
        if (size >= VERIFY_MAX_OUTPUT_BYTES) return
        size += chunk.length
        stdout += chunk
      }
      child.stdout?.on('data', collect)
      child.stderr?.on('data', () => undefined)
      child.on('error', () => {
        clearTimeout(timer)
        finish(new Map())
      })
      child.on('close', () => {
        clearTimeout(timer)
        if (this.child === child) this.child = undefined
        // Владелец проверки печатает один документ JSON и при расхождении, а
        // ненулевой код возврата сам по себе означает запрет. Поэтому разбор
        // выполняется всегда: недоступным состояние считается только тогда,
        // когда документ JSON получить не удалось.
        finish(this.readVerdict(stdout))
      })
    })
  }

  readVerdict(stdout) {
    const report = parseVerdict(stdout)
    const families = report?.details?.families
    if (!Array.isArray(families)) return new Map()
    const states = new Map()
    for (const item of families) {
      if (item === null || typeof item !== 'object') continue
      if (typeof item.server_name !== 'string') continue
      states.set(item.server_name, item)
    }
    return states
  }

  /** Синхронный монотонный запрет для вызова инструмента семейства AzurPilot. */
  reasonFor(server) {
    if (this.missing.length > 0) {
      return (
        `${NOT_PREPARED_CODE}: клиент AzurPilot MCP подключён без ` +
        `подготовленного окружения запускателя (${this.missing.join(', ')}). ` +
        `Вызов инструмента ${server} заблокирован.`
      )
    }
    if (!this.ready) {
      return (
        `${CLIENT_UNAVAILABLE_CODE}: обязательные клиенты AzurPilot MCP ещё не ` +
        `подтверждены. Вызов инструмента ${server} заблокирован.`
      )
    }
    if (!this.isCurrent()) {
      // Проверку обновляем сразу, но решение принимается по уже принятому
      // состоянию: до него вызов остаётся запрещённым.
      void this.refresh()
      return unavailableReason(server)
    }
    const state = this.stateOf(server)
    if (state === undefined || typeof state !== 'object') {
      return unavailableReason(server)
    }
    if (state.status === READY_STATUS) return undefined
    return driftReason(server, state)
  }

  /**
   * Подтвердить вердикт владельца перед аттестацией готовности.
   *
   * Проверка уже прошла в `prepare` запускателя, поэтому короткая повторная
   * попытка отделяет разовый сбой окружения от настоящего расхождения: без неё
   * случайная задержка `azur dsh verify` закрывала бы запуск продукта.
   */
  async confirmVerdict() {
    for (let attempt = 0; attempt < ATTEST_ATTEMPTS; attempt += 1) {
      await this.refresh()
      if (this.verdictState() === READY_STATUS) return
      if (attempt + 1 < ATTEST_ATTEMPTS) {
        await new Promise((resolve) => setTimeout(resolve, ATTEST_RETRY_DELAY_MS))
      }
    }
  }

  /**
   * Записать аттестацию готовности для запускателя `azurpilot-harness`.
   *
   * Готовая аттестация требует уже подтверждённой активации: порядок проверок в
   * `apply` не должен быть единственной защитой от ложного подтверждения.
   */
  attestReady() {
    if (!this.ready || this.stopped) {
      throw new GuardError(
        CLIENT_UNAVAILABLE_CODE,
        'аттестация готовности запрошена без подтверждённой активации ' +
          'обязательных клиентов AzurPilot MCP; запуск Harness остановлен.',
      )
    }
    this.attest({
      guard_installed: true,
      verification: this.verdictState(),
      mcp_families: this.observedFamilies(),
      source_revision: this.revision,
      checkout_root: this.checkoutRoot,
    })
  }

  /** Записать аттестацию отказа: запускатель не должен считать запуск успешным. */
  attestFailure(error) {
    this.attest({
      guard_installed: false,
      reason_code:
        typeof error?.code === 'string' ? error.code : 'AZURPILOT_DSH_GUARD_FAILED',
      message: String(error?.message ?? error).slice(0, REASON_MAX_LENGTH),
    })
  }

  attest(payload) {
    const target = this.readinessFile
    if (target === undefined) return
    const document = `${JSON.stringify(
      {
        schema_version: ATTESTATION_SCHEMA_VERSION,
        nonce: this.readinessNonce,
        ...payload,
      },
      null,
      2,
    )}\n`
    const temporary = `${target}.${String(process.pid)}.tmp`
    try {
      writeFileSync(temporary, document, { encoding: 'utf8', mode: 0o600 })
      renameSync(temporary, target)
    } catch (error) {
      try {
        rmSync(temporary, { force: true })
      } catch {
        // Временный файл уже удалён: ошибка записи остаётся первичной.
      }
      announce(
        this.ctx,
        `[azurpilot-bridge-guard] не удалось записать аттестацию готовности: ` +
          `${String(error?.message ?? error)}`,
      )
    }
  }

  stop() {
    this.stopped = true
    if (this.watchdog !== undefined) {
      clearInterval(this.watchdog)
      this.watchdog = undefined
    }
    if (this.child !== undefined) {
      try {
        this.child.kill()
      } catch {
        // Процесс проверки уже завершился: остановка продолжается.
      }
      this.child = undefined
    }
  }
}

function unavailableReason(server) {
  return (
    `${SOURCE_DRIFT_CODE}: владелец проверки не подтвердил соответствие ` +
    `checkout генерации сессии AzurPilot Harness (${server}). ` +
    'Вызов инструмента заблокирован; перезапустите azurpilot-harness.'
  )
}

function driftReason(server, state) {
  const detail =
    typeof state.message === 'string' && state.message !== ''
      ? state.message
      : String(state.reason_code ?? '')
  return (
    `${SOURCE_DRIFT_CODE}: checkout больше не соответствует генерации сессии ` +
    `AzurPilot Harness (${server}). ${detail} ` +
    'Вызов инструмента заблокирован; перезапустите azurpilot-harness из checkout, ' +
    'который соответствует мосту Windows MCP.'
  ).slice(0, REASON_MAX_LENGTH)
}

export async function apply(ctx) {
  const generation = new Generation(ctx)
  ctx.effect(() => () => generation.stop(), 'azurpilot-bridge-guard.lifecycle')

  ctx.tools.guard((execution) => {
    const server = familyOf(execution?.name)
    if (server === undefined) return undefined
    const reason = generation.reasonFor(server)
    if (reason !== undefined) announce(ctx, `[azurpilot-bridge-guard] ${reason}`)
    return reason
  })

  try {
    await generation.confirmActivation()
    await generation.confirmVerdict()
    generation.attestReady()
  } catch (error) {
    generation.attestFailure(error)
    throw error
  }
  generation.startWatchdog()
}
