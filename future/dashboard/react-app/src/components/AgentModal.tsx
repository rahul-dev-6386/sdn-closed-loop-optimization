/**
 * SDN Autonomous Agent Modal Component.
 * Powered by LangGraph StateGraph & OpenRouter Free Models.
 */
import React, { useState, useEffect, useRef } from 'react'
import {
  Bot,
  X,
  Send,
  Zap,
  CheckCircle2,
  Play,
  RotateCw,
  Sliders,
  ArrowRight,
  ShieldCheck,
  Cpu,
  Activity,
  Layers,
  Key,
  Flame,
} from 'lucide-react'
import {
  api,
  type AgentStateData,
  type AgentConfigData,
  type AgentStepLog,
} from '../api'

interface Props {
  isOpen: boolean
  onClose: () => void
  selectedTopoId: string
  isCongested?: boolean
  isDetourActive?: boolean
  onCreateCongestion?: () => void
  onApplyDetour?: () => void
}

/** Helper to cleanly format inline markdown (`code`, **bold**, bullets) */
function renderFormattedLine(line: string, idx: number) {
  const isBullet = line.trim().startsWith('- ') || line.trim().startsWith('• ')
  const cleanLine = isBullet ? line.trim().replace(/^[-•]\s*/, '') : line

  const parts = cleanLine.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  const renderedParts = parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return (
        <strong key={i} className="agent-text--bold">
          {part.slice(2, -2)}
        </strong>
      )
    }
    if (part.startsWith('`') && part.endsWith('`')) {
      return (
        <code key={i} className="agent-text--code">
          {part.slice(1, -1)}
        </code>
      )
    }
    return part
  })

  if (isBullet) {
    return (
      <div key={idx} className="agent-msg__bullet">
        <span className="agent-msg__bullet-dot">•</span>
        <span>{renderedParts}</span>
      </div>
    )
  }

  return (
    <p key={idx} className="agent-msg__line">
      {renderedParts}
    </p>
  )
}

