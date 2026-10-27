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
    <Transition name="settings-dialog">
    <ProvidersView
      v-if="showProviders"
      :is-admin="isAdmin"
      @close="showProviders = false"
      @changed="onProvidersChanged"
    />
    </Transition>
    <!-- MCP 工具源管理弹层。同样放最外层，避免被 .app 的布局/层级裁剪 -->
    <Transition name="settings-dialog">
      <McpView v-if="showMcp" @close="showMcp = false" />
    </Transition>
    <Transition name="settings-dialog">
      <SettingsView v-if="showSettings" :key="activeWorkspaceId" :user="user"
        :workspace-id="activeWorkspaceId" :workspace-name="selectedWorkspace.title"
        @close="showSettings = false" @password-changed="onPasswordChanged" />
    </Transition>
    <ConfirmDialog :confirmation="confirmation" @resolve="resolveConfirmation" />
    <!-- 轻量提示条 -->
    <div v-if="toast.text" class="toast" :class="toast.kind">{{ toast.text }}</div>

    <!-- 侧边栏：会话列表（宽度可拖拽调整） -->
    <aside class="sidebar" ref="sidebarEl" :style="{ width: sidebarWidth + 'px' }">
      <div class="sb-top">
        <div class="sb-brand">
          <span class="sb-mark">AH</span>
          <span class="sb-name">Agent Harness</span>
        </div>

        <button class="sb-new" @click="newChat()">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg>
          <span>新对话</span>
        </button>
        <button class="sb-new" :disabled="creatingWorkspace" @click="createWorkspace">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4h5l2 2h5v7H2z" /></svg>
          <span>{{ creatingWorkspace ? '请选择目录…' : '新建工作区' }}</span>
        </button>
      </div>

      <nav class="sb-nav">
        <div v-if="loadingSessions" class="sb-hint">载入中…</div>

        <section v-for="workspace in workspaceGroups" :key="workspace.id" class="sb-workspace" :class="{ selected: workspace.id === activeWorkspaceId }">
          <div class="sb-workspace-sticky">
          <div class="sb-workspace-head">
            <button class="sb-workspace-toggle" @click="toggleWorkspace(workspace.id)" :aria-expanded="!collapsedWorkspaces[workspace.id]">
              <svg class="sb-workspace-chevron" :class="{ expanded: !collapsedWorkspaces[workspace.id] }" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
              <svg class="sb-workspace-folder" viewBox="0 0 20 20" aria-hidden="true"><path d="M2.5 5.5h5l2 2h8v9h-15z" /></svg>
              <span class="sb-title" :title="workspace.directory">{{ workspace.title }}</span>
              <span class="sb-workspace-count">{{ workspace.sessions.length }}</span>
            </button>
            <button class="sb-workspace-add" title="在此工作区新建会话" @click="newChat(workspace.id)">+</button>
            <button v-if="workspace.id !== 'legacy'" class="sb-workspace-add" title="删除工作区" aria-label="删除工作区" @click="deleteWorkspace(workspace)">×</button>
          </div>
          </div>
          <template v-if="!collapsedWorkspaces[workspace.id]">
            <div v-for="s in workspace.sessions" :key="s.id" class="sb-item" :class="{ active: s.id === sessionId }" @click="openSession(s.id)">
              <span class="sb-title">{{ s.title || '新会话' }}</span>
              <span v-if="isSessionStreaming(s.id)" class="sb-live" title="后台输出中"></span>
              <button class="sb-del" title="删除会话" @click.stop="deleteSession(s.id)">
                <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" /></svg>
              </button>
            </div>
          </template>
        </section>
      </nav>

      <div class="sb-foot">
        <button class="settings-entry sb-settings" title="个人设置" aria-haspopup="dialog" @click="showSettings = true">
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14M7 3v4M13 8v4M8 13v4" /></svg>
          <span>设置</span>
        </button>
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
          <div class="toolbar-group toolbar-models" role="group" aria-label="模型与工作区">
          <!-- 模型选择器。第一项"跟随默认"= 不绑供应商，按用户/系统默认解析 -->
          <select
            class="model-select"
            :class="{ unset: !chosenProvider }"
            :value="chosenProvider"
            :disabled="!providers.length"
            :title="modelSelectTitle"
            aria-label="选择模型"
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
            <svg class="toolbar-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="m10 2 7 4v8l-7 4-7-4V6zM3 6l7 4 7-4M10 10v8" /></svg>
            模型
          </button>
          <button class="tbtn" title="接入外部 MCP 工具源（网页抓取/数据库/内部接口等）" @click="showMcp = true">
            <svg class="toolbar-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="M7 3v4m6-4v4M5 7h10v3a5 5 0 0 1-10 0zM10 15v3" /></svg>
            工具源
          </button>
          <button class="tbtn" @click="openFilePanel" title="浏览工作区文件">
            <svg class="toolbar-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="M2.5 5h6l2 2h7v9h-15z" /></svg>
            文件
          </button>
          </div>
          <div class="toolbar-group toolbar-status" role="group" aria-label="权限与工具显示">
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
            :aria-pressed="accessFull"
            :aria-busy="accessBusy"
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
            :aria-pressed="hitlOn && !accessFull"
            :aria-busy="hitlBusy"
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
            :aria-pressed="showTools"
            title="显示 / 隐藏工具调用详情"
            @click="showTools = !showTools"
          >
            <span class="tbtn-dot"></span>
            <span>{{ showTools ? '工具调用' : '已隐藏工具' }}</span>
          </button>
          </div>
        </div>
      </header>

      <main
        class="messages"
        ref="scroller"
        @scroll.passive="onMessagesScroll"
        @wheel.passive="onMessagesWheel"
      >
        <div v-if="loadingHistory" class="history-loading" role="status">正在加载会话…</div>
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

        </div>

        <div v-for="(m, i) in messages" :key="i" class="row" :class="m.role">
          <div class="row-inner">
            <div v-if="m.role === 'user'" class="bubble user">
              <div class="md" v-html="renderMarkdown(m.content)"></div>
              <div class="attachment-list" v-if="m.images?.length"><img v-for="(src, index) in m.images" :key="index" :src="src" alt="图片附件" /></div>
            </div>

            <div v-else class="answer">
              <TaskPlan :message="m" />
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

              <!-- 整数秒计时：结束后明确显示状态与用时。 -->
              <div
                v-if="m.startedAt && (m.content || m.streaming || m.thinking)"
                class="meter"
                :class="answerTiming(m, nowTick).state"
              >
                <span class="meter-time">
                  <svg v-if="answerTiming(m, nowTick).state === 'done'" class="meter-check" viewBox="0 0 16 16" aria-hidden="true"><path d="m3 8 3 3 7-7" /></svg>
                  <span v-else class="meter-clock" aria-hidden="true"></span>
                  {{ answerTiming(m, nowTick).text }}
                </span>
              </div>

              <!-- 工具活动：**同一时刻只展示最新一条**。
                   上一条一旦被新的取代，就收进下面的折叠列表（默认不展开）。
                   注意：绝对不能给这里的外层 <details> 绑 :open —— 之前那样写会在
                   "运行中 → 完成"之间反复开合整个块，看起来像页面在抽搐。
                   开合只由用户决定。 -->
              <div v-if="hasActivity(m)" class="activity">
                <div v-if="activityLatest(m)" class="act-block">
                  <div class="act" :class="activityLatest(m).status">
                    <span class="step-number">{{ activityLatest(m).step }}</span>
                    <!-- 优先显示模型给的"人话"意图（看板的"当前步骤"）。
                         没有（功能关闭/历史数据）时回落到原来的动作文案。
                         ⚠️ intent 是**模型生成的文本**，只能用插值（Vue 自动转义），
                         绝不能接 v-html / renderMarkdown —— 一句
                         `<img src=x onerror=...>` 就是一次 XSS。 -->
                    <span
                      v-if="activityLatest(m).intent"
                      class="act-verb act-intent"
                      :title="activityLatest(m).title"
                    >{{ activityLatest(m).intent }}</span>
                    <span v-else class="act-verb">{{ activityLatest(m).verb }}</span>
                    <span class="act-detail" :title="activityLatest(m).full">{{ activityLatest(m).detail }}</span>
                    <span class="act-badge" :class="activityLatest(m).status">{{ activityLatest(m).badge }}</span>
                  </div>

                  <!-- 跑完之后把"结果"贴在卡片上。
                       用户问的是"到底做了什么、成功没有"，不该只能靠展开详情才发现。 -->
                  <p v-if="activityLatest(m).failHead" class="act-note bad">
                    ✕ {{ activityLatest(m).failHead }}
                  </p>
                  <!-- 具体执行了什么 —— 命令/参数 + 完整输出，一次点击就能核对 -->
                  <details class="act-call-wrap">
                    <summary>
                      查看操作<span class="operation-count">{{ activityLatest(m).items.length }} 项</span>
                    </summary>
                    <div
                      v-for="it in activityLatest(m).items"
                      :key="it.id || it.name"
                      class="act-call"
                    >
                      <div v-if="activityLatest(m).items.length > 1" class="act-call-head">
                        <span class="tool-status" :class="it.status"></span>
                        <span class="tool-name">{{ toolName(it.name) }}</span>
                        <span class="tool-badge" :class="it.status">{{ toolOutcomeText(it) }}</span>
                      </div>
                      <div v-if="it.args && Object.keys(it.args).length" class="tool-section">
                        <div class="tool-label">操作</div>
                        <div v-if="it.name === 'execute' && it.args.command" class="md terminal-tool" v-html="renderTerminal(it.args.command, '命令')"></div>
                        <ToolArguments v-else :args="it.args" :workspace-directory="currentWorkspace?.directory || ''" />
                      </div>
                      <div v-if="toolDetailNotice(it)" class="operation-notice" role="status">
                        <p class="operation-notice-title">{{ toolDetailNotice(it).title }}</p>
                        <p>{{ toolDetailNotice(it).text }}</p>
                      </div>
                      <div v-if="it.result" class="tool-section">
                        <div class="tool-label">结果</div>
                        <div v-if="it.name === 'execute'" class="md terminal-tool" v-html="renderTerminal(truncate(it.result, 12000), '执行输出')"></div>
                        <pre v-else class="tool-code" :class="{ 'tool-code-source': ['read_file', 'ls', 'glob', 'grep'].includes(it.name) }">{{ truncate(it.result, 4000) }}</pre>
                      </div>
                    </div>
                  </details>
                </div>

                <!-- 此前的调用：默认不展示，需要时展开 -->
                <details v-if="activityHistory(m).length || doneDelegations(m).length" class="act-history">
                  <summary>
                    <span class="act-history-text">
                      已收纳 {{ activityHistory(m).length + doneDelegations(m).length }} 个步骤
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
                        <span class="tool-name">{{ d.subagent_type || '子智能体' }}</span>
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
                    <details v-for="g in activityHistory(m)" :key="g.key" class="act-item archived-step">
                      <summary class="act-item-head">
                        <span class="step-number">{{ g.step }}</span>
                        <span class="tool-status" :class="g.status"></span>
                        <span class="act-verb" :title="g.title">{{ g.intent || g.verb }}</span>
                        <span class="act-detail" :title="g.full">{{ g.detail }}</span>
                        <span class="tool-badge" :class="g.status">{{ g.badge }}</span>
                      </summary>
                      <p v-if="g.failHead" class="act-note bad">✕ {{ g.failHead }}</p>
                      <div v-for="it in g.items" :key="it.id || it.name" class="tool-detail">
                        <span v-if="g.items.length > 1" class="tool-label">{{ toolName(it.name) }}</span>
                        <div v-if="it.args && Object.keys(it.args).length" class="tool-section">
                          <div v-if="it.name === 'execute' && it.args.command" class="md terminal-tool" v-html="renderTerminal(it.args.command, '命令')"></div>
                          <ToolArguments v-else :args="it.args" :workspace-directory="currentWorkspace?.directory || ''" />
                        </div>
                        <div v-if="toolDetailNotice(it)" class="operation-notice" role="status">
                          <p class="operation-notice-title">{{ toolDetailNotice(it).title }}</p>
                          <p>{{ toolDetailNotice(it).text }}</p>
                        </div>
                        <details v-if="it.result" class="operation-output">
                          <summary>查看结果</summary>
                          <div v-if="it.name === 'execute'" class="md terminal-tool" v-html="renderTerminal(truncate(it.result, 12000), '执行输出')"></div>
                          <pre v-else class="tool-code" :class="{ 'tool-code-source': ['read_file', 'ls', 'glob', 'grep'].includes(it.name) }">{{ truncate(it.result, 4000) }}</pre>
                        </details>
                      </div>
                    </details>
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
      <Transition name="settings-dialog" mode="out-in" :duration="220">
      <DialogOverlay v-if="approval && (approval.clarifications || []).length" :key="`clarification-${approval.session_id}-${approval.clarifications[0].id}`" class="approval-overlay" :dismissible="false">
        <div class="modal approval-modal clarification-modal" role="dialog" aria-modal="true" aria-labelledby="clarification-title" aria-describedby="clarification-question">
          <div class="modal-head clarification-head">
            <span class="clarification-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M7 18 3 21V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v9a3 3 0 0 1-3 3zM8 8h8M8 12h5" /></svg>
            </span>
            <div class="clarification-heading">
              <h3 id="clarification-title">需要你补充信息</h3>
              <p>收到你的回答后，任务将继续。</p>
            </div>
          </div>
          <div class="modal-body clarification-body">
            <p id="clarification-question" class="clarification-question">{{ approval.clarifications[0].question }}</p>
            <div v-if="approval.clarifications[0].options?.length" class="clarification-options" role="group" aria-label="建议回答">
              <button v-for="option in approval.clarifications[0].options" :key="option"
                class="clarification-option" :class="{ selected: clarificationAnswer === option }"
                :aria-pressed="clarificationAnswer === option"
                :disabled="deciding" @click="selectClarificationOption(option)">
                <span class="clarification-option-mark" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="m4 8 2.5 2.5L12 5" /></svg></span>
                <span>{{ option }}</span>
              </button>
            </div>
            <div v-if="clarificationAnswerSelected" class="clarification-selection" role="status">
              <span>已选择，可直接提交</span>
              <button type="button" class="clarification-edit" :disabled="deciding" @click="editClarificationAnswer">修改回答</button>
            </div>
            <div v-else class="clarification-input">
              <label for="clarification-answer">你的回答 <span>{{ approval.clarifications[0].options?.length ? '选择上方选项，或在这里自由输入' : '可自由输入补充信息' }}</span></label>
              <textarea id="clarification-answer" ref="clarificationInputEl" v-model="clarificationAnswer" rows="3" maxlength="10000"
                :disabled="deciding" placeholder="写下你的想法或补充信息…"></textarea>
            </div>
            <p v-if="approval.clarifications.length > 1" class="clarification-note">还有 {{ approval.clarifications.length - 1 }} 个问题等待回答。</p>
            <p v-if="approval.actions.length" class="clarification-note">另有操作等待审批，回答问题不会自动批准这些操作。</p>
          </div>
          <div class="modal-foot">
            <button class="btn-ghost" :disabled="deciding" @click="decide(false)">取消任务</button>
            <button class="btn-primary" :disabled="deciding || !clarificationAnswer.trim()" @click="decide(true)">
              {{ deciding ? '提交中…' : '提交并继续' }}
              <svg class="clarification-submit-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h12m-5-5 5 5-5 5" /></svg>
            </button>
          </div>
        </div>
      </DialogOverlay>
      <DialogOverlay v-else-if="approval" key="approval" class="approval-overlay" :dismissible="false">
        <div class="modal approval-modal" role="dialog" aria-modal="true" aria-labelledby="approval-title">
          <div class="modal-head">
            <h3 id="approval-title">需要你确认</h3>
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
      </DialogOverlay>
      </Transition>

      <!-- 文件面板：浏览虚拟文件系统 -->
      <Transition name="settings-dialog">
      <DialogOverlay v-if="showFilePanel" class="file-overlay" close-on-backdrop @close="showFilePanel = false">
        <div class="modal file-modal" role="dialog" aria-modal="true" aria-labelledby="file-panel-title">
          <div class="modal-head row-head">
            <h3 id="file-panel-title">{{ currentWorkspace?.name || "默认工作区" }} · 文件</h3>
            <div class="modal-head-actions">
              <button class="btn-ghost sm" @click="openWorkspaceFolder">打开目录</button>
              <button class="btn-ghost sm" @click="loadFiles" :disabled="loadingFiles">
                {{ loadingFiles ? '载入中…' : '刷新' }}
              </button>
              <button class="btn-ghost sm" @click="showFilePanel = false">关闭</button>
            </div>
          </div>

          <p class="workspace-directory" :title="currentWorkspace?.directory">{{ currentWorkspace?.directory }}</p>
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
              <svg class="file-icon" viewBox="0 0 20 20" aria-hidden="true"><path v-if="f.is_dir" d="M2.5 5h6l2 2h7v9h-15z" /><path v-else d="M5 2.5h6l4 4v11H5zM11 2.5V7h4" /></svg>
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
      </DialogOverlay>
      </Transition>

      <Transition name="settings-dialog">
      <DialogOverlay v-if="nativeWorkspaceTicket" @close="cancelNativeWorkspace">
        <div class="modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby="native-workspace-title">
          <div class="modal-head">
            <h3 id="native-workspace-title">请在 Windows 窗口中选择目录</h3>
            <p>首次使用时，请允许浏览器打开 Agent Harness。若窗口未出现，可使用网页选择。</p>
          </div>
          <div class="modal-foot">
            <button class="btn-ghost" @click="cancelNativeWorkspace">取消</button>
            <button class="btn-ghost" @click="openBrowserWorkspace">使用网页选择</button>
          </div>
        </div>
      </DialogOverlay>
      </Transition>

      <Transition name="settings-dialog">
      <DialogOverlay v-if="workspacePicker" :dismissible="!creatingWorkspace" @close="workspacePicker = null">
        <div class="modal file-modal" role="dialog" aria-modal="true" aria-labelledby="workspace-picker-title">
          <div class="modal-head">
            <h3 id="workspace-picker-title">选择工作区目录</h3>
            <p>选择已挂载的磁盘目录，或在当前目录中新建文件夹。Docker 路径以容器挂载位置为准。</p>
          </div>
          <div class="modal-body file-list workspace-picker-body">
            <p class="confirm-path">{{ workspacePicker.directory || '可用目录' }}</p>
            <button class="btn-ghost" :disabled="workspaceBrowsing || creatingWorkspace" @click="browseWorkspace(workspacePicker.parent)">返回上级</button>
            <p v-if="workspacePickerError" role="alert">{{ workspacePickerError }}</p>
            <p v-if="workspaceBrowsing">正在读取目录…</p>
            <div v-for="entry in workspacePicker.entries" :key="entry.directory">
              <button class="btn-ghost" :disabled="workspaceBrowsing || creatingWorkspace" @click="browseWorkspace(entry.directory)">📁 {{ entry.name }}</button>
            </div>
            <p v-if="!workspaceBrowsing && !workspacePicker.entries.length">此目录没有子文件夹，可以直接选择或新建文件夹。</p>
            <label v-if="workspacePicker.directory" class="dialog-field">新文件夹名称（可选）
              <input v-model="workspaceFolderName" :disabled="creatingWorkspace" placeholder="例如：我的项目" @keyup.enter="confirmWorkspace" />
            </label>
          </div>
          <div class="modal-foot">
            <button class="btn-ghost" :disabled="creatingWorkspace" @click="workspacePicker = null">取消</button>
            <button class="btn-primary" :disabled="!workspacePicker.directory || workspaceBrowsing || creatingWorkspace" @click="confirmWorkspace">{{ creatingWorkspace ? '创建中…' : workspaceFolderName.trim() ? '新建并选择' : '选择此目录' }}</button>
          </div>
        </div>
      </DialogOverlay>
      </Transition>

      <!-- 删除确认：删除不可恢复，所以必须把完整路径摆给用户看，而不是只问"确定吗"。
           目录是**连同内容一起删**，所以单独一句强调。 -->
      <Transition name="settings-dialog">
      <DialogOverlay v-if="pendingDelete" :dismissible="!deleting" @close="pendingDelete = null">
        <div class="modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby="file-delete-title">
          <div class="modal-head">
            <h3 id="file-delete-title">确认删除</h3>
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
      </DialogOverlay>
      </Transition>

      <!--
        退出登录确认。
        为什么要确认：退出会清掉本地的会话列表、消息与审批弹窗，误点是"页面突然空了"，
        用户第一反应往往是"数据没了"。用与删除文件同一套确认框样式，保持观感一致。
      -->
      <Transition name="settings-dialog">
      <DialogOverlay v-if="logoutConfirm" @close="logoutConfirm = false">
        <div class="modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby="logout-title" aria-describedby="logout-description">
          <div class="modal-head">
            <h3 id="logout-title">确认退出登录</h3>
            <p id="logout-description">
              退出后需要重新输入邮箱和密码。正在生成的回答会继续，重新登录后可以查看。
            </p>
          </div>

          <div class="modal-body">
            <p class="confirm-path confirm-account">{{ user?.email }}</p>
          </div>

          <div class="modal-foot">
            <button class="btn-ghost" @click="logoutConfirm = false">取消</button>
            <button class="btn-danger" @click="confirmLogout">退出登录</button>
          </div>
        </div>
      </DialogOverlay>
      </Transition>

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
        <div class="composer-shell">
        <div class="composer-attachments" v-if="attachments.length" aria-label="待发送附件">
          <div class="attachment-card" v-for="(file, index) in attachments" :key="index">
            <img v-if="file.preview" class="attachment-thumb" :src="file.preview" :alt="file.name" />
            <div v-else class="attachment-file-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6M8 13h8M8 17h5" /></svg>
            </div>
            <div class="attachment-info">
              <span class="attachment-name" :title="file.name">{{ file.name }}</span>
              <span class="attachment-meta">{{ file.preview ? '图片' : '文件' }} · {{ file.size >= 1048576 ? (file.size / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(file.size / 1024)) + ' KB' }}</span>
            </div>
            <button type="button" class="attachment-remove" :aria-label="'移除附件：' + file.name" title="移除附件" @click="attachments.splice(index, 1)" :disabled="streaming">
              <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg>
            </button>
          </div>
        </div>
        <div class="composer-inner">
          <input ref="attachmentInput" type="file" multiple hidden accept=".png,.jpg,.jpeg,.webp,.gif,.txt,.md,.csv,.tsv,.json,.xml,.yaml,.yml,.log,.py,.js,.ts,.html,.css,.pdf,.docx" @change="selectAttachments" />
          <button class="btn-attach" title="添加图片或文件（每个最多 5 MB）" :disabled="streaming || loadingHistory || readingAttachments" @click="attachmentInput.click()" aria-label="添加图片或文件">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m21 11-8.5 8.5a6 6 0 0 1-8.5-8.5l9-9a4 4 0 0 1 5.7 5.7l-9 9a2 2 0 0 1-2.8-2.8L15 6" /></svg>
          </button>
          <textarea
            ref="inputEl"
            v-model="input"
            placeholder="给 Agent Harness 发消息"
            @keydown.enter.exact.prevent="send"
            @input="autoGrow"
            :disabled="streaming || loadingHistory"
            rows="1"
          ></textarea>
          <button v-if="streaming" class="btn-stop" title="停止生成" @click="stopStream">
            <span class="stop-square"></span>
          </button>
          <button
            v-else
            class="btn-send"
            title="发送"
            :disabled="(!input.trim() && !attachments.length) || readingAttachments"
            @click="send"
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 15.5V5M10 5l-4.5 4.5M10 5l4.5 4.5" /></svg>
          </button>
        </div>
        </div>
        <div class="composer-footer">
          <button class="settings-entry mobile-settings" title="个人设置" aria-haspopup="dialog" @click="showSettings = true">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14M7 3v4M13 8v4M8 13v4" /></svg>
            <span>设置</span>
          </button>
        <p class="disclaimer">{{ accessFull
          ? '完全访问：可修改已挂载磁盘；LocalShell 在后端所在环境执行命令。'
          : '默认模式：工作区可读写，已挂载磁盘只读；读磁盘时按审批设置确认。' }}</p>
        </div>
        <div class="main-workspace" aria-label="当前工作区">
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2.5 5.5h5l2 2h8v9h-15z" /></svg>
          <span class="main-workspace-label">当前工作区</span>
          <span class="main-workspace-name" :title="selectedWorkspace.title">{{ selectedWorkspace.title }}</span>
          <span v-if="selectedWorkspace.directory" class="main-workspace-directory" :title="selectedWorkspace.directory">{{ selectedWorkspace.directory }}</span>
        </div>
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
  streamClarification,
  streamChat,
  streamRunAttach,
} from './sse'
import { renderMarkdown, renderTerminal, clearMarkdownCache } from './markdown'
import { resizeComposer } from './chatPresentation'
import { answerTiming, elapsedSecondsLabel, freezeMeter, resumeMeter } from './generationMeter'
import { useSidebarResize } from './composables/useSidebarResize'
import { useMessageScroll } from './composables/useMessageScroll'
import { useToolActivity } from './composables/useToolActivity'
import { useMessageEvents } from './composables/useMessageEvents'
import ToolArguments from './ToolArguments.vue'
import TaskPlan from './TaskPlan.vue'
import { toolName } from './toolPresentation'
import AuthView from './AuthView.vue'
import ProvidersView from './ProvidersView.vue'
import McpView from './McpView.vue'
import SettingsView from './SettingsView.vue'
import DialogOverlay from './DialogOverlay.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { provideConfirmation } from './composables/useConfirmation'
import {
  apiDelete,
  apiFetchRaw,
  apiGet,
  apiPost,
  fetchAvailableProviders,
  fetchMe,
  getUser,
  getSessionVersion,
  logout as apiLogout,
  onUnauthorized,
  setSession,
  setUser,
} from './api'

