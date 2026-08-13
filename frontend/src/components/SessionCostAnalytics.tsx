import {
  Bot,
  CircleDollarSign,
  Cpu,
  Gauge,
  MessageSquare,
  RefreshCw,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { formatCurrencyUnit, formatModelCost } from '../lib/costs'
import type {
  ConversationUsageBreakdown,
  SessionUsageReport,
  VisualPresentation as VisualPresentationData,
} from '../types'
import { VisualPresentation } from './VisualPresentation'

interface SessionCostAnalyticsProps {
  report: SessionUsageReport | null
  loading: boolean
  error: string | null
  variant?: 'full' | 'kpis'
}

const numberFormatter = new Intl.NumberFormat('es-ES')

/** Render root-plus-worker cost accounting for one conversation group. */
export function SessionCostAnalytics({
  report,
  loading,
  error,
  variant = 'full',
}: SessionCostAnalyticsProps) {
  const kpisOnly = variant === 'kpis'
  const costChart = useMemo(() => buildCostChart(report), [report])
  const modelChart = useMemo(() => buildModelTokenChart(report), [report])
  const maxConversationCost = Math.max(
    0,
    ...(report?.conversations.map((conversation) => conversation.cost?.amount ?? 0) ?? []),
  )
  const maxConversationTokens = Math.max(
    1,
    ...(report?.conversations.map((conversation) => conversation.usage.total_tokens) ?? []),
  )

  return (
    <section
      className={`session-cost-analytics ${kpisOnly ? 'session-cost-analytics-kpis' : ''}`}
      aria-labelledby="session-cost-title"
    >
      <header className="session-cost-header">
        <div>
          <span className="eyebrow">Sesión completa</span>
          <h2 id="session-cost-title">
            {kpisOnly ? 'Resumen económico de la sesión' : 'Coste de agente y workers'}
          </h2>
          <p>
            {kpisOnly
              ? 'Coste global con agente principal, workers y llamadas por modelo consolidadas.'
              : 'Incluye la conversación principal y todas las conversaciones internas delegadas.'}
          </p>
        </div>
        {loading && <RefreshCw className="spin" size={16} aria-label="Actualizando costes" />}
      </header>

      {error ? (
        <div className="history-global-usage-error" role="alert">{error}</div>
      ) : report ? (
        <>
          <dl className="session-cost-kpis">
            <MetricCard
              icon={<CircleDollarSign size={17} />}
              label="Coste total"
              value={sessionCostLabel(report)}
              detail={sessionCostDetail(report)}
            />
            <MetricCard
              icon={<Gauge size={17} />}
              label="Tokens"
              value={numberFormatter.format(report.usage.total_tokens)}
              detail={`${numberFormatter.format(report.usage.input_tokens)} entrada - ${numberFormatter.format(report.usage.output_tokens)} salida`}
            />
            <MetricCard
              icon={<Cpu size={17} />}
              label="Modelos"
              value={numberFormatter.format(report.models.length)}
              detail={`${numberFormatter.format(report.model_call_count)} llamadas`}
            />
            <MetricCard
              icon={<Bot size={17} />}
              label="Workers"
              value={numberFormatter.format(
                report.conversations.filter((conversation) => conversation.role === 'worker').length,
              )}
              detail={`${numberFormatter.format(report.turn_count)} turnos medidos`}
            />
          </dl>

          {!kpisOnly && (
            <>
              <div className="session-cost-layout">
                <section className="session-cost-panel" aria-label="Coste por conversación">
                  <header>
                    <strong>Principal vs workers</strong>
                    <span>Coste y tokens por historial aislado</span>
                  </header>
                  <div className="conversation-cost-list">
                    {report.conversations.map((conversation, index) => (
                      <ConversationCostRow
                        key={conversation.conversation_id}
                        conversation={conversation}
                        index={index}
                        maxCost={maxConversationCost}
                        maxTokens={maxConversationTokens}
                      />
                    ))}
                  </div>
                </section>

                <section className="session-cost-panel" aria-label="Coste por modelo">
                  <header>
                    <strong>Modelos</strong>
                    <span>Desglose individual de llamadas</span>
                  </header>
                  <div className="model-cost-table">
                    {report.models.map((model) => (
                      <div className="model-cost-row" key={model.model}>
                        <div>
                          <strong>{model.model}</strong>
                          <span>
                            {numberFormatter.format(model.model_call_count)} llamadas -{' '}
                            {numberFormatter.format(model.usage.total_tokens)} tokens
                          </span>
                        </div>
                        <span>{model.cost ? formatModelCost(model.cost) : 'Sin tarifa'}</span>
                      </div>
                    ))}
                  </div>
                </section>
              </div>

              <div className="session-cost-charts">
                {costChart && <VisualPresentation presentation={costChart} />}
                {modelChart && <VisualPresentation presentation={modelChart} />}
              </div>
            </>
          )}
        </>
      ) : (
        <div className="history-global-usage-loading">
          <RefreshCw className="spin" size={16} />
          Cargando costes de la sesión...
        </div>
      )}
    </section>
  )
}

function MetricCard({
  icon,
  label,
  value,
  detail,
}: {
  icon: ReactNode
  label: string
  value: string
  detail: string
}) {
  return (
    <div>
      <dt>{icon}{label}</dt>
      <dd>{value}</dd>
      <small>{detail}</small>
    </div>
  )
}

function ConversationCostRow({
  conversation,
  index,
  maxCost,
  maxTokens,
}: {
  conversation: ConversationUsageBreakdown
  index: number
  maxCost: number
  maxTokens: number
}) {
  const costShare = maxCost ? ((conversation.cost?.amount ?? 0) / maxCost) * 100 : 0
  const tokenShare = (conversation.usage.total_tokens / maxTokens) * 100
  const label = conversation.role === 'interactive' ? 'Agente usuario' : `Worker ${index}`

  return (
    <article className="conversation-cost-row">
      <div className="conversation-cost-title">
        <span aria-hidden="true">
          {conversation.role === 'interactive' ? <MessageSquare size={16} /> : <Bot size={16} />}
        </span>
        <div>
          <strong>{label}</strong>
          <code>{conversation.conversation_id}</code>
        </div>
      </div>
      <div className="conversation-cost-values">
        <strong>
          {conversation.model_call_count === 0
            ? 'Sin consumo'
            : conversation.cost
              ? formatModelCost(conversation.cost)
              : 'Sin tarifa'}
        </strong>
        <span>{numberFormatter.format(conversation.usage.total_tokens)} tokens</span>
      </div>
      <div className="conversation-cost-bars" aria-hidden="true">
        <span style={{ width: `${Math.max(costShare, conversation.cost ? 4 : 0)}%` }} />
        <span style={{ width: `${Math.max(tokenShare, conversation.usage.total_tokens ? 4 : 0)}%` }} />
      </div>
    </article>
  )
}

function buildCostChart(report: SessionUsageReport | null): VisualPresentationData | null {
  if (!report?.conversations.length || !report.fully_priced || report.cost === null) return null
  const points = report.conversations.map((conversation, index) => ({
    x: conversation.role === 'interactive' ? 'Agente usuario' : `Worker ${index}`,
    y: conversation.cost?.amount ?? 0,
  }))
  return {
    componentId: `session-cost-${report.root_conversation_id}`,
    fallbackText: points.map((point) => `${point.x}: ${point.y}`).join('. '),
    component: {
      kind: 'chart',
      title: 'Coste por participante',
      subtitle: 'Conversación principal frente a workers delegados',
      chart_type: 'bar',
      x_axis: { label: 'Participante' },
      y_axis: { label: 'Coste', unit: formatCurrencyUnit(report.cost?.currency) },
      series: [{ name: 'Coste', points }],
    },
  }
}

function sessionCostLabel(report: SessionUsageReport): string {
  if (report.model_call_count === 0) return 'Sin consumo'
  return report.cost ? formatModelCost(report.cost) : 'Parcial'
}

function sessionCostDetail(report: SessionUsageReport): string {
  if (report.model_call_count === 0) return 'Sin llamadas al modelo'
  return report.fully_priced ? 'Todas las llamadas tienen tarifa' : 'Faltan tarifas'
}

function buildModelTokenChart(report: SessionUsageReport | null): VisualPresentationData | null {
  if (!report?.models.length) return null
  return {
    componentId: `session-model-tokens-${report.root_conversation_id}`,
    fallbackText: report.models
      .map((model) => `${model.model}: ${model.usage.total_tokens} tokens`)
      .join('. '),
    component: {
      kind: 'chart',
      title: 'Tokens por modelo',
      subtitle: 'Entrada, salida y caché por modelo usado en la sesión',
      chart_type: 'bar',
      x_axis: { label: 'Modelo' },
      y_axis: { label: 'Tokens', unit: null },
      series: [
        {
          name: 'Entrada',
          points: report.models.map((model) => ({ x: model.model, y: model.usage.input_tokens })),
        },
        {
          name: 'Salida',
          points: report.models.map((model) => ({ x: model.model, y: model.usage.output_tokens })),
        },
        {
          name: 'Caché',
          points: report.models.map((model) => ({
            x: model.model,
            y: model.usage.cached_input_tokens,
          })),
        },
      ],
    },
  }
}
