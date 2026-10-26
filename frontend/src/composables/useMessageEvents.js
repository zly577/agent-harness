import { ensureDelegation, applySubagentEvent, applyThinkingEvent, applyUsageEvent } from '../delegationEvents.js'
import { reactive } from 'vue'
import { recordDelta, freezeMeter } from '../generationMeter.js'

/** Message reducers shared by sending, approval resume and run replay. */
export function useMessageEvents({ messages, sessionId, sessionStates, approval }) {
  // ===== 流式事件渲染（首轮对话与审批恢复共用同一套处理） =====
  function findToolById(msg, id) {
    if (!id) return null
    return (msg.toolCalls || []).find((t) => t.id === id)
      || (msg.delegations || []).find((d) => d.id === id)
      || null
  }

  function applyToolEvent(msg, evt) {
    const args = evt.args || {}
    // 意图标签随 tool_call 事件到达（`intent_label`），存成 camelCase 与
    // 历史回放的字段名统一。**只做存储，不做渲染** —— 渲染一律走插值转义。
    const intentLabel = typeof evt.intent_label === 'string' ? evt.intent_label : ''
    if (evt.type === 'tool_call') {
      // 参数生成期可能已经建过一张占位卡（`tool_args` 心跳建的，status='generating'）：
      // 这里**就地升级**成正式的"进行中"。直接 return 会留下一条永远停在"生成参数中"
      // 的僵尸记录 —— 那比不显示更糟，用户会以为工具一直没跑起来。
      const existed = findToolById(msg, evt.id)
      if (existed) {
        if (existed.status === 'generating') {
          existed.name = evt.name || existed.name
          existed.args = args
          existed.intentLabel = intentLabel
          existed.status = 'running'
          existed.progress = null
          existed.startedAt = Date.now()
        }
        return
      }
      if (evt.name === 'task') {
        // 委派统一走 ensureDelegation（它带流式状态与用量）；task 本身不再进 toolCalls，
        // 避免同一个委派在"工具调用"里再出现一次（完成后由归档区展示）
        const d = ensureDelegation(msg, String(evt.id || evt.name), false)
        d.subagent_type = d.subagent_type || args.subagent_type || ''
        d.description = d.description || args.description || ''
        d.intentLabel = d.intentLabel || intentLabel
        return
      }
      msg.toolCalls.push({
        id: evt.id || evt.name,
        name: evt.name,
        args,
        result: '',
        status: 'running',
        intentLabel,
        // 记下"这一步什么时候开始的"，工具卡上就能一直显示实时耗时。
        // 用前端收到的时刻（而不是后端时间戳）：跨机器时钟差会让计时器出现负数。
        startedAt: Date.now(),
      })
      return
    }
    // tool_result
    const dIdx = (msg.delegations || []).findIndex(
      (t) => t.id === evt.id || (t.aliasId && t.aliasId === evt.id),
    )
    if (dIdx !== -1) {
      const d = msg.delegations[dIdx]
      d.result = evt.content || ''
      d.status = 'done'
      // 完成后并入工具调用列表（归档展示）：委派到此"隐匿"到工具区里，
      // 需要时展开即可看到完整回报
      d.tools.forEach((t) => { t.status = 'done' })
      msg.toolCalls.push({
        id: d.id,
        name: 'task',
        args: { subagent_type: d.subagent_type, description: d.description },
        result: d.result,
        status: 'done',
        kind: 'subagent',
        subagent: d,
        intentLabel: d.intentLabel || '',
      })
      return
    }
    const idx = (msg.toolCalls || []).findIndex((t) => t.id === evt.id)
    if (idx !== -1) {
      applyToolOutcome(msg.toolCalls[idx], evt)
    } else {
      const t = {
        id: evt.id || evt.name,
        name: evt.name,
        args: {},
        result: '',
        status: 'done',
        startedAt: 0,
      }
      applyToolOutcome(t, evt)
      msg.toolCalls.push(t)
    }
  }

  /**
   * 把 `tool_result` 事件写进一条工具调用。
   *
   * 为什么要**原样采信后端的判定**（`ok` / `exit_code` / `reason` / `duration_ms`）：
   * 成败只写在 `execute` 输出的尾巴上（`[Command failed with exit code N]`），
   * 而正文里照样会出现 `error` 这样的词。前端自己 grep 正文必然误报 ——
   * 用户会以为事情没成，转头把已经做对的操作重做一遍。
   *
   * `status` 用 `error` / `done` / `interrupted`，是工具卡右列徽标的唯一来源。
   */
  function applyToolOutcome(t, evt) {
    t.result = evt.content || ''
    const ok = evt.ok
    t.ok = (ok === true || ok === false) ? ok : null
    t.exitCode = typeof evt.exit_code === 'number' ? evt.exit_code : null
    t.reason = typeof evt.reason === 'string' ? evt.reason : ''
    t.tookMs = typeof evt.duration_ms === 'number' ? evt.duration_ms : null
    t.status = t.ok === false ? 'error' : 'done'
  }

  /**
   * 收尾本轮所有**还挂在 running** 的工具调用。
   *
   * 🔥 这是"界面永远停在进行中"的**前端那一半**成因：流因任何原因结束时
   * （后端被杀、网络断、用户停止），后端不可能再补发 `tool_result`，而这些
   * 调用会一直保持 `running` ⇒ 徽标永远显示"进行中"。以前这里只收尾了
   * delegations，toolCalls 被漏掉了。
   *
   * 标成 `interrupted` 而不是 `done`：如实说明"被打断了"，而不是假装完成。
   */
  function finishTurn(msg) {
    if (!msg) return
    freezeMeter(msg)
    if (!msg.planPaused) msg.planFinished = true
    ;(msg.delegations || []).forEach((d) => {
      if (d.status === 'running') {
        d.status = 'done'
        d.tools.forEach((t) => { t.status = 'done' })
      }
    })
    let orphan = 0
    ;(msg.toolCalls || []).forEach((t) => {
      if (t.status === 'running' || t.status === 'generating') {
        t.status = 'interrupted'
        t.ok = null
        t.reason = '本轮被中断，未收到结果'
        if (!t.result) t.result = '（本轮运行被中断，工具没有返回结果）'
        orphan += 1
      }
    })
    // 后端被杀 / 网络断开时，这一轮会"一句话都没有"地结束 —— 那正是用户最懵的场景。
    // 这里补一句明确说明（用户主动停止时 content 已有"（已停止）"，不会覆盖）。
    if (orphan && !String(msg.content || '').trim()) {
      msg.content = `⚠️ 这一轮没有正常收尾：有 ${orphan} 个操作没有返回结果，`
        + '后端可能已停止或连接断开了。上面工具区能看到它停在哪一步，'
        + '展开「这一步的具体操作」可以看到具体命令和输出。'
      msg.error = true
    }
    return orphan
  }

  /**
   * 工具参数**还在生成**期间的节流心跳（`tool_args` 事件）。
   *
   * 为什么需要：模型把大段内容塞进**工具参数**（不是正文）时，`tool_call` 事件要等参数
   * JSON 拼完整才发得出来。实测一个 `write_file` 的 `content` ≈ 82KB、耗时 **468 秒** ——
   * 这段时间后端一个事件都不发（`content` 是空串，也没有 token 增量），用户只能看到气泡
   * 不再变化，完全无法区分"在慢慢写"和"挂了"。
   *
   * 这里先把卡片建出来（status='generating'），让工具区立刻有东西可看；
   * 真正的 `tool_call` 到达时由 `applyToolEvent` 就地升级（见那里的注释）。
   */
  function applyToolArgsEvent(msg, evt) {
    const id = String(evt.id || evt.name || '')
    if (!id) return
    let t = (msg.toolCalls || []).find((x) => x.id === id)
    if (!t) {
      t = {
        id,
        name: evt.name || '',
        args: {},
        result: '',
        status: 'generating',
        intentLabel: '',
        // 参数生成期就开始计时：那段时间同样是"用户只能干等"的时间
        startedAt: Date.now(),
      }
      msg.toolCalls.push(t)
    }
    // 只带体积/耗时，不带参数正文（正文可能几 MB，推到前端纯属浪费）
    t.progress = {
      chars: Number(evt.chars) || 0,
      kb: Number(evt.kb) || 0,
      elapsed: Number(evt.elapsed) || 0,
    }
  }

  /** 把「还在生成参数」渲染成一句人话（体积 + 已耗时）。 */
  function progressLabel(t) {
    const p = t?.progress
    if (!p) return '正在生成参数…'
    const size = p.kb >= 1024 ? `${(p.kb / 1024).toFixed(1)} MB` : `${p.kb} KB`
    return `正在生成参数… ${size}（已 ${Math.round(p.elapsed)}s）`
  }

  /** 徽标里只放体积（比 detail 更短，给右列用）。 */
  function progressSize(t) {
    const p = t?.progress
    if (!p || !p.kb) return ''
    return p.kb >= 1024 ? `${(p.kb / 1024).toFixed(1)}MB` : `${p.kb}KB`
  }

  /**
   * 处理"数据类"流式事件，落到目标助手消息上。
   *
   * 三个入口共用这一段：新消息（streamChat）、审批恢复（streamApproval）、
   * 连接恢复（streamRunAttach）—— 它们的后端事件格式完全一致，
   * 分散成三份处理器迟早会各自漂移。
   *
   * @returns {boolean} true 表示已处理；false 表示这是控制类事件
   *   （session / approval / error / stopped / done），由各调用方自己决定怎么反应。
   */
  function applyDataEvent(msg, evt) {
    if (msg.streaming && messages.includes(msg)) {
      if (evt.type === 'token') recordDelta(msg, evt.content || '')
      if (evt.type === 'thinking' && evt.scope !== 'subagent' && !evt.subagent_id) recordDelta(msg, evt.delta || '')
    }
    switch (evt.type) {
      case 'token':
        msg.content += evt.content || ''
        return true
      case 'thinking':
        applyThinkingEvent(msg, evt)
        return true
      case 'tool_call':
      case 'tool_result':
        applyToolEvent(msg, evt)
        return true
      case 'tool_args':
        applyToolArgsEvent(msg, evt)
        return true
      case 'subagent_start':
      case 'subagent_token':
      case 'subagent_thinking':
      case 'subagent_tool':
      case 'subagent_usage':
      case 'subagent_end':
        applySubagentEvent(msg, evt)
        return true
      case 'usage':
        applyUsageEvent(msg, evt)
        return true
      case 'result':
        if (!msg.planPaused) msg.planFinished = true
        freezeMeter(msg)
        if (evt.content) msg.content = evt.content
        return true
      default:
        return false
    }
  }

  /**
   * 处理 `approval` 事件（三个入口共用：新消息 / 审批恢复 / 接回运行）。
   *
   * 两件事必须一起做：
   * 1. 记录并弹出审批弹窗；
   * 2. **让气泡里有话**。后端停在审批上时可能还没说过一句话（它正打算调工具），
   *    只弹窗的话这一轮看起来就是"工具都成功跑完了却什么都没说" —— 用户要的
   *    "最终回复"至少要有"我停在哪、在等什么"。
   */
  function applyApprovalEvent(target, evt, sid) {
    const payload = { ...evt, actions: evt.actions || [], clarifications: evt.clarifications || [], session_id: sid }
    if (sid && sessionStates[sid]) sessionStates[sid].approval = payload
    if (sessionId.value === sid) approval.value = payload
    if (target) {
      target.planPaused = true
      freezeMeter(target)
      target.streaming = false
      if (!String(target.content || '').trim()) {
        const n = (evt.actions || []).length
        if ((evt.clarifications || []).length) {
          target.content = `⏸ 等待你补充信息：${evt.clarifications[0].question}`
          return
        }
        target.content = String(evt.text || '').trim()
          || `⏸ 已停在人工审批：有 ${n} 个操作等你在弹窗里确认。确认后我会继续；`
             + '上面工具区里是本轮已经完成的动作。'
      }
    }
  }

  // 取（或新建）该会话的「当前助手消息」，用于把流式事件挂到正确的气泡上
  function currentAssistantMessage(msgs) {
    const tail = msgs[msgs.length - 1]
    if (tail && tail.role === 'assistant') return tail
    const fresh = reactive({
      role: 'assistant', content: '', thinking: '', toolCalls: [], delegations: [],
      streaming: true, time: '', startedAt: Date.now(), usage: null, outTokens: 0, outputTokens: 0,
    })
    msgs.push(fresh)
    return fresh
  }


  return { applyDataEvent, applyApprovalEvent, currentAssistantMessage, finishTurn, progressLabel, progressSize, applyToolOutcome }
}
