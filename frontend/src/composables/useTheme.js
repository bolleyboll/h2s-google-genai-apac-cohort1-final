import { ref } from 'vue';

const THEME_KEY = 'sidekick_theme';

const readDocTheme = () => {
  const t = document.documentElement.getAttribute('data-theme');
  return t === 'light' || t === 'dark' ? t : 'light';
};

const theme = ref(readDocTheme());

const toggleTheme = () => {
  theme.value = theme.value === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', theme.value);
  try {
    localStorage.setItem(THEME_KEY, theme.value);
  } catch {}
};

export const useTheme = () => ({ theme, toggleTheme });
