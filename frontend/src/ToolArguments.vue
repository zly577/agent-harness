<template>
  <dl class="operation-fields">
    <div v-for="field in argumentFields(args)" :key="field.key" class="operation-field">
      <dt>{{ field.label }}</dt>
      <dd>
        <ToolArguments v-if="field.nested" :args="field.value" :workspace-directory="workspaceDirectory" />
        <details v-else-if="argumentText(field.value).length > 240" class="operation-long">
          <summary>{{ argumentText(field.value).slice(0, 100) }}… <span>展开内容</span></summary>
          <pre>{{ argumentText(field.value) }}</pre>
        </details>
        <span v-else class="operation-value">{{ argumentText(field.value) }}</span>
        <small v-if="workspacePath(field.key, field.value, workspaceDirectory)" class="operation-path">
          对应：{{ workspacePath(field.key, field.value, workspaceDirectory) }}
        </small>
      </dd>
    </div>
  </dl>
</template>
<script setup>
import { argumentFields, argumentText, workspacePath } from './toolPresentation'
defineProps({ args: { default: () => ({}) }, workspaceDirectory: { type: String, default: '' } })
</script>
<style scoped>
.operation-fields { margin: 0; display: grid; gap: 9px; }
.operation-field { display: grid; grid-template-columns: 82px minmax(0, 1fr); gap: 10px; font-size: 12px; line-height: 1.65; }
dt { color: var(--text-3); padding-top: 1px; }
dd { margin: 0; min-width: 0; color: var(--text-2); }
.operation-path { display: block; margin-top: 4px; color: var(--text-3); overflow-wrap: anywhere; }
.operation-value { white-space: pre-wrap; overflow-wrap: anywhere; }
.operation-long summary { cursor: pointer; overflow-wrap: anywhere; color: var(--text-2); }
.operation-long summary span { color: var(--text-3); margin-left: 5px; font-size: 11px; }
.operation-long pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 280px; overflow: auto; margin: 8px 0 0; font-family: var(--mono); }
@media (max-width: 600px) { .operation-field { grid-template-columns: 65px minmax(0, 1fr); gap: 6px; } }
</style>
