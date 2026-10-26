import { inject, onBeforeUnmount, provide, ref } from 'vue'

const confirmationKey = Symbol('confirmation')

export function provideConfirmation() {
  const confirmation = ref(null)
  let settle = null

  function resolveConfirmation(accepted = false) {
    const resolve = settle
    settle = null
    confirmation.value = null
    resolve?.(accepted)
  }

  function confirm(options) {
    resolveConfirmation(false)
    confirmation.value = options
    return new Promise(resolve => { settle = resolve })
  }

  provide(confirmationKey, confirm)
  onBeforeUnmount(() => resolveConfirmation(false))
  return { confirmation, confirm, resolveConfirmation }
}

export function useConfirmation() {
  return inject(confirmationKey)
}
