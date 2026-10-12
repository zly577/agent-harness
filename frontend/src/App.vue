<template>
  <!--
    登录门禁。没有引入 vue-router：这个应用只有"未登录/已登录"两种
    顶层形态，用 v-if 切换比引一个路由库更少活动部件，而且天然保证了
    "登录后主界面重新挂载" —— 不会残留上一个用户的会话列表与消息。
  -->
  <div v-if="authBootstrapping" class="auth-boot">正在检查登录状态…</div>
  <AuthView v-else-if="!user" :notice="authNotice" @authed="onAuthed" />
  <div v-else class="app" :class="{ resizing: resizing }">
    <!-- 模型供应商管理弹层。放在最外层，避免被 .app 的布局/层级裁剪 -->
    <ProvidersView
      v-if="showProviders"
      :is-admin="isAdmin"
      @close="showProviders = false"
      @changed="onProvidersChanged"
    />
    <!-- MCP 工具源管理弹层。同样放最外层，避免被 .app 的布局/层级裁剪 -->
    <McpView v-if="showMcp" @close="showMcp = false" />
    <!-- 轻量提示条 -->
    <div v-if="toast.text" class="toast" :class="toast.kind">{{ toast.text }}</div>

    <!-- 侧边栏：会话列表（宽度可拖拽调整） -->
    <aside class="sidebar" ref="sidebarEl" :style="{ width: sidebarWidth + 'px' }">
      <div class="sb-top">
        <button class="sb-new" @click="newChat">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg>
          <span>新对话</span>
        </button>
      </div>

      <nav class="sb-nav">
        <div v-if="loadingSessions" class="sb-hint">载入中…</div>
        <div v-else-if="!sessions.length" class="sb-hint">暂无历史会话</div>
        <div
          v-for="s in sessions"
          :key="s.id"
          class="sb-item"
          :class="{ active: s.id === sessionId }"
          @click="openSession(s.id)"
        >
          <span class="sb-title">{{ s.title || '新会话' }}</span>
          <span v-if="isSessionStreaming(s.id)" class="sb-live" title="后台输出中"></span>
          <button class="sb-del" title="删除会话" @click.stop="deleteSession(s.id)">
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" /></svg>
          </button>
        </div>
      </nav>

      <div class="sb-foot">
        <div class="sb-brand">
          <span class="sb-mark">AH</span>
          <span class="sb-name">Agent Harness</span>
        </div>
        <p class="sb-tip">DeepAgents 通用智能体</p>

        <!-- 当前用户 + 登出 -->
        <div class="sb-user">
          <span class="sb-user-avatar">{{ userInitial }}</span>
          <span class="sb-user-meta">
            <span class="sb-user-name" :title="user?.email">{{ userDisplayName }}</span>
            <span class="sb-user-role">{{ user?.role === 'admin' ? '管理员' : '用户' }}</span>
          </span>
          <button class="sb-user-out" title="退出登录" @click="askLogout">退出</button>
        </div>
      </div>

      <!-- 拖拽手柄：也可聚焦后用 ←/→ 调整 -->
      <div
        class="sb-resizer"
        :class="{ active: resizing }"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整侧边栏宽度"
        :aria-valuenow="sidebarWidth"
        :aria-valuemin="SIDEBAR_MIN"
        :aria-valuemax="SIDEBAR_MAX"
        tabindex="0"
        title="拖动调整宽度（双击复位）"
        @pointerdown="startResize"
        @dblclick="resetSidebarWidth"
        @keydown.left.prevent="nudgeSidebar(-16)"
        @keydown.right.prevent="nudgeSidebar(16)"
      ><span class="sb-resizer-grip"></span></div>
    </aside>

    <!-- 主区 -->
    <div class="main">
      <header class="topbar">
        <div class="topbar-title">
          <span>{{ currentTitle }}</span>
          <span v-if="streaming" class="topbar-live">输出中</span>
        </div>

        <div class="topbar-actions">
          <!-- 模型选择器。第一项"跟随默认"= 不绑供应商，按用户/系统默认解析 -->
          <select
            class="model-select"
            :class="{ unset: !chosenProvider }"
            :value="chosenProvider"
            :disabled="!providers.length"
            :title="modelSelectTitle"
            @change="onProviderChange($event.target.value)"
          >
            <!--
              不再有「跟随默认」这一项（按使用方要求移除）。
              必须**显式**选中一个模型 —— 没有选中时发消息会被拦下并引导去配置，
              而不是悄悄用一个"默认"跑起来。
              disabled 的占位项负责把"还没选"这个状态显示出来。
            -->
            <option v-if="!chosenProvider" value="" disabled>请选择模型</option>
            <option v-for="p in providers" :key="p.id" :value="p.id">
              {{ p.name || p.model }}（{{ p.model }}）
            </option>
          </select>
          <button class="tbtn" title="管理模型供应商（新增/测试/改密钥）" @click="showProviders = true">
            模型
          </button>
          <button class="tbtn" title="接入外部 MCP 工具源（网页抓取/数据库/内部接口等）" @click="showMcp = true">
            工具源
          </button>
          <button class="tbtn" @click="openFilePanel" title="浏览工作区文件">文件</button>
          <!--
            权限模式开关。
            · 「沙箱内」（默认）：文件读写落工作区，execute 进隔离沙箱，
              访问本机真实文件（/mnt/...）会先弹审批。
            · 「完全访问」：execute 直接在这台电脑上跑（deepagents LocalShellBackend），
              本机文件读写都不再审批。
            完全访问是个**危险档位**，所以这里用警示色，并在切换前做二次确认
            （见 toggleAccessMode）；切回沙箱内不需要确认 —— 往安全方向走。
          -->
          <button
            class="tbtn switchable"
            :class="{ danger: accessFull }"
            :disabled="accessBusy || (!accessFull && !accessAllowFull)"
            :title="accessFull
              ? '完全访问：可修改已挂载磁盘，命令由后端 LocalShell 执行；不依赖 OpenSandbox。'
              : '默认模式：工作区可读写，已挂载磁盘只读；代码执行需要 OpenSandbox。'"
            @click="toggleAccessMode"
          >
            <span class="tbtn-dot"></span>
            <span>{{ accessBusy ? '切换中…' : (accessFull ? '完全访问' : '沙箱内') }}</span>
          </button>
          <button
            class="tbtn switchable"
            :class="{ off: !hitlOn || accessFull }"
            :disabled="hitlBusy"
            :title="accessFull
              ? '完全访问模式下不弹审批，这个开关暂时不生效（切回沙箱内后恢复）'
              : (hitlOn
                ? '人工介入已开启：访问本机文件（/mnt 下的真实文件）会先弹窗等你同意'
                : '人工介入已关闭：访问本机文件也不再弹审批窗')"
            @click="toggleHitlBtn"
          >
            <span class="tbtn-dot"></span>
            <span>{{ hitlBusy ? '切换中…' : (hitlOn ? '需要审批' : '自动执行') }}</span>
          </button>
          <button
            class="tbtn switchable"
            :class="{ off: !showTools }"
            title="显示 / 隐藏工具调用详情"
            @click="showTools = !showTools"
          >
            <span class="tbtn-dot"></span>
            <span>{{ showTools ? '工具调用' : '已隐藏工具' }}</span>
          </button>
        </div>
      </header>

      <main
        class="messages"
        ref="scroller"
        @scroll.passive="onMessagesScroll"
        @wheel.passive="onMessagesWheel"
      >
        <div v-if="messages.length <= 1 && !loadingHistory" class="empty">
          <div class="empty-mark">AH</div>
          <h2 class="empty-title">今天想做什么？</h2>
          <p class="empty-sub">可以读写本机文件、跑代码、查资料，也能委派子智能体。</p>

          <!--
            一个模型供应商都没有时给出**明确的下一步**。
            没有这个提示，用户看到的是"发消息没反应/报错"，而真正的原因是
            "还没配模型" —— 这类问题最容易被误判成"服务坏了"。
          -->
          <div v-if="!providersLoading && !providers.length" class="empty-nomodel">
            <p>还没有配置模型供应商，现在发消息会失败。</p>
            <button class="chip" @click="showProviders = true">去配置模型</button>
          </div>

          <div v-else class="chips">
            <button
              v-for="s in suggestions"
              :key="s"
              class="chip"
              @click="useSuggestion(s)"
            >{{ s }}</button>
          </div>
        </div>

        <div v-for="(m, i) in messages" :key="i" class="row" :class="m.role">
          <div class="row-inner">
            <div v-if="m.role === 'user'" class="bubble user">
              <div class="md" v-html="renderMarkdown(m.content)"></div>
            </div>

            <div v-else class="answer">
              <!-- 子智能体委派：运行中的实时卡片（摘要 + 内部流），完成后并入下方工具调用列表 -->
              <div v-if="activeDelegations(m).length" class="delegations">
                <div class="delegations-head">
                  <span class="delegations-title">子智能体</span>
                  <span class="delegations-count">{{ activeDelegations(m).length }} 个运行中</span>
                </div>

                <div
                  v-for="d in activeDelegations(m)"
                  :key="d.id"
                  class="subagent"
                  :class="d.status"
                >
                  <div class="subagent-head">
                    <span class="subagent-pulse"></span>
                    <code class="subagent-name">{{ d.subagent_type || '子智能体' }}</code>
                    <span class="subagent-elapsed" v-if="d.startedAt">{{ elapsedLabel(d.startedAt) }}</span>
                    <span class="subagent-state">{{ d.status === 'running' ? '工作中' : '已结束' }}</span>
                  </div>

                  <!-- 只展示任务摘要；完整 description 收在下面可展开 -->
                  <p class="subagent-summary">{{ d.summary || '正在委派…' }}</p>
                  <details v-if="d.description && d.description !== d.summary" class="subagent-full">
                    <summary>完整任务描述</summary>
                    <div class="md" v-html="renderMarkdown(d.description)"></div>
                  </details>

                  <!-- 子智能体自己的工具调用（实时） -->
                  <div v-if="d.tools && d.tools.length" class="subagent-tools">
                    <span v-for="(t, ti) in d.tools" :key="ti" class="subagent-chip" :class="t.status">
                      {{ t.name }}
                    </span>
                  </div>

                  <!-- 子智能体的思考过程：长推理期间它是唯一会动的信号 -->
                  <details
                    v-if="d.thinking"
                    class="reasoning"
                    :class="{ live: d.status === 'running' && !d.stream }"
                  >
                    <summary>
                      <span class="reasoning-dot"></span>
                      <span>{{ d.status === 'running' && !d.stream ? '思考中' : '思考过程' }}</span>
                      <span class="reasoning-meta">{{ d.thinking.length.toLocaleString() }} 字</span>
                    </summary>
                    <pre class="reasoning-text">{{ truncate(d.thinking, 4000) }}</pre>
                  </details>

                  <!-- 子智能体的输出（实时流式） -->
                  <div v-if="d.stream" class="subagent-stream md" v-html="renderMarkdown(d.stream)"></div>
                  <div v-else-if="d.status === 'running'" class="subagent-wait">
                    <i></i><i></i><i></i>
                    <span class="subagent-wait-text">
                      {{ d.thinking ? '正在思考…' : '正在执行…' }}
                    </span>
                  </div>

                  <div v-if="d.outTokens" class="subagent-usage">
                    子智能体生成 {{ d.outTokens.toLocaleString() }} tokens
                    <span v-if="d.usage && d.usage.reasoning">· 推理 {{ d.usage.reasoning.toLocaleString() }}</span>
                  </div>
                </div>
              </div>

              <!-- 主智能体的思考过程（默认收起；推理期间给实时心跳） -->
              <details
                v-if="m.thinking"
                class="reasoning"
                :class="{ live: m.streaming && !m.content }"
              >
                <summary>
                  <span class="reasoning-dot"></span>
                  <span>{{ m.streaming && !m.content ? '思考中' : '思考过程' }}</span>
                  <span class="reasoning-meta">{{ m.thinking.length.toLocaleString() }} 字</span>
                </summary>
                <pre class="reasoning-text">{{ truncate(m.thinking, 4000) }}</pre>
              </details>

              <div v-if="m.content" class="prose md" v-html="renderMarkdown(m.content)"></div>
              <div v-else-if="m.streaming" class="thinking"><i></i><i></i><i></i></div>

              <!-- 计时器 + 每秒 token：写在大模型返回内容下面，实时刷新 -->
              <div
                v-if="m.startedAt && (m.content || m.streaming || m.thinking)"
                class="meter"
                :class="{ live: m.streaming }"
              >
                <span class="meter-time">
                  <span class="meter-clock"></span>{{ elapsedLabel(m.startedAt) }}
                </span>
                <span class="meter-sep">·</span>
                <span class="meter-rate">{{ rateLabel(m) }}</span>
                <span v-if="meterTokens(m)" class="meter-tokens">{{ meterTokens(m) }}</span>
              </div>

              <!-- 工具活动：**同一时刻只展示最新一条**。
                   上一条一旦被新的取代，就收进下面的折叠列表（默认不展开）。
                   注意：绝对不能给这里的外层 <details> 绑 :open —— 之前那样写会在
                   "运行中 → 完成"之间反复开合整个块，看起来像页面在抽搐。
                   开合只由用户决定。 -->
              <div v-if="hasActivity(m)" class="activity">
                <div v-if="activityLatest(m)" class="act-block">
                  <div class="act" :class="activityLatest(m).status">
                    <span class="act-mark"></span>
                    <!-- 优先显示模型给的"人话"意图（看板的"当前步骤"）。
                         没有（功能关闭/历史数据）时回落到原来的动作文案。
                         ⚠️ intent 是**模型生成的文本**，只能用插值（Vue 自动转义），
                         绝不能接 v-html / renderMarkdown —— 一句
                         `<img src=x onerror=...>` 就是一次 XSS。 -->
                    <code
                      v-if="activityLatest(m).intent"
                      class="act-verb act-intent"
                      :title="activityLatest(m).title"
                    >{{ activityLatest(m).intent }}</code>
                    <code v-else class="act-verb">{{ activityLatest(m).verb }}</code>
                    <span class="act-detail" :title="activityLatest(m).full">{{ activityLatest(m).detail }}</span>
                    <span class="act-badge" :class="activityLatest(m).status">{{ activityLatest(m).badge }}</span>
                  </div>

                  <!-- 跑完之后把"结果"贴在卡片上。
                       用户问的是"到底做了什么、成功没有"，不该只能靠展开详情才发现。 -->
                  <p v-if="activityLatest(m).failHead" class="act-note bad">
                    ✕ {{ activityLatest(m).failHead }}
                  </p>
                  <p v-else-if="activityLatest(m).resultHead" class="act-note">
                    ↳ {{ activityLatest(m).resultHead }}
                  </p>

                  <!-- 回答"它到底还在不在跑"。判据是静默时长，不是卡片状态 ——
                       后端被杀时 SSE 只是安静地断掉，卡片状态不会自己变。 -->
                  <p v-if="runLiveness(m)" class="act-note" :class="runLiveness(m).level">
                    <span>{{ runLiveness(m).level === 'hard' ? '⚠️ ' : '⏳ ' }}</span>
                    <span>{{ runLiveness(m).text }}</span>
                    <button
                      v-if="runLiveness(m).level === 'hard'"
                      class="act-action"
                      @click="reconnect()"
                    >重新连接</button>
                  </p>

                  <!-- 具体执行了什么 —— 命令/参数 + 完整输出，一次点击就能核对 -->
                  <details class="act-call-wrap">
                    <summary>
                      这一步的具体操作（{{ activityLatest(m).items.length }} 次调用）
                    </summary>
                    <div
                      v-for="it in activityLatest(m).items"
                      :key="it.id || it.name"
                      class="act-call"
                    >
                      <div class="act-call-head">
                        <span class="tool-status" :class="it.status"></span>
                        <code class="tool-name">{{ it.name || 'tool' }}</code>
                        <span class="tool-badge" :class="it.status">{{ toolOutcomeText(it) }}</span>
                      </div>
                      <div v-if="it.args && Object.keys(it.args).length" class="tool-section">
                        <div class="tool-label">操作</div>
                        <pre class="tool-code">{{ formatJson(it.args) }}</pre>
                      </div>
                      <div v-if="it.result" class="tool-section">
                        <div class="tool-label">结果</div>
                        <pre class="tool-code">{{ truncate(it.result, 4000) }}</pre>
                      </div>
                    </div>
                  </details>
                </div>

                <!-- 此前的调用：默认不展示，需要时展开 -->
                <details v-if="activityHistory(m).length || doneDelegations(m).length" class="act-history">
                  <summary>
                    <span class="act-history-text">
                      此前的 {{ activityHistory(m).length + doneDelegations(m).length }} 项
                    </span>
                    <svg class="tools-chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M5.5 6.5L8 9l2.5-2.5" /></svg>
                  </summary>

                  <!-- 已完成的子智能体：完整回报在这里 -->
                  <div v-if="doneDelegations(m).length" class="subagent-archive">
                    <div class="tool-group-label">
                      子智能体任务（{{ doneDelegations(m).length }}）
                    </div>
                    <details
                      v-for="d in doneDelegations(m)"
                      :key="d.id"
                      class="tool tool-subagent"
                    >
                      <summary>
                        <span class="tool-status done"></span>
                        <code class="tool-name">{{ d.subagent_type || '子智能体' }}</code>
                        <span class="subagent-summary-inline">{{ d.summary }}</span>
                        <span class="tool-badge">已完成</span>
                      </summary>
                      <div class="tool-detail">
                        <div class="tool-section">
                          <div class="tool-label">任务</div>
                          <div class="md subagent-fulltext" v-html="renderMarkdown(d.description)"></div>
                        </div>
                        <div v-if="d.tools && d.tools.length" class="tool-section">
                          <div class="tool-label">它调用的工具</div>
                          <div class="subagent-tools">
                            <span v-for="(t, ti) in d.tools" :key="ti" class="subagent-chip">{{ t.name }}</span>
                          </div>
                        </div>
                        <div v-if="d.thinking" class="tool-section">
                          <div class="tool-label">思考过程</div>
                          <pre class="tool-code">{{ truncate(d.thinking, 3000) }}</pre>
                        </div>
                        <div v-if="d.stream" class="tool-section">
                          <div class="tool-label">过程输出</div>
                          <pre class="tool-code">{{ truncate(d.stream, 3000) }}</pre>
                        </div>
                        <div v-if="d.result" class="tool-section">
                          <div class="tool-label">回报</div>
                          <div class="md subagent-fulltext" v-html="renderMarkdown(d.result)"></div>
                        </div>
                      </div>
                    </details>
                  </div>

                  <div v-if="activityHistory(m).length" class="tool-group">
                    <div v-for="g in activityHistory(m)" :key="g.key" class="act-item">
                      <div class="act-item-head">
                        <span class="tool-status" :class="g.status"></span>
                        <code v-if="g.intent" class="act-verb act-intent" :title="g.title">{{ g.intent }}</code>
                        <code v-else class="act-verb">{{ g.verb }}</code>
                        <span class="act-detail">{{ g.detail }}</span>
                        <span class="tool-badge" :class="g.status">{{ g.badge }}</span>
                      </div>
                      <!-- 归档里也要能一眼看出"哪一步没成" -->
                      <p v-if="g.failHead" class="act-note bad">✕ {{ g.failHead }}</p>
                      <p v-else-if="g.resultHead" class="act-note">↳ {{ g.resultHead }}</p>
                      <details v-for="it in g.items" :key="it.id || it.name" class="tool">
                        <summary>
                          <code class="tool-name">{{ it.name || 'tool' }}</code>
                        </summary>
                        <div class="tool-detail">
                          <div v-if="it.args && Object.keys(it.args).length" class="tool-section">
                            <div class="tool-label">参数</div>
                            <pre class="tool-code">{{ formatJson(it.args) }}</pre>
                          </div>
                          <div v-if="it.result" class="tool-section">
                            <div class="tool-label">结果</div>
                            <pre class="tool-code">{{ truncate(it.result, 4000) }}</pre>
                          </div>
                        </div>
                      </details>
                    </div>
                  </div>
                </details>
              </div>

              <div v-if="m.error" class="answer-error">本轮出错，可重试或换种说法。</div>
            </div>
          </div>
        </div>
      </main>

      <!-- 审批弹窗：小、紧凑，工具调用只展示内容摘要，按钮永不被长参数挤走。
           注意：不提供"点遮罩关闭"——误关会让这一轮会话悬在待审批状态，必须显式选择同意/拒绝。 -->
      <div v-if="approval" class="overlay">
        <div class="modal approval-modal" role="dialog" aria-modal="true">
          <div class="modal-head">
            <h3>需要你确认</h3>
            <p>
              {{ approval.actions.length > 1
                ? `${approval.actions.length} 个操作等待确认`
                : '该操作会写入本机或工作区，确认后继续' }}
            </p>
            <!-- 并行委派时一次会有多个操作（各子智能体各自的写入）。
                 文案只说明"一次列出、一次按钮对全部生效"，不再提"已处理过的项"：
                 那种陈旧项（langgraph 用 resume map 恢复时不写 per-task 记录，
                 旧中断不消失）已经由后端 `read_pending_approval` 的
                 "interrupts 且 next 非空"判据挡掉了。 -->
            <p v-if="approval.actions.length > 1" class="modal-note">
              并行子任务各自的写入会一次全列出来；「同意 / 拒绝」对所有项一起生效。
            </p>
          </div>

          <div class="modal-body">
            <div v-for="(a, i) in approval.actions" :key="i" class="approve-item">
              <div class="approve-item-head">
                <code class="approve-tool">{{ a.tool || a.type || 'tool_call' }}</code>
                <span v-if="approval.actions.length > 1" class="approve-index">#{{ i + 1 }}</span>
              </div>
              <div v-if="argPairs(a.args).length" class="approve-args">
                <div v-for="p in argPairs(a.args)" :key="p.k" class="approve-arg">
                  <span class="approve-arg-k">{{ p.k }}</span>
                  <span class="approve-arg-v">{{ p.v }}</span>
                </div>
              </div>
              <div v-else class="approve-arg-empty">（无参数）</div>
            </div>
          </div>

          <div class="modal-foot">
            <button class="btn-ghost" :disabled="deciding" @click="decide(false)">拒绝</button>
            <button class="btn-primary" :disabled="deciding" @click="decide(true)">
              {{ deciding ? '处理中…' : '同意' }}
            </button>
          </div>
        </div>
      </div>

      <!-- 文件面板：浏览虚拟文件系统 -->
      <div v-if="showFilePanel" class="overlay" @click.self="showFilePanel = false">
        <div class="modal file-modal">
          <div class="modal-head row-head">
            <h3>工作区文件</h3>
            <div class="modal-head-actions">
              <button class="btn-ghost sm" @click="openWorkspaceFolder">打开目录</button>
              <button class="btn-ghost sm" @click="loadFiles" :disabled="loadingFiles">
                {{ loadingFiles ? '载入中…' : '刷新' }}
              </button>
              <button class="btn-ghost sm" @click="showFilePanel = false">关闭</button>
            </div>
          </div>

          <div class="file-path">
            <nav class="breadcrumb">
              <template v-for="(seg, i) in breadcrumbs" :key="seg.path">
                <span v-if="i > 0" class="crumb-sep">/</span>
                <span
                  class="crumb"
                  :class="{ active: i === breadcrumbs.length - 1, clickable: i < breadcrumbs.length - 1 }"
                  @click="i < breadcrumbs.length - 1 && navigateDir(seg.path)"
                >{{ seg.name }}</span>
              </template>
            </nav>
            <button v-if="canGoUp" class="btn-ghost sm" title="返回上级目录" @click="goParent">上级</button>
          </div>

          <div class="modal-body file-list">
            <div v-if="loadingFiles" class="file-empty">载入中…</div>
            <div v-else-if="!fileList.length" class="file-empty">暂无文件</div>
            <div
              v-for="f in fileList"
              :key="f.path"
              class="file-item"
              :class="{ dir: f.is_dir }"
              @click="f.is_dir ? navigateDir(f.path) : null"
            >
              <span class="file-icon">{{ f.is_dir ? '▸' : '·' }}</span>
              <span class="file-name">{{ f.path.split('/').filter(Boolean).pop() || f.path }}</span>
              <span class="file-size">{{ f.is_dir ? '' : formatSize(f.size) }}</span>
              <button
                v-if="!f.is_dir"
                class="file-dl"
                title="下载到本机"
                @click.stop="downloadFile(f.path)"
              >下载</button>
              <button
                class="file-del"
                :title="f.is_dir ? '删除该目录及其全部内容' : '删除该文件'"
                @click.stop="askDelete(f)"
              >删除</button>
            </div>
          </div>

          <div class="modal-foot tip-only">
            智能体文件位于当前用户的 /workspace；「打开目录」用于部署者本机，远程访问请下载文件。
          </div>
        </div>
      </div>

      <!-- 删除确认：删除不可恢复，所以必须把完整路径摆给用户看，而不是只问"确定吗"。
           目录是**连同内容一起删**，所以单独一句强调。 -->
      <div v-if="pendingDelete" class="overlay">
        <div class="modal confirm-modal" role="dialog" aria-modal="true">
          <div class="modal-head">
            <h3>确认删除</h3>
            <p>{{ pendingDelete.is_dir ? '目录及其中的全部内容都会被删除' : '该文件会被删除' }}，此操作不可恢复。</p>
          </div>

          <div class="modal-body">
            <code class="confirm-path">{{ pendingDelete.path }}</code>
          </div>

          <div class="modal-foot">
            <button class="btn-ghost" :disabled="deleting" @click="pendingDelete = null">取消</button>
            <button class="btn-danger" :disabled="deleting" @click="confirmDelete">
              {{ deleting ? '删除中…' : '删除' }}
            </button>
          </div>
        </div>
      </div>

      <!--
        退出登录确认。
        为什么要确认：退出会清掉本地的会话列表、消息与审批弹窗，误点是"页面突然空了"，
        用户第一反应往往是"数据没了"。用与删除文件同一套确认框样式，保持观感一致。
      -->
      <div v-if="logoutConfirm" class="overlay">
        <div class="modal confirm-modal" role="dialog" aria-modal="true">
          <div class="modal-head">
            <h3>确认退出登录</h3>
            <p>
              退出后需要重新输入邮箱和密码。正在生成的回答会继续，重新登录后可以查看。
            </p>
          </div>

          <div class="modal-body">
            <code class="confirm-path">{{ user?.email }}</code>
          </div>

          <div class="modal-foot">
            <button class="btn-ghost" @click="logoutConfirm = false">取消</button>
            <button class="btn-danger" @click="confirmLogout">退出登录</button>
          </div>
        </div>
      </div>

      <footer class="composer">
        <!-- 回到底部：用户上滑看历史时出现，浮在输入框上方。
             锚在 .composer 上（而不是 .main），这样输入框长高时它会跟着让位，
             不会盖住输入框。有新内容时带一个小圆点提示，但**不抢滚动条**。 -->
        <button
          v-if="!stickToBottom"
          class="jump-bottom"
          :class="{ fresh: hasNewBelow }"
          :title="hasNewBelow ? '下面有新内容' : '回到最新'"
          @click="jumpToBottom"
        >
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3.5v9M4.5 9L8 12.5 11.5 9" /></svg>
          <span>回到底部</span>
          <span v-if="hasNewBelow" class="jump-dot"></span>
        </button>
        <div class="composer-inner">
          <textarea
            ref="inputEl"
            v-model="input"
            placeholder="给 Agent Harness 发消息"
            @keydown.enter.exact.prevent="send"
            @input="autoGrow"
            :disabled="streaming"
            rows="1"
          ></textarea>
          <button v-if="streaming" class="btn-stop" title="停止生成" @click="stopStream">
            <span class="stop-square"></span>
          </button>
          <button
            v-else
            class="btn-send"
            title="发送"
            :disabled="!input.trim()"
            @click="send"
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 15.5V5M10 5l-4.5 4.5M10 5l4.5 4.5" /></svg>
          </button>
        </div>
        <p class="disclaimer">{{ accessFull
          ? '完全访问：可修改已挂载磁盘；LocalShell 在后端所在环境执行命令。'
          : '默认模式：工作区可读写，已挂载磁盘只读；读磁盘时按审批设置确认。' }}</p>
      </footer>
    </div>
  </div>
