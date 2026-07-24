/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          DEFAULT: '#0b0e14',   // app background (deepened for more card contrast)
          card: '#131822',      // default card
          elevated: '#1a2029',  // raised card / hover
          inset: '#0e131b',     // wells, chart backdrops, code
          border: '#252c38',    // hairline
          'border-strong': '#333c4a',
        },
        brand: {
          green: '#3fb950',
          'green-dim': '#238636',
          red: '#f85149',
          'red-dim': '#da3633',
          blue: '#58a6ff',
          yellow: '#d29922',
          purple: '#a371f7',
          cyan: '#39c5cf',
          orange: '#e3b341',
        },
        // Semantic status ramp (state, never reused as a categorical series hue).
        status: {
          good: '#3fb950',
          warn: '#d29922',
          serious: '#e3b341',
          critical: '#f85149',
          neutral: '#8b949e',
        },
        ink: {
          DEFAULT: '#e6edf3',   // primary text
          muted: '#9aa5b1',     // secondary text
          faint: '#6a7480',     // tertiary / labels
        },
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'ui-monospace', 'Consolas', 'monospace'],
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      boxShadow: {
        card: '0 1px 2px 0 rgba(0,0,0,0.3)',
        'card-hover': '0 4px 16px -2px rgba(0,0,0,0.5)',
        pop: '0 8px 32px -8px rgba(0,0,0,0.6)',
      },
      borderRadius: {
        xl: '0.75rem',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fadeIn 0.25s ease-out',
        'ping-slow': 'ping 2.5s cubic-bezier(0, 0, 0.2, 1) infinite',
        shimmer: 'shimmer 1.4s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '0%, 100%': { opacity: '0.35' },
          '50%': { opacity: '0.7' },
        },
      },
    },
  },
  plugins: [],
}
