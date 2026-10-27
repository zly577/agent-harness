import { toolName } from '../toolPresentation.js'

export function useToolActivity({ showTools, elapsedLabel, progressLabel, progressSize }) {
  // tool_args carries progress only; the complete args arrive with tool_call.
  // Keep expanded details useful throughout that transition and on replay.
  function toolDetailNotice(it) {
    if (it?.status === 'generating') {
      return { title: progressLabel(it), text: '操作内容正在生成，尚未开始执行。生成完成后会在这里显示完整操作。' }
    }
    if (it?.result) return null
    if (it?.status === 'running') {
      return { title: '操作正在执行', text: '等待工具返回结果，收到后会自动展示。' }
    }
    if (it?.status === 'interrupted') {
      return { title: '操作已中断', text: it.reason || '未收到工具结果。' }
    }
    if (it?.status === 'error' || it?.ok === false) {
      return { title: '操作失败', text: it.reason || '工具未返回详细错误信息。' }
    }
    if (it?.args && Object.keys(it.args).length) {
      return { title: '未返回文本结果', text: '操作参数已显示在上方。' }
    }
    return { title: '暂无操作详情', text: '此记录没有保存操作参数或文本结果。' }
  }

  /**
   * 是否展示工具活动块：开关打开时展示；
   * 或本轮仍有工具在跑——即使隐藏了工具详情，也得让人看见它正在干活。
   */
  function hasActivity(m) {
    const feed = toolFeed(m)
    const delegations = m?.delegations || []
    if (!feed.length && !delegations.length) return false
    if (showTools.value) return true
    // `generating` 必须算"活着"：参数生成期正是最需要看得见的时候
    // （实测 468 秒），这时若因为"工具区收起"而整块不显示，就等于没做这个功能。
    return feed.some((g) => g.status === 'running' || g.status === 'generating')
      || delegations.some((d) => d.status === 'running')
  }

  // ===== 工具活动：同一时刻只展示最新一条 =====

  /** "看东西"的只读工具：它们会合并成一条，状态走 正在查看 → 已打开 → 已完成 */
  const VIEW_TOOLS = new Set(['ls', 'glob', 'grep', 'read_file'])

  /** 非只读工具的"进行中"文案（完成后一律"已完成"） */
  const TOOL_RUN_VERB = {
    write_file: '正在写入',
    edit_file: '正在修改',
    delete: '正在删除',
    execute: '正在执行命令',
    web_search: '正在联网搜索',
    write_todos: '正在更新任务清单',
    reset_tool_breakers: '正在重置熔断',
    task: '正在委派子智能体',
  }

  /** 从参数里取这次调用"操作了什么"（文件路径 / 匹配式 / 命令 / 查询词）。 */
  function toolTarget(tc) {
    const a = tc?.args || {}
    return String(
      a.file_path || a.path || a.pattern || a.command || a.query || a.description || '',
    ).trim()
  }

  /** 目标太长就截断（完整值挂在 title 上，鼠标悬停能看到）。 */
  function shortTarget(s, n = 52) {
    s = String(s ?? '')
    return s.length > n ? `${s.slice(0, n - 1)}…` : s
  }

  /**
   * 取一条工具调用的意图标签。
   *
   * 两条来源的字段名不同，所以取值集中在这里：
   * - 实时事件（SSE）= `intent_label`（后端契约里的 snake_case）；
   * - 历史回放（GET /messages）= `intentLabel`（跟着同一份 payload 的兄弟字段走）。
   * 两条都读不到就是空串 → 模板回落显示原本的工具文案（**绝不显示 undefined**）。
   */
  function toolIntent(t) {
    const raw = t?.intentLabel || t?.intent_label || ''
    return typeof raw === 'string' ? raw : ''
  }

  /**
   * 一条工具调用的"成败"文案。
   *
   * 数据来源是**后端判定好的** `ok` / `exit_code` / `reason` / `duration_ms`
   * （实时来自 `tool_result` 事件，回放来自历史里同名的 camelCase 字段），
   * 前端**绝不自己从输出正文里猜** —— 正文里出现 "error" 这个词太常见了。
   */
  function toolOutcomeText(it) {
    const st = it?.status
    if (st === 'running') {
      const s = elapsedLabel(it?.startedAt)
      return s ? `进行中 · 已 ${s}` : '进行中'
    }
    if (st === 'generating') {
      const size = progressSize(it)
      return size ? `生成参数 ${size}` : '生成参数中'
    }
    if (st === 'interrupted') return '已中断'
    const took = typeof it?.tookMs === 'number' ? ` · ${formatDuration(it.tookMs)}` : ''
    if (st === 'error' || it?.ok === false) {
      const reason = it?.reason
        || (typeof it?.exitCode === 'number' ? `退出码 ${it.exitCode}` : '')
      return `失败${reason ? `（${reason}）` : ''}${took}`
    }
    if (it?.ok === null || it?.ok === undefined) return `已返回${took}`
    return `成功${took}`
  }

  /**
   * 左列该显示什么：**跑完的一律显示"工具本身"**（工具名），只有正在跑的时候
   * 才显示动作文案（正在查看 / 正在写入 …）。
   *
   * 为什么：右侧徽标已经在说"已完成"了，左边再写一遍就是同一句话说两遍，
   * 而且用户真正想知道的是"这是哪个工具"。合并组显示去重后的工具名。
   */
  function toolLabel(g) {
    if (g.status === 'running') {
      return g.simple ? '正在查看' : (TOOL_RUN_VERB[g.name] || '正在执行')
    }
    const names = []
    for (const it of g.items) {
      const n = toolName(it.name)
      if (!names.includes(n)) names.push(n)
    }
    if (names.length <= 3) return names.join('、')
    return `${names.slice(0, 3).join('、')} 等 ${names.length} 个`
  }

  /**
   * 把本轮的调用整理成"信息流"：
   * **连续出现的只读工具合并成一条**，其余工具各自成条。
   *
   * 为什么要合并：一次调研里 ls/grep/read_file 会连着来十几条，
   * 每条都占一行会把正文挤没；合并成"正在查看 / 已打开 N 项"一条即可。
   * 非只读工具（写文件、跑命令、委派）单独成条 —— 那些是用户真正想知道的动作。
   */
  function toolFeed(m) {
    const tcs = (m?.toolCalls || []).filter((t) => t.kind !== 'subagent' && t.name !== 'write_todos')
    const feed = []
    for (const t of tcs) {
      const simple = VIEW_TOOLS.has(t.name)
      const last = feed[feed.length - 1]
      if (simple && last && last.simple) {
        last.items.push(t)
        continue
      }
      feed.push({
        key: `${t.id || t.name}-${feed.length}`,
        simple,
        items: [t],
        name: t.name || 'tool',
        status: 'done', // 下面按 items 重算
      })
    }
    // 整条只要有任意一个还在跑，就显示"进行中"。
    // ⚠️ `generating` 优先于 `running`：那张卡连参数都还没拿到，说"进行中"会让人
    // 以为工具已经在执行了（实际还在生成参数，两者对用户是完全不同的等待）。
    //
    // 失败/中断也是**终态**，必须能从组状态上看出来 —— 以前不管是跑完了、
    // 报错了还是被杀了，右列一律显示"已完成"，用户根本不知道哪一步没成。
    for (const g of feed) {
      if (g.items.some((it) => it.status === 'generating')) g.status = 'generating'
      else if (g.items.some((it) => it.status === 'running')) g.status = 'running'
      else if (g.items.some((it) => it.status === 'error')) g.status = 'error'
      else if (g.items.some((it) => it.status === 'interrupted')) g.status = 'interrupted'
      else g.status = 'done'
    }
    feed.forEach((g, index) => {
      g.step = index + 1
      // 左列：跑完显示工具本身，正在跑才显示动作文案（见 toolLabel）
      g.verb = toolLabel(g)
      // 看板的"当前步骤"优先显示意图（模型给的人话）；取不到就回落到 verb。
      // 合并只读工具时取**最新一项**的意图 —— 用户关心的是我刚碰的那个文件。
      g.intent = toolIntent(g.items[g.items.length - 1]) || ''
      g.title = g.items[g.items.length - 1]?.name || g.name || ''
      // 整组的最早开始时刻（用于"已 Ns"）与实测耗时合计
      g.startedAt = g.items.reduce(
        (acc, it) => (it.startedAt && (!acc || it.startedAt < acc) ? it.startedAt : acc), 0)
      g.tookMs = g.items.reduce(
        (acc, it) => (typeof it.tookMs === 'number' ? acc + it.tookMs : acc), 0) || null
      // 右列徽标：唯一说"完成/进行中/生成中/失败"的地方
      const generating = g.items.find((it) => it.status === 'generating')
      const genSize = progressSize(generating)
      // "还在等"的两种状态都带上实时耗时 —— 光写"进行中"，用户没法判断
      // 是"长命令正常在跑"还是"早就死了"，那正是这次要解决的问题。
      const live = (startedAtOpt) => {
        const s = elapsedLabel(startedAtOpt || g.startedAt)
        return s ? ` · 已 ${s}` : ''
      }
      if (g.status === 'generating') {
        g.badge = (genSize ? `生成参数 ${genSize}` : '生成参数中') + live()
      } else if (g.status === 'running') {
        g.badge = `进行中${live()}`
      } else if (g.status === 'error') {
        const bad = g.items.find((it) => it.status === 'error')
        const code = bad && typeof bad.exitCode === 'number' ? `（退出码 ${bad.exitCode}）` : ''
        g.badge = `失败${code}`
      } else if (g.status === 'interrupted') {
        g.badge = '已中断'
      } else {
        // 跑完的组直接说"成功 / 已完成"。
        // 两者都不是废话：「成功」= 每一项都被判定为成功（有退出码或正常返回）；
        // 「已完成」= 跑完了但判不出成败（比如工具只回了一句确认、没有退出码），
        // 这种情况**不能**替它说成功 —— 用户要的是真话。
        const allOk = g.items.every((it) => it.ok === true)
        const took = g.tookMs === null ? '' : ` · ${formatDuration(g.tookMs)}`
        g.badge = `${allOk ? '成功' : '已完成'}${took}`
      }
      g.full = g.simple && g.items.length > 1
        ? g.items.map((t) => toolTarget(t)).filter(Boolean).join('\n')
        : toolTarget(g.items[0])
      if (g.status === 'generating') {
        // 参数还没到，`toolTarget` 一定是空串 ⇒ 这里必须有专门的文案，
        // 否则卡片会显示成"什么都没有"（看起来又像是卡住了）。
        g.detail = progressLabel(generating)
        g.full = progressLabel(generating)
      } else if (g.simple && g.items.length > 1) {
        const names = g.items.map((t) => shortTarget(toolTarget(t), 28)).filter(Boolean)
        // 合并组的"最新一项"排在最前，方便一眼看到刚碰的是哪个文件
        g.detail = `${g.items.length} 项 · ${names.slice(-2).reverse().join('、')}`
      } else {
        g.detail = shortTarget(g.full)
      }
      // 跑完之后，把"结果"的**第一行**贴在卡片上 —— 用户要的
      // "操作了什么、成没成"，不应该只能靠展开详情才看得到。
      const lastItem = g.items[g.items.length - 1]
      g.resultHead = g.status === 'running' || g.status === 'generating' || g.status === 'error'
        ? ''
        : shortTarget(firstLine(lastItem?.result), 72)
      // 失败时把原因也贴出来（比"失败"两个字有用得多）
      g.failHead = g.status === 'error' && lastItem?.reason ? shortTarget(lastItem.reason, 72) : ''
    })
    return feed
  }

  /** 取正文第一行非空内容（工具结果通常第一行就已经说明问题）。 */
  function firstLine(s) {
    const text = String(s ?? '')
    for (const line of text.split('\n')) {
      if (line.trim()) return line.trim()
    }
    return ''
  }

  /** 毫秒 → 人话（与后端的 outcome_label 保持同一种写法）。 */
  function formatDuration(ms) {
    const secs = Math.max(Number(ms) || 0, 0) / 1000
    if (secs < 60) return `${secs.toFixed(1)}s`
    return `${Math.floor(secs / 60)}m${(secs % 60).toFixed(0)}s`
  }

  /** 最新一条（始终可见，实时刷新状态文案）。 */
  function activityLatest(m) {
    const feed = toolFeed(m)
    return feed.length ? feed[feed.length - 1] : null
  }

  /** 此前的调用：最近的在前，默认不展示。 */
  function activityHistory(m) {
    return toolFeed(m).slice(0, -1)
  }

  // ===== 子智能体委派：运行中 / 已完成 =====
  /** 还在跑的委派 → 独立卡片（带实时输出）。 */
  function activeDelegations(m) {
    return (m?.delegations || []).filter((d) => d.status === 'running')
  }

  /** 已结束的委派 → 归档进工具调用列表，默认收起。 */
  function doneDelegations(m) {
    return (m?.delegations || []).filter((d) => d.status !== 'running')
  }

  return { hasActivity, toolOutcomeText, toolDetailNotice, activityLatest, activityHistory, activeDelegations, doneDelegations }
}