</template>

<script>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  fetchAccessConfig,
  fetchApproval,
  fetchHitlConfig,
  fetchRunState,
  setAccessConfig,
  setHitlConfig,
  stopRun,
  streamApproval,
  streamChat,
  streamRunAttach,
} from './sse'
import { renderMarkdown } from './markdown'
import AuthView from './AuthView.vue'
import ProvidersView from './ProvidersView.vue'
import McpView from './McpView.vue'
import {
  apiDelete,
  apiFetchRaw,
  apiGet,
  apiPost,
  fetchAvailableProviders,
  fetchMe,
  getUser,
  logout as apiLogout,
  onUnauthorized,
  setSession,
  setUser,
} from './api'

function formatTime() {
  return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

export default {
  components: { AuthView, ProvidersView, McpView },
  setup() {
    // ===== 登录态=====
    // 未登录时整个主界面不挂载（模板里 v-if / v-else），登录后重新挂载，
    // 所以这里不需要"清理上一任用户残留状态"的逻辑 —— 那是最容易出 bug 的做法。
    // 初始值直接取本地缓存：有效的话首屏无闪烁；无效则由 /auth/me 探测清掉。
    const user = ref(getUser())
    const authBootstrapping = ref(true)
    const authNotice = ref('')

    /** 侧边栏展示用的用户名（昵称优先，退回邮箱 @ 前的一段）。 */
    const userDisplayName = computed(() => {
      const u = user.value
      if (!u) return ''
      return u.display_name || (u.email || '').split('@')[0] || u.email || ''
    })

    /** 头像字母：中英文都可（中文取第一个字，英文取首字母大写）。 */
    const userInitial = computed(() => {
      const name = userDisplayName.value.trim()
      return name ? name[0].toUpperCase() : '?'
    })

    // ===== 模型供应商=====
    // `providers`：当前用户**可用**的供应商（系统级 + 自己的，只要启用的）。
    // `chosenProvider`：本会话要用的那个（'' = 跟随默认）。
    const providers = ref([])
    const providersLoading = ref(true)
    const showProviders = ref(false)
    // MCP 工具源弹层。与供应商弹层一样是"点开才挂载"——
    // 挂载时才去拉列表，切账号/登出时组件整个卸载，不会残留上一个用户的数据。
    const showMcp = ref(false)
    const chosenProvider = ref('')
    // 退出登录确认框是否显示
    const logoutConfirm = ref(false)

    const isAdmin = computed(() => (user.value?.role || '') === 'admin')

    /** 选择器 hover 提示（不再有"跟随默认"，所以只在"未选/已选"两种状态间区分）。 */
    const modelSelectTitle = computed(() => {
      if (!providers.value.length) {
        return '还没有配置模型，点右侧「模型」添加一个 —— 不配置就没有模型可用'
      }
      if (!chosenProvider.value) {
        return '还没选择模型：请选一个，否则无法发送消息'
      }
      return '本会话使用的模型；切换后下一条消息生效'
    })

    async function loadProviders() {
      providersLoading.value = true
      try {
        providers.value = await fetchAvailableProviders()
      } catch {
        // 401 已由 api 层统一处理；其它错误就让选择器显示为空（不打断对话）
        providers.value = []
      }
      providersLoading.value = false

      // 选中的供应商不存在了（被删/被停用）→ 先清空，再按下面的规则重新定
      if (chosenProvider.value && !providers.value.some((p) => p.id === chosenProvider.value)) {
        chosenProvider.value = ''
      }
      // 没选过就落到「我自己的默认」。
      // 刻意**只认自己的**（`!p.is_system`）：系统级那条的 is_default 是"全局默认"，
      // 不是这个用户的默认；把它当成我的默认，等于把刚移除的"跟随默认"换个名字又加回来。
      if (!chosenProvider.value) {
        const mine = providers.value.find((p) => p.is_default && !p.is_system)
        if (mine) chosenProvider.value = mine.id
      }
    }

    /**
     * 切换本会话的模型。
     *
     * 只改前端状态 + 记进 sessionStates；**真正的写库发生在下一条消息里**
     * （作为 `provider_id` 随 chat/stream 一起提交）。原因：会话可能还没建
     * （欢迎页），没有 session_id 可写 —— 而"先切个模型再说话"是最自然的操作顺序。
     */
    function onProviderChange(value) {
      chosenProvider.value = value || ''
      const sid = sessionId.value
      if (sid && sessionStates[sid]) sessionStates[sid].providerId = chosenProvider.value
      const name = providers.value.find((p) => p.id === chosenProvider.value)
      notify(name ? `本会话模型：${name.name || name.model}` : '本会话模型：跟随默认')
    }

    function onProvidersChanged() {
      // 增删改之后重新拉一遍；也让会话上的选择跟着校正
      loadProviders()
    }

    /**
     * 这个错误是不是"还没配置模型"？
     *
     * 后端把 `ProviderNotConfigured` 统一转成 **400**（`app/main.py` 的全局异常
     * 处理器），并带上那句可操作的中文。这里据此判定。
     *
     * 为什么还要多看一个"模型"关键字：400 是通用状态码，将来若有别的接口也返回 400，
     * 仅凭状态码会把"真错误"也静默掉 —— 那是更糟的方向（用户什么都看不到）。
     */
    function isModelMissingError(e) {
      return e && e.status === 400 && String(e.message || '').includes('模型')
    }

    /**
     * 把某会话绑定的供应商同步到选择器。
     *
     * 数据来源优先用**会话列表里的 `provider_id`**（后端权威），
     * 本地 sessionStates 只是缓存 —— 后端可能因为供应商被删而回退到默认，
     * 那种情况下界面必须跟着回退，否则用户看到的是"选了 X"但实际在用默认。
     */
    function syncProviderFromSession(id) {
      const row = sessions.value.find((s) => s.id === id)
      const pid = (row && row.provider_id) || ''
      if (pid && providers.value.some((p) => p.id === pid)) {
        chosenProvider.value = pid
        return
      }
      // 该会话没绑定（或绑定的已失效）→ 落到我自己的默认；没有默认就留空，
      // 由发送前的那道校验拦住并引导去配置（而不是替用户"随便选一个"）。
      const mine = providers.value.find((p) => p.is_default && !p.is_system)
      chosenProvider.value = mine ? mine.id : ''
    }

    const sessionId = ref(null)
    const sessions = ref([])
    const loadingSessions = ref(false)
    const loadingHistory = ref(false)
    // 新会话不预置任何消息：空状态由欢迎页承担，不在对话流里塞一条自我介绍
    const messages = reactive([])
    const input = ref('')
    const showTools = ref(true)
    // 待审批弹窗：{ session_id, actions } | null；deciding 表示决策已提交、正在恢复执行
    const approval = ref(null)
    const deciding = ref(false)
    // 人工介入（HITL）热插拔开关：后端为准，这里只做镜像
    const hitlOn = ref(true)
    const hitlBusy = ref(false)
    // 权限模式。'sandbox' = 默认权限（沙箱内）；'full' = 完全访问（本机 shell）。
    // 同样是后端为准、这里只做镜像；后端存的是内存态，所以每次进入界面都要重新拉。
    const accessMode = ref('sandbox')
    const accessBusy = ref(false)
    const accessAllowFull = ref(true)
    const accessFull = computed(() => accessMode.value === 'full')
    // 轻量提示条（不往对话里塞消息，避免污染会话气泡）
    const toast = reactive({ text: '', kind: 'info' })
    let toastTimer = null
    // 计时器心跳（仅在有流式输出时运行）
    let ticker = null
    const scroller = ref(null)

    // 文件面板状态
    const showFilePanel = ref(false)
    const fileList = ref([])
    const filePath = ref('/')
    const loadingFiles = ref(false)

    // 按会话维度管理流式状态：切换会话时原对话在后台继续输出
    // { sessionId: { messages: Array, streaming: boolean, controller: AbortController, loaded: boolean } }
    const sessionStates = reactive({})
    const streaming = computed(() => {
      if (!sessionId.value) return false
      return sessionStates[sessionId.value]?.streaming || false
    })

    function isSessionStreaming(id) {
      // 本地正在流式，或后端那一轮还在跑（切走/刷新后本地不知道，但后端知道）
      if (sessionStates[id]?.streaming) return true
      return !!sessions.value.find((s) => s.id === id)?.running
    }

    // ===== 记住"上次在看的会话" =====
    // 刷新/重新打开后回到原处（而不是掉到欢迎页），也才有可能接回仍在跑的那一轮。
    //
    // key 必须**按用户分区**。否则换个账号登录会去 openSession 上一个用户
    // 留下的 session_id —— 现在后端归属校验会返回 404，用户看到的就是
    // "一进来就报错、会话打不开"，而且很难联想到"这是上一个人的残留"。
    const LAST_SESSION_KEY = 'ah.lastSessionId'
    //: 当前用户的会话 key；未登录时用 'anonymous' 兜底
    let lastSessionKey = `${LAST_SESSION_KEY}:anonymous`

    function useLastSessionKeyFor(user) {
      lastSessionKey = `${LAST_SESSION_KEY}:${user?.id || 'anonymous'}`
    }

    function readLastSession() {
      try {
        return window.localStorage.getItem(lastSessionKey) || null
      } catch {
        return null // 隐私模式等场景下不可用
      }
    }

    watch(sessionId, (id) => {
      try {
        if (id) window.localStorage.setItem(lastSessionKey, id)
        else window.localStorage.removeItem(lastSessionKey)
      } catch {
        // 存不了就算了，只影响"刷新后回到原处"这一便利
      }
    })

    function formatJson(v) {
      if (v === null || v === undefined) return ''
      if (typeof v === 'string') return v
      try {
        return JSON.stringify(v, null, 2)
      } catch {
        return String(v)
      }
    }

    function truncate(s, n) {
      s = String(s ?? '')
      return s.length > n ? `${s.slice(0, n)}\n…（已截断）` : s
    }

    // ===== 顶栏标题 / 空状态建议词 =====
    const suggestions = [
      '看看工作区里有什么文件',
      '写一个 Python 脚本，统计工作区里的代码行数',
      '把一段需求整理成 Markdown 文档存到工作区',
      '委派子智能体调研一个主题',
    ]

    const currentTitle = computed(() => {
      const cur = sessions.value.find((s) => s.id === sessionId.value)
      return cur?.title || '新对话'
    })

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
      tavily_search: '正在联网搜索',
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
        const n = it.name || 'tool'
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
      const tcs = (m?.toolCalls || []).filter((t) => t.kind !== 'subagent')
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
      feed.forEach((g) => {
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

    /**
     * 卡片下方那一行"它到底还在不在跑"的提示。
     *
     * 判据是**距最近一次后端事件的静默时长**，不是"卡片状态"：
     * 后端被杀时 SSE 只是安静地断掉，卡片状态不会自己变，只有静默时长会一路涨上去。
     *
     * 阈值分三档（数字来自实测：正常的长命令/大参数静默可以到几十秒，
     * 而 `llm_stream_chunk_timeout_seconds` 默认 300s 兜"对端静默挂起"）：
     * - < 20s：不提示（正常在跑，卡片上的耗时已经在动了）
     * - 20~90s：如实报"已静默 Ns"，让人知道**接口还活着**，只是还没吐东西
     * - ≥ 90s：明确怀疑失联 —— 后端可能已停止/连接已断开，给一个"重新连接"的入口
     */
    const STALE_SOFT = 20
    const STALE_HARD = 90
    function runLiveness(m) {
      if (!m?.streaming) return null
      const secs = idleSeconds()
      if (secs < STALE_SOFT) return null
      if (secs < STALE_HARD) {
        return { level: 'soft', text: `已 ${Math.round(secs)}s 没有新事件，仍在等待（长命令/大参数可能如此）` }
      }
      return {
        level: 'hard',
        text: `已 ${Math.round(secs)}s 没有收到任何事件 —— 后端可能已停止或连接已断开`,
      }
    }

    /** 最新一条（始终可见，实时刷新状态文案）。 */
    function activityLatest(m) {
      const feed = toolFeed(m)
      return feed.length ? feed[feed.length - 1] : null
    }

    /** 此前的调用：最近的在前，默认不展示。 */
    function activityHistory(m) {
      return toolFeed(m).slice(0, -1).reverse()
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
    function ensureDelegation(msg, id, adoptEmpty = true) {
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
    function applySubagentEvent(msg, evt) {
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
    function applyThinkingEvent(msg, evt) {
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
    function applyUsageEvent(msg, evt) {
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
      // 逐步累加生成本轮真正产出的 token（见 turnTokens 的说明）
      msg.outTokens = (msg.outTokens || 0) + (evt.output || 0)
      if (evt.total) msg.outputTokens = Math.max(msg.outputTokens || 0, evt.output || 0)
    }

    // ===== 计时器 / 每秒 token =====
    const nowTick = ref(Date.now())

    /**
     * 最近一次收到**任何**后端事件的时刻。
     *
     * 用途是回答那个被反复问的问题："它到底还在不在跑？"
     * 只靠"卡片显示进行中"是回答不了的 —— 后端被杀掉时，SSE 只是安静地断掉，
     * 前端再不收事件，卡片就会永远停在"进行中"。所以必须拿"最后一条消息距今多久"
     * 作为判据：短 = 正常等待（长命令/长参数都可能有几十秒静默），
     * 长到不合理 = 大概率已经失联，必须明确告诉用户而不是继续装作在跑。
     */
    const lastEventAt = ref(0)
    /** 是否至少已经收到过一轮事件（避免刚发消息就把"未收到事件"当成异常）。 */
    const sentAt = ref(0)

    /** 距最近一次事件过去了多少秒（无事件时以发出消息的时刻起算）。 */
    function idleSeconds() {
      const base = lastEventAt.value || sentAt.value
      if (!base) return 0
      return Math.max((nowTick.value - base) / 1000, 0)
    }

    /** 秒级刷新的心跳：只在有消息在流式输出时才走。 */
    function startTicker() {
      if (ticker) return
      ticker = setInterval(() => { nowTick.value = Date.now() }, 200)
    }

    function stopTicker() {
      if (ticker) { clearInterval(ticker); ticker = null }
    }

    /** 该消息是否还需要计时（有任意会话仍在流式输出）。 */
    function anyStreaming() {
      if (streaming.value) return true
      return Object.keys(sessionStates).some((id) => sessionStates[id]?.streaming)
    }

    watch(nowTick, () => { if (!anyStreaming()) stopTicker() })

    /**
     * 本轮已产出的**字符数**：主智能体正文 + 主智能体推理 + 各子智能体的正文与推理。
     *
     * 为什么要把推理也算进来：推理阶段是唯一长期没有正文产出的阶段
     * （实测 26.9s），如果只数正文，这段时间速率恒为 0，界面看起来还是死的。
     */
    function turnChars(m) {
      let n = (m.content || '').length + (m.thinking || '').length
      for (const d of m.delegations || []) {
        n += (d.stream || '').length + (d.thinking || '').length
      }
      return n
    }

    /**
     * 本轮**生成**的 token 合计（主智能体 + 各子智能体）。
     *
     * 只累加 `output`（真正生成出来的），**绝不能累加 `total`** ——
     * `total = input + output`，而每个模型步都会把整段上下文（上万 token）重新算一次 input，
     * 多步累加会得到"12 万 token / 1600 tok/s"这种没有意义的数字（实测踩过）。
     */
    function turnTokens(m) {
      let n = m.outTokens || 0
      for (const d of m.delegations || []) n += d.outTokens || 0
      return n
    }

    /**
     * 每秒 token 使用量（实时）。
     *
     * - 精确值来自后端每个模型步的 `usage_metadata.output_tokens`，但**步结束才到**，
     *   所以步内靠字符估算（中文约 1 字 ≈ 1~1.5 token，这里保守按 4 字符≈1 token）。
     * - 两者取较大者：它们都在估同一件事，取大会让数字单调、不出现"越跑越慢"的假象。
     * - 完全靠估算时前面加 `≈`，用真实用量时不加，用户能分清哪个是当真数据。
     */
    function rateLabel(m) {
      const started = m.startedAt
      if (!started) return ''
      // 读 nowTick（而不是 Date.now()）才能让这个数字每 0.2s 重算一次 ——
      // 否则没有新 token 时它会一直停在旧值上，"实时"就无从谈起。
      const secs = Math.max((nowTick.value - started) / 1000, 0.5)
      const exact = turnTokens(m)
      const est = Math.round(turnChars(m) / 4)
      const n = Math.max(exact, est)
      if (!n) return '0.0 tok/s'
      return `${est > exact ? '≈' : ''}${(n / secs).toFixed(1)} tok/s`
    }

    /** token 合计展示：有精确值就用精确值，否则标 ≈ 用估算值。 */
    function meterTokens(m) {
      const exact = turnTokens(m)
      if (exact) return `${exact.toLocaleString()} tokens`
      const est = Math.round(turnChars(m) / 4)
      return est ? `≈${est.toLocaleString()} tokens` : ''
    }

    /** 耗时展示：流式中按 0.2s 刷新，结束后固定。 */
    function elapsedLabel(startedAt) {
      if (!startedAt) return ''
      const end = nowTick.value
      const secs = Math.max((end - startedAt) / 1000, 0)
      if (secs < 60) return `${secs.toFixed(1)}s`
      const min = Math.floor(secs / 60)
      return `${min}m${(secs % 60).toFixed(0)}s`
    }

    // ===== 输入框：自适应高度 / 建议词 / 按钮式开关 =====
    const inputEl = ref(null)

    /** 让 textarea 跟着内容长高（上限由 CSS 的 max-height 兜住）。 */
    function autoGrow() {
      const el = inputEl.value
      if (!el) return
      el.style.height = 'auto'
      el.style.height = `${el.scrollHeight}px`
    }

    /** 点建议词：填入输入框并把光标交回去，不直接发送。 */
    function useSuggestion(text) {
      input.value = text
      nextTick(() => {
        const el = inputEl.value
        if (!el) return
        el.focus()
        autoGrow()
      })
    }

    /** 顶栏的审批开关按钮（原来是 checkbox，现在点按钮切换）。 */
    function toggleHitlBtn() {
      toggleHitl({ target: { checked: !hitlOn.value } })
    }

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

    // ===== 审批弹窗用的参数摘要 =====
    // 只取参数「内容」，并把每项压成单行短文本：长命令 / 长文件内容不能把弹窗撑爆，
    // 否则同意按钮会被挤出可视区、点不到。
    function flattenArgs(args, { maxPairs = 6, maxLen = 160 } = {}) {
      const val = (v) => {
        let s
        if (v === null || v === undefined) s = ''
        else if (typeof v === 'string') s = v
        else if (typeof v === 'number' || typeof v === 'boolean') s = String(v)
        else { try { s = JSON.stringify(v) } catch { s = String(v) } }
        s = s.replace(/\s+/g, ' ').trim()
        if (!s) return '（空）'
        return s.length > maxLen ? `${s.slice(0, maxLen)}…` : s
      }

      const pairs = []
      const push = (k, v) => {
        if (pairs.length < maxPairs) pairs.push({ k, v: val(v) })
      }

      if (args === null || args === undefined) return pairs
      if (typeof args !== 'object') return [{ k: 'value', v: val(args) }]
      if (Array.isArray(args)) {
        args.forEach((item, i) => push(`[${i}]`, item))
      } else {
        Object.keys(args).forEach((k) => push(k, args[k]))
      }

      const total = Array.isArray(args) ? args.length : Object.keys(args).length
      if (total > pairs.length) pairs.push({ k: '…', v: `还有 ${total - pairs.length} 项未显示` })
      return pairs
    }

    function argPairs(args) {
      return flattenArgs(args)
    }

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
      // 任何一个后端事件都算"它还活着"的证据（见 lastEventAt 的注释）
      lastEventAt.value = Date.now()
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
      const payload = { ...evt, session_id: sid }
      if (sid && sessionStates[sid]) sessionStates[sid].approval = payload
      approval.value = payload
      if (target) {
        target.streaming = false
        if (!String(target.content || '').trim()) {
          const n = (evt.actions || []).length
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
    watch(messages, () => {
      if (stickToBottom.value) nextTick(() => scrollToBottom())
      else hasNewBelow.value = true
    }, { deep: true })

    async function loadSessions() {
      loadingSessions.value = true
      try {
        const data = await apiGet('/api/v1/sessions')
        const serverList = Array.isArray(data) ? data : []
        // 保留前端乐观创建的临时项（尚未收到真实 session_id 的新会话）
        const tempItems = sessions.value.filter((s) => s._temp || (typeof s.id === 'string' && s.id.startsWith('temp_')))
        sessions.value = [...tempItems, ...serverList]
      } catch {
        // 忽略网络错误，保留旧列表（401 已由 api 层统一处理为回登录页）
      }
      loadingSessions.value = false
    }

    // ===== 与后端运行"重连" =====
    //
    // 运行跑在后端（见 app/agent/runner.py），跟这个页面没有绑定关系。
    // 所以刷新页面 / 关掉再打开 / 切走再切回来，这里只是"订阅 + 回放"，
    // 不是重新开始 —— 用户看到的仍然是同一轮回答在继续。

    /**
     * 丢掉历史里"本轮尚未完成"的残缺助手消息。
     *
     * 为什么需要：运行途中 checkpoint 里已经存下了这一轮的部分消息（工具调用等），
     * `/messages` 会把它读出来；紧接着回放又会完整重建一遍。不先丢掉就会
     * 出现"半截 + 完整"两份。
     *
     * 判据：本轮用户消息原文（后端随运行一起返回）→ 找到它所在那一行，
     * 后面所有内容都是这一轮的（还没写完的）产物，删掉。
     */
    function dropPartialTurn(msgs, userMessage) {
      if (!userMessage) return false
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === 'user' && msgs[i].content === userMessage) {
          return msgs.splice(i + 1).length > 0
        }
      }
      return false
    }

    /**
     * 用**后端**校验并恢复"待审批"弹窗（刷新 / 切会话后）。
     *
     * 为什么必须单独做：审批中断发生时，这一轮在**后端**就停在那儿等用户点头。
     * 如果那一刻用户不在页面上（刷新、关标签、切走），回来后前端只看历史是
     * 看不出"有东西在等你确认"的 —— 而这一轮又是真的悬着，不会自己往下走。
     *
     * 反向同样重要：**后端说没有待审批时必须把弹窗清掉**。以前这里只负责"弹"，
     * 不负责"收"，于是内存里记着的那个旧弹窗会在切回该会话时重新出现
     * （哪怕它早就处理过、或人工介入已经切到"自动执行"）—— 用户看到的就是
     * "自动执行了还弹审批窗，点同意又说已审批过"。
     */
    async function restorePendingApproval(sid) {
      if (!sid) return
      let info
      try {
        info = await fetchApproval(sid)
      } catch {
        // 后端不可用之类：静默，别在打开会话时弹一堆错
        return
      }
      const pending = !!(info?.pending && (info.actions || []).length)
      if (!pending) {
        if (sessionStates[sid]) sessionStates[sid].approval = null
        if (sessionId.value === sid) approval.value = null
        return
      }
      const payload = { pending: true, actions: info.actions, session_id: sid }
      if (sessionStates[sid]) sessionStates[sid].approval = payload
      approval.value = payload
      notify('有一个操作在等你确认')
    }

    /**
     * 接回该会话正在（或刚刚）进行的回答。
     * @returns {Promise<boolean>} 是否真的接上了一轮
     */
    async function resumeRun(sid, msgs) {
      if (!sid) return false
      const state = sessionStates[sid]
      if (state?.streaming) return false   // 本地已经在看这一轮了
      let run
      try {
        run = await fetchRunState(sid)
      } catch {
        return false   // 后端不可用之类：静默放弃，不打扰用户
      }
      if (!run || run.status === 'idle' || !run.running) return false

      const target = msgs || state?.messages || messages

      // openSession 是把会话消息**拷贝元素**到 `messages` 里的（两个数组不是同一个对象），
      // 所以"删掉半截消息"和"push 接回来的气泡"都必须同步到 `messages` 一次，
      // 否则改动只落在会话数组上，页面纹丝不动（踩过）。
      const syncDisplay = () => {
        if (sessionId.value === sid && messages !== target) {
          messages.splice(0, messages.length, ...target)
        }
      }

      dropPartialTurn(target, run.user_message)
      syncDisplay()

      const controller = new AbortController()
      const fresh = reactive({
        role: 'assistant',
        content: '',
        thinking: '',
        toolCalls: [],
        delegations: [],
        streaming: true,
        time: '',
        // 用后端的开始时间：计时器显示的是整轮真实耗时（包含页面关着的那段）
        startedAt: run.started_at ? run.started_at * 1000 : Date.now(),
        usage: null,
        outTokens: 0,
        outputTokens: 0,
      })
      target.push(fresh)
      syncDisplay()
      startTicker()
      // 起一段新的等待 —— 静默计时从这里重新算（见 lastEventAt）
      sentAt.value = Date.now()
      lastEventAt.value = 0

      if (!sessionStates[sid]) {
        sessionStates[sid] = { messages: target, streaming: true, controller, loaded: true, approval: null }
      } else {
        sessionStates[sid].streaming = true
        sessionStates[sid].controller = controller
        sessionStates[sid].messages = target
      }
      notify('已接回正在进行的回答')

      try {
        await streamRunAttach(sid, controller.signal, (evt) => {
          if (applyDataEvent(fresh, evt)) return
          if (evt.type === 'approval') {
            applyApprovalEvent(fresh, evt, sid)
          } else if (evt.type === 'stopped') {
            fresh.streaming = false
            if (!fresh.content) fresh.content = `（${evt.detail || '已停止'}）`
          } else if (evt.type === 'error') {
            fresh.content = `⚠️ ${evt.detail}`
            fresh.error = true
          }
        })
      } catch (e) {
        if (e.name !== 'AbortError') {
          fresh.content = fresh.content || `⚠️ 接回失败：${e.message}`
        }
      }

      fresh.streaming = false
      fresh.time = formatTime()
      // 与 send() 同一套收尾（工具调用也必须收尾，见 finishTurn 的注释）
      finishTurn(fresh)
      if (sessionStates[sid]) {
        sessionStates[sid].streaming = false
        sessionStates[sid].controller = null
      }
      if (!anyStreaming()) stopTicker()
      loadSessions()
      return true
    }

    /**
     * 失联后的「重新连接」。
     *
     * 为什么不能直接再开一条 `streamRunAttach`：后端被杀掉时，运行已经不存在了，
     * 硬接只会再断一次，用户看到的是"点了没用"。所以顺序是**先问后端事实**：
     * 1. 先把本地这些"卡在进行中"的状态如实收尾（标记已中断），不留假象；
     * 2. `fetchRunState` 问一句"那边到底还有没有一个运行"；
     * 3. 有 → 接回去接着看；没有 → 明确告诉用户"后端没有在跑了"，
     *    而不是继续转圈（用户至少知道该去重启后端了）。
     */
    async function reconnect() {
      const sid = sessionId.value
      if (!sid) return
      const st = sessionStates[sid]
      const msgs = st?.messages || messages
      if (st) st.streaming = false
      const target = msgs[msgs.length - 1]
      if (target && target.role === 'assistant') {
        target.streaming = false
        finishTurn(target)   // 卡在 running 的调用如实标成"已中断"
      }
      let run = null
      try {
        run = await fetchRunState(sid)
      } catch (e) {
        notify(`接不上后端：${e.message}（后端可能已停止，重启后再发消息即可）`, 'error')
        return
      }
      if (run && run.running) {
        const back = await resumeRun(sid, msgs)
        if (!back) notify('后端仍在运行，但这次没接上，刷新页面即可', 'error')
        return
      }
      notify('后端已经没有任何在跑的运行了（多半是后端进程停止了）。重启后端后再发消息即可。')
    }

    /**
     * 兜底：**最后一条**助手消息只有工具调用、没有文字时，给气泡补一句说明。
     *
     * "工具都跑完了却什么都没回我"就来自这里 —— 模型决定调工具的那条 AIMessage
     * 的 content 是空的，于是历史里只剩一个空气泡（外加工具面板）。
     * 刻意只补最后一条、不回头改历史：老会话不该被后续版本重写。
     */
    function noteBlankTail(list) {
      if (!Array.isArray(list) || !list.length) return list
      const last = list[list.length - 1]
      if (!last || last.role !== 'assistant') return list
      if (String(last.content || '').trim()) return list
      const did = (last.toolCalls || []).length + (last.delegations || []).length
      if (!did) return list
      last.content = '（这一轮结束时没有留下文字回复。本轮做过的事见上方工具区；'
        + '产物位置见「文件」面板，可下载。）'
      return list
    }

    async function openSession(id) {
      if (id === sessionId.value) return
      // 临时创建中的会话就是当前新会话，点击不处理
      if (typeof id === 'string' && id.startsWith('temp_')) return
      // 允许在流式输出时切换会话，原对话在后台继续
      sessionId.value = id
      // 换会话先复位跟随状态（新会话永远从"看最新"开始）
      stickToBottom.value = true
      hasNewBelow.value = false

      // 把该会话绑定的模型同步到选择器上。
      // 不这么做的话，切到一个绑了 B 模型的会话、而选择器还显示着 A，
      // 用户会以为"这个会话用的是 A"——界面必须反映真实状态。
      syncProviderFromSession(id)

      // 如果该会话已在内存中（可能正在后台流式输出），直接恢复，不重新加载
      if (sessionStates[id]?.loaded) {
        messages.splice(0, messages.length, ...noteBlankTail(sessionStates[id].messages))
        // 弹窗**不**从内存里照搬：那份记忆可能早就失效（审批已处理 / 已切自动执行），
        // 照搬就是"幽灵弹窗"。一律等下面 restorePendingApproval 拿后端结论。
        approval.value = null
        // 切会话 = 明确想看最新的那一条，强制回到底部并恢复跟随
        await nextTick(() => scrollToBottom(true))
        // 本地没有在跑、但后端还在跑（比如页面刷新过）→ 接回来
        resumeRun(id)
        // 可能正停在一个待审批中断上（审批中断时用户不在页面上）
        restorePendingApproval(id)
        return
      }

      loadingHistory.value = true
      try {
        const data = await apiGet(`/api/v1/sessions/${id}/messages`)
        const list = (Array.isArray(data) ? data : []).map((m) => ({
          role: m.role,
          content: m.content,
          toolCalls: Array.isArray(m.toolCalls) ? m.toolCalls : [],
          delegations: Array.isArray(m.delegations) ? m.delegations : [],
          streaming: false,
          time: '',
        }))
        // 空会话就保持空列表，直接显示欢迎页
        noteBlankTail(list)
        // 存入会话状态，后续切换可直接恢复
        if (!sessionStates[id]) {
          sessionStates[id] = { messages: reactive(list), streaming: false, controller: null, loaded: true, approval: null }
        } else {
          sessionStates[id].messages = reactive(list)
          sessionStates[id].loaded = true
        }
        messages.splice(0, messages.length, ...list)
        // 同上面的理由：弹窗只认后端，不认上一次留下的内存状态
        approval.value = null
      } catch (e) {
        // **"还没配置模型"不该在对话区留一条红字**。
        //
        // 它不是一个"对话出了错"，而是一个"你还没做某件事"的前置条件 ——
        // 把它写成一条助手消息，会让每次打开会话都刷出一句
        // "⚠️ 载入历史失败：你还没有配置模型…"，看起来像历史丢了/系统坏了。
        //
        // 现在改成：这种情况**什么都不显示**，直接落回欢迎页 —— 而欢迎页上本来就有
        // 「还没有配置模型供应商…去配置模型」的引导（见模板里的 .empty-nomodel）。
        // 真正该出现这句话的时机是"用户尝试发消息"（`send()` 的本地校验）。
        //
        // 判据用 HTTP 状态：后端把 ProviderNotConfigured 统一转成 **400**
        // （见 app/main.py 的全局异常处理器）。其它错误（500 等）照旧显示，
        // 否则真出问题时用户会看到一片空白而毫无线索。
        if (!isModelMissingError(e)) {
          messages.splice(0, messages.length, {
            role: 'assistant',
            content: `⚠️ 载入历史失败：${e.message}`,
            toolCalls: [],
            delegations: [],
            streaming: false,
            time: '',
          })
        }
      }
      loadingHistory.value = false
      await nextTick(() => scrollToBottom(true))
      // 历史载入后，接回后端可能仍在进行的这一轮（刷新 / 重新打开页面走这里）
      resumeRun(id)
      // 以及恢复可能悬着的审批弹窗
      restorePendingApproval(id)
    }

    function newChat() {
      // 允许在流式输出时创建新对话，原对话在后台继续
      sessionId.value = null
      // 新会话不继承上一个会话的模型选择，回到"我自己的默认"。
      // 继承的话用户会莫名其妙地用着上一条会话挑的模型，而且看不出来。
      const mine = providers.value.find((p) => p.is_default && !p.is_system)
      chosenProvider.value = mine ? mine.id : ""
      messages.splice(0, messages.length) // 清空 → 回到欢迎页
      approval.value = null
      input.value = ''
      stickToBottom.value = true
      hasNewBelow.value = false    }

    async function deleteSession(id) {
      try {
        await apiDelete(`/api/v1/sessions/${id}`)
      } catch (e) {
        // 404 = 会话不存在或不属于当前用户（后端刻意不区分）；
        // 唯一可信的真相是重新拉列表，所以这里只提示、不阻止后续刷新。
        notify(`删除失败：${e.message}`, 'error')
        await loadSessions()
        return
      }
      if (sessionId.value === id) newChat()
      await loadSessions()
    }

    async function send() {
      const text = input.value.trim()
      if (!text || streaming.value) return

      // 没有可用的模型就**不要发出去**。
      // 以前会把请求发上去、由后端回一句"尚未配置供应商"，用户还得自己猜去哪儿配；
      // 现在在本地就拦住，并直接把人带到配置界面 —— 输入框内容保持不动，不白打一遍。
      if (!chosenProvider.value) {
        notify(
          providers.value.length
            ? '请先选择要使用的模型'
            : '还没有配置模型，去「模型」里添加一个才能对话',
          'error',
        )
        showProviders.value = true
        return
      }
      input.value = ''

      const controller = new AbortController()
      const startingSessionId = sessionId.value // 新会话时为 null
      let resolvedSessionId = startingSessionId

      // 新会话：乐观创建侧边栏项（不等后端返回 session_id）
      let tempId = null
      if (startingSessionId === null) {
        tempId = `temp_${Date.now()}`
        const shortTitle = text.length > 30 ? text.slice(0, 30) + '…' : text
        sessions.value.unshift({
          id: tempId,
          title: shortTitle,
          status: 'active',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          _temp: true,
        })
      }

      // 该会话的独立消息数组（固定引用，切换会话不影响）
      let sessionMsgs
      if (startingSessionId && sessionStates[startingSessionId]) {
        sessionMsgs = sessionStates[startingSessionId].messages
      } else {
        // 新会话：创建独立数组，复制当前 messages 的元素引用（保留 greeting）
        sessionMsgs = reactive([...messages])
      }

      const userMsg = { role: 'user', content: text, time: formatTime() }
      const assistantMsg = reactive({
        role: 'assistant',
        content: '',
        thinking: '',
        toolCalls: [],
        delegations: [],
        streaming: true,
        time: '',
        // 计时器与 token 统计
        startedAt: Date.now(),
        usage: null,
        outTokens: 0,      // 本轮累计生成的 token（见 turnTokens）
        outputTokens: 0,
      })
      startTicker()
      // 静默计时从这里起算 —— 有了它，"后端一声不响"才可能被识别出来
      sentAt.value = Date.now()
      lastEventAt.value = 0

      sessionMsgs.push(userMsg)
      sessionMsgs.push(assistantMsg)

      // 当前正显示该会话时，同步到 messages（共享对象引用，后续修改自动同步）
      if (sessionId.value === startingSessionId) {
        messages.push(userMsg)
        messages.push(assistantMsg)
        // 自己发消息 = 明确要看最新：复位跟随并强制贴底（哪怕刚在翻历史）
        stickToBottom.value = true
        hasNewBelow.value = false
        await nextTick(() => scrollToBottom(true))
      }

      // 标记该会话为流式中
      if (startingSessionId) {
        if (!sessionStates[startingSessionId]) {
          sessionStates[startingSessionId] = { messages: sessionMsgs, streaming: true, controller, loaded: true, approval: null }
        } else {
          sessionStates[startingSessionId].streaming = true
          sessionStates[startingSessionId].controller = controller
        }
      }

      try {
        await streamChat(text, startingSessionId, controller.signal, (evt) => {
          if (evt.type === 'session') {
            resolvedSessionId = evt.session_id
            // 只有用户还停留在新会话页时才切换 sessionId，不把已切走的用户拉回来
            if (sessionId.value === null) {
              sessionId.value = evt.session_id
            }
            // 把侧边栏的乐观临时项替换为真实 session_id
            if (tempId) {
              const idx = sessions.value.findIndex((s) => s.id === tempId)
              if (idx !== -1) {
                sessions.value[idx] = { ...sessions.value[idx], id: evt.session_id, _temp: false }
              }
            }
            if (!sessionStates[resolvedSessionId]) {
              sessionStates[resolvedSessionId] = { messages: sessionMsgs, streaming: true, controller, loaded: true, approval: null }
            } else {
              sessionStates[resolvedSessionId].streaming = true
              sessionStates[resolvedSessionId].controller = controller
              sessionStates[resolvedSessionId].messages = sessionMsgs
            }
          } else if (applyDataEvent(assistantMsg, evt)) {
            // 数据类事件（token/thinking/工具/子智能体/用量/result）已统一处理
          } else if (evt.type === 'approval') {
            const sid = resolvedSessionId || startingSessionId
            applyApprovalEvent(assistantMsg, evt, sid)
          } else if (evt.type === 'stopped') {
            // 用户点了停止：不是错误，如实说明即可
            assistantMsg.streaming = false
            if (!assistantMsg.content) assistantMsg.content = `（${evt.detail || '已停止'}）`
          } else if (evt.type === 'error') {
            assistantMsg.content = `⚠️ ${evt.detail}`
            assistantMsg.error = true
          }
        }, chosenProvider.value || null)
      } catch (e) {
        if (e.name !== 'AbortError') {
          assistantMsg.content = `⚠️ ${e.message}`
          assistantMsg.error = true
        }
      }

      assistantMsg.streaming = false
      assistantMsg.time = formatTime()
      // 收尾：仍在"运行中"的**工具调用**与委派都要标记为结束。
      // 🔥 以前这里只收尾了 delegations，toolCalls 被漏掉 ⇒ 流一旦异常结束
      // （后端被杀、网络断），工具卡就永远停在"进行中"，看起来像还在跑。
      finishTurn(assistantMsg)

      // 清除该会话的流式标记
      if (resolvedSessionId && sessionStates[resolvedSessionId]) {
        sessionStates[resolvedSessionId].streaming = false
        sessionStates[resolvedSessionId].controller = null
      }
      if (!anyStreaming()) stopTicker()

      loadSessions() // 刷新侧边栏（新会话标题/更新时间）
    }

    async function decide(approve) {
      if (deciding.value) return // 正在处理：忽略重复点击
      const info = approval.value
      if (!info) return
      const sid = info.session_id || sessionId.value
      if (!sid) return

      deciding.value = true
      const state = { done: false, error: null, newApproval: false }
      // 注意：从这里到函数末尾**任何**抛出都必须复位 deciding，
      // 否则按钮会永久停在 disabled 的"处理中…"上 —— 表现就是"点什么都无效"。
      try {
        const targetMsgs = sessionStates[sid]?.messages || messages
        const target = currentAssistantMessage(targetMsgs)

        // 点了就立刻关掉弹窗：恢复执行的过程实时出现在对话里，不再挡住页面。
        // 该会话同时进入"生成中"，避免用户在恢复过程中重复操作。
        if (sessionStates[sid]) sessionStates[sid].approval = null
        approval.value = null

        const controller = new AbortController()
        if (!sessionStates[sid]) {
          sessionStates[sid] = { messages: targetMsgs, streaming: true, controller, loaded: true }
        } else {
          sessionStates[sid].streaming = true
          sessionStates[sid].controller = controller
        }
        target.streaming = true
        if (!target.startedAt) target.startedAt = Date.now()
        startTicker()
        // 审批恢复也是一段新的等待，静默计时重算
        sentAt.value = Date.now()
        lastEventAt.value = 0

        try {
          await streamApproval(sid, approve ? 'approve' : 'reject', controller.signal, (evt) => {
            if (evt.type === 'done') {
              state.done = true
            } else if (applyDataEvent(target, evt)) {
              // 数据类事件已统一处理
            } else if (evt.type === 'approval') {
              // 恢复过程中又挂了新的审批（例如模型接着要改另一个文件）
              state.newApproval = true
              applyApprovalEvent(target, evt, sid)
            } else if (evt.type === 'stopped') {
              target.content = target.content || `（${evt.detail || '已停止'}）`
            } else if (evt.type === 'error') {
              state.error = evt.detail || '未知错误'
            }
          })
        } catch (e) {
          if (e.name !== 'AbortError') state.error = e.message
        }

        target.streaming = false
        target.time = formatTime()
        // 与 send() 同一套收尾
        finishTurn(target)
        if (sessionStates[sid]) {
          sessionStates[sid].streaming = false
          sessionStates[sid].controller = null
        }

        if (state.error) {
          if (/正在处理中/.test(state.error)) {
            // 已经提交过、正在跑：不报失败，只提示 + 拉一次最新历史
            if (!state.newApproval) {
              target.content = target.content || '（该审批已提交，正在同步最新结果…）'
            }
            notify('该审批已提交过，正在同步最新结果…')
            refreshSession(sid)
          } else if (/无待审批项/.test(state.error)) {
            // 这条审批**已经不在了**：可能刚被处理掉，也可能人工介入已切到「自动执行」
            // （那次切换会把挂起的中断统一用"拒绝"收尾，见后端 clear_pending_interrupt）。
            // 重点是**把弹窗关掉并同步**：旧实现只提示"已审批过"却留着弹窗，
            // 用户于是反复点同一个按钮，观感就是"点了没用"。
            approval.value = null
            if (sessionStates[sid]) sessionStates[sid].approval = null
            if (!state.newApproval) {
              target.content = target.content || '（这条审批已失效，正在同步最新结果…）'
            }
            notify('这条审批已失效（已处理过，或人工介入已切到自动执行）')
            refreshSession(sid)
          } else {
            target.content = `⚠️ 审批失败：${state.error}`
            target.error = true
            // 仍未决：把弹窗放回来，用户可以直接重试
            if (!state.newApproval && info) {
              approval.value = info
              if (sessionStates[sid]) sessionStates[sid].approval = info
            }
            // 同时给一条明显提示：错误写在对话气泡里时，用户很可能看不到
            // （气泡在视口外），于是以为"点了没反应"。
            notify(`审批失败：${state.error}`, 'error')
          }
        }
      } finally {
        deciding.value = false
        loadSessions()
      }
    }

    /** 静默重载某会话历史（用于审批状态与后端不一致时的兜底同步）。 */
    async function refreshSession(id) {
      try {
        const data = await apiGet(`/api/v1/sessions/${id}/messages`)
        const list = (Array.isArray(data) ? data : []).map((m) => ({
          role: m.role,
          content: m.content,
          toolCalls: Array.isArray(m.toolCalls) ? m.toolCalls : [],
          delegations: Array.isArray(m.delegations) ? m.delegations : [],
          streaming: false,
          time: '',
        }))
        if (!list.length) return
        noteBlankTail(list)
        const state = sessionStates[id]
        if (state) {
          state.messages = reactive(list)
          state.loaded = true
          state.streaming = false
          state.approval = null
          if (id === sessionId.value) messages.splice(0, messages.length, ...list)
        }
      } catch {
        // 忽略：保留当前画面
      }
    }

    /** 轻量提示：2.6 秒后自动消失。 */
    function notify(text, kind = 'info') {
      toast.text = text
      toast.kind = kind
      if (toastTimer) clearTimeout(toastTimer)
      toastTimer = setTimeout(() => { toast.text = '' }, 2600)
    }

    async function loadHitlConfig() {
      try {
        const cfg = await fetchHitlConfig()
        hitlOn.value = !!cfg.enabled
      } catch {
        // 读取失败就保持默认（不影响对话）
      }
    }

    /**
     * 读取权限模式。
     *
     * 读取失败时**保持默认（沙箱内）**而不是显示"完全访问" —— 界面上的显示会直接
     * 影响用户对"命令跑在哪、会不会弹审批"的判断，宁可显示保守值。
     */
    async function loadAccessConfig() {
      try {
        const cfg = await fetchAccessConfig()
        accessMode.value = cfg.mode === 'full' ? 'full' : 'sandbox'
        accessAllowFull.value = (cfg.valid_modes || []).includes('full')
      } catch {
        // 读取失败就保持默认（沙箱内）
      }
    }

    /**
     * 切换权限模式。
     *
     * 切到「完全访问」**必须二次确认**：它意味着之后所有 shell 命令都会直接在这台电脑上
     * 执行、并且不再有任何审批弹窗，误触的代价是"下一轮对话就开始动真格"。
     * 切回「沙箱内」不需要确认（往安全方向走，不需要拦）。
     *
     * 后端会重建该用户的 Agent 缓存，所以新模式**下一轮对话**才生效；
     * 正在跑的那一轮不受影响（权限是编译期输入，见 backend/app/agent/access.py）。
     */
    async function toggleAccessMode() {
      const next = accessFull.value ? 'sandbox' : 'full'
      if (next === 'full') {
        const ok = window.confirm(
          '切换到「完全访问」后：\n' +
          '· 可以修改和删除所有已挂载磁盘中的文件，不再弹审批\n' +
          '· 命令在后端 LocalShell 执行，不需要 OpenSandbox\n' +
          '· Docker 部署使用容器里的 Linux shell，并非 Windows 系统 shell\n' +
          '· 后端重启会自动切回「沙箱内」\n' +
          '· 新模式从下一轮对话开始生效\n\n确定切换吗？',
        )
        if (!ok) return
      }
      accessBusy.value = true
      try {
        const cfg = await setAccessConfig(next)
        accessMode.value = cfg.mode === 'full' ? 'full' : 'sandbox'
        // 切到完全访问时后端会放弃收尾挂起的审批：那些悬着的轮次刚补上了回复，
        // 立刻重载历史，否则用户看到的还是"什么都不说的半轮"（同 toggleHitl）。
        for (const id of cfg.cleared || []) {
          if (sessionStates[id]) refreshSession(id)
        }
        notify(
          accessMode.value === 'full'
            ? '已切换到完全访问：磁盘可修改，LocalShell 执行命令'
            : '已切回默认模式：磁盘只读，工作区可读写',
          accessMode.value === 'full' ? 'error' : 'info',
        )
      } catch (e) {
        notify(`切换权限模式失败：${e.message}`, 'error')
      }
      accessBusy.value = false
    }

    async function toggleHitl(evt) {
      const next = !!evt?.target?.checked
      // 先把界面切过去（乐观更新）：等接口返回才变的话，标签会"慢半拍"。
      hitlOn.value = next
      hitlBusy.value = true
      try {
        if (!next) {
          // 收尾交给**后端**：它掌握"哪些会话真的挂着中断"（含前端内存里没有的那些，
          // 也含关闭后因重建而读不出来的那些）。前端以前只按自己的内存清单逐个
          // POST 拒绝，漏掉的会话之后会在弹窗里"复活"。
          // 这里只负责把界面上记着的弹窗全部清掉。
          for (const id of Object.keys(sessionStates)) {
            if (sessionStates[id]) sessionStates[id].approval = null
          }
          approval.value = null
        }

        const cfg = await setHitlConfig(next)
        hitlOn.value = !!cfg.enabled
        // 被放弃收尾的会话：那些悬着的轮次刚刚补上了回复，立刻重载历史，
        // 否则用户看到的还是"什么都不说的半轮"（后端已写好，界面却停在旧快照）。
        for (const id of cfg.cleared || []) {
          if (sessionStates[id]) refreshSession(id)
        }
      } catch (e) {
        hitlOn.value = !next // 失败就回滚开关显示
        notify(`切换人工介入失败：${e.message}`, 'error')
      }
      hitlBusy.value = false
    }

    /**
     * 停止本轮回答（用户明确点了停止按钮）。
     *
     * 关键语义：**这是唯一会真正停掉后端运行的方式**（另一条是关掉服务）。
     * 刷新页面 / 关标签页只是断开连接 —— 运行会继续跑到完，回来还能接上。
     * 所以这里不能只 abort() 本地请求，必须显式调后端接口。
     */
    async function stopStream() {
      const sid = sessionId.value
      if (!sid) return
      const state = sessionStates[sid]
      try {
        await stopRun(sid)
      } catch (e) {
        notify(`停止失败：${e.message}`, 'error')
        return
      }
      // 后端会推一条 stopped 事件并以 done 收尾，本地流随后自然结束；
      // 但用户可能希望立刻看到反馈，这里先把状态标上。
      if (state?.messages) {
        const last = state.messages[state.messages.length - 1]
        if (last && last.role === 'assistant' && last.streaming) {
          last.streaming = false
          if (!last.content) last.content = '（已停止本轮回答）'
        }
      }
    }

    function stopAllStreams() {
      // 页面卸载时停止所有会话的流式输出
      for (const id of Object.keys(sessionStates)) {
        if (sessionStates[id]?.controller) {
          sessionStates[id].controller.abort()
        }
      }
    }

    // ===== 文件面板 =====
    function formatSize(bytes) {
      if (bytes === 0 || bytes == null) return ''
      if (bytes < 1024) return `${bytes} B`
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    }

    async function loadFiles() {
      loadingFiles.value = true
      try {
        const data = await apiGet(`/api/v1/files?path=${encodeURIComponent(filePath.value)}`)
        fileList.value = Array.isArray(data?.entries) ? data.entries : []
      } catch {
        fileList.value = []
      }
      loadingFiles.value = false
    }

    async function openWorkspaceFolder() {
      try {
        const result = await apiPost('/api/v1/files/open-workspace', {})
        if (result.open_url) {
          window.location.href = result.open_url
          notify('请允许浏览器打开工作区目录；此入口用于部署者本机。', 'info')
        } else {
          notify('已打开当前用户的工作区目录', 'info')
        }
      } catch (e) {
        notify(`无法打开目录：${e.message}`, 'error')
      }
    }

    // 用户视角的工作区根。虚拟根 '/' 只是文件系统顶层（内容与 /workspace 同一份，
    // 因为 deepagents 的 route 命中后会剥前缀，两者必然互为别名），
    // 所以面板只把 '/workspace' 当作用户工作区展示，避免同一批文件出现两次。
    const WORKSPACE_ROOT = '/workspace'

    function openFilePanel() {
      showFilePanel.value = true
      filePath.value = WORKSPACE_ROOT
      loadFiles()
    }

    function navigateDir(path) {
      // 后端 ls 对 '/skills/' 与 '/skills' 都兼容；前端统一存无尾斜杠规范形，便于面包屑/上级计算
      filePath.value = normalizePath(path)
      loadFiles()
    }

    // 目录切换：返回上一级（filePath 规范为无尾斜杠，'/' 表示根）
    function normalizePath(p) {
      if (!p || p === '/') return '/'
      const norm = String(p).replace(/\/+$/, '')
      return norm || '/'
    }

    // 面包屑：从工作区根开始，不再显示虚拟根；'/workspace/x' → [工作区, x]
    const breadcrumbs = computed(() => {
      const norm = normalizePath(filePath.value)
      if (norm === WORKSPACE_ROOT || norm === '/') return [{ name: '工作区', path: WORKSPACE_ROOT }]
      const segs = [{ name: '工作区', path: WORKSPACE_ROOT }]
      const rest = norm.startsWith(WORKSPACE_ROOT + '/') ? norm.slice(WORKSPACE_ROOT.length) : norm
      let acc = WORKSPACE_ROOT
      for (const part of rest.split('/').filter(Boolean)) {
        acc += `/${part}`
        segs.push({ name: part, path: acc })
      }
      return segs
    })

    // 不允许退到工作区根之上（虚拟根会让同一批文件重复出现）
    const canGoUp = computed(() => {
      const norm = normalizePath(filePath.value)
      return norm !== WORKSPACE_ROOT && norm !== '/'
    })

    function goParent() {
      const norm = normalizePath(filePath.value)
      if (!canGoUp.value) return
      const idx = norm.lastIndexOf('/')
      const parent = idx <= 0 ? WORKSPACE_ROOT : norm.slice(0, idx)
      navigateDir(parent.length < WORKSPACE_ROOT.length ? WORKSPACE_ROOT : parent)
    }

    async function downloadFile(path) {
      // ⚠️ **不能**再写成 `<a href="/api/v1/files/download?...">` 直接跳转。
      // 浏览器对 <a> 的导航是"裸请求"，不会带上我们注入的 Authorization 头 ——
      // 认证上线后这种写法必定 401，而表现是"点了下载什么也没发生"（最难查的一类）。
      // 改成先带凭证把字节流取回来，再用 blob URL 触发保存；顺带能给出真实失败原因。
      const name = path.split('/').pop() || 'download'
      try {
        const resp = await apiFetchRaw(`/api/v1/files/download?path=${encodeURIComponent(path)}`)
        const blob = await resp.blob()
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = name
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        // 立刻 revoke 有些浏览器会来不及读完；延后释放即可
        setTimeout(() => URL.revokeObjectURL(url), 10000)
      } catch (e) {
        notify(`下载失败：${e.message}`, 'error')
      }
    }

    // ===== 删除（文件面板）=====
    // 删除不可恢复，所以"先确认再动手"：确认框里摆出**完整路径**（而不是只问"确定吗"），
    // 目录再额外强调"连同内容一起删"。真正的请求在 confirmDelete 里。
    const pendingDelete = ref(null) // { path, is_dir } | null
    const deleting = ref(false)

    function askDelete(item) {
      if (!item?.path) return
      pendingDelete.value = { path: item.path, is_dir: !!item.is_dir }
    }

    async function confirmDelete() {
      const info = pendingDelete.value
      if (!info || deleting.value) return
      deleting.value = true
      try {
        await apiDelete(`/api/v1/files?path=${encodeURIComponent(info.path)}`)
        pendingDelete.value = null
        notify(`已删除 ${info.path.split('/').filter(Boolean).pop() || info.path}`)
        await loadFiles()
      } catch (e) {
        notify(`删除失败：${e.message}`, 'error')
      } finally {
        deleting.value = false
      }
    }

    // ===== 登录态生命周期=====

    /**
     * 进入已登录状态后要做的事。
     *
     * 单列一个函数是因为它有三个入口：启动时 token 仍有效、登录成功、
     * 以及（将来）切换用户 —— 三处都必须做同样的事，写两遍必然漏一处。
     */
    async function startAuthedSession() {
      useLastSessionKeyFor(user.value)
      await loadHitlConfig()
      // 权限模式也要在进入界面时拉一次（后端是内存态，重启后回到沙箱内；
      // 不拉的话界面会继续显示上一次的"完全访问"，与后端实际状态不符）。
      await loadAccessConfig()
      // 先把可用供应商拉出来 —— 会话列表里要显示/恢复各自的模型绑定，
      // 而且"一个都没有"时欢迎页要给引导。
      await loadProviders()
      // 刷新 / 关掉再打开：回到上次在看的会话。
      // 如果那一轮回答还在后端跑着，openSession 里会自动接回来 —— 这就是
      // "刷新不打断回答"的用户可见效果。
      await loadSessions()
      const last = readLastSession()
      if (last && sessions.value.some((s) => s.id === last)) {
        openSession(last)
      }
    }

    /** 登录成功后由 AuthView 触发。 */
    async function onAuthed(payload) {
      // api.js 在 login() 里已经写过 localStorage，这里同步内存态即可
      user.value = payload.user
      setSession(payload.token, payload.user)
      authNotice.value = ''
      await startAuthedSession()
    }

    /**
     * 点「退出」时先弹确认框（不直接退）。
     *
     * 拆成 askLogout / confirmLogout 两个函数，是为了让"弹确认"和"真的退出"
     * 在代码上也是两件事 —— 401 自动回登录页那条路径（`handleUnauthorized`）
     * 必须**直接**清状态，不能弹框问用户（凭证都失效了还问什么）。
     */
    function askLogout() {
      logoutConfirm.value = true
    }

    function confirmLogout() {
      logoutConfirm.value = false
      doLogout()
    }

    /** 退出登录：清凭证 → 回到登录视图。 */
    function doLogout() {
      // 停掉本地所有流（后端的运行不受影响，这是的语义：连接断了它继续跑）
      stopAllStreams()
      sessionId.value = null
      sessions.value = []
      messages.splice(0, messages.length)
      approval.value = null
      resetProviderState()
      apiLogout()
      user.value = null
    }

    /**
     * 清掉模型相关的本地状态。
     *
     * 登出时必须做：否则换个账号登录，选择器里还留着**上一个用户**的供应商 id
     * （虽然发请求时后端会因为权限查不到而回退，但界面上显示着别人的配置，
     * 这本身就是个不该出现的状态）。
     */
    function resetProviderState() {
      providers.value = []
      chosenProvider.value = ''
      showProviders.value = false
      // MCP 工具源弹层也要一起关（同供应商，换账号后不能留着上一个用户的界面）
      showMcp.value = false
    }

    /** 凭证失效（任意请求 401）→ 回登录页并说明原因。 */
    function handleUnauthorized(detail) {
      if (!user.value) return // 本来就没登录，不用再"登出一次"
      stopAllStreams()
      sessionId.value = null
      sessions.value = []
      messages.splice(0, messages.length)
      approval.value = null
      resetProviderState()
      user.value = null
      authNotice.value = detail || '登录已失效，请重新登录'
    }

    let stopAuthWatch = null

    onMounted(async () => {
      // 401 的统一出口：任何一个请求（含 SSE 流里读到的）失效，
      // 都在这里切回登录视图 —— 不需要每个调用点自己处理。
      stopAuthWatch = onUnauthorized(handleUnauthorized)

      // 初始宽度可能超过当前窗口允许值，先夹一次
      sidebarWidth.value = clampSidebar(sidebarWidth.value)
      window.addEventListener('resize', onWindowResize)

      // 启动探测：本地有 token 就拿 /auth/me 验一次（防止"看着像登录了，
      // 其实 token 早过期"，那种状态会让首屏到处报错）。
      if (user.value) {
        try {
          const me = await fetchMe()
          user.value = me
          setUser(me)
        } catch {
          // 失效：api 层已清掉本地凭证，这里只需回到登录视图
          user.value = null
        }
      }
      authBootstrapping.value = false

      if (user.value) {
        await startAuthedSession()
      }
    })
    onBeforeUnmount(() => {
      if (stopAuthWatch) stopAuthWatch()
      stopAllStreams()
      stopTicker()
      window.removeEventListener('resize', onWindowResize)
      // 卸载时若还在拖拽，清掉挂在 window 上的监听，避免泄漏
      onResizeEnd()
    })

    return {
      // 登录态
      user,
      authBootstrapping,
      authNotice,
      onAuthed,
      askLogout,
      confirmLogout,
      logoutConfirm,
      doLogout,
      userDisplayName,
      userInitial,
      isAdmin,
      // 模型供应商
      providers,
      providersLoading,
      showProviders,
      showMcp,
      chosenProvider,
      modelSelectTitle,
      onProviderChange,
      onProvidersChanged,
      sessionId,
      sessions,
      loadingSessions,
      loadingHistory,
      messages,
      input,
      streaming,
      showTools,
      approval,
      deciding,
      hitlOn,
      hitlBusy,
      toast,
      scroller,
      send,
      decide,
      stopStream,
      openSession,
      newChat,
      deleteSession,
      renderMarkdown,
      formatJson,
      truncate,
      argPairs,
      isSessionStreaming,
      // 新版界面
      suggestions,
      currentTitle,
      hasActivity,
      // 工具活动：只展示最新一条
      activityLatest,
      activityHistory,
      activeDelegations,
      doneDelegations,
      elapsedLabel,
      // 运行可见性（成败 / 实时耗时 / 失联判定 / 重新连接）
      toolOutcomeText,
      runLiveness,
      reconnect,
      rateLabel,
      meterTokens,
      // 滚动跟随
      stickToBottom,
      hasNewBelow,
      onMessagesScroll,
      onMessagesWheel,
      jumpToBottom,
      inputEl,
      autoGrow,
      useSuggestion,
      toggleHitlBtn,
      // 权限模式（沙箱内 / 完全访问）
      accessFull,
      accessBusy,
      toggleAccessMode,
      // 侧边栏拖拽
      sidebarEl,
      sidebarWidth,
      resizing,
      SIDEBAR_MIN,
      SIDEBAR_MAX,
      startResize,
      nudgeSidebar,
      resetSidebarWidth,
      // 文件面板
      showFilePanel,
      fileList,
      loadingFiles,
      openFilePanel,
      openWorkspaceFolder,
      accessAllowFull,
      loadFiles,
      downloadFile,
      // 删除（文件面板）
      pendingDelete,
      deleting,
      askDelete,
      confirmDelete,
      navigateDir,
      breadcrumbs,
      canGoUp,
      goParent,
      formatSize,
    }
  },
}
</script>

<style>
/* =========================================================================
   Agent Harness — UI 设计系统
   参考 Codex Web / chatgpt.com：中性灰阶、宽留白、无气泡纯文本排版、
   极细边框、等宽字体用于代码与工具输出；跟随系统浅色 / 深色。
   ========================================================================= */

*,
*::before,
*::after { box-sizing: border-box; }

:root {
  color-scheme: light;

  --bg: #ffffff;
  --sidebar: #f9f9f9;
  --text: #0d0d0d;
  --text-2: #5d5d5d;
  --text-3: #8f8f8f;
  --border: #e8e8e8;
  --border-2: #dcdcdc;
  --hover: #f2f2f2;
  --active: #ececec;
  --fill: #f4f4f4;
  --fill-2: #ededed;
  --btn: #0d0d0d;
  --btn-text: #ffffff;
  --code-bg: #f2f2f2;
  --code-border: #e6e6e6;
  --pre-bg: #fbfbfb;
  --pre-text: #24292f;
  --pre-head-bg: #f2f2f2;

  /* 语法高亮（浅色） */
  --syn-comment: #8a8f98;
  --syn-keyword: #cf222e;
  --syn-string: #0a3069;
  --syn-number: #0550ae;
  --syn-title: #8250df;
  --syn-builtin: #953800;
  --syn-tag: #116329;
  --syn-meta: #6e7781;
  --syn-del: #82071e;

  --danger: #c0392b;
  --warn: #9a6700;
  --ok: #1f7a4d;
  --overlay: rgba(0, 0, 0, 0.32);

  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  --mono: ui-monospace, SFMono-Regular, "Cascadia Code", "Cascadia Mono",
    Consolas, "Liberation Mono", Menlo, monospace;
}

@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;

    --bg: #212121;
    --sidebar: #171717;
    --text: #ececec;
    --text-2: #a8a8a8;
    --text-3: #7a7a7a;
    --border: #2f2f2f;
    --border-2: #3a3a3a;
    --hover: #282828;
    --active: #303030;
    --fill: #2a2a2a;
    --fill-2: #333333;
    --btn: #ececec;
    --btn-text: #171717;
    --code-bg: #2b2b2b;
    --code-border: #353535;
    --pre-bg: #1b1b1b;
    --pre-text: #d6d6d6;
    --pre-head-bg: #242424;

    /* 语法高亮（深色） */
    --syn-comment: #7d8590;
    --syn-keyword: #ff7b72;
    --syn-string: #a5d6ff;
    --syn-number: #79c0ff;
    --syn-title: #d2a8ff;
    --syn-builtin: #ffa657;
    --syn-tag: #7ee787;
    --syn-meta: #8b949e;
    --syn-del: #ffa198;

    --danger: #e07a6f;
    --warn: #d4a72c;
    --ok: #5fb98a;
    --overlay: rgba(0, 0, 0, 0.6);
  }
}

html, body, #app { height: 100%; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 15px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

button { font-family: inherit; }
::selection { background: var(--active); }

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: var(--border-2);
  border: 3px solid transparent;
  background-clip: padding-box;
  border-radius: 8px;
}
::-webkit-scrollbar-thumb:hover { background: var(--text-3); background-clip: padding-box; }

.app { display: flex; height: 100vh; overflow: hidden; }

/* ===== 侧边栏 ===== */
/* 宽度由内联 style 驱动（可拖拽），这里的值只是首次渲染的兜底 */
.sidebar {
  position: relative;
  width: 260px;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  background: var(--sidebar);
  border-right: 1px solid var(--border);
}

/* ===== 侧边栏拖拽手柄 ===== */
/* 命中区压在边框上（左右各溢出约 3px），平时不可见，hover / 拖拽 / 聚焦时显形 */
.sb-resizer {
  position: absolute;
  top: 0;
  right: -3px;
  bottom: 0;
  z-index: 5;
  width: 7px;
  cursor: col-resize;
  touch-action: none; /* 触屏拖动时不触发页面滚动 */
  display: flex;
  align-items: center;
  justify-content: center;
}
.sb-resizer-grip {
  width: 2px;
  height: 100%;
  background: transparent;
  transition: background 0.15s;
}
.sb-resizer:hover .sb-resizer-grip,
.sb-resizer.active .sb-resizer-grip { background: var(--border-2); }
.sb-resizer:focus-visible { outline: none; }
.sb-resizer:focus-visible .sb-resizer-grip { background: var(--text-3); }

/* 拖拽中：全局禁用选中与按钮指针态，避免拖出文字高亮和手型闪烁 */
.app.resizing { cursor: col-resize; user-select: none; }
.app.resizing * { cursor: col-resize !important; }
.app.resizing iframe { pointer-events: none; } /* 拖拽经过 iframe 时不吞事件 */

.sb-top { padding: 12px 12px 4px; }

.sb-new {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--text);
  font-size: 14px;
  font-weight: 500;
  text-align: left;
  cursor: pointer;
  transition: background 0.12s;
}
.sb-new:hover { background: var(--hover); }
.sb-new svg {
  width: 16px; height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
}

.sb-nav { flex: 1; overflow-y: auto; padding: 8px 8px 12px; }
.sb-hint { padding: 20px 10px; color: var(--text-3); font-size: 13px; text-align: center; }

.sb-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 36px;
  padding: 0 8px 0 10px;
  margin-bottom: 1px;
  border-radius: 9px;
  cursor: pointer;
  transition: background 0.12s;
}
.sb-item:hover { background: var(--hover); }
.sb-item.active { background: var(--active); }

.sb-title {
  flex: 1;
  min-width: 0;
  font-size: 13.5px;
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sb-item.active .sb-title,
.sb-item:hover .sb-title { color: var(--text); }

.sb-live {
  flex: 0 0 auto;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--ok);
  animation: breathe 1.6s ease-in-out infinite;
}
@keyframes breathe { 50% { opacity: 0.25; } }

.sb-del {
  flex: 0 0 auto;
  width: 24px; height: 24px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s, background 0.12s, color 0.12s;
}
.sb-del svg {
  width: 13px; height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
}
.sb-item:hover .sb-del { opacity: 1; }
.sb-del:hover { background: var(--fill-2); color: var(--text); }

.sb-foot { padding: 10px 12px 14px; border-top: 1px solid var(--border); }
.sb-brand { display: flex; align-items: center; gap: 9px; }
.sb-mark {
  width: 24px; height: 24px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--text);
  color: var(--bg);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.sb-name { font-size: 13px; font-weight: 600; color: var(--text); }
.sb-tip { margin: 4px 0 0 33px; font-size: 11.5px; color: var(--text-3); }

/*===== 当前用户 + 登出===== */
.sb-user {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.sb-user-avatar {
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  border-radius: 50%;
  background: var(--accent, #111);
  color: #fff;
  display: grid;
  place-items: center;
  font-size: 11.5px;
  font-weight: 600;
}
.sb-user-meta { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.sb-user-name {
  font-size: 12.5px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-user-role { font-size: 11px; color: var(--text-3); }
.sb-user-out {
  flex: 0 0 auto;
  padding: 3px 8px;
  font-size: 11.5px;
  color: var(--text-2);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}
.sb-user-out:hover { color: var(--text); border-color: var(--text-3); }

/* 启动时"正在检查登录状态"的占位（避免一瞬闪出登录表单） */
.auth-boot {
  min-height: 100vh;
  display: grid;
  place-items: center;
  font-size: 13px;
  color: var(--text-3);
}

/*===== 模型选择器===== */
.model-select {
  max-width: 210px;
  padding: 5px 8px;
  font-size: 12.5px;
  color: var(--text-2);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 7px;
  cursor: pointer;
  outline: none;
}
.model-select:hover:not(:disabled) { background: var(--hover); color: var(--text); }
.model-select:disabled { opacity: 0.5; cursor: default; }
/* 还没选模型时给个显眼的描边：这个状态下发不出消息，得让人一眼看见 */
.model-select.unset { border-color: var(--warn); color: var(--warn); }

/* 欢迎页"还没配模型"的引导 */
.empty-nomodel {
  margin-top: 4px;
  padding: 14px 16px;
  border: 1px dashed var(--border-2);
  border-radius: 10px;
  text-align: center;
}
.empty-nomodel p {
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--text-2);
}

/* ===== 主区与顶栏 ===== */
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; position: relative; }

.topbar {
  flex: 0 0 auto;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 16px 0 22px;
}

.topbar-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.topbar-title > span:first-child {
  max-width: 46vw;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.topbar-live { flex: 0 0 auto; font-size: 11.5px; font-weight: 500; color: var(--text-3); }

.topbar-actions { display: flex; align-items: center; gap: 4px; }

.tbtn {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 11px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--text-2);
  font-size: 13px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.tbtn:hover { background: var(--hover); color: var(--text); }
.tbtn:disabled { opacity: 0.55; cursor: progress; }
.tbtn:focus-visible { outline: 2px solid var(--border-2); outline-offset: 1px; }
.tbtn.switchable { border-color: var(--border); }
.tbtn-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--text);
}
.tbtn.switchable.off { color: var(--text-3); }
.tbtn.switchable.off .tbtn-dot { background: var(--text-3); }
/*完全访问是危险档位 —— 用警示色常亮，别让它看起来和普通开关一样。 */
.tbtn.switchable.danger { color: var(--danger); border-color: var(--danger); }
.tbtn.switchable.danger .tbtn-dot { background: var(--danger); }
.tbtn.switchable.danger:hover { background: var(--danger); color: #fff; }

/* ===== 消息流 ===== */
.messages {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
  /* 注意：这里**不能**用 scroll-behavior: smooth。
     流式输出时每个 token 都会触发一次程序化滚动，平滑动画会和用户的手动滚动
     抢控制权（表现为"想往回翻却被反复拽下去"）。跟随一律瞬时，
     只有点"回到底部"那种显式跳转才用平滑（见 jumpToBottom 的 scrollTo）。 */
}

/* 回到底部：浮在输入区上方，不挡正文也不挡输入框 */
.jump-bottom {
  position: absolute;
  left: 50%;
  bottom: calc(100% + 8px);
  transform: translateX(-50%);
  z-index: 6;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 13px 6px 11px;
  border: 1px solid var(--border-2);
  border-radius: 999px;
  background: var(--bg);
  color: var(--text-2);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.1);
}
.jump-bottom:hover { color: var(--text); border-color: var(--text-3); }
.jump-bottom svg {
  width: 13px; height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.jump-bottom.fresh { color: var(--text); border-color: var(--border-2); }
/* 有新内容：按钮上一个小圆点，只提示、不抢滚动条 */
.jump-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--warn);
  animation: breathe 1.4s ease-in-out infinite;
}

.row { display: flex; justify-content: center; padding: 12px 24px; }
.row.user { padding-top: 6px; }
.row-inner {
  width: 100%;
  max-width: 760px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}
.row.user .row-inner { align-items: flex-end; }

/* 用户消息：浅底圆角块，占据自身内容宽度 */
.bubble.user {
  max-width: 78%;
  padding: 9px 16px;
  border-radius: 20px;
  background: var(--fill);
  font-size: 15px;
}

/* 助手消息：无气泡、无头像，纯净排版 */
.answer {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 12px;
}
.prose, .answer .md { width: 100%; font-size: 15px; line-height: 1.75; color: var(--text); }

.thinking { display: inline-flex; gap: 4px; padding: 7px 0; }
.thinking i {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--text-3);
  animation: bounce 1.3s ease-in-out infinite;
}
.thinking i:nth-child(2) { animation-delay: 0.16s; }
.thinking i:nth-child(3) { animation-delay: 0.32s; }
@keyframes bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.35; }
  30% { transform: translateY(-4px); opacity: 1; }
}

