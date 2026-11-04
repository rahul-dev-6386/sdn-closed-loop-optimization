/**
 * Accessible Custom Popover / Listbox Component.
 *
 * Implements Section 2:
 * - Replaces native <select> elements across the entire dashboard.
 * - Triggers show concise labels; popovers show full title plus secondary metadata.
 * - Search filter activated when options > 8.
 * - Optional Card Grid layout for topology selection.
 * - Fully keyboard navigable (Escape, ArrowUp, ArrowDown, Enter, Tab).
 */
import React, { useState, useRef, useEffect, useId } from 'react'
import { ChevronDown, Search, Check } from 'lucide-react'

export interface SelectOption {
  value: string
  label: string
  shortLabel?: string
  meta?: string
  badge?: string
  badgeColor?: 'normal' | 'warn' | 'danger' | 'info'
  icon?: React.ReactNode
}

interface Props {
  value: string
  onChange: (val: string) => void
  options: SelectOption[]
  placeholder?: string
  isCardGrid?: boolean
  className?: string
  triggerPrefix?: React.ReactNode
  ariaLabel?: string
  align?: 'left' | 'right'
}

export function CustomSelect({
  value,
  onChange,
  options,
  placeholder = 'Select option...',
  isCardGrid = false,
  className = '',
  triggerPrefix,
  ariaLabel = 'Selection menu',
  align = 'left',
}: Props) {
  const [isOpen, setIsOpen] = useState(false)
  const [searchTerm, setSearchTerm] = useState('')
  const [highlightIndex, setHighlightIndex] = useState(0)

  const containerRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const listboxId = useId()

  const selectedOpt = options.find((o) => o.value === value)

  const filteredOptions = options.filter(
    (o) =>
      o.label.toLowerCase().includes(searchTerm.toLowerCase()) ||
      (o.meta && o.meta.toLowerCase().includes(searchTerm.toLowerCase())),
  )

  // Close on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false)
        setSearchTerm('')
      }
    }
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside)
    }
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isOpen])

  // Focus search input when popover opens
  useEffect(() => {
    if (isOpen && options.length > 8) {
      setTimeout(() => searchInputRef.current?.focus(), 50)
    }
  }, [isOpen, options.length])

  // Keyboard navigation
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!isOpen) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
        e.preventDefault()
        setIsOpen(true)
        setHighlightIndex(Math.max(0, options.findIndex((o) => o.value === value)))
      }
      return
    }

    switch (e.key) {
      case 'Escape':
        e.preventDefault()
        setIsOpen(false)
        setSearchTerm('')
        break
      case 'ArrowDown':
        e.preventDefault()
        setHighlightIndex((prev) => (prev + 1) % Math.max(1, filteredOptions.length))
        break
      case 'ArrowUp':
        e.preventDefault()
        setHighlightIndex((prev) => (prev - 1 + filteredOptions.length) % Math.max(1, filteredOptions.length))
        break
      case 'Enter':
        e.preventDefault()
        if (filteredOptions[highlightIndex]) {
          onChange(filteredOptions[highlightIndex].value)
          setIsOpen(false)
          setSearchTerm('')
        }
        break
      case 'Tab':
        setIsOpen(false)
        setSearchTerm('')
        break
    }
  }

  const triggerLabel = selectedOpt
    ? selectedOpt.shortLabel || selectedOpt.label
    : placeholder

  return (
    <div
      className={`custom-select-container ${className}`}
      ref={containerRef}
      onKeyDown={handleKeyDown}
    >
      {/* Trigger Button */}
      <button
        type="button"
        className="custom-select-trigger"
        onClick={() => setIsOpen((prev) => !prev)}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-controls={listboxId}
        aria-label={ariaLabel}
      >
        <span className="custom-select-trigger-content">
          {triggerPrefix && <span className="custom-select-prefix">{triggerPrefix}</span>}
          <span className="custom-select-label" title={selectedOpt?.label || triggerLabel}>
            {triggerLabel}
          </span>
        </span>
        <ChevronDown size={12} className={`custom-select-chevron ${isOpen ? 'custom-select-chevron--open' : ''}`} />
      </button>

      {/* Popover Listbox */}
      {isOpen && (
        <div
          id={listboxId}
          role="listbox"
          className={`custom-select-popover ${align === 'right' ? 'custom-select-popover--right' : ''} ${
            isCardGrid ? 'custom-select-popover--grid' : ''
          }`}
        >
          {/* Optional Search Filter when > 8 items */}
          {options.length > 8 && (
            <div className="custom-select-search-wrap">
              <Search size={12} className="custom-select-search-icon" />
              <input
                ref={searchInputRef}
                type="text"
                className="custom-select-search-input"
                placeholder="Search..."
                value={searchTerm}
                onChange={(e) => {
                  setSearchTerm(e.target.value)
                  setHighlightIndex(0)
                }}
                onClick={(e) => e.stopPropagation()}
              />
            </div>
          )}

          {/* Options List / Grid */}
          <div className={isCardGrid ? 'custom-select-grid' : 'custom-select-list'}>
            {filteredOptions.length === 0 ? (
              <div className="custom-select-empty">No matching options</div>
            ) : (
              filteredOptions.map((opt, idx) => {
                const isSelected = opt.value === value
                const isHighlighted = idx === highlightIndex

                if (isCardGrid) {
                  return (
                    <div
                      key={opt.value}
                      role="option"
                      aria-selected={isSelected}
                      className={`custom-select-card ${isSelected ? 'custom-select-card--selected' : ''} ${
                        isHighlighted ? 'custom-select-card--highlighted' : ''
                      }`}
                      onClick={() => {
                        onChange(opt.value)
                        setIsOpen(false)
                        setSearchTerm('')
                      }}
                      onMouseEnter={() => setHighlightIndex(idx)}
                    >
                      <div className="custom-select-card-head">
                        <span className="custom-select-card-title">{opt.label}</span>
                        {isSelected && <Check size={12} className="custom-select-check" />}
                      </div>
                      {opt.meta && <div className="custom-select-card-meta">{opt.meta}</div>}
                    </div>
                  )
                }

                return (
                  <div
                    key={opt.value}
                    role="option"
                    aria-selected={isSelected}
                    className={`custom-select-item ${isSelected ? 'custom-select-item--selected' : ''} ${
                      isHighlighted ? 'custom-select-item--highlighted' : ''
                    }`}
                    onClick={() => {
                      onChange(opt.value)
                      setIsOpen(false)
                      setSearchTerm('')
                    }}
                    onMouseEnter={() => setHighlightIndex(idx)}
                  >
                    <div className="custom-select-item-main">
                      {opt.icon && <span className="custom-select-item-icon">{opt.icon}</span>}
                      <div className="custom-select-item-text">
                        <span className="custom-select-item-label">{opt.label}</span>
                        {opt.meta && <span className="custom-select-item-meta">{opt.meta}</span>}
                      </div>
                    </div>
                    {opt.badge && (
                      <span className={`custom-select-badge custom-select-badge--${opt.badgeColor || 'normal'}`}>
                        {opt.badge}
                      </span>
                    )}
                    {isSelected && <Check size={12} className="custom-select-check" />}
                  </div>
                )
              })
            )}
          </div>
        </div>
      )}
    </div>
  )
}
