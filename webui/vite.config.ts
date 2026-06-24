import { defineConfig } from 'vite';
import UnoCSS from 'unocss/vite';

export default defineConfig({
  root: 'src',
  plugins: [UnoCSS()],
  server: {
    host: '127.0.0.1',
    port: 5173,
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
  },
});
