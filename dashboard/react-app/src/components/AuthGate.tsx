/** Sign-in / sign-up gate. */
import { useState } from 'react'
import { api, tokens } from '../api'

interface Props {
  onAuthenticated: () => void
}

export function AuthGate({ onAuthenticated }: Props) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const pair =
        mode === 'login'
          ? await api.login(username, password)
          : await api.register(username, password)
      tokens.set(pair)
      onAuthenticated()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gate">
      <form className="gate__panel" onSubmit={submit}>
        <h1 className="gate__title">Control plane</h1>
        <p className="gate__sub">
          Sign in to observe the congestion control loop.
        </p>

        {error && (
          <p className="gate__error" role="alert">
            {error}
          </p>
        )}

        <div className="field">
          <label className="field__label" htmlFor="u">Operator</label>
          <input
            id="u"
            className="field__input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
            minLength={3}
          />
        </div>

        <div className="field">
          <label className="field__label" htmlFor="p">Passphrase</label>
          <input
            id="p"
            className="field__input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            required
            minLength={8}
          />
        </div>

        <div className="gate__row">
          <button className="btn" type="submit" disabled={busy}>
            {busy ? 'Working…' : mode === 'login' ? 'Sign in' : 'Create account'}
          </button>
          <button
            className="btn btn--quiet"
            type="button"
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login')
              setError(null)
            }}
          >
            {mode === 'login' ? 'Create account' : 'I have an account'}
          </button>
        </div>

        <p className="gate__hint">
          First run? Switch to <code>Create account</code>. Passphrases need at
          least 8 characters. Sessions use a short-lived access token and a
          refresh token that <code>Sign out</code> revokes.
        </p>
      </form>
    </div>
  )
}