import { nextTick, ref, watch } from 'vue'

export function scrollSignature(messages) {
  const last = messages.at(-1)
  if (!last) return [messages.length]
  return [messages.length, last.content?.length, last.thinking?.length, last.streaming,
    ...(last.toolCalls || []).flatMap(tool => [tool.id, tool.status, tool.result?.length, tool.progress?.chars]),
    ...(last.delegations || []).flatMap(task => [task.id, task.status, task.stream?.length, task.thinking?.length, task.result?.length, task.tools?.length])]
}

export function useMessageScroll(messages, scroller) {
  // ===== 滚动跟随：贴底才自动跟随，用户一上滑就停手 =====
  //
  // 旧行为是"内容一变就 scrollTop = scrollHeight"，等于和用户抢滚动条：
  // 模型一边吐字，用户想往回看就只能被反复拽到底部（配合 CSS 的
  // `scroll-behavior: smooth`，连动画都在跟用户较劲）。
  //
  // 新策略与聊天软件一致：
  //   - 视口贴着底部（距离 ≤ STICK_THRESHOLD）→ 视为"在看最新"，自动跟随；
  //   - 用户往上滚 → 立即停止跟随，之后再多内容也不动视口，只亮一个"回到底部"；
  //   - 用户自己滚回底部、或点"回到底部" → 恢复跟随。
  // 因此程序化跟随一律用**瞬时**滚动（behavior:'auto'），只有显式跳转才用平滑动画。

  /** 距离底部多少像素以内仍算"贴底"（留一点余量，避免抖动时反复切换） */
  const STICK_THRESHOLD = 56
  /** 是否自动跟随最新内容 */
  const stickToBottom = ref(true)
  /** 不跟随时，下方是否已经有新内容（只用于按钮上提示，不改视口） */
  const hasNewBelow = ref(false)

  function atBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD
  }

  /** 内容区滚动：贴底就恢复跟随，离开底部就停手。 */
  function onMessagesScroll() {
    const el = scroller.value
    if (!el) return
    if (atBottom(el)) {
      stickToBottom.value = true
      hasNewBelow.value = false
    } else if (stickToBottom.value) {
      // 只在"原本在跟随"时停手；已经在看历史就不重复改状态
      stickToBottom.value = false
    }
  }

  /**
   * 滚轮向上：比 scroll 事件更早生效，手势一往下翻就立刻解除跟随，
   * 不会出现"先被拽回底部、再被解除"的一帧抖动。
   * 内容不足一屏（压根没得滚）时不处理。
   */
  function onMessagesWheel(e) {
    const el = scroller.value
    if (!el) return
    const scrollable = el.scrollHeight - el.clientHeight > 4
    if (scrollable && e.deltaY < 0 && stickToBottom.value) {
      stickToBottom.value = false
    }
  }

  /**
   * 滚到底部。
   * @param force 忽略"用户在看历史"的停手状态（切会话、自己发消息、点按钮时用）
   */
  function scrollToBottom(force = false) {
    const el = scroller.value
    if (!el) return
    if (!force && !stickToBottom.value) return
    el.scrollTop = el.scrollHeight
  }

  /** 点"回到底部"：平滑滚下去并恢复跟随。 */
  function jumpToBottom() {
    const el = scroller.value
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    }
    stickToBottom.value = true
    hasNewBelow.value = false
  }

  // 内容变化：还在跟随就滚到底；已经停手就只标记"下面有新内容"
  watch(() => scrollSignature(messages), () => {
    if (stickToBottom.value) nextTick(() => scrollToBottom())
    else hasNewBelow.value = true
  }, { flush: 'post' })

  return { stickToBottom, hasNewBelow, onMessagesScroll, onMessagesWheel, scrollToBottom, jumpToBottom }
}