.answer-error {
  font-size: 13px;
  color: var(--warn);
  border-left: 2px solid var(--warn);
  padding-left: 10px;
}

/* Markdown 排版 */
.md > :first-child { margin-top: 0; }
.md > :last-child { margin-bottom: 0; }
.md p { margin: 0.75em 0; }
.md h1, .md h2, .md h3, .md h4 {
  margin: 1.5em 0 0.6em;
  line-height: 1.3;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.md h1 { font-size: 1.4em; }
.md h2 { font-size: 1.22em; }
.md h3 { font-size: 1.08em; }
.md h4 { font-size: 1em; }
.md ul, .md ol { margin: 0.75em 0; padding-left: 1.5em; }
.md li { margin: 0.3em 0; }
.md li::marker { color: var(--text-3); }
/* 行内代码：贴正文基线的浅底 chip，不抢眼但边界清楚 */
.md code {
  font-family: var(--mono);
  font-size: 0.87em;
  background: var(--code-bg);
  padding: 0.14em 0.4em;
  border-radius: 5px;
  word-break: break-word;
  font-variant-ligatures: none;
}
/* 兼容兜底：万一有未走 code-block 的裸 <pre> */
.md pre {
  margin: 1em 0;
  padding: 14px 16px;
  background: var(--pre-bg);
  border: 1px solid var(--code-border);
  border-radius: 10px;
  color: var(--pre-text);
  overflow-x: auto;
  line-height: 1.6;
}
.md pre code { background: transparent; padding: 0; font-size: 12.5px; }

/* ===== 代码块：卡头（语言 + 复制）+ 高亮正文 ===== */
.md .code-block {
  margin: 1em 0;
  border: 1px solid var(--code-border);
  border-radius: 10px;
  background: var(--pre-bg);
  overflow: hidden;
}
.md .code-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 7px 5px 12px;
  background: var(--pre-head-bg);
  border-bottom: 1px solid var(--code-border);
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-3);
  user-select: none;
}
.md .code-lang {
  letter-spacing: 0.05em;
  text-transform: lowercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.md .code-copy {
  margin-left: auto;
  flex: none;
  padding: 2px 9px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-2);
  font-family: inherit;
  font-size: 11px;
  line-height: 1.6;
  cursor: pointer;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.md .code-copy:hover { background: var(--hover); color: var(--text); border-color: var(--code-border); }
.md .code-copy.done { color: var(--ok); border-color: var(--ok); background: transparent; }
.md .code-block pre {
  margin: 0;
  padding: 12px 14px;
  border: 0;
  border-radius: 0;
  background: transparent;
  overflow-x: auto;
}
.md .code-block pre code {
  display: block;
  background: transparent;
  padding: 0;
  font-size: 12.5px;
  line-height: 1.62;
  tab-size: 2;
  font-variant-ligatures: none;
}
/* 横向滚动条收窄一点，别像原生那样笨重 */
.md .code-block pre::-webkit-scrollbar { height: 9px; }
.md .code-block pre::-webkit-scrollbar-track { background: transparent; }
.md .code-block pre::-webkit-scrollbar-thumb {
  background: var(--border-2);
  border-radius: 5px;
}

/* ===== highlight.js 配色（自建，浅/深色共用一套变量） ===== */
.md .hljs { color: var(--pre-text); background: transparent; }
.md .hljs-comment,
.md .hljs-quote { color: var(--syn-comment); font-style: italic; }
.md .hljs-keyword,
.md .hljs-selector-tag,
.md .hljs-literal,
.md .hljs-doctag,
.md .hljs-name { color: var(--syn-keyword); }
.md .hljs-string,
.md .hljs-regexp,
.md .hljs-addition,
.md .hljs-attribute,
.md .hljs-meta .hljs-string { color: var(--syn-string); }
.md .hljs-number,
.md .hljs-symbol,
.md .hljs-bullet,
.md .hljs-link,
.md .hljs-selector-attr,
.md .hljs-selector-pseudo { color: var(--syn-number); }
.md .hljs-title,
.md .hljs-title.class_,
.md .hljs-title.function_,
.md .hljs-section,
.md .hljs-selector-id { color: var(--syn-title); }
.md .hljs-built_in,
.md .hljs-type,
.md .hljs-attr,
.md .hljs-variable,
.md .hljs-template-variable,
.md .hljs-params { color: var(--syn-builtin); }
.md .hljs-tag,
.md .hljs-selector-class { color: var(--syn-tag); }
.md .hljs-meta,
.md .hljs-meta .hljs-keyword,
.md .hljs-subst { color: var(--syn-meta); }
.md .hljs-deletion { color: var(--syn-del); }
.md .hljs-emphasis { font-style: italic; }
.md .hljs-strong { font-weight: 600; }

/* ===== KaTeX 公式 ===== */
.md .katex { font-size: 1.05em; }
.md .katex-display {
  margin: 1.1em 0;
  padding: 2px 0;
  overflow-x: auto;
  overflow-y: hidden;
}
.md .katex-display > .katex { white-space: normal; }
/* 块级公式本身自撑宽度，别让外层 <p> 再叠一层间距 */
.md p:has(> .katex-block) { margin: 0; }
.md .katex-block { text-align: center; }
.md .katex .katex-html { white-space: nowrap; }
.md .katex-error,
.md .math-fallback {
  color: var(--danger);
  font-family: var(--mono);
  font-size: 0.9em;
}
.md blockquote {
  margin: 1em 0;
  padding: 2px 0 2px 14px;
  border-left: 2px solid var(--border-2);
  color: var(--text-2);
}
.md a { color: inherit; text-decoration: underline; text-underline-offset: 2px; }
.md a:hover { color: var(--text-2); }
.md table {
  margin: 1em 0;
  border-collapse: collapse;
  font-size: 0.92em;
  display: block;
  width: max-content;
  max-width: 100%;
  overflow-x: auto;
}
.md th, .md td {
  border: 1px solid var(--border);
  padding: 6px 12px;
  text-align: left;
}
.md th { background: var(--fill); font-weight: 600; }
.md hr { border: 0; border-top: 1px solid var(--border); margin: 1.5em 0; }
.md img { max-width: 100%; border-radius: 8px; }

/* ===== 工具调用（可折叠活动块，比正文更宽一点，便于看长输出） ===== */
/* ===== 工具活动：只展示最新一条 =====
   之前是一整个 <details> 绑 :open（运行中就展开、完成就收起），于是每来一个新工具
   整个块就开合一次，看起来像页面在抽搐。现在拆成两层：
     .act         最新一条，始终可见，状态文案实时变
     .act-history 此前的调用，默认不展开，开合只由用户决定 */
.activity { width: 100%; max-width: 860px; display: flex; flex-direction: column; gap: 4px; }

.act {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg);
  font-size: 13px;
}
.act-mark {
  flex: 0 0 auto;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--ok);
}
.act.running .act-mark { background: var(--warn); animation: breathe 1.3s ease-in-out infinite; }
/* 参数还在生成（`tool_args` 心跳）：用虚线环区分"工具尚未开始执行" */
.act.generating .act-mark {
  background: transparent;
  border: 1.5px solid var(--warn);
  animation: breathe 1.3s ease-in-out infinite;
}
.act-verb { flex: 0 0 auto; font-family: var(--mono); font-size: 12.5px; color: var(--text); }
/*「当前步骤」显示的是模型写的自然语言意图，用正文字体更像一句话；
   工具名回落到 .act-verb 时保持等宽（那是标识符）。 */