function formatTime() {
  return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

export default {
  components: { ToolArguments, TaskPlan, AuthView, ProvidersView, McpView, SettingsView, DialogOverlay, ConfirmDialog },
  setup() {
    const { confirmation, confirm, resolveConfirmation } = provideConfirmation()
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
    const showSettings = ref(false)
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
      const version = getSessionVersion()
      providersLoading.value = true
      try {
        const data = await fetchAvailableProviders()
        if (version !== getSessionVersion()) return
        providers.value = data
      } catch (error) {
        if (error.name === 'StaleSessionError' || version !== getSessionVersion()) return
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
    const attachments = ref([])
    const attachmentInput = ref(null)
    const readingAttachments = ref(false)
    async function selectAttachments(event) {
      const files = Array.from(event.target.files || [])
      event.target.value = ''
      if (attachments.value.length + files.length > 5) return notify('最多添加 5 个附件', 'error')
      const total = attachments.value.reduce((sum, file) => sum + file.size, 0) + files.reduce((sum, file) => sum + file.size, 0)
      if (files.some(file => !file.size || file.size > 5 * 1024 * 1024) || total > 15 * 1024 * 1024) return notify('每个附件最多 5 MB，合计最多 15 MB，不能上传空文件', 'error')
      readingAttachments.value = true
      try {
        const added = await Promise.all(files.map(file => new Promise((resolve, reject) => {
          const reader = new FileReader()
          reader.onerror = () => reject(new Error('文件读取失败'))
          reader.onload = () => resolve({ name: file.name, size: file.size, data: String(reader.result).split(',')[1], preview: file.type.startsWith('image/') ? reader.result : null })
          reader.readAsDataURL(file)
        })))
        attachments.value.push(...added)
      } catch (e) { notify(e.message, 'error') }
      finally { readingAttachments.value = false }
    }
    const showTools = ref(true)
    // 待审批弹窗：{ session_id, actions } | null；deciding 表示决策已提交、正在恢复执行
    const approval = ref(null)
    const clarificationAnswer = ref('')
    const clarificationEditing = ref(false)
    const clarificationInputEl = ref(null)
    const clarificationAnswerSelected = computed(() => !clarificationEditing.value
      && !!approval.value?.clarifications?.[0]?.options?.includes(clarificationAnswer.value)
      && !!clarificationAnswer.value.trim())
    function selectClarificationOption(option) {
      clarificationAnswer.value = option
      clarificationEditing.value = false
    }
    async function editClarificationAnswer() {
      clarificationEditing.value = true
      await nextTick()
      clarificationInputEl.value?.focus()
    }
    watch(() => [approval.value?.session_id, approval.value?.clarifications?.[0]?.id], ([session, id], previous = []) => {
      if (id !== previous[1] || session !== previous[0]) {
        clarificationAnswer.value = ''
        clarificationEditing.value = false
      }
    }, { flush: 'sync' })
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
    let filesViewVersion = 0

    // 按会话维度管理流式状态：切换会话时原对话在后台继续输出
    // { sessionId: { messages: Array, streaming: boolean, controller: AbortController, loaded: boolean } }
    const sessionStates = reactive({})
    const { applyDataEvent, applyApprovalEvent, currentAssistantMessage, finishTurn, progressLabel, progressSize, applyToolOutcome } = useMessageEvents({ messages, sessionId, sessionStates, approval })

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

    let sessionViewVersion = 0
    watch(sessionId, (id) => {
      sessionViewVersion += 1
      loadingHistory.value = false
      try {
        if (id) window.localStorage.setItem(lastSessionKey, id)
        else window.localStorage.removeItem(lastSessionKey)
      } catch {
        // 存不了就算了，只影响"刷新后回到原处"这一便利
      }
    }, { flush: 'sync' })

    function truncate(s, n) {
      s = String(s ?? '')
      return s.length > n ? `${s.slice(0, n)}\n…（已截断）` : s
    }

    // ===== 顶栏标题 / 空状态建议词 =====
    const workspaces = ref([])
    const creatingWorkspace = ref(false)
    const nativeWorkspaceTicket = ref(null)
    const workspacePicker = ref(null)
    const workspaceBrowsing = ref(false)
    const workspacePickerError = ref('')
    const workspaceFolderName = ref('')
    const creatingChat = ref(false)
    const draftWorkspaceId = ref("legacy")
    const collapsedWorkspaces = reactive({})
    const workspaceGroups = computed(() => {
      const groups = new Map(workspaces.value.map(w => [w.id, { id: w.id, title: w.name, directory: w.directory, sessions: [] }]))
      for (const session of sessions.value) {
        const id = session.workspace_id || 'legacy'
        if (!groups.has(id)) groups.set(id, { id, title: id === 'legacy' ? '默认工作区' : '工作区', sessions: [] })
        groups.get(id).sessions.push(session)
      }
      return [...groups.values()]
    })

    const activeWorkspaceId = computed(() => {
      const session = sessions.value.find(s => s.id === sessionId.value)
      return session ? (session.workspace_id || 'legacy') : draftWorkspaceId.value
    })
    const selectedWorkspace = computed(() => workspaceGroups.value.find(w => w.id === activeWorkspaceId.value)
      || { id: activeWorkspaceId.value, title: activeWorkspaceId.value === 'legacy' ? '默认工作区' : '工作区', directory: '' })
    const currentWorkspace = computed(() => workspaces.value.find(w => w.id === activeWorkspaceId.value))

    function toggleWorkspace(id) { collapsedWorkspaces[id] = !collapsedWorkspaces[id] }

    const currentTitle = computed(() => {
      const cur = sessions.value.find((s) => s.id === sessionId.value)
      return cur?.title || '新对话'
    })

    const { hasActivity, toolOutcomeText, toolDetailNotice, activityLatest, activityHistory, activeDelegations, doneDelegations } = useToolActivity({ showTools, elapsedLabel, progressLabel, progressSize })

    // ===== 整数秒计时器 =====
    const nowTick = ref(Date.now())

    /** 秒级刷新的心跳：只在有消息在流式输出时才走。 */
    function startTicker() {
      if (ticker) return
      nowTick.value = Date.now()
      ticker = setInterval(() => { if (anyStreaming()) nowTick.value = Date.now(); else stopTicker() }, 1000)
    }

    function stopTicker() {
      if (ticker) { clearInterval(ticker); ticker = null }
    }

    // Only the visible conversation drives the UI clock.
    function anyStreaming() {
      return messages.some(message => message.role === 'assistant' && message.streaming)
    }
    watch(() => [sessionId.value, anyStreaming()], () => {
      if (anyStreaming()) startTicker()
      else stopTicker()
    })
    /** 耗时展示：只显示完整秒数，结束后固定。 */
    function elapsedLabel(startedAt, endedAt = null) {
      return elapsedSecondsLabel(startedAt, endedAt, nowTick.value)
    }

    // ===== 输入框：自适应高度 / 建议词 / 按钮式开关 =====
    const inputEl = ref(null)

    /** 让 textarea 跟着内容长高（上限由 CSS 的 max-height 兜住）。 */
    function autoGrow() { resizeComposer(inputEl.value) }
    watch(input, () => { nextTick(autoGrow) })



    /** 顶栏的审批开关按钮（原来是 checkbox，现在点按钮切换）。 */
    function toggleHitlBtn() {
      toggleHitl({ target: { checked: !hitlOn.value } })
    }

    const { SIDEBAR_MIN, SIDEBAR_MAX, sidebarEl, sidebarWidth, resizing, startResize, nudgeSidebar, resetSidebarWidth } = useSidebarResize()

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

    const { stickToBottom, hasNewBelow, onMessagesScroll, onMessagesWheel, scrollToBottom, jumpToBottom } = useMessageScroll(messages, scroller)

    function fileSessionQuery() { return `&workspace_id=${encodeURIComponent(activeWorkspaceId.value)}` + (sessionId.value ? `&session_id=${encodeURIComponent(sessionId.value)}` : '') }

    async function loadSessions() {
      const version = getSessionVersion()
      loadingSessions.value = true
      try {
        const [data, workspaceData] = await Promise.all([apiGet('/api/v1/sessions'), apiGet('/api/v1/workspaces')])
        if (version !== getSessionVersion()) return
        workspaces.value = workspaceData
        const serverList = Array.isArray(data) ? data : []
        // 保留前端乐观创建的临时项（尚未收到真实 session_id 的新会话）
        const tempItems = sessions.value.filter((s) => s._temp || (typeof s.id === 'string' && s.id.startsWith('temp_')))
        sessions.value = [...tempItems, ...serverList]
      } catch {
        // 忽略网络错误，保留旧列表（401 已由 api 层统一处理为回登录页）
      }
      if (version === getSessionVersion()) loadingSessions.value = false
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
      const viewVersion = sessionViewVersion
      let info
      try {
        info = await fetchApproval(sid)
      } catch {
        // 后端不可用之类：静默，别在打开会话时弹一堆错
        return
      }
      const pending = !!(info?.pending && ((info.actions || []).length || (info.clarifications || []).length))
      if (!pending) {
        if (sessionStates[sid]) sessionStates[sid].approval = null
        if (sessionId.value === sid && sessionViewVersion === viewVersion) approval.value = null
        return
      }
      const payload = { ...info, actions: info.actions || [], clarifications: info.clarifications || [], session_id: sid }
      if (sessionStates[sid]) sessionStates[sid].approval = payload
      if (sessionId.value === sid && sessionViewVersion === viewVersion) {
        approval.value = payload
        notify(payload.clarifications.length ? '任务正在等待你补充信息' : '有一个操作在等你确认')
      }
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

      if (!state || sessionStates[sid] !== state || state.streaming) return false
      const target = msgs || state.messages

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
          if ((evt.type === 'approval' || evt.type === 'clarification')) {
            applyApprovalEvent(fresh, evt, sid)
          } else if (evt.type === 'stopped') {
            fresh.stopped = true
            fresh.streaming = false
            if (!fresh.content) fresh.content = `（${evt.detail || '已停止'}）`
          } else if (evt.type === 'error') {
            fresh.content = `⚠️ ${evt.detail}`
            fresh.error = true
          }
        })
      } catch (e) {
        if (e.name === 'StreamDisconnectedError') {
          markConnectionLost(fresh, e)
        } else if (e.name !== 'AbortError' && e.name !== 'StaleSessionError') {
          fresh.error = true
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
      if ((id === sessionId.value && (loadingHistory.value || sessionStates[id]?.loaded)) || (typeof id === 'string' && id.startsWith('temp_'))) return
      sessionId.value = id
      const viewVersion = ++sessionViewVersion
      const isCurrent = () => sessionId.value === id && sessionViewVersion === viewVersion
      stickToBottom.value = true
      hasNewBelow.value = false
      syncProviderFromSession(id)
      approval.value = null
      // Immediately stop showing the previous conversation under the new title.
      messages.splice(0, messages.length)
      if (sessionStates[id]?.loaded) {
        messages.push(...noteBlankTail(sessionStates[id].messages))
        loadingHistory.value = false
      } else {
        loadingHistory.value = true
        try {
          const data = await apiGet(`/api/v1/sessions/${id}/messages`)
          if (!isCurrent()) return
          const list = (Array.isArray(data) ? data : []).map(m => ({
            ...m,
            toolCalls: Array.isArray(m.toolCalls) ? m.toolCalls : [],
            delegations: Array.isArray(m.delegations) ? m.delegations : [],
            streaming: false,
            time: '',
          }))
          noteBlankTail(list)
          sessionStates[id] = { messages: reactive(list), streaming: false, controller: null, loaded: true, approval: null }
          messages.push(...sessionStates[id].messages)
        } catch (e) {
          if (!isCurrent()) return
          if (!isModelMissingError(e)) {
            messages.push({ role: 'assistant', content: `⚠️ 载入历史失败：${e.message}`, toolCalls: [], delegations: [], streaming: false, time: '' })
          }
        } finally {
          if (isCurrent()) loadingHistory.value = false
        }
      }
      await nextTick()
      if (!isCurrent()) return
      scrollToBottom(true)
      resumeRun(id)
      restorePendingApproval(id)
    }

    async function newChat(workspaceId = 'legacy') {
      if (creatingChat.value || loadingHistory.value) return
      const current = sessions.value.find(s => s.id === sessionId.value)
      const currentWorkspaceId = current?.workspace_id || draftWorkspaceId.value
      if (!streaming.value && messages.length === 0 && currentWorkspaceId === workspaceId) return
      resetChat()
      draftWorkspaceId.value = workspaceId
      collapsedWorkspaces[workspaceId] = false
    }

    async function browseWorkspace(directory = null) {
      if (workspaceBrowsing.value) return
      workspaceBrowsing.value = true
      workspacePickerError.value = ''
      try {
        workspacePicker.value = await apiGet('/api/v1/workspaces/directories' + (directory ? `?directory=${encodeURIComponent(directory)}` : ''))
        workspaceFolderName.value = ''
      } catch (e) { workspacePickerError.value = e.message }
      finally { workspaceBrowsing.value = false }
    }

    function cancelNativeWorkspace() {
      nativeWorkspaceTicket.value = null
      creatingWorkspace.value = false
    }

    async function openBrowserWorkspace() {
      cancelNativeWorkspace()
      workspacePicker.value = { directory: null, parent: null, entries: [] }
      await browseWorkspace()
    }

    async function createWorkspace() {
      if (creatingWorkspace.value || workspaceBrowsing.value) return
      // A Windows protocol is installed on the deployment's host, so remote
      // browser clients should use the server directory browser instead.
      if (!['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname)) {
        await openBrowserWorkspace()
        return
      }
      creatingWorkspace.value = true
      let ticket = null
      try {
        let workspace = await apiPost('/api/v1/workspaces/pick-directory', {})
        if (workspace.browser_picker) {
          await openBrowserWorkspace()
          return
        }
        if (workspace.open_url) {
          ticket = workspace.ticket
          nativeWorkspaceTicket.value = ticket
          window.location.href = workspace.open_url
          const deadline = Date.now() + 300000
          while (nativeWorkspaceTicket.value === ticket && Date.now() < deadline) {
            await new Promise(resolve => setTimeout(resolve, 800))
            if (nativeWorkspaceTicket.value !== ticket) return
            workspace = await apiGet(`/api/v1/workspaces/picker/${ticket}`)
            if (nativeWorkspaceTicket.value !== ticket) return
            if (!workspace.pending) break
          }
          if (nativeWorkspaceTicket.value !== ticket) return
          if (workspace.pending) throw new Error('目录选择已超时，请重试。')
        }
        if (workspace.cancelled) return
        await loadSessions()
        await newChat(workspace.id)
      } catch (e) {
        if (!ticket || nativeWorkspaceTicket.value === ticket) notify(`新建工作区失败：${e.message}`, 'error')
      } finally {
        if (!ticket || nativeWorkspaceTicket.value === ticket) cancelNativeWorkspace()
      }
    }

    async function confirmWorkspace() {
      if (creatingWorkspace.value || workspaceBrowsing.value || !workspacePicker.value?.directory) return
      const name = workspaceFolderName.value.trim()
      if (name && (name === '.' || name === '..' || /[\\/:*?"<>|]/.test(name))) {
        workspacePickerError.value = '请输入有效的文件夹名称，不能包含路径分隔符。'
        return
      }
      creatingWorkspace.value = true
      workspacePickerError.value = ''
      try {
        const base = workspacePicker.value.directory
        const directory = name ? `${base.replace(/[\\/]$/, '')}/${name}` : base
        const workspace = await apiPost('/api/v1/workspaces', { directory, create: Boolean(name) })
        await loadSessions()
        await newChat(workspace.id)
        workspacePicker.value = null
      } catch (e) { workspacePickerError.value = e.message }
      finally { creatingWorkspace.value = false }
    }

    async function deleteWorkspace(workspace) {
      if (!await confirm({ title: '删除工作区', description: '工作区及其全部会话将被删除，目录中的文件会保留。', details: workspace.title, confirmLabel: '删除工作区', danger: true })) return
      try {
        await apiDelete(`/api/v1/workspaces/${workspace.id}`)
        if (workspace.sessions.some(s => s.id === sessionId.value) || (!sessionId.value && draftWorkspaceId.value === workspace.id)) resetChat()
        for (const session of workspace.sessions) {
          sessionStates[session.id]?.controller?.abort()
          delete sessionStates[session.id]
        }
        await loadSessions()
      } catch (e) { notify(`删除工作区失败：${e.message}`, 'error') }
    }

    function resetChat() {
      sessionViewVersion += 1
      draftWorkspaceId.value = "legacy"
      // 允许在流式输出时创建新对话，原对话在后台继续
      sessionId.value = null
      // 新会话不继承上一个会话的模型选择，回到"我自己的默认"。
      // 继承的话用户会莫名其妙地用着上一条会话挑的模型，而且看不出来。
      const mine = providers.value.find((p) => p.is_default && !p.is_system)
      chosenProvider.value = mine ? mine.id : ""
      messages.splice(0, messages.length) // 清空 → 回到欢迎页
      approval.value = null
      input.value = ''
      attachments.value = []
      stickToBottom.value = true
      hasNewBelow.value = false    }

    async function deleteSession(id) {
      const session = sessions.value.find(s => s.id === id)
      if (!await confirm({ title: '删除会话', description: '此会话的消息记录将被删除，此操作不可恢复。', details: session?.title || '新会话', confirmLabel: '删除会话', danger: true })) return
      try {
        await apiDelete(`/api/v1/sessions/${id}`)
      } catch (e) {
        // 404 = 会话不存在或不属于当前用户（后端刻意不区分）；
        // 唯一可信的真相是重新拉列表，所以这里只提示、不阻止后续刷新。
        notify(`删除失败：${e.message}`, 'error')
        await loadSessions()
        return
      }
      sessionStates[id]?.controller?.abort()
      delete sessionStates[id]
      if (sessionId.value === id) resetChat()
      await loadSessions()
    }

    async function send() {
      const text = input.value.trim() || (attachments.value.length ? '请分析这些附件。' : '')
      if (!text || streaming.value || loadingHistory.value || readingAttachments.value) return
      if (!sessionId.value && sessions.value.some(s => s._temp && s.workspace_id === draftWorkspaceId.value)) return

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
      const sentImages = attachments.value.filter(file => file.preview).map(file => file.preview)
      const sentAttachments = attachments.value.map(file => ({ name: file.name, data: file.data }))
      attachments.value = []
      input.value = ''

      const controller = new AbortController()
      const startingSessionId = sessionId.value // 新会话时为 null
      const startingViewVersion = sessionViewVersion
      const startingWorkspaceId = draftWorkspaceId.value
      let resolvedSessionId = startingSessionId

      // 新会话：乐观创建侧边栏项（不等后端返回 session_id）
      let tempId = null
      if (startingSessionId === null) {
        tempId = `temp_${Date.now()}`
        const shortTitle = text.length > 30 ? text.slice(0, 30) + '…' : text
        sessions.value.unshift({
          id: tempId,
          workspace_id: startingWorkspaceId,
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

      const userMsg = { role: 'user', content: text + sentAttachments.map(file => '\n[附件：' + file.name + ']').join(''), time: formatTime(), images: sentImages }
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
        outTokens: 0,      // 后端生成用量
        outputTokens: 0,
      })
      startTicker()

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
            if (sessionId.value === null && sessionViewVersion === startingViewVersion) {
              sessionId.value = evt.session_id
            }
            // 把侧边栏的乐观临时项替换为真实 session_id
            if (tempId) {
              const idx = sessions.value.findIndex((s) => s.id === tempId)
              if (idx !== -1) {
                sessions.value[idx] = { ...sessions.value[idx], id: evt.session_id, workspace_id: startingWorkspaceId, _temp: false }
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
          } else if ((evt.type === 'approval' || evt.type === 'clarification')) {
            const sid = resolvedSessionId || startingSessionId
            applyApprovalEvent(assistantMsg, evt, sid)
          } else if (evt.type === 'stopped') {
            // 用户点了停止：不是错误，如实说明即可
            assistantMsg.stopped = true
            assistantMsg.streaming = false
            if (!assistantMsg.content) assistantMsg.content = `（${evt.detail || '已停止'}）`
          } else if (evt.type === 'error') {
            assistantMsg.content = `⚠️ ${evt.detail}`
            assistantMsg.error = true
          }
        }, chosenProvider.value || null, startingWorkspaceId, sentAttachments)
      } catch (e) {
        if (e.name === 'StreamDisconnectedError') {
          markConnectionLost(assistantMsg, e)
        } else if (e.name !== 'AbortError' && e.name !== 'StaleSessionError') {
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

    function markConnectionLost(message, error) {
      message.connectionLost = true
      message.error = true
      message.content = `${message.content || ''}${message.content ? '\n\n' : ''}⚠️ ${error.message}`
    }

    async function decide(approve) {
      if (deciding.value) return // 正在处理：忽略重复点击
      const info = approval.value
      if (!info) return
      const sid = info.session_id || sessionId.value
      if (!sid) return

      const question = info.clarifications?.[0]
      const answer = clarificationAnswer.value.trim()
      const actionLabel = question ? '回答' : '审批'
      if (question && approve && !answer) return
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
        target.planPaused = false
        target.stopped = false
        target.planFinished = false
        resumeMeter(target)
        target.streaming = true
        if (!target.startedAt) target.startedAt = Date.now()
        startTicker()

        try {
          const submit = question
            ? (signal, onEvent) => streamClarification(sid, question.id, answer, !approve, signal, onEvent)
            : (signal, onEvent) => streamApproval(sid, approve ? 'approve' : 'reject', signal, onEvent)
          await submit(controller.signal, (evt) => {
            if (evt.type === 'done') {
              state.done = true
            } else if (applyDataEvent(target, evt)) {
              // 数据类事件已统一处理
            } else if ((evt.type === 'approval' || evt.type === 'clarification')) {
              // 恢复过程中又挂了新的审批（例如模型接着要改另一个文件）
              state.newApproval = true
              applyApprovalEvent(target, evt, sid)
            } else if (evt.type === 'stopped') {
              target.stopped = true
              target.content = target.content || `（${evt.detail || '已停止'}）`
            } else if (evt.type === 'error') {
              state.error = evt.detail || '未知错误'
            }
          })
        } catch (e) {
          if (e.name === 'StreamDisconnectedError') {
            markConnectionLost(target, e)
            notify(e.message, 'error')
          } else if (e.name !== 'AbortError' && e.name !== 'StaleSessionError') state.error = e.message
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
              target.content = target.content || `（${actionLabel}已提交，正在同步最新结果…）`
            }
            notify(`${actionLabel}已提交过，正在同步最新结果…`)
            refreshSession(sid)
          } else if (/无待审批项|无待回答问题/.test(state.error)) {
            // 这条审批**已经不在了**：可能刚被处理掉，也可能人工介入已切到「自动执行」
            // （那次切换会把挂起的中断统一用"拒绝"收尾，见后端 clear_pending_interrupt）。
            // 重点是**把弹窗关掉并同步**：旧实现只提示"已审批过"却留着弹窗，
            // 用户于是反复点同一个按钮，观感就是"点了没用"。
            approval.value = null
            if (sessionStates[sid]) sessionStates[sid].approval = null
            if (!state.newApproval) {
              target.content = target.content || '（该待处理事项已失效，正在同步最新结果…）'
            }
            notify(question ? '问题已处理或已失效，正在同步' : '审批已处理或已失效，正在同步')
            refreshSession(sid)
            restorePendingApproval(sid)
          } else {
            target.content = `⚠️ ${question ? "回答提交" : "审批"}失败：${state.error}`
            target.error = true
            // 仍未决：把弹窗放回来，用户可以直接重试
            if (!state.newApproval && info) {
              approval.value = info
              clarificationAnswer.value = answer
              if (sessionStates[sid]) sessionStates[sid].approval = info
            }
            // 同时给一条明显提示：错误写在对话气泡里时，用户很可能看不到
            // （气泡在视口外），于是以为"点了没反应"。
            notify(`${question ? "回答提交" : "审批"}失败：${state.error}`, 'error')
          }
        }
      } finally {
        deciding.value = false
        loadSessions()
      }
    }

    /** 静默重载某会话历史（用于审批状态与后端不一致时的兜底同步）。 */
    async function refreshSession(id) {
      const originalState = sessionStates[id]
      const viewVersion = sessionViewVersion
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
        if (state && state === originalState && !state.streaming) {
          state.messages = reactive(list)
          state.loaded = true
          state.streaming = false
          state.approval = null
          if (id === sessionId.value && viewVersion === sessionViewVersion && !loadingHistory.value) messages.splice(0, messages.length, ...list)
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
      const version = getSessionVersion()
      try {
        const cfg = await fetchHitlConfig()
        if (version !== getSessionVersion()) return
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
      const version = getSessionVersion()
      try {
        const cfg = await fetchAccessConfig()
        if (version !== getSessionVersion()) return
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
        const ok = await confirm({
          title: '切换到完全访问',
          description: '可以修改和删除所有已挂载磁盘中的文件，不再弹出审批。新模式从下一轮对话开始生效。',
          details: '命令将在后端 LocalShell 执行，无需 OpenSandbox。Docker 部署使用容器里的 Linux shell。后端重启后会恢复为沙箱模式。',
          confirmLabel: '切换到完全访问', danger: true,
        })
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
            if (sessionStates[id] && !sessionStates[id].approval?.clarifications?.length) sessionStates[id].approval = null
          }
          if (!approval.value?.clarifications?.length) approval.value = null
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
        const result = await stopRun(sid)
        if (result.status === 'stopping') {
          notify(result.detail || '任务正在停止，请稍候')
          return
        }
      } catch (e) {
        notify(`停止失败：${e.message}`, 'error')
        return
      }
      // 后端会推一条 stopped 事件并以 done 收尾，本地流随后自然结束；
      // 但用户可能希望立刻看到反馈，这里先把状态标上。
      if (state?.messages) {
        const last = state.messages[state.messages.length - 1]
        if (last && last.role === 'assistant' && last.streaming) {
          last.stopped = true
          freezeMeter(last)
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
      const viewVersion = ++filesViewVersion
      const query = fileSessionQuery()
      const path = filePath.value
      loadingFiles.value = true
      try {
        const data = await apiGet(`/api/v1/files?path=${encodeURIComponent(path)}${query}`)
        if (viewVersion !== filesViewVersion || query !== fileSessionQuery()) return
        fileList.value = Array.isArray(data?.entries) ? data.entries : []
      } catch {
        if (viewVersion === filesViewVersion && query === fileSessionQuery()) fileList.value = []
      } finally {
        if (viewVersion === filesViewVersion) loadingFiles.value = false
      }
    }

    async function openWorkspaceFolder() {
      const query = fileSessionQuery()
      try {
        const result = await apiPost(`/api/v1/files/open-workspace?${query.slice(1)}`, {})
        if (query !== fileSessionQuery()) return
        if (result.open_url) {
          window.location.href = result.open_url
          notify('请允许浏览器打开工作区目录；此入口用于部署者本机。', 'info')
        } else {
          notify('已打开当前工作区目录', 'info')
        }
      } catch (e) {
        notify(`无法打开目录：${e.message}`, 'error')
      }
    }

    // 用户视角的工作区根。虚拟根 '/' 只是文件系统顶层（内容与 /workspace 同一份，
    // 因为 deepagents 的 route 命中后会剥前缀，两者必然互为别名），
    // 所以面板只把 '/workspace' 当作用户工作区展示，避免同一批文件出现两次。
    const WORKSPACE_ROOT = '/workspace'

    watch(() => [sessionId.value, activeWorkspaceId.value], () => {
      filesViewVersion += 1
      fileList.value = []
      filePath.value = WORKSPACE_ROOT
      loadingFiles.value = false
      pendingDelete.value = null
      if (showFilePanel.value) loadFiles()
    })

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
        const resp = await apiFetchRaw(`/api/v1/files/download?path=${encodeURIComponent(path)}${fileSessionQuery()}`)
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
        await apiDelete(`/api/v1/files?path=${encodeURIComponent(info.path)}${fileSessionQuery()}`)
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
      const version = getSessionVersion()
      useLastSessionKeyFor(user.value)
      await loadHitlConfig()
      if (version !== getSessionVersion()) return
      // 权限模式也要在进入界面时拉一次（后端是内存态，重启后回到沙箱内；
      // 不拉的话界面会继续显示上一次的"完全访问"，与后端实际状态不符）。
      await loadAccessConfig()
      if (version !== getSessionVersion()) return
      // 先把可用供应商拉出来 —— 会话列表里要显示/恢复各自的模型绑定，
      // 而且"一个都没有"时欢迎页要给引导。
      await loadProviders()
      if (version !== getSessionVersion()) return
      // 刷新 / 关掉再打开：回到上次在看的会话。
      // 如果那一轮回答还在后端跑着，openSession 里会自动接回来 —— 这就是
      // "刷新不打断回答"的用户可见效果。
      await loadSessions()
      if (version !== getSessionVersion()) return
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
      cancelNativeWorkspace()
      workspacePicker.value = null
      // 停掉本地所有流（后端的运行不受影响，这是的语义：连接断了它继续跑）
      stopAllStreams()
      sessionId.value = null
      sessions.value = []
      workspaces.value = []
      draftWorkspaceId.value = "legacy"
      messages.splice(0, messages.length)
      approval.value = null
      resetProviderState()
      resetPrivateState()
      apiLogout()
      user.value = null
    }

    function onPasswordChanged() {
      doLogout()
      authNotice.value = '密码已更新，请使用新密码重新登录。'
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
      showSettings.value = false
    }

    /** 凭证失效（任意请求 401）→ 回登录页并说明原因。 */
    function handleUnauthorized(detail) {
      if (!user.value) return // 本来就没登录，不用再"登出一次"
      stopAllStreams()
      sessionId.value = null
      sessions.value = []
      workspaces.value = []
      draftWorkspaceId.value = "legacy"
      messages.splice(0, messages.length)
      approval.value = null
      resetProviderState()
      resetPrivateState()
      user.value = null
      authNotice.value = detail || '登录已失效，请重新登录'
    }

    function resetPrivateState() {
      resolveConfirmation(false)
      sessionViewVersion += 1
      filesViewVersion += 1
      cancelNativeWorkspace()
      workspacePicker.value = null
      for (const key of Object.keys(sessionStates)) delete sessionStates[key]
      for (const key of Object.keys(collapsedWorkspaces)) delete collapsedWorkspaces[key]
      attachments.value = []
      fileList.value = []
      filePath.value = '/workspace'
      pendingDelete.value = null
      showFilePanel.value = false
      loadingFiles.value = false
      providersLoading.value = false
      accessMode.value = 'sandbox'
      clearMarkdownCache()
    }

    let stopAuthWatch = null

    onMounted(async () => {
      // 401 的统一出口：任何一个请求（含 SSE 流里读到的）失效，
      // 都在这里切回登录视图 —— 不需要每个调用点自己处理。
      stopAuthWatch = onUnauthorized(handleUnauthorized)


      // 启动探测：本地有 token 就拿 /auth/me 验一次（防止"看着像登录了，
      // 其实 token 早过期"，那种状态会让首屏到处报错）。
      if (user.value) {
        try {
          const me = await fetchMe()
          user.value = me
          setUser(me)
        } catch (error) {
          // 失效：api 层已清掉本地凭证，这里只需回到登录视图
          if (error.name !== 'StaleSessionError') user.value = null
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
      confirmation, resolveConfirmation,
      doLogout,
      userDisplayName,
      userInitial,
      isAdmin,
      // 模型供应商
      providers,
      providersLoading,
      showProviders,
      showMcp,
      showSettings,
      onPasswordChanged,
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
      clarificationAnswer,
      clarificationAnswerSelected,
      clarificationInputEl,
      selectClarificationOption,
      editClarificationAnswer,
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
      renderMarkdown, renderTerminal, toolName,
      truncate,
      argPairs,
      isSessionStreaming,
      // 新版界面
      workspaceGroups, activeWorkspaceId, selectedWorkspace,
      nativeWorkspaceTicket, cancelNativeWorkspace, openBrowserWorkspace,
      creatingWorkspace, workspacePicker, workspaceBrowsing, workspacePickerError, workspaceFolderName, browseWorkspace, confirmWorkspace,
      currentWorkspace,
      createWorkspace,
      deleteWorkspace,
      collapsedWorkspaces,
      toggleWorkspace,
      currentTitle,
      hasActivity,
      // 工具活动：只展示最新一条
      activityLatest,
      activityHistory,
      activeDelegations,
      doneDelegations,
      elapsedLabel,
      answerTiming,
      nowTick,
      // 运行可见性（成败 / 实时耗时 / 失联判定 / 重新连接）
      toolOutcomeText,
      toolDetailNotice,
      // 滚动跟随
      stickToBottom,
      hasNewBelow,
      onMessagesScroll,
      onMessagesWheel,
      jumpToBottom,
      inputEl, attachments, attachmentInput, readingAttachments, selectAttachments,
      autoGrow,
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

<style src="./styles/app.css"></style>

<style scoped>
.sb-workspace { position: relative; margin-bottom: 14px; }
.sb-workspace-sticky { position: sticky; top: 0; z-index: 3; padding: 6px 0; background: var(--sidebar); }
.sb-workspace-head { display: flex; align-items: center; gap: 2px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg); }
.sb-workspace.selected .sb-workspace-head { border-color: var(--border-2); background: var(--active); }
.sb-workspace-toggle { display: flex; align-items: center; gap: 7px; min-width: 0; flex: 1; border: 0; background: transparent; padding: 10px 8px; color: var(--text-2); cursor: pointer; text-align: left; }
.sb-workspace-toggle .sb-title { font-weight: 600; color: var(--text-2); font-size: 12px; }
.sb-workspace.selected .sb-workspace-toggle .sb-title { color: var(--text); }
.sb-workspace-chevron { width: 12px; height: 12px; flex-shrink: 0; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; transition: transform 0.15s; }
.sb-workspace-chevron.expanded { transform: rotate(90deg); }
.sb-workspace-folder { width: 18px; height: 18px; flex-shrink: 0; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linejoin: round; }
.sb-workspace-count { font-size: 10px; color: var(--text-3); padding: 1px 5px; background: var(--fill); border-radius: 5px; }
.sb-workspace-add { display: grid; place-items: center; width: 26px; height: 28px; flex-shrink: 0; border: 0; border-radius: 6px; background: transparent; color: var(--text-3); font-size: 18px; cursor: pointer; padding: 0; }
.sb-workspace-add:hover { background: var(--hover); color: var(--text); }
.sb-workspace-head > :last-child { margin-right: 4px; }
.main-workspace { display: flex; align-items: center; gap: 7px; min-width: 0; margin-top: 10px; padding-top: 6px; font-size: 11px; color: var(--text-3); }
.main-workspace svg { width: 15px; height: 15px; flex-shrink: 0; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linejoin: round; }
.main-workspace-label { flex-shrink: 0; }
.main-workspace-name { flex-shrink: 1; min-width: 0; max-width: 240px; color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.main-workspace-directory { min-width: 0; flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding-left: 7px; border-left: 1px solid var(--border); font-size: 10px; }
@media (max-width: 600px) { .main-workspace-name { max-width: 130px; } }
.sb-workspace .sb-item { margin-left: 16px; }
</style>

<style scoped>
.sb-top .sb-brand { padding: 8px 8px 18px; }
.sb-new:disabled { opacity: .55; cursor: wait; }
</style>

<style scoped>
.workspace-directory { margin: 0; padding: 10px 20px; border-bottom: 1px solid var(--border); color: var(--text-2); font-size: 12px; overflow-wrap: anywhere; }
</style>

<style scoped>
.history-loading { display: grid; place-items: center; height: 100%; color: var(--text-2); font-size: 14px; }
</style>

<style scoped>
.attachment-list { display: flex; flex-wrap: wrap; gap: 8px; padding: 8px; }
.attachment-list span { display: flex; align-items: center; gap: 6px; max-width: 100%; overflow-wrap: anywhere; font-size: 13px; }
.attachment-list img { width: 48px; height: 48px; object-fit: cover; border-radius: 6px; }
.composer-attachments { display: flex; gap: 8px; overflow-x: auto; padding: 12px 12px 2px; }
.attachment-card { display: flex; align-items: center; gap: 10px; flex: 0 0 244px; min-width: 0; padding: 8px; border: 1px solid var(--border); border-radius: 14px; background: var(--fill); }
.attachment-thumb, .attachment-file-icon { width: 44px; height: 44px; flex-shrink: 0; border-radius: 9px; }
.attachment-thumb { object-fit: cover; background: var(--bg); }
.attachment-file-icon { display: grid; place-items: center; background: var(--bg); color: var(--text-2); }
.attachment-file-icon svg { width: 23px; height: 23px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.attachment-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.attachment-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); font-size: 12px; font-weight: 500; }
.attachment-meta { font-size: 11px; color: var(--text-3); }
.attachment-remove { display: grid; place-items: center; width: 26px; height: 26px; flex-shrink: 0; border: 0; border-radius: 50%; background: transparent; color: var(--text-3); cursor: pointer; transition: background 0.15s, color 0.15s; }
.attachment-remove svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; }
.attachment-remove:hover { background: var(--fill-2); color: var(--text); }
.btn-attach { display: grid; place-items: center; flex: 0 0 32px; height: 32px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--text-2); cursor: pointer; transition: background 0.15s, color 0.15s; }
.btn-attach svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.btn-attach:hover { background: var(--fill); color: var(--text); }
.btn-attach:disabled, .attachment-remove:disabled { opacity: 0.4; cursor: default; }
.btn-attach:focus-visible, .attachment-remove:focus-visible { outline: 2px solid var(--text-2); outline-offset: 2px; }
@media (max-width: 600px) { .attachment-card { flex-basis: 210px; } }
</style>

<style src="./styles/markdown.css"></style>

<style src="./styles/activity.css"></style>

<style src="./styles/activity-archive.css"></style>
