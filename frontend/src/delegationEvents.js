import { reactive } from 'vue'

/**
 * 找到（或按需创建）某次 task 调用对应的委派记录。
 *
 * @param id 事件里的 id
 * @param adoptEmpty id 认不出来时，是否允许"认领"一张刚建好、还没有任何内容的卡片。
 *        `subagent_start` 带的是权威的 task 调用 id，必须独立建卡（传 false）；
 *        其余事件传 true。
 *
 * 为什么要认领：后端已把子图命名空间 id 归一到 task 调用 id（见
 * SubagentCallRegistry），正常情况下这里不会走到认领分支。但两个委派并发时
 * 认领顺序可能错位，此时**凭空多出一张"幽灵卡片"**（只有正文、没有摘要和类型，
 * 而原本那张永远空白）比认领错更糟 —— 用户看到的第一张卡片会一直"工作中"却没内容。
 */
export function ensureDelegation(msg, id, adoptEmpty = true) {
  if (!msg.delegations) msg.delegations = []
  let d = msg.delegations.find((x) => x.id === id || (x.aliasId && x.aliasId === id))
  if (!d) {
    if (adoptEmpty) {
      const fresh = msg.delegations.filter(
        (x) => x.status === 'running' && !x.stream && !x.thinking && !x.aliasId,
      )
      if (fresh.length === 1) {
        d = fresh[0]
        d.aliasId = id // 记住别名，后续同 id 的事件直接命中
        return d
      }
    }
    d = reactive({
      id,
      aliasId: '',         // 子图命名空间 id（与 id 不同时记在这里，用于命中）
      subagent_type: '',
      description: '',
      summary: '',
      stream: '',          // 子智能体实时输出（正文，累积）
      thinking: '',        // 子智能体的推理增量（累积）
      tools: [],           // 它自己调用的工具
      usage: null,         // 它最后一次模型步的 token 用量
      outTokens: 0,        // 它累计**生成**的 token（逐步累加，用于速率与合计）
      result: '',
      status: 'running',
      startedAt: Date.now(),
    })
    msg.delegations.push(d)
  }
  return d
}

/**
 * 按 id 找委派记录；找不到时退化为"最近一个还在跑的委派"。
 *
 * 为什么要兜底：用量/推理事件里带的 id 可能来自子图命名空间；后端已经做过归一，
 * 但两者分属不同渠道，一旦对不上就会**静默丢失**（卡片上永远没有 token 数）。
 * 宁可落到"当前唯一在跑的那个委派"上，也不要把数据丢掉。
 */
function findDelegation(msg, id) {
  const list = msg.delegations || []
  if (id) {
    const hit = list.find((x) => x.id === String(id) || x.aliasId === String(id))
    if (hit) return hit
  }
  const running = list.filter((x) => x.status === 'running')
  return running.length === 1 ? running[0] : null
}

/**
 * 处理子智能体事件。
 * 后端把 task 委派的内部过程按 `subagent_*` 事件实时推过来（见后端
 * subagent_stream 模块），这里落到对应的委派记录上，供卡片实时展示。
 */
export function applySubagentEvent(msg, evt) {
  const id = String(evt.id || '')
  if (!id) return
  // subagent_start 带的是权威 task 调用 id，必须独立建卡；其余事件才允许认领空卡片
  const d = ensureDelegation(msg, id, evt.type !== 'subagent_start')

  if (evt.type === 'subagent_start') {
    if (evt.subagent_type) d.subagent_type = evt.subagent_type
    if (evt.summary) d.summary = evt.summary
    d.status = 'running'
    if (!d.startedAt) d.startedAt = Date.now()
    return
  }
  if (evt.type === 'subagent_token') {
    d.stream += evt.delta || ''
    return
  }
  if (evt.type === 'subagent_thinking') {
    // 推理增量：长思考期间靠它让卡片保持"活着"
    d.thinking += evt.delta || ''
    return
  }
  if (evt.type === 'subagent_tool') {
    const name = evt.name || ''
    if (!name) return
    if (evt.status === 'running') {
      // 同名并发只记一条；执行中的排在最前
      if (!d.tools.some((t) => t.name === name && t.status === 'running')) {
        d.tools.push({ name, status: 'running' })
      }
    } else {
      const t = d.tools.find((x) => x.name === name && x.status === 'running')
      if (t) t.status = 'done'
      else if (!d.tools.some((x) => x.name === name)) d.tools.push({ name, status: 'done' })
    }
    return
  }
  if (evt.type === 'subagent_usage') {
    d.usage = {
      input: evt.input || 0,
      output: evt.output || 0,
      reasoning: evt.reasoning || 0,
      total: evt.total || 0,
    }
    // 逐步累加**生成**量（.total 含重复计入的输入上下文，不能用来求和）
    d.outTokens = (d.outTokens || 0) + (evt.output || 0)
    return
  }
  if (evt.type === 'subagent_end') {
    // 结束只标记状态；完整回报由随后的 tool_result 事件填入
    d.status = 'done'
    d.tools.forEach((t) => { t.status = 'done' })
  }
}

/**
 * 处理推理（thinking）增量。
 * 这是长推理期间**唯一**会持续推进的流：没有它，界面会长时间一动不动，
 * 用户无法区分"在思考"和"挂了"（实测一次 31.8s 的回答里前 26.9s 全是推理）。
 */
export function applyThinkingEvent(msg, evt) {
  const delta = evt.delta || ''
  if (!delta) return
  if (evt.scope === 'subagent' || evt.subagent_id) {
    const d = findDelegation(msg, evt.subagent_id)
    if (d) {
      d.thinking += delta
      return
    }
  }
  msg.thinking = (msg.thinking || '') + delta
}

/** 主智能体的 token 用量（精确，来自后端的 usage_metadata）。 */
export function applyUsageEvent(msg, evt) {
  // 子智能体的用量要记在它自己的卡片上，不能混进主智能体的总量/速率
  if (evt.scope === 'subagent') {
    const d = findDelegation(msg, evt.subagent_id)
    if (d) {
      d.usage = {
        input: evt.input || 0,
        output: evt.output || 0,
        reasoning: evt.reasoning || 0,
        total: evt.total || 0,
      }
      d.outTokens = (d.outTokens || 0) + (evt.output || 0)
    }
    return
  }
  const prev = msg.usage || { input: 0, output: 0, reasoning: 0, total: 0 }
  msg.usage = {
    input: evt.input || 0,
    output: evt.output || 0,
    reasoning: evt.reasoning || 0,
    total: (evt.total || 0) > (prev.total || 0) ? evt.total : prev.total,
  }
  // 保存后端的生成用量；瞬时速度使用流式增量。
  msg.outTokens = (msg.outTokens || 0) + (evt.output || 0)
  if (evt.total) msg.outputTokens = Math.max(msg.outputTokens || 0, evt.output || 0)
}
