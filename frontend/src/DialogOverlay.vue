<template>
  <div ref="overlay" class="overlay" @click.self="closeOnBackdrop && dismiss()">
    <slot />
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps({
  dismissible: { type: Boolean, default: true },
  closeOnBackdrop: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])
const overlay = ref(null)
let previousFocus = null
const focusable = 'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'

function dismiss() {
  if (props.dismissible) emit('close')
}

function isTopmost() {
  const layers = [...document.querySelectorAll('.overlay')]
  const top = layers.reduce((current, layer) =>
    !current || Number(getComputedStyle(layer).zIndex) >= Number(getComputedStyle(current).zIndex) ? layer : current, null)
  return top === overlay.value
}

function onKeydown(event) {
  if (!isTopmost()) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    dismiss()
  } else if (event.key === 'Tab') {
    const controls = [...overlay.value.querySelectorAll(focusable)].filter(el => el.getClientRects().length)
    const first = controls[0], last = controls.at(-1)
    if (!first) {
      event.preventDefault()
    } else if (!controls.includes(document.activeElement) || (event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault()
      ;(event.shiftKey ? last : first).focus()
    }
  }
}

onMounted(async () => {
  previousFocus = document.activeElement
  document.addEventListener('keydown', onKeydown, true)
  await nextTick()
  if (!overlay.value || !isTopmost()) return
  const surface = overlay.value.querySelector('[role="dialog"]')
  if (surface) {
    surface.tabIndex = -1
    // Focus the surface first so destructive actions are never selected by default.
    surface.focus({ preventScroll: true })
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown, true)
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
})
</script>
