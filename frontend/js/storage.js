/**
 * storage.js — Client-Side State & LocalStorage Manager
 *
 * Module 10: Frontend Chat Interface
 *
 * Responsibilities:
 *   - Persist active conversation ID across page refreshes.
 *   - Store unsent message drafts during 404 recovery.
 *   - Provide transparent in-memory fallback for private browsing or restricted environments.
 *   - Never persist sensitive secrets or credentials.
 */

(function (window) {
  'use strict';

  var CONV_KEY = 'dpm101_active_conv_id';
  var DRAFT_KEY = 'dpm101_preserved_draft';

  // In-memory fallback dictionary if localStorage throws SecurityError
  var inMemoryStore = {};

  /**
   * Check if localStorage is available and accessible.
   */
  function isLocalStorageAvailable() {
    try {
      if (typeof window.localStorage === 'undefined') {
        return false;
      }
      var testKey = '__dpm101_test__';
      window.localStorage.setItem(testKey, testKey);
      window.localStorage.removeItem(testKey);
      return true;
    } catch (_) {
      return false;
    }
  }

  var storageAvailable = isLocalStorageAvailable();

  var ChatStorage = {
    /**
     * Get the active conversation ID.
     * @returns {string|null}
     */
    getActiveConversationId: function () {
      try {
        if (storageAvailable) {
          return window.localStorage.getItem(CONV_KEY);
        }
      } catch (err) {
        console.warn('LocalStorage read failed, falling back to in-memory:', err);
      }
      return inMemoryStore[CONV_KEY] || null;
    },

    /**
     * Set the active conversation ID.
     * @param {string} id
     */
    setActiveConversationId: function (id) {
      if (!id || typeof id !== 'string') {
        this.clearActiveConversationId();
        return;
      }
      try {
        if (storageAvailable) {
          window.localStorage.setItem(CONV_KEY, id);
        }
      } catch (err) {
        console.warn('LocalStorage write failed, using in-memory:', err);
      }
      inMemoryStore[CONV_KEY] = id;
    },

    /**
     * Clear the active conversation ID.
     */
    clearActiveConversationId: function () {
      try {
        if (storageAvailable) {
          window.localStorage.removeItem(CONV_KEY);
        }
      } catch (err) {
        console.warn('LocalStorage remove failed:', err);
      }
      delete inMemoryStore[CONV_KEY];
    },

    /**
     * Get preserved draft text (used for 404 recovery).
     * @returns {string}
     */
    getPreservedDraft: function () {
      try {
        if (storageAvailable) {
          return window.localStorage.getItem(DRAFT_KEY) || '';
        }
      } catch (_) {}
      return inMemoryStore[DRAFT_KEY] || '';
    },

    /**
     * Set preserved draft text.
     * @param {string} draft
     */
    setPreservedDraft: function (draft) {
      var text = (draft && typeof draft === 'string') ? draft : '';
      try {
        if (storageAvailable) {
          window.localStorage.setItem(DRAFT_KEY, text);
        }
      } catch (_) {}
      inMemoryStore[DRAFT_KEY] = text;
    },

    /**
     * Clear preserved draft text.
     */
    clearPreservedDraft: function () {
      try {
        if (storageAvailable) {
          window.localStorage.removeItem(DRAFT_KEY);
        }
      } catch (_) {}
      delete inMemoryStore[DRAFT_KEY];
    },
  };

  // Attach to window namespace
  window.ChatStorage = ChatStorage;

})(window);
