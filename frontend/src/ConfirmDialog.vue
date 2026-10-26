<template>
  <Transition name="settings-dialog">
    <DialogOverlay v-if="confirmation" class="confirmation-overlay" @close="$emit('resolve', false)">
      <div class="modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirmation-title" aria-describedby="confirmation-description">
        <div class="modal-head">
          <h3 id="confirmation-title">{{ confirmation.title }}</h3>
          <p id="confirmation-description">{{ confirmation.description }}</p>
        </div>
        <div v-if="confirmation.details" class="modal-body">
          <p class="confirm-path">{{ confirmation.details }}</p>
        </div>
        <div class="modal-foot">
          <button class="btn-ghost" @click="$emit('resolve', false)">取消</button>
          <button :class="confirmation.danger ? 'btn-danger' : 'btn-primary'" @click="$emit('resolve', true)">{{ confirmation.confirmLabel || '确认' }}</button>
        </div>
      </div>
    </DialogOverlay>
  </Transition>
</template>

<script setup>
import DialogOverlay from './DialogOverlay.vue'
defineProps({ confirmation: { type: Object, default: null } })
defineEmits(['resolve'])
</script>