.act-verb.act-intent { font-family: var(--font); max-width: 46%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.act.running .act-verb { color: var(--text); }
.act-detail {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: var(--text-3);
  font-family: var(--mono);
  font-size: 12px;
}
.act-badge { flex: 0 0 auto; font-size: 11.5px; color: var(--text-3); }
/*右列徽标要说清"成没成"，失败必须一眼可见 */
.act-badge.error { color: var(--danger); }
.act-badge.interrupted { color: var(--text-2); }
.act.error .act-mark { background: var(--danger); }
.act.interrupted .act-mark { background: var(--text-3); }

/*===== 工具卡的"成果 / 具体操作 / 失联"三段附属信息 ===== */
.act-block { display: flex; flex-direction: column; gap: 6px; width: 100%; }
.act-note {
  display: flex; align-items: center; gap: 8px;
  margin: 0; padding: 0 4px;
  font-size: 12px; line-height: 1.5;
  color: var(--text-3);
  font-family: var(--mono);
  word-break: break-word;
}
.act-note.bad { color: var(--danger); }
.act-note.soft { color: var(--warn); }
.act-note.hard { color: var(--danger); }
.act-action {
  flex: 0 0 auto;
  padding: 2px 9px;
  border: 1px solid currentColor;
  border-radius: 999px;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}
.act-action:hover { background: var(--hover); }

.act-call-wrap {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg);
  overflow: hidden;
}
.act-call-wrap > summary {
  display: flex; align-items: center;
  padding: 7px 12px;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-3);
  list-style: none;
}
.act-call-wrap > summary::-webkit-details-marker { display: none; }
.act-call-wrap > summary:hover { background: var(--hover); }
.act-call { border-top: 1px solid var(--border); padding: 8px 12px 2px; }
.act-call-head { display: flex; align-items: center; gap: 8px; margin-bottom: 2px; }

