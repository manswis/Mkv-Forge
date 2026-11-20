/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/static/**/*.html",
    "./app/static/js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        canvas: "rgb(var(--rgb-canvas) / <alpha-value>)",
        surface: "rgb(var(--rgb-surface) / <alpha-value>)",
        hairline: "rgb(var(--rgb-hairline) / <alpha-value>)",
        accent: {
          DEFAULT: "rgb(var(--rgb-accent) / <alpha-value>)",
          hover: "rgb(var(--rgb-accent-hover) / <alpha-value>)",
          soft: "rgb(var(--rgb-accent-soft) / <alpha-value>)",
        },
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
