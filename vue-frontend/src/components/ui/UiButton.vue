<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost' | 'inverse' | 'link' | 'toolbar' | 'card-action' | 'tab' | 'plain-danger'
  tone?: 'neutral' | 'primary' | 'danger' | 'success' | 'warning' | 'info'
  size?: 'lg' | 'md' | 'sm' | 'xs'
  type?: 'button' | 'submit' | 'reset'
  disabled?: boolean
  loading?: boolean
  block?: boolean
  icon?: boolean
}>(), {
  variant: 'secondary',
  tone: 'neutral',
  size: 'md',
  type: 'button',
  disabled: false,
  loading: false,
  block: false,
  icon: false,
})

const isDisabled = computed(() => props.disabled || props.loading)
const isBareStyledVariant = computed(() => ['link', 'toolbar', 'card-action', 'tab', 'plain-danger'].includes(props.variant))
const buttonClasses = computed(() => {
  return [
    'ui-button',
    `ui-button--${props.variant}`,
    `ui-button--${props.size}`,
    `ui-button--tone-${props.tone}`,
    {
      'ui-button--bare': isBareStyledVariant.value,
      'ui-button--block': props.block,
      'ui-button--icon': props.icon,
      'ui-button--loading': props.loading,
    },
  ]
})
</script>

<template>
  <button
    :class="buttonClasses"
    :type="type"
    :disabled="isDisabled"
    :aria-busy="loading ? 'true' : undefined"
  >
    <slot />
  </button>
</template>

<style scoped>
.ui-button:not(:where(.ui-button--bare)) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: var(--ui-button-padding, 10px 20px);
  border: var(--ui-button-border, none);
  border-radius: var(--ui-button-radius, 8px);
  font-size: var(--ui-button-font-size, 14px);
  font-weight: 500;
  cursor: pointer;
  transition: background-color var(--motion-fast, 150ms ease), color var(--motion-fast, 150ms ease), border-color var(--motion-fast, 150ms ease), opacity var(--motion-fast, 150ms ease);
  text-decoration: none;
  width: var(--ui-button-width, auto);
  white-space: var(--ui-button-white-space, nowrap);
  user-select: none;
}

.ui-button:not(:where(.ui-button--bare)):disabled {
  border: var(--ui-button-disabled-border, var(--internal-ui-button-disabled-border, 1px solid var(--color-border-default)));
  background: var(--ui-button-disabled-background, var(--internal-ui-button-disabled-background, var(--color-surface-muted)));
  color: var(--ui-button-disabled-color, var(--internal-ui-button-disabled-color, var(--color-text-supporting)));
  box-shadow: none;
  opacity: var(--ui-button-disabled-opacity, 1);
  transform: none;
  cursor: var(--ui-button-disabled-cursor, not-allowed);
}

.ui-button:not(:where(.ui-button--bare)):active:not(:disabled) {
  opacity: 0.9;
}

.ui-button--primary {
  background: var(--ui-button-primary-background, var(--color-action-brand));
  color: var(--ui-button-primary-color, var(--color-text-inverse));
  box-shadow: var(--ui-button-primary-shadow, none);
}

.ui-button--primary:hover:not(:disabled) {
  background: var(--ui-button-primary-hover-background, var(--color-action-brand-strong));
  box-shadow: var(--ui-button-primary-hover-shadow, none);
}

.ui-button--primary.ui-button--tone-success {
  background: var(--color-action-success);
  color: var(--color-text-inverse);
  box-shadow: none;
}

.ui-button--primary.ui-button--tone-success:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-action-success) 88%, #000000);
  box-shadow: none;
}

.ui-button--primary.ui-button--tone-success.ui-button--sm {
  padding: 8px 16px;
  border-radius: var(--radius-control, 8px);
  font-size: var(--font-size-sm, 14px);
}

.ui-button--primary:disabled {
  border: var(--ui-button-primary-disabled-border, none);
  background: var(
    --ui-button-primary-disabled-background,
    var(
      --ui-button-primary-background,
      var(--color-action-brand)
    )
  );
  color: var(--ui-button-primary-disabled-color, var(--color-text-inverse));
  opacity: var(--ui-button-primary-disabled-opacity, 0.55);
}

.ui-button--secondary {
  background: var(--ui-button-secondary-background, var(--color-surface-card));
  color: var(--ui-button-secondary-color, var(--color-text-default));
  border: var(--ui-button-secondary-border, 1px solid var(--color-border-muted));
}

.ui-button--secondary:hover:not(:disabled) {
  background: var(--ui-button-secondary-hover-background, var(--color-surface-interactive-hover));
  border-color: var(--ui-button-secondary-hover-border-color, var(--color-text-supporting));
  color: var(--ui-button-secondary-hover-color, var(--ui-button-secondary-color, var(--color-text-default)));
}

.ui-button--secondary.ui-button--tone-info {
  --internal-ui-button-disabled-background: var(--color-status-info-fill, var(--color-status-info));
  --internal-ui-button-disabled-border: 0;
  --internal-ui-button-disabled-color: var(--color-text-inverse);

  border-color: transparent;
  background: var(--color-status-info-fill, var(--color-status-info));
  color: var(--color-text-inverse);
}

.ui-button--secondary.ui-button--tone-info:disabled {
  opacity: 0.6;
}

