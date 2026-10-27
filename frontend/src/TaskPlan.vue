<template>
  <Transition name="task-plan-collapse" appear>
    <section v-if="state.hasCall && !hidden" class="task-plan-shell" aria-label="任务清单" aria-live="polite" :aria-busy="state.updating">
      <div class="task-plan-clip">
        <div class="task-plan" :class="{ 'is-complete': state.complete }">
          <div class="task-plan-head">
            <span>{{ state.complete ? '任务全部完成' : '任务清单' }}</span>
            <span>{{ completed }} / {{ state.items.length }}</span>
          </div>
          <div v-if="state.items.length" class="task-plan-progress" aria-hidden="true">
            <span :style="{ width: `${completed / state.items.length * 100}%` }"></span>
          </div>
          <TransitionGroup name="task-plan-item" tag="ol" v-if="state.items.length">
            <li v-for="item in state.items" :key="item.key" :class="item.status">
              <span class="task-plan-mark" aria-hidden="true">
                <svg class="task-plan-check" viewBox="0 0 16 16"><path pathLength="1" d="m3 8 3 3 7-7" /></svg>
                <span v-if="item.status === 'running'" class="task-plan-spinner"></span>
                <span v-else-if="item.status !== 'completed'" class="task-plan-dot"></span>
              </span>
              <span class="task-plan-text"><span class="task-plan-label">{{ item.text }}</span></span>
              <span class="task-plan-state">{{ item.status === 'completed' ? '已完成' : item.status === 'running' ? '运行中' : '等待中' }}</span>
            </li>
          </TransitionGroup>
          <p v-else class="task-plan-notice">{{ state.updating ? '正在生成任务清单…' : '清单中暂无任务' }}</p>
          <p v-if="state.error" class="task-plan-notice is-error" role="status">{{ state.error }}</p>
          <p v-else-if="state.updating && state.items.length" class="task-plan-notice">正在保存清单…</p>
        </div>
      </div>
    </section>
  </Transition>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { taskPlanState } from './toolPresentation.js'

const props = defineProps({ message: { type: Object, required: true } })
const state = computed(() => taskPlanState(props.message))
const completed = computed(() => state.value.items.filter(item => item.status === 'completed').length)
const hidden = ref(false)
let hideTimer

// Unrelated tool calls and token events must not restart the completion delay.
watch([() => props.message, () => JSON.stringify(state.value)], ([message], previous) => {
  const plan = state.value
  clearTimeout(hideTimer)
  const switched = !previous || message !== previous[0]
  if (switched) {
    // Historical finished plans stay hidden instead of replaying on every visit.
    hidden.value = Boolean(plan.complete && !message.streaming && !message.planPaused)
  } else if (!plan.complete) {
    hidden.value = false
  }
  if (plan.complete && !hidden.value) {
    // Let the final check and strike-through finish before collapsing the card.
    hideTimer = setTimeout(() => { hidden.value = true }, 1400)
  }
}, { immediate: true })

onBeforeUnmount(() => clearTimeout(hideTimer))
</script>
