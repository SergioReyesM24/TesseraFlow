import { Bot, LoaderCircle, X } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import type { VisualPresentation as VisualPresentationData } from '../types'
import { VisualPresentation } from './VisualPresentation'

const VISUAL_GENERATION_DELAY_MS = 1050

interface VisualInspectorProps {
  presentations: VisualPresentationData[]
  onClose: () => void
}

/** Dock one or more agent visuals in a side panel with stable tab navigation. */
export function VisualInspector({ presentations, onClose }: VisualInspectorProps) {
  const shouldAnimate =
    typeof window !== 'undefined' &&
    !window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const groupKey = presentations.map((presentation) => presentation.componentId).join('|')
  const [isReady, setIsReady] = useState(!shouldAnimate)
  const [activeIndex, setActiveIndex] = useState(() => Math.max(0, presentations.length - 1))
  const selectedIndex = Math.min(activeIndex, Math.max(0, presentations.length - 1))
  const activePresentation = presentations[selectedIndex]
  const tabListId = useId()
  const panelId = `${tabListId}-panel`
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])

  useEffect(() => {
    if (!shouldAnimate) return

    const timer = window.setTimeout(() => setIsReady(true), VISUAL_GENERATION_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [shouldAnimate, groupKey])

  if (!activePresentation) return null

  const selectTab = (index: number) => {
    setActiveIndex(index)
    tabRefs.current[index]?.focus()
  }

  const navigateTabs = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % presentations.length
    if (event.key === 'ArrowLeft') {
      nextIndex = (index - 1 + presentations.length) % presentations.length
    }
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = presentations.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    selectTab(nextIndex)
  }

  return (
    <aside
      className={`visual-inspector ${isReady ? 'is-ready' : 'is-generating'}`}
      aria-label="Componentes visuales del agente"
      aria-busy={!isReady}
    >
      <header className="visual-inspector-header">
        <div className="visual-assistant-message" aria-live="polite">
          <span className="visual-assistant-avatar" aria-hidden="true">
            <Bot size={16} />
          </span>
          <span className="visual-assistant-copy">
            <span>TesseraFlow</span>
            <strong>
              {isReady
                ? visualReadyCopy(presentations.length)
                : 'Generando componente visual...'}
            </strong>
          </span>
          {!isReady && <LoaderCircle className="spin" size={15} aria-hidden="true" />}
        </div>
        <button
          className="icon-button"
          type="button"
          onClick={onClose}
          aria-label="Cerrar vista visual"
        >
          <X size={19} />
        </button>
      </header>
      {isReady && presentations.length > 1 && (
        <div className="visual-inspector-tabs" role="tablist" aria-label="Vistas visuales">
          {presentations.map((presentation, index) => {
            const active = index === selectedIndex
            const tabId = `${tabListId}-tab-${index}`
            return (
              <button
                className={active ? 'active' : ''}
                type="button"
                role="tab"
                aria-selected={active}
                aria-controls={panelId}
                id={tabId}
                tabIndex={active ? 0 : -1}
                key={presentation.componentId}
                onClick={() => setActiveIndex(index)}
                onKeyDown={(event) => navigateTabs(event, index)}
                ref={(element) => {
                  tabRefs.current[index] = element
                }}
              >
                <span>{index + 1}</span>
                {visualTabLabel(presentation, index)}
              </button>
            )
          })}
        </div>
      )}
      <div className="visual-inspector-content">
        {isReady ? (
          <div
            className="visual-generated-content"
            id={panelId}
            role="tabpanel"
            aria-labelledby={
              presentations.length > 1 ? `${tabListId}-tab-${selectedIndex}` : undefined
            }
          >
            <VisualPresentation presentation={activePresentation} />
          </div>
        ) : (
          <div className="visual-generating-placeholder" aria-hidden="true">
            <span className="visual-skeleton-title" />
            <span className="visual-skeleton-subtitle" />
            <div className="visual-skeleton-canvas">
              <i />
              <i />
              <i />
              <i />
            </div>
          </div>
        )}
      </div>
    </aside>
  )
}

/** Name tab buttons from the semantic visual title, with a bounded fallback. */
function visualTabLabel(presentation: VisualPresentationData, index: number): string {
  return presentation.component?.title ?? `Vista ${index + 1}`
}

/** Keep the header copy singular/plural without leaking implementation details. */
function visualReadyCopy(count: number): string {
  return count > 1 ? `He preparado ${count} vistas para ti` : 'He preparado esta vista para ti'
}