.act-history {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg);
  overflow: hidden;
}
.act-history > summary {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 12px;
  cursor: pointer;
  list-style: none;
  font-size: 12.5px;
  color: var(--text-2);
  user-select: none;
}
.act-history > summary::-webkit-details-marker { display: none; }
.act-history > summary:hover { background: var(--hover); }
.act-history-text { flex: 1; min-width: 0; }
.act-history[open] .tools-chev { transform: rotate(180deg); }
.act-history > .subagent-archive,
.act-history > .tool-group {
  border-top: 1px solid var(--border);
  padding: 10px 12px;
}

/* 历史里的每一条（含它内部的原始调用明细） */
.act-item { display: flex; flex-direction: column; gap: 2px; margin-bottom: 6px; }
.act-item-head {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 6px;
  border-radius: 7px;
  font-size: 12.5px;
}
.act-item-head .act-detail { font-size: 11.5px; }

.tool-group { display: flex; flex-direction: column; gap: 2px; }
.tool-group-label {
  font-size: 11px;
  color: var(--text-3);
  margin-bottom: 2px;
  letter-spacing: 0.02em;
}

.tool { border-radius: 7px; }
.tool summary {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 6px;
  border-radius: 7px;
  cursor: pointer;
  list-style: none;
  font-size: 12.5px;
  user-select: none;
}
.tool summary::-webkit-details-marker { display: none; }
.tool summary:hover { background: var(--hover); }

