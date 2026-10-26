import { onBeforeUnmount, onMounted, ref } from 'vue'

export function useSidebarResize() {
  // ===== 侧边栏宽度：拖拽拉伸 =====
  // 上限不只是常数，还要给主对话区留出足够空间，否则窄窗口一拖就把正文挤没了。
  const SIDEBAR_MIN = 200
  const SIDEBAR_DEFAULT = 260
  const SIDEBAR_MAX = 460
  const SIDEBAR_KEEP_MAIN = 420 // 拖拽时至少给主区留这么多像素
  const SIDEBAR_KEY = 'ah.sidebarWidth'

  const sidebarEl = ref(null)
  const resizing = ref(false)

  function maxSidebarWidth() {
    const avail = (window.innerWidth || 1440) - SIDEBAR_KEEP_MAIN
    return Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, avail))
  }

  function clampSidebar(w) {
    const n = Math.round(Number(w))
    // 非有限值 = 没有有效目标（例如指针拖到窗口外时 clientX 溢出）。
    // 此时保持当前宽度不动，绝不能回落到默认值，否则拖到边缘会突然跳变。
    if (!Number.isFinite(n)) return sidebarWidth.value
    return Math.min(maxSidebarWidth(), Math.max(SIDEBAR_MIN, n))
  }

  // 宽度持久化：刷新后保持用户拖过的宽度
  function readStoredWidth() {
    try {
      const raw = window.localStorage.getItem(SIDEBAR_KEY)
      if (raw === null) return SIDEBAR_DEFAULT
      const n = Number(raw)
      if (!Number.isFinite(n)) return SIDEBAR_DEFAULT // 存储损坏
      return Math.min(maxSidebarWidth(), Math.max(SIDEBAR_MIN, Math.round(n)))
    } catch {
      return SIDEBAR_DEFAULT // 隐私模式等场景下 localStorage 不可用
    }
  }

  const sidebarWidth = ref(readStoredWidth())

  function persistSidebarWidth() {
    try {
      window.localStorage.setItem(SIDEBAR_KEY, String(sidebarWidth.value))
    } catch {
      // 存不了就算了，不影响本次会话内的拖拽
    }
  }

  // 拖拽中：pointermove 挂在 window 上，指针移出手柄也不会丢事件
  let dragStartX = 0
  let dragStartW = 0

  function onResizeMove(e) {
    sidebarWidth.value = clampSidebar(dragStartW + (e.clientX - dragStartX))
  }

  function onResizeEnd() {
    resizing.value = false
    window.removeEventListener('pointermove', onResizeMove)
    window.removeEventListener('pointerup', onResizeEnd)
    window.removeEventListener('pointercancel', onResizeEnd)
    persistSidebarWidth()
  }

  function startResize(e) {
    if (e.button !== undefined && e.button !== 0) return // 只响应左键
    e.preventDefault()
    resizing.value = true
    dragStartX = e.clientX
    dragStartW = sidebarWidth.value
    window.addEventListener('pointermove', onResizeMove)
    window.addEventListener('pointerup', onResizeEnd)
    window.addEventListener('pointercancel', onResizeEnd)
  }

  /** 键盘 ←/→ 微调，配合 role="separator" 让手柄可聚焦可操作。 */
  function nudgeSidebar(delta) {
    sidebarWidth.value = clampSidebar(sidebarWidth.value + delta)
    persistSidebarWidth()
  }

  function resetSidebarWidth() {
    sidebarWidth.value = clampSidebar(SIDEBAR_DEFAULT)
    persistSidebarWidth()
  }

  /** 窗口变窄时把宽度收回合法范围。 */
  function onWindowResize() {
    const next = clampSidebar(sidebarWidth.value)
    if (next !== sidebarWidth.value) sidebarWidth.value = next
  }

  onMounted(() => {
    sidebarWidth.value = clampSidebar(sidebarWidth.value)
    window.addEventListener('resize', onWindowResize)
  })
  onBeforeUnmount(() => {
    window.removeEventListener('resize', onWindowResize)
    onResizeEnd()
  })
  return { SIDEBAR_MIN, SIDEBAR_MAX, sidebarEl, sidebarWidth, resizing, startResize, nudgeSidebar, resetSidebarWidth }
}
