import { defineConfig, presetAttributify, presetIcons, presetUno } from 'unocss';

export default defineConfig({
  presets: [presetUno(), presetAttributify(), presetIcons()],
  content: {
    filesystem: ['src/**/*.{html,js,ts}'],
  },
});
