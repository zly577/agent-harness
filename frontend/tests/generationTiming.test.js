import test from 'node:test'
import assert from 'node:assert/strict'
import { answerTiming, elapsedSecondsLabel, freezeMeter, resumeMeter } from '../src/generationMeter.js'

test('elapsed time uses completed integer seconds, including minute boundaries', () => {
  assert.equal(elapsedSecondsLabel(1000, null, 1999), '0 秒')
  assert.equal(elapsedSecondsLabel(1000, null, 2000), '1 秒')
  assert.equal(elapsedSecondsLabel(1000, 60999, 90000), '59 秒')
  assert.equal(elapsedSecondsLabel(1000, 61000, 90000), '60 秒')
  assert.equal(elapsedSecondsLabel(1000, 126999, 90000), '125 秒')
  assert.equal(elapsedSecondsLabel(2000, null, 1000), '0 秒')
})

test('completion changes the running label immediately and freezes elapsed time', () => {
  const message = { startedAt: 1000, streaming: true }
  assert.equal(answerTiming(message, 4500).text, '运行中 · 3 秒')
  freezeMeter(message, 4500)
  // The final result may arrive before the stream's done event.
  assert.deepEqual(answerTiming(message, 9000), { state: 'done', text: '已完成 · 用时 3 秒' })
  message.streaming = false
  assert.equal(answerTiming(message, 20000).text, '已完成 · 用时 3 秒')
  message.planPaused = true
  assert.equal(answerTiming(message, 20000).state, 'paused')
  resumeMeter(message, 20000)
  message.planPaused = false
  message.streaming = true
  assert.equal(answerTiming(message, 21000).text, '运行中 · 4 秒')
})

test('stopped, failed, disconnected and paused turns never claim completion', () => {
  const message = { startedAt: 1000, endedAt: 4555, streaming: false }
  for (const [flag, state, label] of [
    ['stopped', 'stopped', '已停止'], ['error', 'error', '未完成'],
    ['connectionLost', 'interrupted', '连接中断'], ['planPaused', 'paused', '等待确认'],
  ]) {
    assert.deepEqual(answerTiming({ ...message, [flag]: true }, 10000), { state, text: `${label} · 用时 3 秒` })
  }
})