export function AgentModal({
  isOpen,
  onClose,
  selectedTopoId,
  isCongested = false,
  isDetourActive = false,
  onCreateCongestion,
  onApplyDetour,
}: Props) {
  const [messages, setMessages] = useState<Array<{ role: string; content: string; time?: string }>>([])
  const [inputMsg, setInputMsg] = useState('')
  const [isRunning, setIsRunning] = useState(false)
  const [agentState, setAgentState] = useState<AgentStateData | null>(null)
  const [config, setConfig] = useState<AgentConfigData | null>(null)
  const [showConfig, setShowConfig] = useState(false)
  const [apiKeyInput, setApiKeyInput] = useState('')
  const [selectedModel, setSelectedModel] = useState('nvidia/nemotron-3.5-lightning:free')
  const [configSaveMsg, setConfigSaveMsg] = useState('')

  const chatScrollRef = useRef<HTMLDivElement>(null)

  // Fetch initial config and state
  useEffect(() => {
    if (!isOpen) return
    api.agentConfig().then((cfg) => {
      setConfig(cfg)
      setSelectedModel(cfg.model)
    }).catch(() => {})

    api.agentState().then((st) => {
      setAgentState(st)
      if (st.messages && st.messages.length > 0) {
        setMessages(st.messages)
      } else {
        setMessages([
          {
            role: 'assistant',
            content: `**SDN Agent Ready**\nI monitor telemetry, evaluate machine learning congestion forecasts, and execute OpenFlow 1.3 rerouting via **LangGraph**.\n\nYou can click **Create Congestion** to inject test traffic, or ask me to resolve congestion and route around bottlenecks.`,
            time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          },
        ])
      }
    }).catch(() => {})
  }, [isOpen])

  // Scroll chat to bottom on new messages
  useEffect(() => {
    chatScrollRef.current?.scrollTo({
      top: chatScrollRef.current.scrollHeight,
      behavior: 'smooth',
    })
  }, [messages, isRunning])

  if (!isOpen) return null

  const handleSend = async (textToSend?: string) => {
    const text = textToSend || inputMsg.trim()
    if (!text || isRunning) return

    const nowStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    setMessages((prev) => [...prev, { role: 'user', content: text, time: nowStr }])
    if (!textToSend) setInputMsg('')
    setIsRunning(true)

    const lower = text.toLowerCase()
    const isCreateCmd = [
      'create congestion', 'inject spike', 'spike traffic', 'cause congestion',
      'simulate congestion', 'make congestion', 'inject congestion', 'start spike'
    ].some((k) => lower.includes(k))
    const isFixCmd = [
      'fix', 'mitigate', 'reroute', 'detour', 'resolve', 'relieve', 'auto'
    ].some((k) => lower.includes(k))

    if (isCreateCmd) {
      onCreateCongestion?.()
    }

    try {
      const res = await api.agentChat(text, selectedTopoId)
      setAgentState(res)
      if (res.messages && res.messages.length > 0) {
        setMessages(res.messages)
      }
      if (isFixCmd && (res.status === 'completed' || res.verification?.passed)) {
        onApplyDetour?.()
      }
    } catch (err: any) {
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: `⚠️ Error executing agent task: ${err.message || 'Connection error'}`,
          time: nowStr,
        },
      ])
    } finally {
      setIsRunning(false)
    }
  }

  const handleCreateCongestion = async () => {
    if (isRunning) return
    onCreateCongestion?.()
    setIsRunning(true)
    const nowStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    setMessages((prev) => [
      ...prev,
      {
        role: 'user',
        content: `Create 14.0 Mbps congestion traffic spike on ${selectedTopoId}`,
        time: nowStr,
      },
    ])

    try {
      const res = await api.agentCreateCongestion(selectedTopoId, 14.0)
      setAgentState(res)
      if (res.messages && res.messages.length > 0) {
        setMessages(res.messages)
      }
    } catch (err: any) {
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: `⚠️ Failed to create congestion: ${err.message}`,
          time: nowStr,
        },
      ])
    } finally {
      setIsRunning(false)
    }
  }

  const handleRunLoop = async () => {
    if (isRunning) return
    setIsRunning(true)
    const nowStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    setMessages((prev) => [
      ...prev,
      {
        role: 'user',
        content: `Run autonomous closed-loop congestion mitigation on ${selectedTopoId}`,
        time: nowStr,
      },
    ])

    try {
      const res = await api.agentRun(selectedTopoId)
      setAgentState(res)
      if (res.messages) setMessages(res.messages)
      if (res.status === 'completed' || res.verification?.passed) {
        onApplyDetour?.()
      }
    } catch (err: any) {
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: `⚠️ Error in closed-loop run: ${err.message}`,
          time: nowStr,
        },
      ])
    } finally {
      setIsRunning(false)
    }
  }

  const handleSaveConfig = async () => {
    try {
      const res = await api.saveAgentConfig(apiKeyInput, selectedModel)
      setConfig(res)
      setConfigSaveMsg('Settings saved successfully!')
      setTimeout(() => setConfigSaveMsg(''), 3000)
    } catch {
      setConfigSaveMsg('Failed to update config.')
    }
  }

  const activeNode = agentState?.status || (isRunning ? 'monitoring' : 'idle')
  const stepsLog: AgentStepLog[] = agentState?.steps_log || []

  const nodeMap = [
    { key: 'monitor', label: 'Monitor', icon: <Activity size={13} /> },
    { key: 'analyze', label: 'Analyze', icon: <Cpu size={13} /> },
    { key: 'plan', label: 'Plan', icon: <Layers size={13} /> },
    { key: 'act', label: 'Act', icon: <Zap size={13} /> },
    { key: 'verify', label: 'Verify', icon: <ShieldCheck size={13} /> },
  ]

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="agent-modal" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="agent-modal__header">
          <div className="agent-modal__title-row">
            <div className="agent-modal__avatar">
              <Bot size={18} />
            </div>
            <div>
              <div className="agent-modal__title-wrap">
                <h3 className="agent-modal__title">Autonomous Congestion Controller</h3>
                <span className="agent-badge agent-badge--langgraph">LangGraph StateGraph</span>
                <span className="agent-badge agent-badge--model" title="OpenRouter Active Model">
                  {config?.model ? config.model.split('/')[1] || config.model : 'OpenRouter Free Model'}
                </span>
                {isCongested ? (
                  <span className="agent-badge agent-badge--hazard" title="Bottleneck link experiencing traffic surge">
                    CONGESTED (88%)
                  </span>
                ) : (
                  <span className="agent-badge agent-badge--ok" title="All link utilisations below threshold">
                    HEALTHY (&lt;80%)
                  </span>
                )}
                {isDetourActive && (
                  <span className="agent-badge agent-badge--detour" title="Make-Before-Break Detour Active">
                    DETOUR ACTIVE
                  </span>
                )}
              </div>
              <p className="agent-modal__subtitle">
                Closed-Loop SDN Telemetry Reasoning, Make-Before-Break Detours &amp; Verification
              </p>
            </div>
          </div>

          <div className="agent-modal__header-actions">
            <button
              className={`btn btn--xs ${showConfig ? 'btn--primary' : 'btn--quiet'}`}
              onClick={() => setShowConfig(!showConfig)}
              title="Agent & OpenRouter Settings"
            >
              <Sliders size={12} />
              <span>Settings</span>
            </button>
            <button className="pipeline-modal__close" onClick={onClose} title="Close Agent Panel">
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Settings Popdown Drawer */}
        {showConfig && (
          <div className="agent-settings-bar">
            <div className="agent-settings-grid">
              <div className="agent-field">
                <label className="agent-label">
                  <Key size={11} />
                  <span>OpenRouter API Key:</span>
                </label>
                <div className="agent-input-row">
                  <input
                    type="password"
                    className="agent-input"
                    placeholder={config?.has_api_key ? `Configured (${config.masked_key})` : 'Paste sk-or-v1-... key'}
                    value={apiKeyInput}
                    onChange={(e) => setApiKeyInput(e.target.value)}
                  />
                </div>
              </div>

              <div className="agent-field">
                <label className="agent-label">
                  <Cpu size={11} />
                  <span>LLM Model:</span>
                </label>
                <select
                  className="agent-select"
                  value={selectedModel}
                  onChange={(e) => setSelectedModel(e.target.value)}
                >
                  {config?.available_models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="agent-settings-actions">
                <button className="btn btn--primary btn--xs" onClick={handleSaveConfig}>
                  Save Settings
                </button>
                {configSaveMsg && <span className="agent-save-msg">{configSaveMsg}</span>}
              </div>
            </div>
          </div>
        )}

        {/* Main Body: Dual-Pane Layout */}
        <div className="agent-modal__body">
          {/* LEFT PANE: Interactive Chat */}
          <div className="agent-pane agent-pane--chat">
            <div className="agent-chat-list" ref={chatScrollRef}>
              {messages.map((m, i) => (
                <div key={i} className={`agent-msg agent-msg--${m.role}`}>
                  <div className="agent-msg__header">
                    <span className="agent-msg__author">{m.role === 'user' ? 'Operator' : 'SDN Agent'}</span>
                    {m.time && <span className="agent-msg__time">{m.time}</span>}
                  </div>
                  <div className="agent-msg__bubble">
                    {m.content.split('\n').map((line, idx) => renderFormattedLine(line, idx))}
                  </div>
                </div>
              ))}
              {isRunning && (
                <div className="agent-msg agent-msg--assistant">
                  <div className="agent-msg__header">
                    <span className="agent-msg__author">SDN Agent</span>
                    <span className="agent-msg__time">Thinking...</span>
                  </div>
                  <div className="agent-msg__bubble agent-msg__bubble--thinking">
                    <RotateCw size={13} className="spin" />
                    <span>Executing LangGraph StateGraph traversal...</span>
                  </div>
                </div>
              )}
            </div>

            {/* Quick Prompt Pills */}
            <div className="agent-quick-prompts">
              {/* Option to create congestion */}
              <button
                type="button"
                className="agent-quick-btn agent-quick-btn--spike"
                onClick={handleCreateCongestion}
                disabled={isRunning}
                title="Inject 14 Mbps traffic spike to simulate bottleneck congestion"
              >
                <Flame size={11} />
                <span>Create Congestion (14M)</span>
              </button>

              {/* Option to fix congestion */}
              <button
                type="button"
                className="agent-quick-btn agent-quick-btn--action"
                onClick={() => handleSend('Fix congestion on the primary bottleneck link')}
                disabled={isRunning}
                title="Run autonomous detour rerouting to resolve congestion"
              >
                <Zap size={11} />
                <span>Fix Congestion</span>
              </button>

              <button
                type="button"
                className="agent-quick-btn"
                onClick={() => handleSend('Inspect current network telemetry and ML forecasts')}
                disabled={isRunning}
              >
                <Activity size={11} />
                <span>Audit Telemetry</span>
              </button>

              <button
                type="button"
                className="agent-quick-btn"
                onClick={handleRunLoop}
                disabled={isRunning}
              >
                <Play size={11} />
                <span>Run Closed-Loop</span>
              </button>
            </div>

            {/* Chat Input Bar */}
            <div className="agent-input-bar">
              <input
                type="text"
                className="agent-chat-input"
                placeholder="Ask agent: 'create congestion', 'fix congestion', or 'explain status'..."
                value={inputMsg}
                onChange={(e) => setInputMsg(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleSend()
                }}
                disabled={isRunning}
              />
              <button
                className="btn btn--primary agent-send-btn"
                onClick={() => handleSend()}
                disabled={isRunning || !inputMsg.trim()}
              >
                <Send size={13} />
              </button>
            </div>
          </div>

          {/* RIGHT PANE: LangGraph Workflow Visualizer */}
          <div className="agent-pane agent-pane--graph">
            <div className="agent-graph-header">
              <span className="agent-graph-title">LangGraph Execution Pipeline</span>
              <span className={`agent-status-badge agent-status-badge--${activeNode}`}>
                {activeNode.toUpperCase()}
              </span>
            </div>

            {/* 5-Node Graph Strip */}
            <div className="agent-flow-strip">
              {nodeMap.map((node, i) => {
                const isCurrent = activeNode === node.key
                const isPassed = stepsLog.some((s) => s.node === node.key && s.status === 'success')
                return (
                  <React.Fragment key={node.key}>
                    <div
                      className={`agent-flow-node ${
                        isCurrent ? 'agent-flow-node--active' : isPassed ? 'agent-flow-node--passed' : ''
                      }`}
                    >
                      <div className="agent-flow-node__icon">
                        {isPassed ? <CheckCircle2 size={13} /> : node.icon}
                      </div>
                      <span className="agent-flow-node__label">{node.label}</span>
                    </div>
                    {i < nodeMap.length - 1 && (
                      <div className={`agent-flow-edge ${isPassed ? 'agent-flow-edge--passed' : ''}`}>
                        <ArrowRight size={11} />
                      </div>
                    )}
                  </React.Fragment>
                )
              })}
            </div>

            {/* Steps Log Cards */}
            <div className="agent-steps-container">
              <div className="agent-steps-label">Execution Trace &amp; Verification:</div>
              {stepsLog.length === 0 ? (
                <div className="agent-steps-empty">
                  <span>No active pipeline runs. Send a command like &quot;Fix congestion&quot; to view the execution trace.</span>
                </div>
              ) : (
                <div className="agent-steps-list">
                  {stepsLog.map((step, idx) => (
                    <div key={idx} className={`agent-step-card agent-step-card--${step.status}`}>
                      <div className="agent-step-card__top">
                        <span className="agent-step-card__title">{step.title}</span>
                        <span className="agent-step-card__time">{step.timestamp}</span>
                      </div>
                      <p className="agent-step-card__desc">{step.description}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
