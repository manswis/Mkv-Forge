/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/static/**/*.html",
    "./app/static/js/**/*.js",
  ],
  safelist: [
    {
      pattern:
        /^ui-(panel|list-group|row|btn|segmented|tag|status|input|select|sheet|num|section-title|page-title|meta)/,
    },
  ],
  theme: {
    extend: {
      colors: {
        canvas: "rgb(var(--rgb-canvas) / <alpha-value>)",
        surface: "rgb(var(--rgb-surface) / <alpha-value>)",
        elevated: "rgb(var(--rgb-elevated) / <alpha-value>)",
        inset: "rgb(var(--rgb-inset) / <alpha-value>)",
        hairline: "rgb(var(--rgb-hairline) / <alpha-value>)",
        ink: {
          primary: "rgb(var(--rgb-text-primary) / <alpha-value>)",
          secondary: "rgb(var(--rgb-text-secondary) / <alpha-value>)",
          muted: "rgb(var(--rgb-text-muted) / <alpha-value>)",
        },
        accent: {
          DEFAULT: "rgb(var(--rgb-accent) / <alpha-value>)",
          hover: "rgb(var(--rgb-accent-hover) / <alpha-value>)",
          soft: "rgb(var(--rgb-accent-soft) / <alpha-value>)",
        },
      },
      borderColor: {
        subtle: "var(--color-border-subtle)",
        default: "var(--color-border-default)",
        strong: "var(--color-border-strong)",
      },
      boxShadow: {
        panel: "var(--shadow-panel)",
        elevated: "var(--shadow-elevated)",
      },
      borderRadius: {
        "ui-sm": "var(--radius-sm)",
        "ui-md": "var(--radius-md)",
        "ui-lg": "var(--radius-lg)",
      },
      keyframes: {
        "toast-progress": {
          from: { transform: "scaleX(1)" },
          to: { transform: "scaleX(0)" },
        },
      },
      animation: {
        "toast-progress": "toast-progress linear forwards",
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};
