function timeParts(value, timeZone) {
  const date = new Date(value)
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  })
  return Object.fromEntries(formatter.formatToParts(date).map(({ type, value }) => [type, value]))
}

export function scheduleLocalInput(value, timezone) {
  if (!value) return ''
  try {
    const { year, month, day, hour, minute } = timeParts(value, timezone)
    return `${year}-${month}-${day}T${hour}:${minute}`
  } catch (error) {
    if (!(error instanceof RangeError)) throw error
    return ''
  }
}

export function formatScheduleTime(value, timezone) {
  const local = scheduleLocalInput(value, timezone)
  if (local) return local.replace('T', ' ')
  const utc = scheduleLocalInput(value, 'UTC')
  return utc ? `${utc.replace('T', ' ')} UTC` : ''
}

export function schedulePayload(form) {
  return {
    title: form.title.trim(),
    prompt: form.prompt,
    workspace_id: form.workspace_id === 'legacy' ? null : form.workspace_id,
    provider_id: form.provider_id || null,
    timezone: form.timezone.trim(),
    frequency: form.frequency,
    once_at: form.frequency === 'once' ? form.once_at : null,
    time_of_day: form.frequency === 'once' ? null : form.time_of_day,
    weekdays: form.frequency === 'weekly' ? [...form.weekdays].sort((a, b) => a - b) : [],
  }
}

export function scheduleChanges(before, after) {
  const changes = Object.fromEntries(Object.entries(after).filter(([key, value]) =>
    JSON.stringify(before[key]) !== JSON.stringify(value)))
  if (changes.timezone && after.frequency === 'once') changes.once_at = after.once_at
  return changes
}
