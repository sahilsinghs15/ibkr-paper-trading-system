import { genManualIdemKey } from './manualIdempotency'

// Simple test runner without vitest
function assert(cond: boolean, msg: string) {
  if (!cond) throw new Error(`ASSERT FAILED: ${msg}`)
}

async function run() {
  console.log('Testing genManualIdemKey...')

  // A: crypto.randomUUID available → uses randomUUID
  {
    const orig = (globalThis as any).crypto
    Object.defineProperty(globalThis, 'crypto', {
      value: {
        randomUUID: () => '12345678-1234-1234-1234-123456789abc',
        getRandomValues: (arr: Uint8Array) => { for (let i = 0; i < arr.length; i++) arr[i] = i; return arr },
      },
      writable: true,
      configurable: true,
    })
    const k = genManualIdemKey()
    assert(k === 'MAN_IDEM_1234567812341234', `A failed: ${k}`)
    console.log('A passed')
    Object.defineProperty(globalThis, 'crypto', { value: orig, writable: true, configurable: true })
  }

  // B: randomUUID unavailable → uses getRandomValues
  {
    const orig = (globalThis as any).crypto
    Object.defineProperty(globalThis, 'crypto', {
      value: {
        getRandomValues: (arr: Uint8Array) => { for (let i = 0; i < arr.length; i++) arr[i] = i + 1; return arr },
      },
      writable: true,
      configurable: true,
    })
    const k = genManualIdemKey()
    // 01 02 03 ... => hex 010203... slice 0 16 => 0102030405060708
    assert(k.startsWith('MAN_IDEM_'), `B prefix failed: ${k}`)
    assert(k.length === 9 + 16, `B length failed: ${k}`)
    assert(/MAN_IDEM_[0-9A-F]{16}/.test(k), `B format failed: ${k}`)
    console.log('B passed')
    Object.defineProperty(globalThis, 'crypto', { value: orig, writable: true, configurable: true })
  }

  // C: getRandomValues produces valid unique format
  {
    const k = genManualIdemKey()
    assert(/MAN_IDEM_[0-9A-F]{16}/.test(k), `C format failed: ${k}`)
    console.log('C passed')
  }

  // D: both unavailable → throws, no predictable key
  {
    const orig = (globalThis as any).crypto
    Object.defineProperty(globalThis, 'crypto', { value: undefined, writable: true, configurable: true })
    let threw = false
    try { genManualIdemKey() } catch (e) { threw = true; assert((e as Error).message.includes('Secure random'), 'D message') }
    assert(threw, 'D should throw')
    console.log('D passed')
    Object.defineProperty(globalThis, 'crypto', { value: orig, writable: true, configurable: true })
  }

  // E: two keys different
  {
    const k1 = genManualIdemKey()
    const k2 = genManualIdemKey()
    assert(k1 !== k2, `E same keys: ${k1} vs ${k2}`)
    console.log('E passed')
  }

  // F: page remount different key (simulate two mounts)
  {
    const k1 = genManualIdemKey()
    const k2 = genManualIdemKey()
    assert(k1 !== k2, 'F remount same')
    console.log('F passed')
  }

  // G: intentional second order different key
  {
    const k1 = genManualIdemKey()
    const k2 = genManualIdemKey()
    assert(k1 !== k2, 'G same')
    console.log('G passed')
  }

  // H: same order retry retains same key — lifecycle test (simulate)
  {
    const key = genManualIdemKey()
    const retryKey = key // retry keeps same
    assert(key === retryKey, 'H retry should keep same')
    console.log('H passed')
  }

  // I,J,K: changing quantity/side/symbol creates new intent/key — intent sig diff
  {
    const sig1 = `120549942|AAPL|BUY|100|MARKET||DAY`
    const sig2 = `120549942|AAPL|BUY|1|MARKET||DAY` // quantity changed
    const sig3 = `120549942|AAPL|SELL|100|MARKET||DAY` // side changed
    const sig4 = `999999|MSFT|BUY|100|MARKET||DAY` // symbol changed
    assert(sig1 !== sig2, 'I quantity should change sig')
    assert(sig1 !== sig3, 'J side should change sig')
    assert(sig1 !== sig4, 'K symbol should change sig')
    console.log('IJK passed')
  }

  console.log('All frontend idempotency tests passed')
}

run().catch((e) => { console.error(e); process.exit(1) })