.ui-button--secondary.ui-button--tone-info:hover:not(:disabled) {
  border-color: transparent;
  background: color-mix(in srgb, var(--color-status-info) 84%, var(--color-text-default));
  color: var(--color-text-inverse);
}

.ui-button--inverse {
  border: 1px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  background: transparent;
  color: var(--color-text-inverse);
}

.ui-button--inverse:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--color-text-inverse) 50%, transparent);
  background: var(--color-overlay-inverse-subtle);
  color: var(--color-text-inverse);
}

.ui-button--inverse:disabled {
  border: 1px solid var(--color-overlay-inverse-muted);
  background: var(--color-overlay-inverse-subtle);
  color: var(--color-text-inverse);
  opacity: 0.4;
}

.ui-button--inverse.ui-button--sm {
  padding: 8px 16px;
  border-radius: var(--radius-control, 8px);
  font-size: var(--font-size-sm, 14px);
}

:where(.ui-button--bare) {
  border: 0;
  padding: 0;
  background: none;
  color: inherit;
  font: inherit;
  line-height: normal;
  cursor: pointer;
}

:where(.ui-button--bare):disabled {
  cursor: not-allowed;
}

.ui-button--link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--ui-button-link-gap, 4px);
  color: var(--ui-button-link-color, var(--color-action-primary));
  font-size: var(--ui-button-link-font-size, 12px);
  font-weight: var(--ui-button-link-font-weight, 500);
  text-decoration: var(--ui-button-link-text-decoration, none);
  border-radius: var(--ui-button-link-radius, 8px);
}

.ui-button--link:hover:not(:disabled) {
  color: var(--ui-button-link-hover-color, var(--color-action-primary-hover));
  text-decoration: underline;
}

.ui-button--link:focus-visible {
  outline: 2px solid var(--color-border-brand);
  outline-offset: 2px;
}

.ui-button--ghost {
  border: var(--ui-button-ghost-border, 1px solid var(--color-border-muted));
  background: var(--ui-button-ghost-background, var(--color-surface-raised));
  color: var(--ui-button-ghost-color, var(--color-text-default));
}

.ui-button--ghost:hover:not(:disabled) {
  background: var(--ui-button-ghost-hover-background, var(--color-surface-interactive-hover));
  border: var(--ui-button-ghost-hover-border, var(--ui-button-ghost-border, 1px solid var(--color-border-muted)));
  color: var(--ui-button-ghost-hover-color, var(--ui-button-ghost-color, var(--color-text-default)));
}

.ui-button--danger {
  background: var(--ui-button-danger-background, var(--ui-button-danger-background-start, var(--color-status-error)));
  color: var(--ui-button-danger-color, var(--color-text-inverse));
  border: var(--ui-button-danger-border, var(--ui-button-border, none));
  box-shadow: var(--ui-button-danger-shadow, none);
}

.ui-button--danger:hover:not(:disabled) {
  background: var(--ui-button-danger-hover-background, var(--ui-button-danger-hover-background-start, var(--color-status-error-hover)));
  border-color: var(--ui-button-danger-hover-border-color, currentColor);
  box-shadow: var(--ui-button-danger-hover-shadow, none);
}

.ui-button--lg:not(:where(.ui-button--bare)) {
  padding: var(--ui-button-lg-padding, 14px 28px);
  font-size: var(--ui-button-lg-font-size, 16px);
}

.ui-button--sm:not(:where(.ui-button--bare)) {
  padding: var(--ui-button-sm-padding, 6px 14px);
  font-size: var(--ui-button-sm-font-size, 12px);
}

.ui-button--xs:not(:where(.ui-button--bare)) {
  padding: var(--ui-button-xs-padding, 4px 10px);
  font-size: var(--ui-button-xs-font-size, 12px);
}

.ui-button--block:not(:where(.ui-button--bare)) {
  width: var(--ui-button-block-width, 100%);
}

.ui-button--icon:not(:where(.ui-button--bare)) {
  width: var(--ui-button-icon-width, 36px);
  height: var(--ui-button-icon-height, 36px);
  padding: 0;
}

.ui-button--tone-danger:not(:where(.ui-button--bare, .ui-button--primary, .ui-button--danger, .ui-button--inverse)) {
  color: var(--color-status-error, var(--color-text-danger));
}

.ui-button--tone-success:not(:where(.ui-button--bare, .ui-button--primary, .ui-button--danger, .ui-button--inverse)) {
  color: var(--ui-button-status-success-color, var(--color-status-success, var(--ui-button-status-success-text)));
}

.ui-button--tone-warning:not(:where(.ui-button--bare, .ui-button--primary, .ui-button--danger, .ui-button--inverse)) {
  color: var(--ui-button-status-warning-color, var(--color-status-warning, var(--ui-button-status-warning-text)));
}

.ui-button--tone-primary:not(:where(.ui-button--bare, .ui-button--primary, .ui-button--danger, .ui-button--inverse)) {
  color: var(--color-action-primary);
}

.ui-button--loading:not(:where(.ui-button--bare)) {
  pointer-events: none;
}

[data-theme="dark"] .ui-button--secondary.ui-button--tone-info:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-status-info-fill) 84%, var(--color-overlay-backdrop-solid));
}

</style>
