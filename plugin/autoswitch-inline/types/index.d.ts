export type AutoswitchInlineBudget = 'save' | 'balanced' | 'quality'

/** The router's decision for the last main-loop turn, as the band shows it. */
export type AutoswitchInlineLast = {
  turnId: string
  /** The model the engine resolved for the turn (the picker's). */
  from: string
  /** The model the mod sent the turn to; null when it left the model alone. */
  to: string | null
  reason: string
}

declare module 'claude-code' {
  interface PluginState {
    'autoswitch-inline': {
      isActive: boolean
      budget: AutoswitchInlineBudget
      last: AutoswitchInlineLast | null
    }
  }
}