.tools-chev {
  flex: 0 0 auto;
  width: 14px; height: 14px;
  fill: none;
  stroke: var(--text-3);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.18s;
}

.tool-status {
  flex: 0 0 auto;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--text-3);
}
.tool-status.running { background: var(--warn); animation: breathe 1.3s ease-in-out infinite; }
.tool-status.generating {
  background: transparent;
  border: 1.5px solid var(--warn);
  animation: breathe 1.3s ease-in-out infinite;
}
.tool-status.done { background: var(--ok); }
/*失败 / 被中断是两种要能被眼睛立刻分出来的终态 */
.tool-status.error { background: var(--danger); }
.tool-status.interrupted { background: var(--text-3); }

.tool-name { font-family: var(--mono); font-size: 12.5px; color: var(--text); }
.tool-badge { margin-left: auto; font-size: 11.5px; color: var(--text-3); }
.tool-badge.error { color: var(--danger); }
.tool-badge.interrupted { color: var(--text-2); }

.tool-detail {
  border-top: 1px solid var(--border);
  padding: 10px 6px 4px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tool-section { display: flex; flex-direction: column; gap: 5px; }
.tool-label {
  font-size: 11px;
  color: var(--text-3);
  letter-spacing: 0.02em;
}
.tool-code {
  margin: 0;
  padding: 10px 12px;
  background: var(--pre-bg);
  border: 1px solid var(--code-border);
  border-radius: 10px;
  color: var(--pre-text);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.58;
  tab-size: 2;
  font-variant-ligatures: none;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
.tool-code::-webkit-scrollbar { width: 9px; height: 9px; }
.tool-code::-webkit-scrollbar-track { background: transparent; }
.tool-code::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 5px; }

/* ===== 子智能体委派：运行中卡片（摘要 + 实时输出） ===== */
.delegations { width: 100%; display: flex; flex-direction: column; gap: 10px; }
.delegations-head { display: flex; align-items: center; gap: 8px; }
.delegations-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.delegations-count {
  padding: 0 6px;
  border-radius: 9px;
  background: var(--fill);
  color: var(--text-3);
  font-size: 11px;
}

.subagent {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 11px 13px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--bg);
}
.subagent.running { border-color: var(--border-2); }

