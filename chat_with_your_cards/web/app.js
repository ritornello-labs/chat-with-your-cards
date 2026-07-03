/* Chat With Your Cards - webview UI.
 *
 * Renders the chat transcript and forwards user intent to Python via
 * pycmd("cwyc:" + JSON). Python pushes events through chatUI.dispatch().
 * Event vocabulary mirrors backends/base.py plus UI-directed messages
 * ("reset", "cancelled"). Unknown types are ignored (forward-compatible).
 */
(function () {
    "use strict";

    var transcript = null;
    var input = null;
    var sendButton = null;

    var streaming = false;
    var currentAssistant = null; // {element, markdown}
    var toolChips = {}; // call_id -> element
    var pinnedToBottom = true;

    function post(msg) {
        if (typeof pycmd === "function") {
            pycmd("cwyc:" + JSON.stringify(msg));
        }
    }

    function renderMarkdown(markdown) {
        if (typeof marked !== "undefined" && marked.parse) {
            return marked.parse(markdown);
        }
        var div = document.createElement("div");
        div.textContent = markdown;
        return div.innerHTML;
    }

    function scrollToBottomIfPinned() {
        if (pinnedToBottom) {
            transcript.scrollTop = transcript.scrollHeight;
        }
    }

    function addUserMessage(text) {
        var row = document.createElement("div");
        row.className = "cwyc-row cwyc-row-user";
        var bubble = document.createElement("div");
        bubble.className = "cwyc-msg cwyc-msg-user msg-user";
        bubble.textContent = text;
        row.appendChild(bubble);
        transcript.appendChild(row);
        scrollToBottomIfPinned();
    }

    function startAssistantMessage() {
        var row = document.createElement("div");
        row.className = "cwyc-row cwyc-row-assistant";
        var body = document.createElement("div");
        body.className = "cwyc-msg cwyc-msg-assistant msg-assistant cwyc-streaming";
        row.appendChild(body);
        transcript.appendChild(row);
        currentAssistant = { element: body, markdown: "" };
        scrollToBottomIfPinned();
    }

    function appendDelta(text) {
        if (!currentAssistant) {
            startAssistantMessage();
        }
        currentAssistant.markdown += text;
        currentAssistant.element.innerHTML = renderMarkdown(currentAssistant.markdown);
        scrollToBottomIfPinned();
    }

    function addToolChip(callId, tool, summary) {
        var chip = document.createElement("div");
        chip.className = "cwyc-tool-chip tool-chip cwyc-tool-running";
        chip.innerHTML =
            '<span class="cwyc-tool-spinner"></span>' +
            '<span class="cwyc-tool-name"></span>' +
            '<span class="cwyc-tool-summary"></span>' +
            '<span class="cwyc-tool-result"></span>';
        chip.querySelector(".cwyc-tool-name").textContent = tool;
        chip.querySelector(".cwyc-tool-summary").textContent = summary;
        var row = document.createElement("div");
        row.className = "cwyc-row cwyc-row-tool";
        row.appendChild(chip);
        transcript.appendChild(row);
        toolChips[callId] = chip;
        // The next text deltas belong to a fresh assistant block after the
        // chip; the block before it is finished, so stop its cursor.
        if (currentAssistant) {
            currentAssistant.element.classList.remove("cwyc-streaming");
        }
        currentAssistant = null;
        scrollToBottomIfPinned();
    }

    function finishToolChip(callId, ok, summary) {
        var chip = toolChips[callId];
        if (!chip) {
            return;
        }
        chip.classList.remove("cwyc-tool-running");
        chip.classList.add(ok ? "cwyc-tool-ok" : "cwyc-tool-failed");
        chip.querySelector(".cwyc-tool-result").textContent = summary;
        scrollToBottomIfPinned();
    }

    function finalizeStream(stopped) {
        if (currentAssistant) {
            currentAssistant.element.classList.remove("cwyc-streaming");
            if (stopped) {
                var note = document.createElement("div");
                note.className = "cwyc-stopped-note";
                note.textContent = "Stopped";
                currentAssistant.element.appendChild(note);
            }
        }
        currentAssistant = null;
        setStreaming(false);
    }

    function setStreaming(value) {
        streaming = value;
        sendButton.classList.toggle("cwyc-streaming-btn", value);
        sendButton.title = value ? "Stop (Esc)" : "Send (Enter)";
        sendButton.setAttribute("aria-label", value ? "Stop" : "Send");
    }

    function sendCurrentInput() {
        var text = input.value.trim();
        if (!text || streaming) {
            return;
        }
        input.value = "";
        autosizeInput();
        addUserMessage(text);
        startAssistantMessage();
        setStreaming(true);
        post({ type: "send", text: text });
    }

    function resetTranscript() {
        transcript.innerHTML = "";
        toolChips = {};
        currentAssistant = null;
        pinnedToBottom = true;
        setStreaming(false);
    }

    function autosizeInput() {
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 160) + "px";
    }

    function dispatch(payload) {
        switch (payload.type) {
            case "text_delta":
                appendDelta(payload.text);
                break;
            case "tool_call_started":
                addToolChip(payload.call_id, payload.tool, payload.summary);
                break;
            case "tool_call_finished":
                finishToolChip(payload.call_id, payload.ok, payload.summary);
                break;
            case "done":
                finalizeStream(false);
                break;
            case "cancelled":
                finalizeStream(true);
                break;
            case "error":
                appendDelta("\n\n**Error:** " + payload.message);
                finalizeStream(false);
                break;
            case "reset":
                resetTranscript();
                break;
            default:
                // Unknown event types are ignored on purpose.
                break;
        }
    }

    function focusComposer() {
        input.focus();
    }

    function onKeydownCapture(event) {
        var mod = event.metaKey || event.ctrlKey;
        var key = event.key.toLowerCase();
        if (mod && key === "j") {
            event.preventDefault();
            event.stopImmediatePropagation();
            post(event.shiftKey ? { type: "new_chat" } : { type: "toggle_focus" });
            return;
        }
        if (event.key === "Escape") {
            // AnkiWebView injects its own bubble-phase Escape handler that
            // pycmd("close")s; intercept in capture phase and stop it.
            event.preventDefault();
            event.stopImmediatePropagation();
            post(streaming ? { type: "cancel" } : { type: "focus_reviewer" });
        }
    }

    function init() {
        transcript = document.getElementById("cwyc-transcript");
        input = document.getElementById("cwyc-input");
        sendButton = document.getElementById("cwyc-send");

        sendButton.addEventListener("click", function () {
            if (streaming) {
                post({ type: "cancel" });
            } else {
                sendCurrentInput();
            }
        });
        document.getElementById("cwyc-new-chat").addEventListener("click", function () {
            post({ type: "new_chat" });
        });
        input.addEventListener("keydown", function (event) {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendCurrentInput();
            }
        });
        input.addEventListener("input", autosizeInput);
        transcript.addEventListener("scroll", function () {
            var distance =
                transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
            pinnedToBottom = distance < 40;
        });
        document.addEventListener("keydown", onKeydownCapture, true);

        autosizeInput();
        pingReadyUntilAcked();
    }

    var readyAcked = false;
    var readyAttempts = 0;

    function pingReadyUntilAcked() {
        // pycmd may not be wired the instant our script runs; retry until
        // Python acknowledges via ackReady() (dispatched on first "ready").
        if (readyAcked || readyAttempts > 40) {
            return;
        }
        readyAttempts += 1;
        post({ type: "ready" });
        setTimeout(pingReadyUntilAcked, 250);
    }

    window.chatUI = {
        dispatch: dispatch,
        focusComposer: focusComposer,
        ackReady: function () {
            readyAcked = true;
        },
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
