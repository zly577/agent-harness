import test from 'node:test'
import assert from 'node:assert/strict'
import { formatScheduleTime, scheduleChanges, scheduleLocalInput, schedulePayload } from '../src/schedulePresentation.js'

const base = {
  title: ' 日报 ', prompt: '汇总工作日志', workspace_id: 'legacy', provider_id: '',
  timezone: ' Asia/Shanghai ', frequency: 'weekly', weekdays: [7, 1, 3],
  time_of_day: '09:00', once_at: '2026-11-10T09:00',
}

test('schedule forms submit canonical resource IDs and only the selected frequency fields', () => {
  const weekly = schedulePayload(base)
  assert.equal(weekly.title, '日报')
  assert.equal(weekly.workspace_id, null)
  assert.equal(weekly.provider_id, null)
  assert.equal(weekly.timezone, 'Asia/Shanghai')
  assert.equal(weekly.once_at, null)
  assert.equal(weekly.time_of_day, '09:00')
  assert.deepEqual(weekly.weekdays, [1, 3, 7])
  const once = schedulePayload({ ...base, frequency: 'once', workspace_id: 'workspace', provider_id: 'provider' })
  assert.equal(once.workspace_id, 'workspace')
  assert.equal(once.provider_id, 'provider')
  assert.equal(once.once_at, '2026-11-10T09:00')
  assert.equal(once.time_of_day, null)
  assert.deepEqual(once.weekdays, [])
})

test('one-off timestamps round trip as wall time in the task zone, including midnight and DST', () => {
  assert.equal(scheduleLocalInput('2026-11-09T16:00:00Z', 'Asia/Shanghai'), '2026-11-10T00:00')
  assert.equal(scheduleLocalInput('2026-11-01T06:30:00Z', 'America/New_York'), '2026-11-01T01:30')
  assert.equal(scheduleLocalInput(null, 'Asia/Shanghai'), '')
})

test('editing a completed one-off title leaves its old execution time untouched', () => {
  const before = schedulePayload({ ...base, frequency: 'once' })
  assert.deepEqual(scheduleChanges(before, { ...before, title: '新标题' }), { title: '新标题' })
  assert.deepEqual(scheduleChanges(before, { ...before, timezone: 'UTC' }), {
    timezone: 'UTC', once_at: '2026-11-10T09:00',
  })
})

test('unavailable browser time zones display labeled UTC without suggesting a false local time', () => {
  const value = '2026-11-10T00:30:00Z'
  const unknownZone = 'Unsupported/Timezone'
  assert.equal(scheduleLocalInput(value, unknownZone), '')
  assert.equal(formatScheduleTime(value, unknownZone), '2026-11-10 00:30 UTC')
  assert.equal(formatScheduleTime('invalid-date', unknownZone), '')
})
