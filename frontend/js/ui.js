/**
 * ui.js — Safe DOM Rendering Engine & UI Controllers
 *
 * Module 10: Frontend Chat Interface
 *
 * MANDATORY SECURITY RULES:
 *   - Zero innerHTML on any untrusted or backend content.
 *   - Strict DOM text nodes via document.createElement, document.createTextNode,
 *     textContent, and replaceChildren.
 *   - Custom safe tokenizer for bold (**text**), line breaks, and bullet lists.
 *   - Dynamic source attribution matching backend PolicyAnswer/ChatService structures.
 *   - Distinct styling for unsupported topics (is_supported === false).
 *   - Duplicate replay pill rendering (is_duplicate === true).
 */

(function (window) {
  'use strict';

  /**
   * Safely formats and renders plain text / basic markdown into a container DOM node.
   * Completely safe against XSS payloads (e.g. <script>, <img onerror>, <iframe>).
   *
   * Supported formatting:
   *   - Paragraphs separated by blank lines (\n\n)
   *   - Bullet lists (lines starting with -, *, •)
   *   - Line breaks (\n) within paragraphs
   *   - Bold text (**bold text**)
   *
   * @param {HTMLElement} container
   * @param {string} rawText
   */
  function renderSafeContent(container, rawText) {
    if (typeof container.replaceChildren === 'function') {
      container.replaceChildren();
    } else {
      while (container.firstChild) {
        container.removeChild(container.firstChild);
      }
    }

    if (!rawText || typeof rawText !== 'string') {
      return;
    }

    // Split text into paragraph blocks by blank lines
    var blocks = rawText.split(/\n\s*\n/);

    for (var i = 0; i < blocks.length; i++) {
      var block = blocks[i].trim();
      if (!block) continue;

      var lines = block.split('\n');

      // Check if this block represents a bullet list
      var isList = lines.length > 0 && lines.every(function (l) {
        return /^\s*[-*•]\s+/.test(l);
      });

      if (isList) {
        var ul = document.createElement('ul');
        ul.className = 'content-list';
        for (var j = 0; j < lines.length; j++) {
          var li = document.createElement('li');
          var itemText = lines[j].replace(/^\s*[-*•]\s+/, '');
          renderInlineSafe(li, itemText);
          ul.appendChild(li);
        }
        container.appendChild(ul);
      } else {
        var p = document.createElement('p');
        p.className = 'content-para';
        for (var k = 0; k < lines.length; k++) {
          if (k > 0) {
            p.appendChild(document.createElement('br'));
          }
          renderInlineSafe(p, lines[k]);
        }
        container.appendChild(p);
      }
    }
  }

  /**
   * Safely formats inline tokens (such as **bold**) by constructing DOM text and strong nodes.
   * @param {HTMLElement} parent
   * @param {string} text
   */
  function renderInlineSafe(parent, text) {
    if (!text) return;

    // Tokenize by bold delimiters: **text**
    var parts = text.split(/(\*\*[^*]+\*\*)/g);
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i];
      if (part.slice(0, 2) === '**' && part.slice(-2) === '**' && part.length > 4) {
        var strong = document.createElement('strong');
        strong.textContent = part.slice(2, -2);
        parent.appendChild(strong);
      } else if (part) {
        parent.appendChild(document.createTextNode(part));
      }
    }
  }

  /**
   * Format ISO date string into a friendly localized time.
   * @param {string} isoString
   * @returns {string}
   */
  function formatTime(isoString) {
    if (!isoString) return '';
    try {
      var date = new Date(isoString);
      if (isNaN(date.getTime())) return '';
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (_) {
      return '';
    }
  }

  /**
   * Format ISO date string into a friendly relative or short date for conversation sidebar.
   * @param {string} isoString
   * @returns {string}
   */
  function formatConversationDate(isoString) {
    if (!isoString) return '';
    try {
      var date = new Date(isoString);
      if (isNaN(date.getTime())) return '';
      var now = new Date();
      var diffMs = now - date;
      var diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

      if (diffDays === 0) {
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      } else if (diffDays === 1) {
        return 'Yesterday';
      } else if (diffDays < 7) {
        return diffDays + 'd ago';
      } else {
        return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
      }
    } catch (_) {
      return '';
    }
  }

  /**
   * Format seconds into friendly hours, minutes, seconds string.
   * @param {number} totalSeconds
   * @returns {string}
   */
  function formatUptime(totalSeconds) {
    var secs = Math.max(0, parseInt(totalSeconds, 10) || 0);
    var days = Math.floor(secs / 86400);
    var hours = Math.floor((secs % 86400) / 3600);
    var minutes = Math.floor((secs % 3600) / 60);
    var seconds = secs % 60;

    var parts = [];
    if (days > 0) parts.push(days + 'd');
    if (hours > 0 || days > 0) parts.push(hours + 'h');
    if (minutes > 0 || hours > 0 || days > 0) parts.push(minutes + 'm');
    parts.push(seconds + 's');

    return parts.join(' ');
  }

  var ChatUI = {
    renderSafeContent: renderSafeContent,
    formatTime: formatTime,
    formatConversationDate: formatConversationDate,
    formatUptime: formatUptime,

    /**
     * Create a complete DOM message row element.
     * @param {Object} msgData
     *   - id: string
     *   - role: 'user' | 'assistant'
     *   - content: string
     *   - created_at: string (ISO)
     *   - sources: string[] | null
     *   - is_supported: boolean (optional)
     *   - is_duplicate: boolean (optional)
     * @returns {HTMLElement}
     */
    createMessageElement: function (msgData) {
      var isUser = msgData.role === 'user';
      var row = document.createElement('div');
      row.className = 'message-row ' + (isUser ? 'message-user' : 'message-assistant');
      if (msgData.id) {
        row.setAttribute('data-message-id', msgData.id);
      }

      // 1. Avatar
      var avatar = document.createElement('div');
      avatar.className = 'avatar ' + (isUser ? 'avatar-user' : 'avatar-assistant');
      avatar.setAttribute('aria-hidden', 'true');
      avatar.textContent = isUser ? '🎓' : '🤖';
      row.appendChild(avatar);

      // 2. Bubble
      var bubble = document.createElement('div');
      bubble.className = 'message-bubble ' + (isUser ? 'bubble-user' : 'bubble-assistant');

      // 3. Bubble Content
      var contentDiv = document.createElement('div');
      contentDiv.className = 'bubble-content';
      renderSafeContent(contentDiv, msgData.content);
      bubble.appendChild(contentDiv);

      // 4. Assistant Attribution & Sources
      if (!isUser) {
        // A. Dynamic Source Attribution Badges
        if (Array.isArray(msgData.sources) && msgData.sources.length > 0) {
          var sourcesContainer = document.createElement('div');
          sourcesContainer.className = 'sources-container';

          var sourcesLabel = document.createElement('span');
          sourcesLabel.className = 'sources-label';
          sourcesLabel.textContent = 'Official Source' + (msgData.sources.length > 1 ? 's:' : ':');
          sourcesContainer.appendChild(sourcesLabel);

          var pillsList = document.createElement('div');
          pillsList.className = 'source-pills-list';

          for (var i = 0; i < msgData.sources.length; i++) {
            var srcTitle = msgData.sources[i];
            if (!srcTitle || typeof srcTitle !== 'string') continue;

            var pill = document.createElement('span');
            pill.className = 'source-pill';

            var icon = document.createElement('span');
            icon.className = 'source-pill-icon';
            icon.setAttribute('aria-hidden', 'true');
            icon.textContent = '📄';
            pill.appendChild(icon);

            var titleNode = document.createTextNode(srcTitle);
            pill.appendChild(titleNode);

            pillsList.appendChild(pill);
          }

          sourcesContainer.appendChild(pillsList);
          bubble.appendChild(sourcesContainer);
        }

        // B. Unsupported Topic Warning Pill (is_supported === false)
        if (msgData.is_supported === false) {
          var unsupportedDiv = document.createElement('div');
          unsupportedDiv.className = 'unsupported-note';
          unsupportedDiv.setAttribute('role', 'note');

          var unIcon = document.createElement('span');
          unIcon.setAttribute('aria-hidden', 'true');
          unIcon.textContent = 'ℹ️';
          unsupportedDiv.appendChild(unIcon);

          var unText = document.createTextNode('Information not specified in official course documents.');
          unsupportedDiv.appendChild(unText);

          bubble.appendChild(unsupportedDiv);
        }
      }

      // 5. Message Metadata (Timestamp & Duplicate Badge)
      var meta = document.createElement('div');
      meta.className = 'message-meta';

      if (msgData.is_duplicate === true) {
        var dupBadge = document.createElement('span');
        dupBadge.className = 'duplicate-pill';
        dupBadge.textContent = '⚡ Replayed';
        meta.appendChild(dupBadge);
      }

      var timeText = formatTime(msgData.created_at);
      if (timeText) {
        var timeSpan = document.createElement('span');
        timeSpan.textContent = timeText;
        meta.appendChild(timeSpan);
      }

      bubble.appendChild(meta);
      row.appendChild(bubble);

      return row;
    },

    /**
     * Scroll message feed to the latest message.
     * @param {HTMLElement} container
     * @param {boolean} force
     */
    scrollToBottom: function (container, force) {
      if (!container) return;
      var isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
      if (force || isNearBottom) {
        container.scrollTop = container.scrollHeight;
      }
    },

    /**
     * Show typing indicator in the feed.
     */
    showTypingIndicator: function () {
      var el = document.getElementById('typing-indicator');
      if (el) {
        el.style.display = 'flex';
      }
    },

    /**
     * Hide typing indicator in the feed.
     */
    hideTypingIndicator: function () {
      var el = document.getElementById('typing-indicator');
      if (el) {
        el.style.display = 'none';
      }
    },

    /**
     * Render the sidebar conversation list.
     * @param {Array} conversations
     * @param {string|null} activeId
     * @param {Function} onSelect
     * @param {Function} onDelete
     */
    renderConversationList: function (conversations, activeId, onSelect, onDelete) {
      var listEl = document.getElementById('conversation-list');
      if (!listEl) return;

      if (typeof listEl.replaceChildren === 'function') {
        listEl.replaceChildren();
      } else {
        while (listEl.firstChild) {
          listEl.removeChild(listEl.firstChild);
        }
      }

      if (!conversations || conversations.length === 0) {
        var emptyLi = document.createElement('li');
        emptyLi.className = 'conversation-item-empty';
        var emptySpan = document.createElement('span');
        emptySpan.textContent = 'No conversations yet.';
        emptyLi.appendChild(emptySpan);
        listEl.appendChild(emptyLi);
        return;
      }

      for (var i = 0; i < conversations.length; i++) {
        (function (conv) {
          var li = document.createElement('li');
          li.className = 'conversation-item' + (conv.id === activeId ? ' active' : '');
          li.setAttribute('data-id', conv.id);

          var metaGroup = document.createElement('div');
          metaGroup.className = 'conversation-meta-group';

          var titleSpan = document.createElement('span');
          titleSpan.className = 'conversation-title';
          var displayTitle = (conv && conv.title) ? conv.title : (conv && conv.id ? ('Chat ' + conv.id.slice(0, 8)) : 'New Chat');
          titleSpan.textContent = displayTitle;
          titleSpan.setAttribute('title', displayTitle);

          var timeSpan = document.createElement('span');
          timeSpan.className = 'conversation-time';
          timeSpan.textContent = formatConversationDate(conv.updated_at || conv.created_at);

          metaGroup.appendChild(titleSpan);
          metaGroup.appendChild(timeSpan);
          li.appendChild(metaGroup);

          // Delete icon button (SVG created via DOM createElementNS)
          var deleteBtn = document.createElement('button');
          deleteBtn.type = 'button';
          deleteBtn.className = 'btn-icon btn-delete-conv';
          deleteBtn.setAttribute('aria-label', 'Delete conversation');
          deleteBtn.setAttribute('title', 'Delete conversation');

          var svgNS = 'http://www.w3.org/2000/svg';
          var svg = document.createElementNS(svgNS, 'svg');
          svg.setAttribute('width', '15');
          svg.setAttribute('height', '15');
          svg.setAttribute('viewBox', '0 0 24 24');
          svg.setAttribute('fill', 'none');
          svg.setAttribute('stroke', 'currentColor');
          svg.setAttribute('stroke-width', '2');
          svg.setAttribute('stroke-linecap', 'round');
          svg.setAttribute('stroke-linejoin', 'round');
          svg.setAttribute('aria-hidden', 'true');

          var polyline = document.createElementNS(svgNS, 'polyline');
          polyline.setAttribute('points', '3 6 5 6 21 6');
          svg.appendChild(polyline);

          var path = document.createElementNS(svgNS, 'path');
          path.setAttribute('d', 'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2');
          svg.appendChild(path);

          deleteBtn.appendChild(svg);

          deleteBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (typeof onDelete === 'function') {
              onDelete(conv.id);
            }
          });

          li.appendChild(deleteBtn);

          li.addEventListener('click', function () {
            if (typeof onSelect === 'function') {
              onSelect(conv.id);
            }
          });

          listEl.appendChild(li);
        })(conversations[i]);
      }
    },

    /**
     * Populate operational statistics modal fields with exact backend data.
     * @param {Object} data
     */
    populateStats: function (data) {
      if (!data) return;

      var totalEl = document.getElementById('stat-total-requests');
      var cacheEl = document.getElementById('stat-cache-hits');
      var geminiEl = document.getElementById('stat-gemini-calls');
      var errorEl = document.getElementById('stat-errors');
      var uptimeEl = document.getElementById('stat-uptime');
      var uptimeSecsEl = document.getElementById('stat-uptime-seconds');
      var workerNoteEl = document.getElementById('stat-worker-note');

      if (totalEl) totalEl.textContent = typeof data.total_requests === 'number' ? data.total_requests.toLocaleString() : '0';
      if (cacheEl) cacheEl.textContent = typeof data.cache_hits === 'number' ? data.cache_hits.toLocaleString() : '0';
      if (geminiEl) geminiEl.textContent = typeof data.gemini_calls === 'number' ? data.gemini_calls.toLocaleString() : '0';
      if (errorEl) errorEl.textContent = typeof data.errors === 'number' ? data.errors.toLocaleString() : '0';

      var uptime = typeof data.uptime_seconds === 'number' ? data.uptime_seconds : 0;
      if (uptimeEl) uptimeEl.textContent = formatUptime(uptime);
      if (uptimeSecsEl) uptimeSecsEl.textContent = uptime.toLocaleString() + ' seconds';

      if (workerNoteEl && data.worker_note) {
        workerNoteEl.textContent = data.worker_note;
      }
    },

    /**
     * Toggle rate limit countdown banner.
     * @param {number} seconds
     * @param {Function} onTick
     * @param {Function} onComplete
     * @returns {number} timerId
     */
    startRateLimitCountdown: function (seconds, onTick, onComplete) {
      var banner = document.getElementById('rate-limit-banner');
      var secsEl = document.getElementById('rate-limit-seconds');
      var remaining = Math.max(1, parseInt(seconds, 10) || 15);

      if (banner) banner.style.display = 'flex';
      if (secsEl) secsEl.textContent = remaining;

      var intervalId = window.setInterval(function () {
        remaining -= 1;
        if (secsEl) secsEl.textContent = remaining;
        if (typeof onTick === 'function') onTick(remaining);

        if (remaining <= 0) {
          window.clearInterval(intervalId);
          if (banner) banner.style.display = 'none';
          if (typeof onComplete === 'function') onComplete();
        }
      }, 1000);

      return intervalId;
    },

    /**
     * Hide rate limit banner.
     */
    hideRateLimitBanner: function () {
      var banner = document.getElementById('rate-limit-banner');
      if (banner) banner.style.display = 'none';
    },

    /**
     * Show conversation 404 recovery banner.
     */
    showRecoveryBanner: function () {
      var banner = document.getElementById('recovery-banner');
      if (banner) banner.style.display = 'flex';
    },

    /**
     * Hide conversation 404 recovery banner.
     */
    hideRecoveryBanner: function () {
      var banner = document.getElementById('recovery-banner');
      if (banner) banner.style.display = 'none';
    },

    /**
     * Show general error alert banner.
     */
    showGeneralError: function (title, message) {
      var banner = document.getElementById('general-error-banner');
      var titleEl = document.getElementById('general-error-title');
      var textEl = document.getElementById('general-error-text');

      if (titleEl) titleEl.textContent = title || 'Error';
      if (textEl) textEl.textContent = message || 'An error occurred while communicating with the server.';
      if (banner) banner.style.display = 'flex';
    },

    /**
     * Hide general error alert banner.
     */
    hideGeneralError: function () {
      var banner = document.getElementById('general-error-banner');
      if (banner) banner.style.display = 'none';
    },
  };

  // Attach to window namespace
  window.ChatUI = ChatUI;

})(window);
