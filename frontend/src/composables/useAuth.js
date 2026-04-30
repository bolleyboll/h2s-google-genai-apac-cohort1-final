import { ref } from 'vue';
import { apiGet } from '../api.js';

/** App-wide singleton — one auth check per page load. */
const authReady = ref(false);
const oauthEnabled = ref(false);
const signedIn = ref(false);
const userSub = ref(null);
const userEmail = ref('');

const initialize = async () => {
  try {
    const data = await apiGet('/auth/me');
    oauthEnabled.value = data.oauth_enabled === true;
    if (!oauthEnabled.value) {
      signedIn.value = true;
    } else if (data.sub) {
      signedIn.value = true;
      userSub.value = data.sub;
      userEmail.value = data.email || '';
    }
  } catch (e) {
    if (e.status === 401) {
      // Backend says "go log in" — surface as not-signed-in.
      oauthEnabled.value = true;
      signedIn.value = false;
    } else {
      console.error('auth/me failed', e);
    }
  } finally {
    authReady.value = true;
  }
};

export const useAuth = () => ({
  authReady,
  oauthEnabled,
  signedIn,
  userSub,
  userEmail,
  initialize,
});