.subagent-head { display: flex; align-items: center; gap: 8px; }
.subagent-pulse {
  flex: 0 0 auto;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--warn);
  animation: breathe 1.3s ease-in-out infinite;
}
.subagent.done .subagent-pulse { background: var(--ok); animation: none; }
.subagent-name {
  font-family: var(--mono);
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
}
.subagent-elapsed {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-3);
}
.subagent-state { margin-left: auto; font-size: 11.5px; color: var(--text-3); }
.subagent.running .subagent-state { color: var(--warn); }

/* 摘要：默认只给用户看这一行 */
.subagent-summary {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.6;
  color: var(--text-2);
}

/* 完整任务描述：折叠 */
.subagent-full { font-size: 12.5px; }
.subagent-full > summary {
  cursor: pointer;
  list-style: none;
  color: var(--text-3);
  font-size: 11.5px;
  user-select: none;
  padding: 2px 0;
}
.subagent-full > summary::-webkit-details-marker { display: none; }
.subagent-full > summary:hover { color: var(--text-2); }
.subagent-full .md {
  margin-top: 6px;
  padding: 8px 10px;
  border-left: 2px solid var(--border);
  color: var(--text-2);
  font-size: 12.5px;
  max-height: 220px;
  overflow: auto;
}

/* 子智能体自己的工具调用（实时出现） */
.subagent-tools { display: flex; flex-wrap: wrap; gap: 5px; }
.subagent-chip {
  font-family: var(--mono);
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 5px;
  background: var(--fill);
  color: var(--text-2);
}
.subagent-chip.running { color: var(--warn); background: var(--fill-2); }

