import assert from 'node:assert/strict'
import { recordDelta, instantaneousRate, freezeMeter, resumeMeter } from '../src/generationMeter.js'
const first = { startedAt: 1000 }
recordDelta(first, 'a'.repeat(80), 1500)
assert.equal(instantaneousRate(first, 2000), 20)
assert.equal(instantaneousRate(first, 3600), 0, 'stale tokens must fall out of the window')
recordDelta(first, '汉'.repeat(40), 4000)
assert.equal(instantaneousRate(first, 4500), 20, 'recent speed must not divide by total duration')
freezeMeter(first, 4500)
assert.equal(first.endedAt - first.startedAt, 3500)
const second = { startedAt: 6000 }
recordDelta(second, 'test'.repeat(30), 6500)
assert.equal(instantaneousRate(first, 7000), 20, 'new message must not change the finished speed')
assert.equal(first.endedAt - first.startedAt, 3500, 'finished duration stays frozen')
recordDelta(first, 'ignored', 7000)
assert.deepEqual(first.rateSamples, [])
resumeMeter(first, 8000)
assert.equal(first.startedAt, 4500, 'approval wait must not count as generation time')
assert.equal(instantaneousRate(first, 8500), 0)
recordDelta(first, '汉汉汉汉', 8500)
assert.equal(instantaneousRate(first, 9000), 4)
console.log('Generation meter checks passed')
