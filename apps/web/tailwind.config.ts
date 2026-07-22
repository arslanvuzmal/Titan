import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        admin: {
          primary: "#3c8dbc",
          primaryDark: "#367fa9",
          success: "#00a65a",
          warning: "#f39c12",
          danger: "#dd4b39",
          info: "#00c0ef",
          sidebar: "#222d32",
          sidebarHover: "#1e282c",
          bg: "#f4f6f9",
        }
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'Helvetica Neue', 'Arial', 'sans-serif'],
      }
    },
  },
  plugins: [],
};

export default config;