/* 子智能体的实时输出 */
.subagent-stream {
  max-height: 320px;
  overflow: auto;
  padding: 9px 11px;
  border-radius: 8px;
  background: var(--code-bg);
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-2);
}
.subagent-wait { display: inline-flex; align-items: center; gap: 4px; padding: 4px 0; }
.subagent-wait-text { margin-left: 5px; font-size: 11.5px; color: var(--text-3); }
.subagent-wait i {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--text-3);
  animation: bounce 1.3s ease-in-out infinite;
}
.subagent-wait i:nth-child(2) { animation-delay: 0.16s; }
.subagent-wait i:nth-child(3) { animation-delay: 0.32s; }

.subagent-usage {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-3);
}

/* 归档进工具调用列表后的呈现 */
.subagent-archive { display: flex; flex-direction: column; gap: 2px; }
.subagent-summary-inline {
  flex: 1;
  min-width: 0;
  margin: 0 6px;
  font-size: 12px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tool-subagent .tool-detail { padding-left: 8px; }
.subagent-fulltext {
  font-size: 12.5px;
  color: var(--text-2);
  max-height: 300px;
  overflow: auto;
}

/* ===== 思考过程（推理增量）=====
   长推理期间正文一个字都没有，这块是唯一的存活信号：
   默认收起，但标题上的点会呼吸、字数会实时增长。 */
.reasoning {
  width: 100%;
  margin: 2px 0 6px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--fill);
  font-size: 12px;
}
.reasoning.live { border-color: var(--border-2); }
.reasoning > summary {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 9px;
  cursor: pointer;
  list-style: none;
  color: var(--text-2);
  user-select: none;
}
.reasoning > summary::-webkit-details-marker { display: none; }
.reasoning-dot {
  width: 5px; height: 5px;
  flex: none;
  border-radius: 50%;
  background: var(--text-3);
}
.reasoning.live .reasoning-dot {
  background: var(--warn);
  animation: breathe 1.1s ease-in-out infinite;
}
.reasoning-meta { margin-left: auto; color: var(--text-3); font-family: var(--mono); font-size: 11px; }
.reasoning-text {
  margin: 0;
  padding: 0 9px 8px;
  max-height: 240px;
  overflow: auto;
  border: 0;
  background: transparent;
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.68;
  white-space: pre-wrap;
  word-break: break-word;
}

/* ===== 计时器 / 每秒 token ===== */
.meter {
  display: flex;
  align-items: center;
  gap: 7px;
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-3);
}
.meter-time { color: var(--text-2); display: inline-flex; align-items: center; gap: 5px; }
.meter-clock {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--text-3);
}
.meter.live .meter-clock { background: var(--warn); animation: breathe 1.1s ease-in-out infinite; }
.meter-rate { color: var(--text-2); }
.meter-sep { color: var(--border-2); }
.meter-tokens { margin-left: auto; }

/* ===== 空状态（新会话欢迎页） ===== */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  height: 100%;
  padding: 0 24px 64px;
  text-align: center;
}
.empty-mark {
  width: 40px; height: 40px;
  display: grid;
  place-items: center;
  margin: auto 0 18px;
  border-radius: 50%;
  border: 1px solid var(--border-2);
  color: var(--text-2);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.empty-title {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}
.empty-sub { margin: 0; font-size: 14px; color: var(--text-2); }

.chips {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 26px;
  margin-bottom: auto;
}
.chip {
  padding: 8px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: transparent;
  color: var(--text-2);
  font-size: 13px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.chip:hover { background: var(--hover); color: var(--text); border-color: var(--border-2); }

/* ===== 输入区 ===== */
.composer { flex: 0 0 auto; position: relative; padding: 4px 24px 12px; }

.composer-inner {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  width: 100%;
  max-width: 760px;
  margin: 0 auto;
  padding: 8px 8px 8px 16px;
  border: 1px solid var(--border);
  border-radius: 26px;
  background: var(--bg);
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.04);
  transition: border-color 0.15s, box-shadow 0.15s;
}
@media (prefers-color-scheme: dark) {
  .composer-inner { box-shadow: none; }
}
.composer-inner:focus-within { border-color: var(--border-2); }

.composer textarea {
  flex: 1;
  min-width: 0;
  height: 24px;
  max-height: 200px;
  padding: 0;
  border: 0;
  outline: 0;
  resize: none;
  overflow-y: auto;
  background: transparent;
  color: var(--text);
  font-family: inherit;
  font-size: 15px;
  line-height: 24px;
}
.composer textarea::placeholder { color: var(--text-3); }

.btn-send, .btn-stop {
  flex: 0 0 auto;
  width: 32px; height: 32px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 50%;
  background: var(--btn);
  color: var(--btn-text);
  cursor: pointer;
  transition: opacity 0.12s, background 0.12s;
}
.btn-send svg {
  width: 17px; height: 17px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.btn-send:disabled { background: var(--fill-2); color: var(--text-3); cursor: default; }
.btn-stop { background: var(--fill-2); color: var(--text); }
.stop-square { width: 10px; height: 10px; border-radius: 2px; background: currentColor; }

.disclaimer {
  width: 100%;
  max-width: 760px;
  margin: 8px auto 0;
  font-size: 11.5px;
  color: var(--text-3);
  text-align: center;
}

/* ===== 弹窗（审批 / 文件面板） ===== */
.overlay {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--overlay);
}
.modal {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-height: min(72vh, 560px);
  overflow: hidden;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.24);
}
.approval-modal { max-width: 400px; }
.file-modal { max-width: 560px; }

.modal-head { flex: 0 0 auto; padding: 16px 18px 12px; border-bottom: 1px solid var(--border); }
.modal-head h3 { margin: 0; font-size: 15px; font-weight: 600; }
.modal-head p { margin: 4px 0 0; font-size: 12.5px; color: var(--text-2); }
.modal-note { font-size: 11.5px !important; color: var(--text-3) !important; line-height: 1.5; }
.modal-head.row-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px 12px 18px;
}
.modal-head-actions { display: flex; gap: 6px; }

.modal-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.modal-foot {
  flex: 0 0 auto;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 14px;
  border-top: 1px solid var(--border);
}
.modal-foot.tip-only {
  display: block;
  padding: 10px 18px;
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.5;
}

.btn-primary {
  height: 34px;
  padding: 0 16px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: var(--btn);
  color: var(--btn-text);
  font-size: 13.5px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.12s;
}
.btn-primary:hover:not(:disabled) { opacity: 0.85; }
.btn-primary:disabled { opacity: 0.5; cursor: progress; }

.btn-ghost {
  height: 34px;
  padding: 0 14px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: transparent;
  color: var(--text);
  font-size: 13.5px;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.btn-ghost:hover:not(:disabled) { background: var(--hover); border-color: var(--border-2); }
.btn-ghost:disabled { opacity: 0.5; cursor: progress; }
.btn-ghost.sm { height: 28px; padding: 0 10px; font-size: 12.5px; }

/* 审批项：只展示参数内容摘要，按钮永不被长参数挤走 */
.approve-item {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
}
.approve-item-head { display: flex; align-items: center; gap: 6px; margin-bottom: 7px; }
.approve-tool {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  max-width: 100%;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.approve-index { margin-left: auto; font-size: 11px; color: var(--text-3); }
.approve-args { display: flex; flex-direction: column; gap: 4px; }
.approve-arg { display: flex; gap: 8px; font-size: 12px; line-height: 1.55; }
.approve-arg-k {
  flex: 0 0 auto;
  min-width: 46px;
  max-width: 96px;
  font-family: var(--mono);
  color: var(--text-3);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.approve-arg-v {
  flex: 1;
  min-width: 0;
  font-family: var(--mono);
  color: var(--text-2);
  word-break: break-all;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.approve-arg-empty { font-size: 12px; color: var(--text-3); }

/* 文件面板 */
.file-path {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px 8px 18px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text-3);
}
.breadcrumb {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 3px;
  overflow-x: auto;
  white-space: nowrap;
}
.crumb { color: var(--text-3); }
.crumb.clickable { cursor: pointer; }
.crumb.clickable:hover { color: var(--text); text-decoration: underline; }
.crumb.active { color: var(--text); font-weight: 600; }
.crumb-sep { color: var(--border-2); }

.file-list { padding: 0 8px 10px; gap: 0; }
.file-empty { padding: 32px 12px; color: var(--text-3); font-size: 13px; text-align: center; }
.file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 34px;
  padding: 0 10px;
  border-radius: 7px;
  font-size: 13px;
  cursor: default;
}
.file-item.dir { cursor: pointer; }
.file-item:hover { background: var(--hover); }
.file-icon { flex: 0 0 auto; width: 12px; color: var(--text-3); font-size: 11px; }
.file-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.file-size { flex: 0 0 auto; font-size: 11.5px; color: var(--text-3); }
.file-dl {
  flex: 0 0 auto;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-2);
  font-size: 11.5px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s, background 0.12s;
}
.file-item:hover .file-dl { opacity: 1; }
.file-dl:hover { background: var(--fill); color: var(--text); }

/* 删除按钮：与"下载"同形，但悬停时用危险色，避免误点 */
.file-del {
  flex: 0 0 auto;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-2);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s, background 0.12s, color 0.12s, border-color 0.12s;
}
.file-item:hover .file-del { opacity: 1; }
.file-del:hover { background: var(--fill); color: var(--danger); border-color: var(--danger); }

/* 删除确认框：把完整路径摆出来，别只问"确定吗" */
.confirm-modal { max-width: 430px; }
.confirm-path {
  display: block;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
  color: var(--text-2);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
}
.btn-danger {
  height: 34px;
  padding: 0 16px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: var(--danger);
  color: #fff;
  font-size: 13.5px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.12s;
}
.btn-danger:hover:not(:disabled) { opacity: 0.88; }
.btn-danger:disabled { opacity: 0.5; cursor: progress; }

/* ===== 轻量提示条 ===== */
.toast {
  position: fixed;
  top: 16px;
  left: 50%;
  z-index: 300;
  max-width: 80vw;
  padding: 9px 15px;
  border-radius: 10px;
  background: var(--text);
  color: var(--bg);
  font-size: 13px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  transform: translateX(-50%);
  animation: toast-in 0.18s ease-out;
}
@keyframes toast-in {
  from { opacity: 0; transform: translate(-50%, -6px); }
  to { opacity: 1; transform: translate(-50%, 0); }
}
.toast.error { background: var(--danger); color: #fff; }
.toast.ok { background: var(--ok); color: #fff; }

/* ===== 焦点可见性 ===== */
:focus-visible { outline: 2px solid var(--border-2); outline-offset: 2px; }

/* ===== 响应式 ===== */
@media (max-width: 900px) {
  /* 这里不再写死侧栏宽度：宽度由拖拽状态 + 脚本夹取（会为窄窗口自动收窄）保底 */
  .row { padding: 10px 16px; }
  .composer { padding: 4px 16px 10px; }
}

@media (max-width: 720px) {
  /* 窄屏侧栏隐藏，拖拽手柄一并失效 */
  .sidebar { display: none; }
  .sb-resizer { display: none; }
  .bubble.user { max-width: 88%; }
  .topbar { padding: 0 10px 0 16px; }
  .empty-title { font-size: 21px; }
}

@media (prefers-reduced-motion: reduce) {
  * { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; }
  .messages { scroll-behavior: auto; }
}

</style>
