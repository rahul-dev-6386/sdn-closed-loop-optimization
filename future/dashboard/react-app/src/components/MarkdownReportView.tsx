/**
 * Publication-Grade Markdown Research Report Viewer.
 *
 * Renders RESULTS.md and empirical evaluation findings in GitHub-style Markdown format,
 * complete with statistical separation badges, monospace metrics tables, and alert callouts.
 */
import { useMemo, useState } from 'react'
import {
  FileText,
  Copy,
  Check,
  Download,
  AlertCircle,
  CheckCircle2,
  Bookmark,
  Award,
} from 'lucide-react'

interface Props {
  markdownText: string
  provenanceFiles?: {
    experiments: string
    telemetry: string
    model: string
  }
}

export function MarkdownReportView({ markdownText, provenanceFiles }: Props) {
  const [copied, setCopied] = useState(false)

  const handleCopy = () => {
    navigator.clipboard.writeText(markdownText)
    setCopied(true)
    setTimeout(() => setCopied(false), 2500)
  }

  const handleDownload = () => {
    const blob = new Blob([markdownText], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'SDN_EMPIRICAL_RESULTS.md'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  // Parse lines into structured blocks
  const blocks = useMemo(() => {
    const lines = markdownText.split('\n')
    const result: Array<{
      type: 'h1' | 'h2' | 'h3' | 'p' | 'table' | 'bullet' | 'quote'
      content: any
      raw?: string
    }> = []

    let i = 0
    while (i < lines.length) {
      const line = lines[i]
      const trimmed = line.trim()

      if (!trimmed) {
        i++
        continue
      }

      // Headings
      if (trimmed.startsWith('# ')) {
        result.push({ type: 'h1', content: trimmed.replace('# ', '') })
        i++
        continue
      }
      if (trimmed.startsWith('## ')) {
        result.push({ type: 'h2', content: trimmed.replace('## ', '') })
        i++
        continue
      }
      if (trimmed.startsWith('### ')) {
        result.push({ type: 'h3', content: trimmed.replace('### ', '') })
        i++
        continue
      }

      // Markdown Tables
      if (trimmed.startsWith('|') && trimmed.endsWith('|')) {
        const tableLines: string[] = []
        while (i < lines.length && lines[i].trim().startsWith('|') && lines[i].trim().endsWith('|')) {
          tableLines.push(lines[i].trim())
          i++
        }
        if (tableLines.length >= 2) {
          const header = tableLines[0]
            .split('|')
            .slice(1, -1)
            .map((c) => c.trim())
          // Skip separator line (tableLines[1])
          const rows = tableLines.slice(2).map((r) =>
            r
              .split('|')
              .slice(1, -1)
              .map((c) => c.trim()),
          )
          result.push({ type: 'table', content: { header, rows } })
        }
        continue
      }

      // Blockquotes
      if (trimmed.startsWith('> ')) {
        result.push({ type: 'quote', content: trimmed.replace('> ', '') })
        i++
        continue
      }

      // Bullets
      if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
        result.push({ type: 'bullet', content: trimmed.slice(2) })
        i++
        continue
      }

      // Standard paragraph
      result.push({ type: 'p', content: trimmed })
      i++
    }

    return result
  }, [markdownText])

  /** Inline parser for bold, italics, code, and verdict chips */
  const renderInline = (text: string) => {
    // Regex splits: `code`, **bold**, *italic*
    const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g)

    return parts.map((part, idx) => {
      if (part.startsWith('`') && part.endsWith('`')) {
        const code = part.slice(1, -1)
        return (
          <code key={idx} className="md-code">
            {code}
          </code>
        )
      }
      if (part.startsWith('**') && part.endsWith('**')) {
        const bold = part.slice(2, -2)
        // Check for specific highlight phrases
        if (bold.toLowerCase().includes('separated from best') || bold.toLowerCase().includes('best')) {
          return (
            <strong key={idx} className="md-bold text-green">
              {bold}
            </strong>
          )
        }
        return (
          <strong key={idx} className="md-bold">
            {bold}
          </strong>
        )
      }
      if (part.startsWith('*') && part.endsWith('*')) {
        return (
          <em key={idx} className="md-em text-amber">
            {part.slice(1, -1)}
          </em>
        )
      }

      // Check verdict strings in table cells
      if (part === 'separated from best') {
        return (
          <span key={idx} className="md-verdict-badge md-verdict-badge--separated">
            <CheckCircle2 size={11} />
            <span>Separated (p &lt; 0.05)</span>
          </span>
        )
      }
      if (part === 'best' || part === 'best *') {
        return (
          <span key={idx} className="md-verdict-badge md-verdict-badge--best">
            <Award size={11} />
            <span>Best Observed</span>
          </span>
        )
      }
      if (part === 'overlapping with best') {
        return (
          <span key={idx} className="md-verdict-badge md-verdict-badge--overlap">
            <span>Overlapping 95% CI</span>
          </span>
        )
      }
      if (part === 'insufficient repetitions') {
        return (
          <span key={idx} className="md-verdict-badge md-verdict-badge--warning">
            <AlertCircle size={11} />
            <span>n &lt; 3 (Low Reps)</span>
          </span>
        )
      }

      return part
    })
  }

  return (
    <div className="md-report-container">
      {/* Report Action Bar */}
      <div className="md-report-toolbar">
        <div className="md-report-toolbar__left">
          <FileText size={16} className="text-cyan" />
          <span className="md-report-title">RESULTS.md — Empirical Statistical Evaluation</span>
          <span className="badge badge--cyan">Gfm Markdown</span>
          <span className="badge badge--green">46 Mininet Runs</span>
        </div>

        <div className="md-report-toolbar__right">
          <button type="button" className="btn btn--quiet btn--xs" onClick={handleCopy} title="Copy Raw Markdown">
            {copied ? <Check size={12} className="text-green" /> : <Copy size={12} />}
            <span>{copied ? 'Copied Markdown!' : 'Copy Markdown'}</span>
          </button>
          <button
            type="button"
            className="btn btn--secondary btn--xs"
            onClick={handleDownload}
            title="Download RESULTS.md File"
          >
            <Download size={12} />
            <span>Download .md</span>
          </button>
        </div>
      </div>

      {/* Dataset Provenance Alert */}
      <div className="md-callout md-callout--note">
        <div className="md-callout__title">
          <Bookmark size={13} />
          <span>Artifact Provenance &amp; Verification</span>
        </div>
        <p>
          This document is generated from recorded run artefacts:{' '}
          <code>{provenanceFiles?.experiments || 'results/experiments.csv (46 Mininet runs)'}</code>,{' '}
          <code>{provenanceFiles?.telemetry || 'data/telemetry_live_labeled.csv (12,180 rows)'}</code>, and{' '}
          <code>{provenanceFiles?.model || 'results/ml_metrics.json'}</code>. Note: it does not cover the Abilene WAN figure on the Analytics page, which has no backing run records in this repository.
        </p>
      </div>

      {/* Rendered Document Body */}
      <div className="md-document-body">
        {blocks.map((block, idx) => {
          switch (block.type) {
            case 'h1':
              return (
                <h1 key={idx} className="md-h1">
                  {block.content}
                </h1>
              )
            case 'h2':
              return (
                <h2 key={idx} className="md-h2">
                  <span className="md-h2-hash">##</span>
                  <span>{block.content}</span>
                </h2>
              )
            case 'h3':
              return (
                <h3 key={idx} className="md-h3">
                  <span className="md-h3-hash">###</span>
                  <span>{block.content}</span>
                </h3>
              )
            case 'quote':
              return (
                <blockquote key={idx} className="md-blockquote">
                  {renderInline(block.content)}
                </blockquote>
              )
            case 'bullet':
              return (
                <div key={idx} className="md-bullet-item">
                  <span className="md-bullet-dot">•</span>
                  <span>{renderInline(block.content)}</span>
                </div>
              )
            case 'table': {
              const { header, rows } = block.content
              return (
                <div key={idx} className="md-table-wrap">
                  <table className="md-table">
                    <thead>
                      <tr>
                        {header.map((col: string, cIdx: number) => (
                          <th key={cIdx}>{renderInline(col)}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row: string[], rIdx: number) => (
                        <tr key={rIdx}>
                          {row.map((cell: string, cIdx: number) => (
                            <td key={cIdx}>{renderInline(cell)}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }
            case 'p':
            default:
              return (
                <p key={idx} className="md-p">
                  {renderInline(block.content)}
                </p>
              )
          }
        })}
      </div>
    </div>
  )
}
