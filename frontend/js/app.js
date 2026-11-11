/**
 * app.js — Main Application Controller & Event Orchestrator
 *
 * Module 10: Frontend Chat Interface
 *
 * Responsibilities:
 *   - Orchestrate startup lifecycle: health check, conversation restoration, draft loading.
 *   - Wire event listeners for composer, suggested chips, sidebar, and modals.
 *   - Enforce idempotency: fresh RFC 4122 v4 UUID for new turns; reuse ID for retries.
 *   - Handle duplicate replay deduplication (prevent duplicate bubbles in DOM).
 *   - Enforce 404 recovery without silent auto-resending (preserves student draft).
 *   - Manage HTTP 429 countdown timer and composer locking.
 *   - Manage live statistics polling lifecycle (every 30s strictly while open).
 *   - Provide complete keyboard accessibility (Enter to send, Shift+Enter for newline, Escape for modals).
 */

(function (window, document) {
  'use strict';

  // ── State Variables ────────────────────────────────────────────────────────
  var activeConversationId = null;
  var currentRequestId = null;       // Tracks ID for current turn or retry
  var isSubmitting = false;
  var rateLimitTimerId = null;
  var statsPollingIntervalId = null;
  var pendingDeleteConvId = null;

  // ── DOM Element Cache ──────────────────────────────────────────────────────
  var elements = {};

  function initElements() {
    elements.statusIndicator = document.getElementById('status-indicator');
    elements.statusText = document.getElementById('status-text');
    elements.btnOpenStats = document.getElementById('btn-open-stats');
    elements.btnHeaderNewChat = document.getElementById('btn-header-new-chat');
    elements.btnSidebarNewChat = document.getElementById('btn-sidebar-new-chat');
    elements.btnToggleSidebar = document.getElementById('btn-toggle-sidebar');
    elements.sidebar = document.getElementById('sidebar');
    elements.conversationList = document.getElementById('conversation-list');
    elements.welcomeView = document.getElementById('welcome-view');
    elements.messagesContainer = document.getElementById('messages-container');
    elements.messageFeed = document.getElementById('message-feed');
    elements.composerForm = document.getElementById('composer-form');
    elements.messageInput = document.getElementById('message-input');
    elements.btnSend = document.getElementById('btn-send');
    elements.charCounter = document.getElementById('char-counter');

    // Banners
    elements.rateLimitBanner = document.getElementById('rate-limit-banner');
    elements.recoveryBanner = document.getElementById('recovery-banner');
    elements.btnRecoveryNewChat = document.getElementById('btn-recovery-new-chat');
    elements.generalErrorBanner = document.getElementById('general-error-banner');
    elements.btnDismissError = document.getElementById('btn-dismiss-error');

    // Dialogs
    elements.statsDialog = document.getElementById('stats-dialog');
    elements.btnCloseStats = document.getElementById('btn-close-stats');
    elements.btnRefreshStats = document.getElementById('btn-refresh-stats');
    elements.statsLoading = document.getElementById('stats-loading-state');
    elements.statsError = document.getElementById('stats-error-state');
    elements.statsContent = document.getElementById('stats-content');

    elements.deleteDialog = document.getElementById('delete-dialog');
    elements.btnCancelDelete = document.getElementById('btn-cancel-delete');
    elements.btnConfirmDelete = document.getElementById('btn-confirm-delete');
  }

  // ── Backend Health Monitoring ──────────────────────────────────────────────
  function checkSystemHealth() {
    window.ChatApi.checkHealth()
      .then(function (health) {
        if (elements.statusIndicator) {
          elements.statusIndicator.className = 'status-indicator status-online';
        }
        if (elements.statusText) {
          elements.statusText.textContent = 'Helpdesk Active';
        }
      })
      .catch(function () {
        if (elements.statusIndicator) {
          elements.statusIndicator.className = 'status-indicator status-offline';
        }
        if (elements.statusText) {
          elements.statusText.textContent = 'Connecting...';
        }
      });
  }

  // ── Conversation Lifecycle ─────────────────────────────────────────────────

  /**
   * Refresh the sidebar list of conversations.
   */
  function refreshConversationList() {
    return window.ChatApi.listConversations(50, 0)
      .then(function (resp) {
        var convs = (resp && Array.isArray(resp.conversations)) ? resp.conversations : [];
        window.ChatUI.renderConversationList(
          convs,
          activeConversationId,
          handleSelectConversation,
          promptDeleteConversation
        );
      })
      .catch(function (err) {
        console.warn('Failed to load conversation list:', err);
      });
  }

  /**
   * Start a clean new conversation.
   */
  function startNewConversation() {
    setComposerDisabled(true);
    window.ChatUI.hideRecoveryBanner();
    window.ChatUI.hideGeneralError();

    return window.ChatApi.createConversation()
      .then(function (newConv) {
        activeConversationId = newConv.id;
        window.ChatStorage.setActiveConversationId(newConv.id);
        currentRequestId = null;

        // Reset message container to empty state with welcome view
        clearMessagesView();
        showWelcomeView(true);

        // Check if there was a preserved draft from a 404 recovery
        var draft = window.ChatStorage.getPreservedDraft();
        if (draft && elements.messageInput) {
          elements.messageInput.value = draft;
          updateCharCounter();
        }

        refreshConversationList();
        closeMobileSidebar();
        if (elements.messageInput) {
          elements.messageInput.focus();
        }
      })
      .catch(function (err) {
        window.ChatUI.showGeneralError('Failed to Create Chat', 'Could not create a new conversation. ' + err.message);
      })
      .finally(function () {
        setComposerDisabled(false);
      });
  }

  /**
   * Select and load a conversation by ID.
   */
  function handleSelectConversation(convId) {
    if (!convId || convId === activeConversationId) {
      closeMobileSidebar();
      return;
    }

    setComposerDisabled(true);
    window.ChatUI.hideRecoveryBanner();
    window.ChatUI.hideGeneralError();

    return window.ChatApi.getMessages(convId, 100, 0)
      .then(function (resp) {
        activeConversationId = convId;
        window.ChatStorage.setActiveConversationId(convId);
        currentRequestId = null;

        clearMessagesView();
        var msgs = (resp && Array.isArray(resp.messages)) ? resp.messages : [];

        if (msgs.length === 0) {
          showWelcomeView(true);
        } else {
          showWelcomeView(false);
          for (var i = 0; i < msgs.length; i++) {
            var msgEl = window.ChatUI.createMessageElement(msgs[i]);
            elements.messagesContainer.appendChild(msgEl);
          }
          window.ChatUI.scrollToBottom(elements.messageFeed, true);
        }

        refreshConversationList();
        closeMobileSidebar();
      })
      .catch(function (err) {
        if (err.status === 404) {
          window.ChatStorage.clearActiveConversationId();
          activeConversationId = null;
          clearMessagesView();
          showWelcomeView(true);
          window.ChatUI.showRecoveryBanner();
          refreshConversationList();
        } else {
          window.ChatUI.showGeneralError('Error Loading Messages', err.message);
        }
      })
      .finally(function () {
        setComposerDisabled(false);
      });
  }

  /**
   * Prompt confirmation to delete a conversation.
   */
  function promptDeleteConversation(convId) {
    pendingDeleteConvId = convId;
    if (elements.deleteDialog && typeof elements.deleteDialog.showModal === 'function') {
      elements.deleteDialog.showModal();
    } else if (window.confirm('Delete this conversation permanently?')) {
      executeDeleteConversation(convId);
    }
  }

  /**
   * Execute conversation deletion.
   */
  function executeDeleteConversation(convId) {
    if (!convId) return;

    window.ChatApi.deleteConversation(convId)
      .then(function () {
        if (convId === activeConversationId) {
          window.ChatStorage.clearActiveConversationId();
          activeConversationId = null;
          clearMessagesView();
          showWelcomeView(true);
        }
        refreshConversationList();
      })
      .catch(function (err) {
        window.ChatUI.showGeneralError('Deletion Failed', 'Unable to delete conversation: ' + err.message);
      })
      .finally(function () {
        pendingDeleteConvId = null;
        if (elements.deleteDialog && elements.deleteDialog.open) {
          elements.deleteDialog.close();
        }
      });
  }

  // ── Message Submission Flow ────────────────────────────────────────────────

  /**
   * Submit a student message turn.
   */
  function handleSubmitMessage() {
    if (isSubmitting) return;

    var input = elements.messageInput;
    if (!input) return;

    var rawText = input.value;
    var cleanText = (rawText || '').trim();

    if (!cleanText) {
      input.focus();
      return;
    }

    if (cleanText.length > 4000) {
      window.ChatUI.showGeneralError('Message Too Long', 'Your message exceeds the 4,000 character limit.');
      return;
    }

    isSubmitting = true;
    setComposerDisabled(true);
    window.ChatUI.hideRecoveryBanner();
    window.ChatUI.hideGeneralError();

    // Ensure we have an active conversation ID before sending
    var ensureConvPromise = activeConversationId
      ? Promise.resolve(activeConversationId)
      : window.ChatApi.createConversation().then(function (newConv) {
          activeConversationId = newConv.id;
          window.ChatStorage.setActiveConversationId(newConv.id);
          refreshConversationList();
          return newConv.id;
        });

    ensureConvPromise
      .then(function (convId) {
        // Generate a new request ID if one does not exist for this turn (retry reuses existing ID)
        if (!currentRequestId) {
          currentRequestId = window.ChatApi.generateRequestId();
        }

        // 1. Immediately append user message to DOM feed
        showWelcomeView(false);
        var tempUserMsg = {
          role: 'user',
          content: cleanText,
          created_at: new Date().toISOString(),
        };
        var userEl = window.ChatUI.createMessageElement(tempUserMsg);
        elements.messagesContainer.appendChild(userEl);
        window.ChatUI.scrollToBottom(elements.messageFeed, true);

        // 2. Show typing indicator
        window.ChatUI.showTypingIndicator();
        window.ChatUI.scrollToBottom(elements.messageFeed, true);

        // 3. Clear composer textarea while in-flight
        input.value = '';
        updateCharCounter();
        adjustTextareaHeight(input);

        // 4. Send request to backend
        return window.ChatApi.sendMessage(convId, cleanText, currentRequestId)
          .then(function (data) {
            window.ChatUI.hideTypingIndicator();

            // Check if this response is a duplicate and already rendered
            var existingEl = null;
            if (data.message_id) {
              existingEl = elements.messagesContainer.querySelector('[data-message-id="' + data.message_id + '"]');
            }

            if (!existingEl) {
              var assistantEl = window.ChatUI.createMessageElement({
                id: data.message_id,
                role: 'assistant',
                content: data.content,
                sources: data.sources,
                is_supported: data.is_supported,
                is_duplicate: data.is_duplicate,
                created_at: data.created_at || new Date().toISOString(),
              });
              elements.messagesContainer.appendChild(assistantEl);
              window.ChatUI.scrollToBottom(elements.messageFeed, true);
            }

            // Successfully completed turn: clear preserved draft and reset request ID
            window.ChatStorage.clearPreservedDraft();
            currentRequestId = null;
            refreshConversationList();
          });
      })
      .catch(function (err) {
        window.ChatUI.hideTypingIndicator();

        // ── 404 CONVERSATION RECOVERY ──────────────────────────────────────────
        // Do NOT silently re-create or auto-resend.
        // Preserve user's text in composer, show clear explanation, offer New Chat.
        if (err.status === 404) {
          input.value = cleanText;
          updateCharCounter();
          adjustTextareaHeight(input);
          window.ChatStorage.setPreservedDraft(cleanText);

          window.ChatUI.showRecoveryBanner();
        } else if (err.status === 429) {
          // ── 429 RATE LIMIT COUNTDOWN ─────────────────────────────────────────
          input.value = cleanText;
          updateCharCounter();
          adjustTextareaHeight(input);

          var retrySecs = err.retryAfter || 15;
          setComposerDisabled(true);

          rateLimitTimerId = window.ChatUI.startRateLimitCountdown(
            retrySecs,
            function (remaining) {
              // On tick
            },
            function () {
              // On countdown complete
              rateLimitTimerId = null;
              setComposerDisabled(false);
              input.focus();
            }
          );
        } else if (err.status === 409) {
          // In-flight concurrency conflict
          window.ChatUI.showGeneralError(
            'Message in Progress',
            'Another message is currently being processed for this conversation. Please wait a moment before sending.'
          );
        } else {
          // Network or server error: preserve draft so student can click send again (reuses request ID)
          input.value = cleanText;
          updateCharCounter();
          adjustTextareaHeight(input);
          window.ChatUI.showGeneralError('Server Error', err.message);
        }
      })
      .finally(function () {
        isSubmitting = false;
        if (!rateLimitTimerId) {
          setComposerDisabled(false);
          if (elements.messageInput) {
            elements.messageInput.focus();
          }
        }
      });
  }

  // ── Operational Statistics Modal Lifecycle ─────────────────────────────────

  function openStatsModal() {
    if (!elements.statsDialog) return;

    if (elements.statsLoading) elements.statsLoading.style.display = 'flex';
    if (elements.statsError) elements.statsError.style.display = 'none';
    if (elements.statsContent) elements.statsContent.style.opacity = '0.5';

    if (typeof elements.statsDialog.showModal === 'function') {
      elements.statsDialog.showModal();
    } else {
      elements.statsDialog.setAttribute('open', 'true');
    }

    // Immediate initial fetch
    fetchStatsData();

    // Start 30s open-only polling
    stopStatsPolling();
    statsPollingIntervalId = window.setInterval(fetchStatsData, 30000);
  }

  function closeStatsModal() {
    stopStatsPolling();
    if (elements.statsDialog && elements.statsDialog.open) {
      elements.statsDialog.close();
    }
  }

  function stopStatsPolling() {
    if (statsPollingIntervalId) {
      window.clearInterval(statsPollingIntervalId);
      statsPollingIntervalId = null;
    }
  }

  function fetchStatsData() {
    window.ChatApi.getStats()
      .then(function (statsData) {
        if (elements.statsLoading) elements.statsLoading.style.display = 'none';
        if (elements.statsError) elements.statsError.style.display = 'none';
        if (elements.statsContent) elements.statsContent.style.opacity = '1';
        window.ChatUI.populateStats(statsData);
      })
      .catch(function (err) {
        if (elements.statsLoading) elements.statsLoading.style.display = 'none';
        if (elements.statsError) {
          elements.statsError.style.display = 'flex';
          var errText = document.getElementById('stats-error-text');
          if (errText) errText.textContent = err.message || 'Failed to retrieve operational counters.';
        }
      });
  }

  // ── UI Helper Functions ────────────────────────────────────────────────────

  function clearMessagesView() {
    if (!elements.messagesContainer) return;
    if (typeof elements.messagesContainer.replaceChildren === 'function') {
      elements.messagesContainer.replaceChildren();
    } else {
      while (elements.messagesContainer.firstChild) {
        elements.messagesContainer.removeChild(elements.messagesContainer.firstChild);
      }
    }
  }

  function showWelcomeView(show) {
    if (elements.welcomeView) {
      elements.welcomeView.style.display = show ? 'block' : 'none';
    }
  }

  function setComposerDisabled(disabled) {
    if (elements.messageInput) {
      elements.messageInput.disabled = disabled;
    }
    if (elements.btnSend) {
      elements.btnSend.disabled = disabled;
    }
  }

  function updateCharCounter() {
    if (!elements.messageInput || !elements.charCounter) return;
    var len = elements.messageInput.value.length;
    elements.charCounter.textContent = len + ' / 4000';

    if (len >= 4000) {
      elements.charCounter.className = 'char-counter limit-reached';
    } else if (len >= 3800) {
      elements.charCounter.className = 'char-counter limit-near';
    } else {
      elements.charCounter.className = 'char-counter';
    }
  }

  function adjustTextareaHeight(textarea) {
    if (!textarea) return;
    textarea.style.height = 'auto';
    var newHeight = Math.min(textarea.scrollHeight, 160);
    textarea.style.height = newHeight + 'px';
  }

  function toggleMobileSidebar() {
    if (!elements.sidebar) return;
    var isOpen = elements.sidebar.classList.toggle('open');

    // Create or remove mobile backdrop
    var backdrop = document.getElementById('sidebar-backdrop');
    if (isOpen && !backdrop) {
      backdrop = document.createElement('div');
      backdrop.id = 'sidebar-backdrop';
      backdrop.className = 'sidebar-backdrop';
      backdrop.addEventListener('click', closeMobileSidebar);
      document.body.appendChild(backdrop);
    } else if (!isOpen && backdrop) {
      backdrop.remove();
    }
  }

  function closeMobileSidebar() {
    if (elements.sidebar && elements.sidebar.classList.contains('open')) {
      elements.sidebar.classList.remove('open');
      var backdrop = document.getElementById('sidebar-backdrop');
      if (backdrop) backdrop.remove();
    }
  }

  // ── Setup Event Listeners ──────────────────────────────────────────────────
  function setupEvents() {
    // Composer Form Submit
    if (elements.composerForm) {
      elements.composerForm.addEventListener('submit', function (e) {
        e.preventDefault();
        handleSubmitMessage();
      });
    }

    // Textarea: Enter to send, Shift+Enter for newline
    if (elements.messageInput) {
      elements.messageInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          handleSubmitMessage();
        }
      });

      elements.messageInput.addEventListener('input', function () {
        updateCharCounter();
        adjustTextareaHeight(elements.messageInput);
      });
    }

    // Suggested Questions Chips
    var suggestedChips = document.querySelectorAll('.suggested-chip');
    for (var i = 0; i < suggestedChips.length; i++) {
      suggestedChips[i].addEventListener('click', function () {
        var question = this.getAttribute('data-question');
        if (question && elements.messageInput) {
          elements.messageInput.value = question;
          updateCharCounter();
          adjustTextareaHeight(elements.messageInput);
          handleSubmitMessage();
        }
      });
    }

    // Header & Sidebar "New Chat" Buttons
    if (elements.btnHeaderNewChat) {
      elements.btnHeaderNewChat.addEventListener('click', startNewConversation);
    }
    if (elements.btnSidebarNewChat) {
      elements.btnSidebarNewChat.addEventListener('click', startNewConversation);
    }
    if (elements.btnRecoveryNewChat) {
      elements.btnRecoveryNewChat.addEventListener('click', startNewConversation);
    }

    // Mobile Sidebar Toggle
    if (elements.btnToggleSidebar) {
      elements.btnToggleSidebar.addEventListener('click', toggleMobileSidebar);
    }

    // Stats Dialog
    if (elements.btnOpenStats) {
      elements.btnOpenStats.addEventListener('click', openStatsModal);
    }
    if (elements.btnCloseStats) {
      elements.btnCloseStats.addEventListener('click', closeStatsModal);
    }
    if (elements.btnRefreshStats) {
      elements.btnRefreshStats.addEventListener('click', fetchStatsData);
    }

    // Delete Confirmation Dialog
    if (elements.btnCancelDelete) {
      elements.btnCancelDelete.addEventListener('click', function () {
        pendingDeleteConvId = null;
        if (elements.deleteDialog && elements.deleteDialog.open) {
          elements.deleteDialog.close();
        }
      });
    }
    if (elements.btnConfirmDelete) {
      elements.btnConfirmDelete.addEventListener('click', function () {
        executeDeleteConversation(pendingDeleteConvId);
      });
    }

    // Error Banner Dismiss
    if (elements.btnDismissError) {
      elements.btnDismissError.addEventListener('click', function () {
        window.ChatUI.hideGeneralError();
      });
    }

    // Tab Visibility change: pause/resume stats polling
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') {
        stopStatsPolling();
      } else if (elements.statsDialog && elements.statsDialog.open) {
        fetchStatsData();
        statsPollingIntervalId = window.setInterval(fetchStatsData, 30000);
      }
    });

    // Escape Key: closes open dialogs
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (elements.statsDialog && elements.statsDialog.open) {
          closeStatsModal();
        }
        if (elements.deleteDialog && elements.deleteDialog.open) {
          pendingDeleteConvId = null;
          elements.deleteDialog.close();
        }
      }
    });
  }

  // ── Application Initialization ─────────────────────────────────────────────
  function initializeApp() {
    initElements();
    setupEvents();
    checkSystemHealth();

    // Check stored active conversation
    var savedConvId = window.ChatStorage.getActiveConversationId();
    if (savedConvId && window.ChatApi.isValidConversationId(savedConvId)) {
      handleSelectConversation(savedConvId).then(function () {
        refreshConversationList();
      });
    } else {
      refreshConversationList();
      showWelcomeView(true);
    }

    // Restore preserved draft if any
    var draft = window.ChatStorage.getPreservedDraft();
    if (draft && elements.messageInput) {
      elements.messageInput.value = draft;
      updateCharCounter();
      adjustTextareaHeight(elements.messageInput);
    }
  }

  // Run on DOMContentLoaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeApp);
  } else {
    initializeApp();
  }

})(window, document);
